#!/usr/bin/env python3
"""
Qwen Remote Client
Interactive CLI for connecting to the Qwen Multi-Agent Server
"""
import sys
import os
import json
import argparse
import requests
import re
import subprocess
import shlex
import threading
import uuid
import atexit
import time
from datetime import datetime
from collections import deque
from typing import Optional, List

# Readline for command history and CLI editing
try:
    import readline
    READLINE_AVAILABLE = True
except ImportError:
    READLINE_AVAILABLE = False

# History file configuration
HISTORY_FILE = os.path.expanduser("~/.qwen_client_history")
CHAT_HISTORY_FILE = os.path.expanduser("~/.qwen_chat_history.json")
HISTORY_MAX_LENGTH = 1000

# Configuration
LINUX_SERVER_IP = os.getenv("QWEN_SERVER_IP", "192.168.50.101")
API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/chat/completions"
MEMORY_API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/memory"
SEARCH_API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/tools/search"
DEEP_DOCS_API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/tools/apple_deep_docs"
UNLOAD_API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/admin/unload"
HEALTH_URL = f"http://{LINUX_SERVER_IP}:5000/health"

COLORS = {
    # Wrapped in \001 and \002 for readline compatibility
    "HEADER": "\001\033[95m\002",
    "BLUE": "\001\033[94m\002",
    "GREEN": "\001\033[92m\002",
    "WARNING": "\001\033[93m\002",
    "FAIL": "\001\033[91m\002",
    "ENDC": "\001\033[0m\002",
    "BOLD": "\001\033[1m\002",
    "CYAN": "\001\033[96m\002"
}


# Agent UI Themes
AGENT_THEMES = {
    "implementer": {"color": COLORS['GREEN'], "icon": "💻", "prompt": "Implementer", "desc": "High-Capability Code & Feature Implementation"},
    "architect":   {"color": COLORS['HEADER'], "icon": "🏗️", "prompt": "Architect", "desc": "System Design & Architectural Planning"},
    "reviewer":    {"color": COLORS['CYAN'], "icon": "🔍", "prompt": "Reviewer", "desc": "Detailed Code Review & Best Practices"},
    "debugger":    {"color": COLORS['FAIL'], "icon": "🐞", "prompt": "Debugger", "desc": "Advanced Debugging & Error Analysis"},
    "metal_implementer": {"color": COLORS['BLUE'], "icon": "🤘", "prompt": "Metal", "desc": "Specialized Metal 4 & Compute + Apple Docs"},
}

def cleanup_server_resources():
    """Tell the server to unload models and free VRAM"""
    try:
        requests.post(UNLOAD_API_URL, timeout=5)
        # We don't print here to keep exit clean, but server logs will show it
    except:
        pass

# Register cleanup on exit
atexit.register(cleanup_server_resources)

def save_chat_history(history, current_agent="implementer"):
    """Save full chat history and metadata to file"""
    try:
        data = {
            "messages": history,
            "last_agent": current_agent,
            "timestamp": datetime.now().isoformat()
        }
        with open(CHAT_HISTORY_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print_colored(f"Warning: Failed to save chat history: {e}", COLORS['WARNING'])

def load_chat_history():
    """Load chat history from file if it exists. Returns (history, last_agent)"""
    if os.path.exists(CHAT_HISTORY_FILE):
        try:
            with open(CHAT_HISTORY_FILE, 'r') as f:
                data = json.load(f)
            
            # Handle both old format (list) and new format (dict)
            if isinstance(data, list):
                history = data
                last_agent = "implementer"
            else:
                history = data.get("messages", [])
                last_agent = data.get("last_agent", "implementer")
                
            print_colored(f"Found saved session with {len(history)} messages.", COLORS['CYAN'])
            if last_agent != "implementer":
                print_colored(f"Last used agent: {last_agent}", COLORS['CYAN'])

            choice = input(f"{COLORS['BOLD']}Restore previous session? [Y/n] > {COLORS['ENDC']}")
            if choice.lower() not in ['n', 'no']:
                return history, last_agent
        except Exception as e:
            print_colored(f"Warning: Failed to load chat history: {e}", COLORS['WARNING'])
    return [], "implementer"

def wait_for_server():
    """Poll server health endpoint until it comes back online"""
    print_colored(f"\nConnection lost. Waiting for server at {LINUX_SERVER_IP}...", COLORS['WARNING'])
    while True:
        try:
            response = requests.get(HEALTH_URL, timeout=2)
            if response.status_code == 200:
                print_colored("\nServer is back online! Resuming...", COLORS['GREEN'])
                return True
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            pass
        
        try:
            time.sleep(5)
            print(".", end="", flush=True)
        except KeyboardInterrupt:
            print_colored("\nPolling cancelled by user.", COLORS['FAIL'])
            return False

# Readline history management
def setup_readline():
    """Configure readline for command history and editing"""
    if not READLINE_AVAILABLE:
        return

    # Load history from file
    try:
        if os.path.exists(HISTORY_FILE):
            readline.read_history_file(HISTORY_FILE)
    except (IOError, OSError):
        pass  # History file doesn't exist or isn't readable

    # Set history length
    readline.set_history_length(HISTORY_MAX_LENGTH)

    # Register save on exit
    atexit.register(save_readline_history)

    # Configure readline behavior
    # Enable auto-complete on tab (basic filename completion)
    if 'libedit' in readline.__doc__ or sys.platform == 'darwin':
        readline.parse_and_bind("bind ^I rl_complete")
        # history-search-backward/forward are not supported by default libedit on macOS
        # Standard Up/Down arrows will work for previous/next history by default
    else:
        readline.parse_and_bind('tab: complete')
        # Some terminals need these bindings explicitly
        # readline.parse_and_bind(r'"\e[A": history-search-backward')  # Up arrow
        # readline.parse_and_bind(r'"\e[B": history-search-forward')   # Down arrow


def save_readline_history():
    """Save command history to file"""
    if not READLINE_AVAILABLE:
        return
    try:
        readline.write_history_file(HISTORY_FILE)
    except (IOError, OSError):
        pass  # Can't write history file


def add_to_history(line):
    """Add a line to readline history (avoiding duplicates of the last entry)"""
    if not READLINE_AVAILABLE:
        return
    if not line or not line.strip():
        return
    # Avoid adding duplicate of the most recent history entry
    history_len = readline.get_current_history_length()
    if history_len > 0:
        last_item = readline.get_history_item(history_len)
        if last_item == line:
            return
    readline.add_history(line)


# Command execution security settings
ALLOW_SHELL_MODE = os.getenv('ALLOW_SHELL_MODE', 'true').lower() == 'true'
ALLOW_ALL = os.getenv('ALLOW_ALL', 'false').lower() == 'true'
COMMAND_WHITELIST = os.getenv('COMMAND_WHITELIST', '').split(',') if os.getenv('COMMAND_WHITELIST') else None


class JobTracker:
    """Tracks background async command jobs"""

    def __init__(self, max_jobs=100, job_ttl_hours=1):
        self.jobs = {}
        self.lock = threading.Lock()
        self.max_jobs = max_jobs
        self.job_ttl_seconds = job_ttl_hours * 3600

    def _cleanup_old_jobs(self):
        """Remove old completed jobs. Must be called with lock held."""
        now = datetime.now()

        jobs_to_remove = []
        for job_id, job in self.jobs.items():
            if job['status'] in ['completed', 'failed'] and job['completed_at']:
                try:
                    completed_time = datetime.fromisoformat(job['completed_at'])
                    age_seconds = (now - completed_time).total_seconds()
                    if age_seconds > self.job_ttl_seconds:
                        jobs_to_remove.append(job_id)
                except (ValueError, TypeError):
                    jobs_to_remove.append(job_id)

        for job_id in jobs_to_remove:
            del self.jobs[job_id]

        if len(self.jobs) > self.max_jobs:
            completed_jobs = [
                (job_id, job) for job_id, job in self.jobs.items()
                if job['status'] in ['completed', 'failed']
            ]
            completed_jobs.sort(key=lambda x: x[1].get('completed_at', ''))
            num_to_remove = len(self.jobs) - self.max_jobs
            for i in range(min(num_to_remove, len(completed_jobs))):
                job_id = completed_jobs[i][0]
                del self.jobs[job_id]

    def create_job(self, command):
        job_id = str(uuid.uuid4())[:8]
        with self.lock:
            self._cleanup_old_jobs()
            self.jobs[job_id] = {
                "command": command,
                "status": "pending",
                "output": deque(maxlen=1000),
                "exit_code": None,
                "started_at": datetime.now().isoformat(),
                "completed_at": None,
                "process": None
            }
        return job_id

    def get_job(self, job_id):
        with self.lock:
            return self.jobs.get(job_id)

    def update_job(self, job_id, **kwargs):
        with self.lock:
            if job_id in self.jobs:
                self.jobs[job_id].update(kwargs)

    def add_output(self, job_id, line):
        with self.lock:
            if job_id in self.jobs:
                self.jobs[job_id]["output"].append(line)

    def get_status(self, job_id):
        job = self.get_job(job_id)
        if not job:
            return "Job not found"

        output_lines = list(job["output"])
        recent_output = "\n".join(output_lines[-20:]) if output_lines else "(no output yet)"

        status_msg = f"Job ID: {job_id}\n"
        status_msg += f"Command: {job['command']}\n"
        status_msg += f"Status: {job['status']}\n"
        status_msg += f"Started: {job['started_at']}\n"

        if job['completed_at']:
            status_msg += f"Completed: {job['completed_at']}\n"
            status_msg += f"Exit Code: {job['exit_code']}\n"

        status_msg += f"\nRecent Output (last 20 lines):\n{recent_output}\n"
        status_msg += f"\nTotal Output Lines: {len(output_lines)}"

        return status_msg

    def get_full_output(self, job_id):
        job = self.get_job(job_id)
        if not job:
            return "Job not found"

        output_lines = list(job["output"])
        return "\n".join(output_lines) if output_lines else "(no output)"

    def cleanup(self):
        """Manually trigger cleanup of old jobs"""
        with self.lock:
            before_count = len(self.jobs)
            self._cleanup_old_jobs()
            after_count = len(self.jobs)
            return before_count - after_count

    def get_stats(self):
        """Get statistics about job tracker"""
        with self.lock:
            total = len(self.jobs)
            by_status = {'pending': 0, 'running': 0, 'completed': 0, 'failed': 0}
            for job in self.jobs.values():
                status = job['status']
                by_status[status] = by_status.get(status, 0) + 1

            return {
                'total_jobs': total,
                'max_jobs': self.max_jobs,
                'ttl_hours': self.job_ttl_seconds / 3600,
                'by_status': by_status
            }

    def terminate_all(self):
        """Terminate all running background jobs"""
        with self.lock:
            count = 0
            for job_id, job in self.jobs.items():
                if job['status'] == 'running' and job['process']:
                    try:
                        print(f"Terminating background job {job_id}...", end="", flush=True)
                        job['process'].terminate()
                        # Give it a moment to die gracefully, else kill
                        try:
                            job['process'].wait(timeout=0.5)
                        except subprocess.TimeoutExpired:
                            job['process'].kill()
                        print(" Done.")
                        job['status'] = 'failed'
                        job['completed_at'] = datetime.now().isoformat()
                        count += 1
                    except Exception as e:
                        print(f" Error: {e}")
            if count > 0:
                print(f"Terminated {count} background jobs.")


# Initialize job tracker
job_tracker = JobTracker(
    max_jobs=int(os.getenv('JOB_TRACKER_MAX_JOBS', 100)),
    job_ttl_hours=float(os.getenv('JOB_TRACKER_TTL_HOURS', 1))
)

# Register cleanup on exit
atexit.register(job_tracker.terminate_all)



def print_colored(text, color):
    print(f"{color}{text}{COLORS['ENDC']}")


def save_memory(text):
    """Send a memory/fact to the server to be saved"""
    try:
        response = requests.post(MEMORY_API_URL, json={"text": text}, timeout=10)
        if response.status_code == 200:
            print_colored(f"Memory Saved: {text[:60]}...", COLORS['GREEN'])
            return f"Memory saved successfully."
        else:
            return f"Failed to save memory: {response.text}"
    except Exception as e:
        return f"Error saving memory: {str(e)}"


def decode_escape_sequences(text: str) -> str:
    """Decode JSON-style escape sequences in text
    
    CRITICAL: We do NOT decode \\" or \\' because that breaks shell commands 
    that use escaped quotes for nesting (e.g. python -c "print(\"hi\")").
    We DO decode \\n, \\t, etc. to ensure multi-line commands format correctly.
    """
    replacements = [
        ('\\\\', '\x00'),  # Temporarily replace \\ with null char
        ('\\n', '\n'),
        ('\\t', '\t'),
        ('\\r', '\r'),
        ('\\b', '\b'),
        ('\\f', '\f'),
        ('\\v', '\v'),
        # ('\\"', '"'),  <-- REMOVED: Breaks shell quoting
        # ("\\'", "'"),  <-- REMOVED: Breaks shell quoting
        ('\x00', '\\'),    # Replace null char back with single backslash
    ]

    result = text
    for escaped, unescaped in replacements:
        result = result.replace(escaped, unescaped)

    return result


def parse_command_safely(command: str) -> List[str]:
    """Parse command string into argument list safely"""
    if not ALLOW_SHELL_MODE:
        dangerous_chars = ['|', '&', ';', '$', '`', '\n', '>', '<', '(', ')']
        if any(char in command for char in dangerous_chars):
            raise ValueError(
                f"Command contains shell metacharacters. "
                f"Set ALLOW_SHELL_MODE=true to enable shell features, "
                f"or rewrite command without: {', '.join(dangerous_chars)}"
            )

    try:
        return shlex.split(command)
    except ValueError as e:
        raise ValueError(f"Failed to parse command: {e}")


def expand_paths_in_args(command_args: List[str]) -> List[str]:
    """Expand tilde (~) in command arguments for proper path resolution

    When using shell=False, tilde expansion doesn't happen automatically.
    This function expands ~ in arguments that look like paths.
    """
    expanded_args = []
    for arg in command_args:
        # Expand tilde if argument starts with ~ or contains =~ 
        if arg.startswith('~'):
            expanded_args.append(os.path.expanduser(arg))
        elif '=~' in arg:
            # Handle cases like --file=~/path or VAR=~/path
            key, value = arg.split('=', 1)
            if value.startswith('~'):
                expanded_args.append(f"{key}={os.path.expanduser(value)}")
            else:
                expanded_args.append(arg)
        else:
            expanded_args.append(arg)
    return expanded_args


def is_command_allowed(command_args: List[str]) -> tuple:
    """Check if command is allowed based on whitelist"""
    if not COMMAND_WHITELIST:
        return True, "No whitelist configured (all commands allowed)"

    if not command_args:
        return False, "Empty command"

    base_command = command_args[0]

    if base_command in COMMAND_WHITELIST:
        return True, f"Command '{base_command}' is whitelisted"

    if '/' in base_command:
        base_name = os.path.basename(base_command)
        if base_name in COMMAND_WHITELIST:
            return True, f"Command '{base_name}' is whitelisted"

    return False, f"Command '{base_command}' not in whitelist: {', '.join(COMMAND_WHITELIST)}"


def run_command_async(job_id, command):
    """Run command in background thread and capture output in real-time"""
    try:
        job_tracker.update_job(job_id, status="running")

        if ALLOW_SHELL_MODE:
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
        else:
            command_args = parse_command_safely(command)
            command_args = expand_paths_in_args(command_args)
            process = subprocess.Popen(
                command_args,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )

        job_tracker.update_job(job_id, process=process)

        for line in iter(process.stdout.readline, ''):
            if line:
                job_tracker.add_output(job_id, line.rstrip())

        process.wait()

        job_tracker.update_job(
            job_id,
            status="completed" if process.returncode == 0 else "failed",
            exit_code=process.returncode,
            completed_at=datetime.now().isoformat()
        )

    except Exception as e:
        job_tracker.add_output(job_id, f"ERROR: {str(e)}")
        job_tracker.update_job(
            job_id,
            status="failed",
            exit_code=-1,
            completed_at=datetime.now().isoformat()
        )


def execute_remote_command(command, async_mode=False):
    """Execute command synchronously or asynchronously with security checks"""
    print_colored(f"\nAgent wants to run command on your machine: {command}", COLORS['WARNING'])
    if async_mode:
        print_colored(f"   (async mode - will run in background)", COLORS['BLUE'])

    if ALLOW_SHELL_MODE:
        print_colored(f"   Shell mode enabled (less safe)", COLORS['WARNING'])
    else:
        print_colored(f"   Safe mode (shell=False)", COLORS['GREEN'])

    try:
        if not ALLOW_SHELL_MODE:
            command_args = parse_command_safely(command)
            allowed, msg = is_command_allowed(command_args)
            if not allowed:
                print_colored(f"   {msg}", COLORS['FAIL'])
                return f"Command rejected: {msg}"
            print_colored(f"   {msg}", COLORS['GREEN'])
    except ValueError as e:
        print_colored(f"   {str(e)}", COLORS['FAIL'])
        return f"Command validation failed: {str(e)}"

    if ALLOW_ALL:
        print_colored(f"   Auto-approved (ALLOW_ALL mode enabled)", COLORS['GREEN'])
        choice = 'y'
    else:
        try:
            choice = input(f"{COLORS['BOLD']}Allow? [y/N] > {COLORS['ENDC']}")
        except (EOFError, KeyboardInterrupt):
            return "User cancelled command execution."

        if choice.lower() != 'y':
            return "User denied command execution."

    if async_mode:
        job_id = job_tracker.create_job(command)
        thread = threading.Thread(target=run_command_async, args=(job_id, command), daemon=True)
        thread.start()

        print_colored(f"Command started in background", COLORS['GREEN'])
        print_colored(f"Job ID: {job_id}", COLORS['CYAN'])

        return f"Command started in background.\nJob ID: {job_id}\n\nUse <<<REMOTE_CHECK_STATUS>>>{job_id}<<<REMOTE_CHECK_STATUS>>> to check progress.\nUse <<<REMOTE_GET_OUTPUT>>>{job_id}<<<REMOTE_GET_OUTPUT>>> to get full output."

    else:
        try:
            if ALLOW_SHELL_MODE:
                result = subprocess.run(command, shell=True, capture_output=True, text=True, errors='replace', timeout=240)
            else:
                command_args = parse_command_safely(command)
                command_args = expand_paths_in_args(command_args)
                result = subprocess.run(command_args, shell=False, capture_output=True, text=True, errors='replace', timeout=240)

            output = result.stdout + result.stderr
            print_colored(f"Output:\n{output}", COLORS['CYAN'])
            return f"Command executed successfully.\nExit Code: {result.returncode}\nOutput:\n{output}"
        except subprocess.TimeoutExpired:
            return "Command timed out (240s limit for sync commands). Consider using async mode for long-running commands."
        except Exception as e:
            return f"Error executing command: {str(e)}"


def check_job_status(job_id):
    """Check status of a background job"""
    return job_tracker.get_status(job_id)


def get_job_output(job_id):
    """Get full output of a background job"""
    job = job_tracker.get_job(job_id)
    if not job:
        return "Job not found"

    output = job_tracker.get_full_output(job_id)
    status = f"Job ID: {job_id}\n"
    status += f"Status: {job['status']}\n"
    status += f"Exit Code: {job.get('exit_code', 'N/A')}\n"
    status += f"\nFull Output:\n{output}"

    return status


def list_all_jobs():
    """List all jobs with their current status"""
    stats = job_tracker.get_stats()

    result = "Job Tracker Status:\n" + "=" * 60 + "\n"
    result += f"Total Jobs: {stats['total_jobs']} / {stats['max_jobs']} (max)\n"
    result += f"TTL for completed jobs: {stats['ttl_hours']} hours\n"
    result += f"By Status: "
    result += f"Pending={stats['by_status']['pending']}, "
    result += f"Running={stats['by_status']['running']}, "
    result += f"Completed={stats['by_status']['completed']}, "
    result += f"Failed={stats['by_status']['failed']}\n"
    result += "=" * 60 + "\n"

    with job_tracker.lock:
        if not job_tracker.jobs:
            result += "\nNo jobs found."
            return result

        result += "\nJobs:\n"
        for job_id, job in job_tracker.jobs.items():
            result += f"\nJob ID: {job_id}\n"
            result += f"Command: {job['command'][:60]}{'...' if len(job['command']) > 60 else ''}\n"
            result += f"Status: {job['status']}\n"
            result += f"Started: {job['started_at']}\n"
            if job['completed_at']:
                result += f"Completed: {job['completed_at']}\n"
                result += f"Exit Code: {job['exit_code']}\n"
            result += "-" * 60 + "\n"

        return result


def web_search(query):
    """Send a search query to the server"""
    try:
        print_colored(f"Searching web for: {query}", COLORS['CYAN'])
        response = requests.post(SEARCH_API_URL, json={"query": query}, timeout=30)
        if response.status_code == 200:
            result = response.json().get("result", "No results")
            print_colored(f"\n{result}\n", COLORS['GREEN']) # Print results to console
            return f"Search Results:\n{result}"
        else:
            return f"Failed to search: {response.text}"
    except Exception as e:
        return f"Error searching: {str(e)}"


class CupertinoMCPClient:
    """Client for interacting with the Cupertino MCP server on macOS"""
    
    def __init__(self):
        self.process = None
        self.msg_id = 1
        self.lock = threading.Lock()

    def start(self):
        """Start the Cupertino MCP server process"""
        if self.process and self.process.poll() is None:
            return True
            
        try:
            # Find cupertino executable
            cupertino_path = subprocess.check_output(["which", "cupertino"], text=True).strip()
            if not cupertino_path:
                return False
                
            self.process = subprocess.Popen(
                [cupertino_path, "serve"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )
            return True
        except Exception as e:
            print_colored(f"Error starting Cupertino MCP: {e}", COLORS['FAIL'])
            return False

    def _send_request(self, method, params):
        """Send a JSON-RPC request to the MCP server and wait for response"""
        if not self.start():
            return {"error": "Cupertino MCP not found or failed to start"}
            
        with self.lock:
            req_id = self.msg_id
            self.msg_id += 1
            
            request = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params
            }
            
            try:
                self.process.stdin.write(json.dumps(request) + "\n")
                self.process.stdin.flush()
                
                # Wait for response line
                line = self.process.stdout.readline()
                if not line:
                    return {"error": "No response from Cupertino MCP"}
                    
                response = json.loads(line)
                if response.get("id") == req_id:
                    return response.get("result", {})
                return {"error": "Response ID mismatch"}
            except Exception as e:
                return {"error": f"Communication error: {e}"}

    def search(self, query):
        """Search Apple documentation using the MCP tool"""
        # Cupertino usually exposes tools. Let's try the 'search_docs' tool.
        # Format based on standard MCP tool invocation
        params = {
            "name": "search_docs",
            "arguments": {"query": query}
        }
        return self._send_request("tools/call", params)

    def read_resource(self, uri):
        """Read a specific documentation resource"""
        return self._send_request("resources/read", {"uri": uri})

cupertino_client = CupertinoMCPClient()


def handle_cupertino_search(query):
    """Execute search via Cupertino MCP and save to server memory"""
    print_colored(f"Searching Apple Documentation for: {query}...", COLORS['BLUE'])
    
    result = cupertino_client.search(query)
    if "error" in result:
        error_msg = f"Cupertino Error: {result['error']}"
        print_colored(error_msg, COLORS['FAIL'])
        return error_msg
        
    # Process results (Cupertino results are typically tool-call output objects)
    # result['content'] is usually a list of text objects
    content_list = result.get("content", [])
    text_results = []
    for item in content_list:
        if item.get("type") == "text":
            text_results.append(item.get("text", ""))
            
    combined_results = "\n\n".join(text_results)
    if not combined_results:
        return "No documentation found for that query."
        
    # Save the retrieved docs to the Linux server's memory for future grounding
    print_colored("Saving retrieved documentation to server memory...", COLORS['CYAN'])
    save_memory(f"Apple Documentation ({query}):\n{combined_results[:5000]}") # Save first 5k chars
    
    return f"Retrieved Apple Documentation for '{query}':\n\n{combined_results}"


def apple_deep_docs_search(payload_str):
    """Send a deep doc search query to the server"""
    try:
        payload = json.loads(payload_str)
        tool = payload.get("tool")
        args = payload.get("arguments", {})
        
        print_colored(f"Calling Apple Deep Docs ({tool}): {args}", COLORS['CYAN'])
        
        response = requests.post(DEEP_DOCS_API_URL, json=payload, timeout=60)
        if response.status_code == 200:
            result = response.json().get("result", "No results")
            # Save to memory for grounding
            save_memory(f"Apple Deep Doc ({tool}): {str(args)}\n{result[:5000]}")
            return f"Apple Deep Docs Result ({tool}):\n{result}"
        else:
            return f"Failed to call Deep Docs: {response.text}"
    except Exception as e:
        return f"Error in Deep Docs call: {str(e)}"


def process_remote_commands(response_text: str) -> Optional[str]:
    """Process remote command markers in agent response"""
    # Note: We do NOT use decode_escape_sequences here because json.loads in the main loop
    # has already handled standard JSON escapes. Further decoding breaks code that relies on
    # literal escape sequences (e.g. print("a\\nb")).
    
    commands = [
        (r'<<<REMOTE_EXEC_ASYNC>>>\s*(.*?)\s*<<<REMOTE_EXEC_ASYNC>>>',
         lambda cmd: execute_remote_command(cmd.strip(), async_mode=True),
         True),
        (r'<<<REMOTE_EXEC>>>\s*(.*?)\s*<<<REMOTE_EXEC>>>',
         lambda cmd: execute_remote_command(cmd.strip(), async_mode=False),
         True),
        (r'<<<REMOTE_CHECK_STATUS>>>\s*(.*?)\s*<<<REMOTE_CHECK_STATUS>>>',
         lambda job_id: check_job_status(job_id.strip()),
         True),
        (r'<<<REMOTE_GET_OUTPUT>>>\s*(.*?)\s*<<<REMOTE_GET_OUTPUT>>>',
         lambda job_id: get_job_output(job_id.strip()),
         True),
        (r'<<<SAVE_MEMORY>>>\s*(.*?)\s*<<<SAVE_MEMORY>>>',
         lambda text: save_memory(text.strip()),
         True),
        (r'<<<WEB_SEARCH>>>\s*(.*?)\s*<<<WEB_SEARCH>>>',
         lambda query: web_search(query.strip()),
         True),
        (r'<<<CUPERTINO>>>\s*(.*?)\s*<<<CUPERTINO>>>',
         lambda query: handle_cupertino_search(query.strip()),
         True),
        (r'<<<APPLE_DEEP_DOCS>>>\s*(.*?)\s*<<<APPLE_DEEP_DOCS>>>',
         lambda payload: apple_deep_docs_search(payload.strip()),
         True),
        (r'<<<REMOTE_LIST_JOBS>>>',
         lambda _: list_all_jobs(),
         False),
    ]

    for pattern, handler, has_capture in commands:
        match = re.search(pattern, response_text, re.DOTALL)
        if match:
            arg = match.group(1) if has_capture else None
            return handler(arg)

    return None


def chat(model="implementer"):
    # Initialize readline for command history and editing
    setup_readline()

    print_colored(f"\nQwen Remote CLI (Connected to {LINUX_SERVER_IP})", COLORS['HEADER'])
    
    # Get initial theme
    agent_theme = AGENT_THEMES.get(model, AGENT_THEMES["implementer"])
    print_colored(f"Agent: {model} {agent_theme['icon']}", COLORS['WARNING'])
    print_colored(f"({agent_theme['desc']})", COLORS['BLUE'])

    print_colored("\nSecurity Settings:", COLORS['HEADER'])
    if ALLOW_SHELL_MODE:
        print_colored("  Shell mode: ENABLED (allows pipes, redirects, etc.)", COLORS['WARNING'])
    else:
        print_colored("  Shell mode: DISABLED (safer, no shell injection)", COLORS['GREEN'])

    if COMMAND_WHITELIST:
        print_colored(f"  Whitelist: {len(COMMAND_WHITELIST)} commands allowed", COLORS['GREEN'])
        print_colored(f"    {', '.join(COMMAND_WHITELIST[:5])}{'...' if len(COMMAND_WHITELIST) > 5 else ''}", COLORS['CYAN'])
    else:
        print_colored("  Whitelist: DISABLED (all commands allowed)", COLORS['WARNING'])

    if ALLOW_ALL:
        print_colored("  Command approval: AUTO-APPROVE ALL (⚠️  NO PROMPTS - DANGEROUS!)", COLORS['FAIL'])
    else:
        print_colored("  Command approval: Manual (will prompt for each command)", COLORS['GREEN'])

    print_colored("\nCommands: /help, /exit, /model <name>, /history, /cupertino <query>, /apple <tool> <args>", COLORS['BLUE'])
    if READLINE_AVAILABLE:
        print_colored("Use ↑/↓ arrows to navigate history. History saved to ~/.qwen_client_history\n", COLORS['BLUE'])
    else:
        print_colored("(Install readline for command history support)\n", COLORS['WARNING'])

    history, loaded_agent = load_chat_history()
    
    # If we loaded a history and it had a specific agent, switch to it
    if history and loaded_agent and loaded_agent in AGENT_THEMES:
        model = loaded_agent
        agent_theme = AGENT_THEMES[model]  # Update theme to match loaded agent
        print_colored(f"Resuming with agent: {model}", COLORS['GREEN'])
        
        # Show recent context
        if history:
            print_colored("\n--- Previous Context ---", COLORS['HEADER'])
            # Show last 4 messages (approx 2 exchanges)
            start_idx = max(0, len(history) - 4)
            for msg in history[start_idx:]:
                role = msg["role"].capitalize()
                content = msg["content"]
                # Truncate very long messages for display
                display_content = content[:200] + "..." if len(content) > 200 else content
                color = COLORS['CYAN'] if msg["role"] == "user" else COLORS['GREEN']
                print(f"{color}{role}: {display_content}{COLORS['ENDC']}")
            print_colored("------------------------\n", COLORS['HEADER'])

    while True:
        try:
            # Use agent-specific color and prompt
            prompt_text = f"{agent_theme['prompt']} {agent_theme['icon']} > "
            full_prompt = f"{agent_theme['color']}{prompt_text}{COLORS['ENDC']}"
            
            # Pass the prompt directly to input() so readline handles it correctly
            # This fixes the issue where previous text remains on screen when cycling history
            try:
                if history and history[-1]["role"] == "user" and history[-1].get("auto_send", False):
                    print_colored("Sending tool output to agent...", COLORS['BLUE'])
                    user_input = history[-1]["content"]
                else:
                    user_input = input(full_prompt)
                    # Add user input to readline history (for up/down arrow navigation)
                    if user_input.strip():
                        add_to_history(user_input)
            except EOFError:
                break # Handle Ctrl+D gracefully

            if not user_input.strip():
                continue
            
            # Help Command
            if user_input.lower() == '/help':
                print_colored("\n--- Qwen Remote CLI Help ---", COLORS['HEADER'])
                print_colored(f"{COLORS['BOLD']}GENERAL COMMANDS:{COLORS['ENDC']}", COLORS['BLUE'])
                print(f"  /help                - Show this help menu")
                print(f"  /exit, /quit         - Exit the CLI and cleanup resources")
                print(f"  /model <name>        - Switch the active agent (e.g. /model architect)")
                print(f"  /history             - Show recent command history")
                print(f"  /history clear       - Clear command history")
                
                print_colored(f"\n{COLORS['BOLD']}DOCUMENTATION TOOLS:{COLORS['ENDC']}", COLORS['BLUE'])
                print(f"  /cupertino <query>   - Search local Apple documentation on macOS")
                print(f"                         Example: /cupertino MTLMeshRenderPipelineDescriptor")
                print(f"  /apple <tool> <args> - Search Apple Deep Docs on the Linux server")
                print(f"                         Example: /apple search_swift_evolution {{\"feature\": \"actors\"}}")
                print(f"                         Example: /apple fetch_apple_documentation {{\"url\": \"https://developer.apple.com/...\"}}")
                
                print_colored(f"\n{COLORS['BOLD']}AGENT SHORTCUTS:{COLORS['ENDC']}", COLORS['BLUE'])
                print(f"  @<agent_name> [msg]  - Switch agent and optionally send message in one go")
                print(f"                         Example: @architect Design a Metal 4 renderer")
                print(f"                         Example: @debugger Why is this kernel crashing?")
                
                print_colored(f"\n{COLORS['BOLD']}AVAILABLE AGENTS:{COLORS['ENDC']}", COLORS['BLUE'])
                for name, theme in AGENT_THEMES.items():
                    print(f"  {name.ljust(18)} - {theme['desc']}")
                print_colored("----------------------------\n", COLORS['HEADER'])
                continue

            if user_input.lower() in ['/exit', '/quit']:
                break

            # Quick Agent Switch (e.g. @architect or @architect Please design...)
            if user_input.startswith('@'):
                parts = user_input.split(' ', 1)
                potential_agent = parts[0][1:].lower() # remove @
                
                # Handle fuzzy matching or aliases if we wanted, but strict for now
                if potential_agent in AGENT_THEMES:
                    model = potential_agent
                    agent_theme = AGENT_THEMES[model]
                    print_colored(f"\nSwitched to agent: {model} {agent_theme['icon']}", COLORS['WARNING'])
                    print_colored(f"Description: {agent_theme['desc']}", COLORS['BLUE'])
                    

                    
                    # If there's content after the mention, update user_input to be that content
                    # effectively switching AND sending in one go.
                    if len(parts) > 1:
                        user_input = parts[1].strip()
                        if not user_input:
                            continue
                    else:
                        # Just a switch, don't send anything
                        continue
                else:
                    # If it looks like a mention but isn't a valid agent, warn but don't crash
                    # Alternatively, we could just treat it as text. Let's warn.
                    print_colored(f"Unknown agent '{potential_agent}'. Available: {', '.join(AGENT_THEMES.keys())}", COLORS['FAIL'])
                    print_colored("Treating as normal text...", COLORS['BLUE'])

            # Help Command
            if user_input.lower() == '/help':
                print_colored("\n--- Qwen Remote CLI Help ---", COLORS['HEADER'])
                print_colored(f"{COLORS['BOLD']}GENERAL COMMANDS:{COLORS['ENDC']}", COLORS['BLUE'])
                print(f"  /help                - Show this help menu")
                print(f"  /exit, /quit         - Exit the CLI and cleanup resources")
                print(f"  /model <name>        - Switch the active agent (e.g. /model architect)")
                print(f"  /history             - Show recent command history")
                print(f"  /history clear       - Clear command history")
                
                print_colored(f"\n{COLORS['BOLD']}DOCUMENTATION TOOLS:{COLORS['ENDC']}", COLORS['BLUE'])
                print(f"  /cupertino <query>   - Search local Apple documentation on macOS")
                print(f"                         Example: /cupertino MTLMeshRenderPipelineDescriptor")
                print(f"  /apple <tool> <args> - Search Apple Deep Docs on the Linux server")
                print(f"                         Example: /apple search_swift_evolution {{\"feature\": \"actors\"}}")
                print(f"                         Example: /apple fetch_apple_documentation {{\"url\": \"https://developer.apple.com/...\"}}")
                
                print_colored(f"\n{COLORS['BOLD']}AGENT SHORTCUTS:{COLORS['ENDC']}", COLORS['BLUE'])
                print(f"  @<agent_name> [msg]  - Switch agent and optionally send message in one go")
                print(f"                         Example: @architect Design a Metal 4 renderer")
                print(f"                         Example: @debugger Why is this kernel crashing?")
                
                print_colored(f"\n{COLORS['BOLD']}AVAILABLE AGENTS:{COLORS['ENDC']}", COLORS['BLUE'])
                for name, theme in AGENT_THEMES.items():
                    print(f"  {name.ljust(18)} - {theme['desc']}")
                print_colored("----------------------------\n", COLORS['HEADER'])
                continue

            # Apple Documentation Search (Cupertino MCP)
            if user_input.lower().startswith('/cupertino '):
                parts = user_input.split(' ', 1)
                if len(parts) < 2 or not parts[1].strip():
                    print_colored("Usage: /cupertino <query>", COLORS['FAIL'])
                    continue
                query = parts[1].strip()
                result = handle_cupertino_search(query)
                print_colored(f"\n{result}\n", COLORS['GREEN'])
                continue

            # Apple Deep Docs Search (Server-side MCP)
            if user_input.lower().startswith('/apple '):
                parts = user_input.split(' ', 2)
                if len(parts) < 2:
                    print_colored("Usage: /apple <tool_name> [args_json]", COLORS['FAIL'])
                    print_colored("Example: /apple search_swift_evolution {\"feature\": \"actors\"}", COLORS['BLUE'])
                    continue
                
                tool = parts[1]
                args_str = parts[2].strip() if len(parts) > 2 else "{}"
                if not args_str: args_str = "{}"
                
                try:
                    args = json.loads(args_str)
                    if not isinstance(args, dict):
                        print_colored("Error: Arguments must be a JSON object (dictionary).", COLORS['FAIL'])
                        print_colored("Example: /apple tool {\"key\": \"value\"}", COLORS['BLUE'])
                        continue
                        
                    payload = json.dumps({"tool": tool, "arguments": args})
                    result = apple_deep_docs_search(payload)
                    print_colored(f"\n{result}\n", COLORS['GREEN'])
                except json.JSONDecodeError as e:
                    print_colored(f"Error: Invalid JSON arguments: {e}", COLORS['FAIL'])
                    print_colored("Hint: Ensure keys and values are in double quotes.", COLORS['BLUE'])
                    print_colored("Example: /apple tool {\"query\": \"something\"}", COLORS['CYAN'])
                continue

            # History management commands
            if user_input.lower() == '/history':
                if READLINE_AVAILABLE:
                    history_len = readline.get_current_history_length()
                    print_colored(f"\nCommand History ({history_len} entries):", COLORS['HEADER'])
                    for i in range(1, min(history_len + 1, 21)):  # Show last 20
                        idx = max(1, history_len - 20 + i)
                        if idx <= history_len:
                            item = readline.get_history_item(idx)
                            print_colored(f"  {idx}: {item}", COLORS['CYAN'])
                    if history_len > 20:
                        print_colored(f"  ... ({history_len - 20} older entries)", COLORS['BLUE'])
                else:
                    print_colored("Readline not available - no history support", COLORS['WARNING'])
                continue

            if user_input.lower() == '/history clear':
                if READLINE_AVAILABLE:
                    readline.clear_history()
                    print_colored("Command history cleared.", COLORS['GREEN'])
                else:
                    print_colored("Readline not available - no history support", COLORS['WARNING'])
                continue

            if user_input.lower().startswith('/model ') or user_input.lower().startswith('/m '):
                parts = user_input.split(' ')
                if len(parts) < 2 or not parts[1].strip():
                    print_colored("Usage: /model <agent_name>", COLORS['FAIL'])
                    print_colored(f"Available agents: {', '.join(AGENT_THEMES.keys())}", COLORS['BLUE'])
                    continue
                    
                model_name = parts[1].lower()
                if model_name in AGENT_THEMES:
                    model = model_name
                    agent_theme = AGENT_THEMES[model]
                    print_colored(f"\nSwitched to agent: {model} {agent_theme['icon']}", COLORS['WARNING'])
                    print_colored(f"Description: {agent_theme['desc']}", COLORS['BLUE'])

                else:
                    print_colored(f"Unknown agent: {model_name}. Available: {', '.join(AGENT_THEMES.keys())}", COLORS['FAIL'])
                continue

            if not (history and history[-1]["role"] == "user" and history[-1].get("auto_send", False)):
                history.append({"role": "user", "content": user_input})
                save_chat_history(history, model)

            # Sanitize history to remove internal flags like 'auto_send' before sending to server
            sanitized_history = [
                {"role": msg["role"], "content": msg["content"]} 
                for msg in history
            ]
            # Request a large token limit to support generating massive files (e.g., pbxproj)
            # The server/model will stop earlier if the context window (e.g., 20k or 32k) is filled.
            payload = {
                "model": model, 
                "messages": sanitized_history, 
                "stream": True,
                "max_tokens": 30000 
            }
            
            # Smart Reloading:
            # If this is an auto-send (tool output), keep the model loaded for speed.
            # If this is a new user prompt, force reload to clear VRAM/Cache for stability.
            is_auto_send = history and history[-1]["role"] == "user" and history[-1].get("auto_send", False)
            headers = {
                "X-Qwen-Force-Reload": "false" if is_auto_send else "true"
            }
            
            full_response = ""
            server_error_occurred = False
            
            # Progress tracking variables
            start_time = time.time()
            first_token_time = None
            token_count = 0
            stop_progress = threading.Event()

            def show_progress():
                """Display elapsed time while waiting for server and send heartbeats"""
                last_heartbeat = time.time()
                while not stop_progress.is_set():
                    now = time.time()
                    elapsed = now - start_time
                    
                    # Heartbeat every 30 seconds to keep connection alive
                    if now - last_heartbeat > 30:
                        try:
                            requests.get(HEALTH_URL, timeout=2)
                            last_heartbeat = now
                        except:
                            pass # Ignore heartbeat failures

                    # Use carriage return to keep progress on one line
                    sys.stdout.write(f"\r{COLORS['BLUE']}Waiting for server... ({elapsed:.1f}s){COLORS['ENDC']}")
                    sys.stdout.flush()
                    time.sleep(0.1)
                # Clear the line when done
                sys.stdout.write("\r" + " " * 40 + "\r")
                sys.stdout.flush()

            progress_thread = threading.Thread(target=show_progress)
            progress_thread.daemon = True
            progress_thread.start()

            context_retries = 0
            MAX_CONTEXT_RETRIES = 3

            while True: # Retry loop (Connection + Context)
                try:
                    # Increased timeout for heavy model switching and large model inference
                    response = requests.post(API_URL, json=payload, headers=headers, stream=True, timeout=7200)
                    
                    # Handle HTTP Errors (Non-200)
                    if response.status_code != 200:
                        stop_progress.set()
                        error_text = response.text
                        
                        # Check for Context Error
                        if "exceed context window" in error_text or "context_length_exceeded" in error_text:
                            if context_retries < MAX_CONTEXT_RETRIES:
                                print_colored(f"\n[Client] Context limit reached. Trimming history and retrying ({context_retries+1}/{MAX_CONTEXT_RETRIES})...", COLORS['WARNING'])
                                
                                # Trim oldest 25% of history, but keep the last message (current prompt)
                                if len(history) > 2:
                                    current_prompt = history[-1]
                                    trim_index = max(1, int(len(history) * 0.25))
                                    # Ensure we remove pairs if possible to keep flow natural
                                    if trim_index % 2 != 0: trim_index += 1
                                    
                                    # Slice: remove from beginning, keep end
                                    # history[:-1] is past context. history[-1] is current.
                                    trimmed_past = history[:-1][trim_index:]
                                    history = trimmed_past + [current_prompt]
                                    
                                    # Update payload with new history
                                    sanitized_history = [
                                        {"role": msg["role"], "content": msg["content"]} 
                                        for msg in history
                                    ]
                                    payload["messages"] = sanitized_history
                                    
                                    context_retries += 1
                                    # Restart progress indicator
                                    stop_progress = threading.Event()
                                    progress_thread = threading.Thread(target=show_progress)
                                    progress_thread.daemon = True
                                    progress_thread.start()
                                    continue # Retry Request
                                else:
                                     print_colored("\n[Client] Context limit reached, but history is too short to trim.", COLORS['FAIL'])
                        
                        print_colored(f"\nError: {error_text}", COLORS['FAIL'])
                        break

                    # Process Stream
                    for line in response.iter_lines():
                        if line:
                            line = line.decode('utf-8')
                            if line.startswith("data: "):
                                data_str = line[6:]
                                if data_str == "[DONE]":
                                    break
                                try:
                                    data = json.loads(data_str)
                                    if "choices" in data and len(data["choices"]) > 0:
                                        delta = data["choices"][0].get("delta", {})
                                        content = delta.get("content", "")
                                        if content:
                                            if not first_token_time:
                                                first_token_time = time.time()
                                                stop_progress.set() # Stop "Waiting..." timer
                                            
                                            print(content, end="", flush=True)
                                            full_response += content
                                            token_count += 1
                                    elif "error" in data:
                                        stop_progress.set()
                                        error_msg = data['error'].get('message', 'Unknown error')
                                        
                                        # Check for Context Error in Stream
                                        if "exceed context window" in error_msg or "context_length_exceeded" in error_msg:
                                            if context_retries < MAX_CONTEXT_RETRIES:
                                                print_colored(f"\n[Client] Context limit reached (during generation). Trimming history and retrying ({context_retries+1}/{MAX_CONTEXT_RETRIES})...", COLORS['WARNING'])
                                                
                                                if len(history) > 2:
                                                    current_prompt = history[-1]
                                                    trim_index = max(1, int(len(history) * 0.25))
                                                    if trim_index % 2 != 0: trim_index += 1
                                                    
                                                    trimmed_past = history[:-1][trim_index:]
                                                    history = trimmed_past + [current_prompt]
                                                    
                                                    sanitized_history = [
                                                        {"role": msg["role"], "content": msg["content"]} 
                                                        for msg in history
                                                    ]
                                                    payload["messages"] = sanitized_history
                                                    
                                                    context_retries += 1
                                                    server_error_occurred = True # Mark as error to trigger continue logic below
                                                    break # Break inner stream loop to trigger continue
                                        
                                        print_colored(f"\nServer Error: {error_msg}", COLORS['FAIL'])
                                        if "exceed context window" in error_msg:
                                            server_error_occurred = True
                                        break
                                except (json.JSONDecodeError, KeyError, IndexError):
                                    pass
                    
                    # Handle Retry from Stream Error
                    if server_error_occurred and context_retries > 0 and context_retries <= MAX_CONTEXT_RETRIES:
                         # If we marked error AND incremented retries, it means we want to retry
                         # Reset flags
                         server_error_occurred = False
                         full_response = ""
                         token_count = 0
                         # Restart progress
                         stop_progress = threading.Event()
                         progress_thread = threading.Thread(target=show_progress)
                         progress_thread.daemon = True
                         progress_thread.start()
                         continue

                    break # Success, exit retry loop

                except requests.exceptions.Timeout:
                    stop_progress.set()
                    print_colored(f"\nRequest timed out after 7200s.", COLORS['FAIL'])
                    break
                except requests.exceptions.ConnectionError:
                    stop_progress.set()
                    if token_count > 0:
                        print_colored(f"\n[Client] Connection interrupted. Response truncated.", COLORS['WARNING'])
                        break # Do not retry if we already output text (prevents duplicate output loop)

                    if wait_for_server():
                        # Reset timer for retry
                        start_time = time.time()
                        stop_progress = threading.Event()
                        progress_thread = threading.Thread(target=show_progress)
                        progress_thread.daemon = True
                        progress_thread.start()
                        continue # Retry request
                    else:
                        break # User aborted
                except Exception as e:
                    stop_progress.set()
                    print_colored(f"\nUnexpected error during chat: {e}", COLORS['FAIL'])
                    break

            stop_progress.set() # Ensure timer is stopped
            print()
            
            # Print generation stats
            if token_count > 0:
                end_time = time.time()
                total_duration = end_time - start_time
                ttft = first_token_time - start_time
                gen_duration = end_time - first_token_time
                tps = token_count / gen_duration if gen_duration > 0 else 0
                
                stats_msg = f"[Stats] TTFT: {ttft:.2f}s | Total: {total_duration:.2f}s | {token_count} tokens | {tps:.2f} tps"
                print_colored(stats_msg, COLORS['BLUE'])
            
            # Only append non-empty responses to history to avoid 422 errors on next turn
            if full_response and full_response.strip():
                history.append({"role": "assistant", "content": full_response})
                save_chat_history(history, model)
                
                if server_error_occurred:
                    print_colored("\n[Client] Stopping tool execution loop due to server context error.", COLORS['WARNING'])
                else:
                    tool_output = process_remote_commands(full_response)

                    if tool_output:
                        history.append({"role": "user", "content": f"Tool output:\n{tool_output}", "auto_send": True})
                        save_chat_history(history, model)
                        continue
            elif not full_response and not server_error_occurred:
                print_colored("\nWarning: Received empty response or connection closed prematurely.", COLORS['WARNING'])

        except KeyboardInterrupt:
            break

    print_colored("\nGoodbye!", COLORS['HEADER'])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Qwen Remote Client")
    parser.add_argument("--model", type=str, default="implementer", help="Agent model to use")
    args = parser.parse_args()
    
    try:
        chat(args.model)
    except Exception as e:
        print_colored(f"\nCRITICAL CLIENT ERROR: {e}", COLORS['FAIL'])
        import traceback
        traceback.print_exc()
        sys.exit(1)