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
from datetime import datetime
from collections import deque
from typing import Optional, List

# Configuration
LINUX_SERVER_IP = os.getenv("QWEN_SERVER_IP", "192.168.50.101")
API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/chat/completions"

COLORS = {
    "HEADER": "\033[95m",
    "BLUE": "\033[94m",
    "GREEN": "\033[92m",
    "WARNING": "\033[93m",
    "FAIL": "\033[91m",
    "ENDC": "\033[0m",
    "BOLD": "\033[1m",
    "CYAN": "\033[96m"
}

# Agent UI Themes
AGENT_THEMES = {
    "implementer": {"color": COLORS['GREEN'], "icon": "💻", "prompt": "Implementer", "desc": "Code Implementation"},
    "architect":   {"color": COLORS['HEADER'], "icon": "🏗️", "prompt": "Architect", "desc": "System Design (480B Model - Slow Load)"},
    "reviewer":    {"color": COLORS['CYAN'], "icon": "🔍", "prompt": "Reviewer", "desc": "Code Review (480B Model - Slow Load)"},
    "debugger":    {"color": COLORS['FAIL'], "icon": "🐞", "prompt": "Debugger", "desc": "Debugging"},
}

# Command execution security settings
ALLOW_SHELL_MODE = os.getenv('ALLOW_SHELL_MODE', 'false').lower() == 'true'
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


# Initialize job tracker
job_tracker = JobTracker(
    max_jobs=int(os.getenv('JOB_TRACKER_MAX_JOBS', 100)),
    job_ttl_hours=float(os.getenv('JOB_TRACKER_TTL_HOURS', 1))
)


def print_colored(text, color):
    print(f"{color}{text}{COLORS['ENDC']}")


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
                result = subprocess.run(command, shell=True, capture_output=True, text=True, errors='replace', timeout=30)
            else:
                command_args = parse_command_safely(command)
                command_args = expand_paths_in_args(command_args)
                result = subprocess.run(command_args, shell=False, capture_output=True, text=True, errors='replace', timeout=30)

            output = result.stdout + result.stderr
            print_colored(f"Output:\n{output}", COLORS['CYAN'])
            return f"Command executed successfully.\nExit Code: {result.returncode}\nOutput:\n{output}"
        except subprocess.TimeoutExpired:
            return "Command timed out (30s limit for sync commands). Consider using async mode for long-running commands."
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


def process_remote_commands(response_text: str) -> Optional[str]:
    """Process remote command markers in agent response"""
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

    print_colored("\nType '/exit' to quit. Type '/model <name>' to switch agents.\n", COLORS['BLUE'])

    history = []

    while True:
        try:
            if history and history[-1]["role"] == "user" and history[-1].get("auto_send", False):
                print_colored("Sending tool output to agent...", COLORS['BLUE'])
                user_input = history[-1]["content"]
            else:
                user_input = input(f"{COLORS['BOLD']}You > {COLORS['ENDC']}")

            if not user_input.strip():
                continue
            if user_input.lower() in ['/exit', '/quit']:
                break

            if user_input.lower().startswith('/model '):
                model_name = user_input.split(' ')[1]
                if model_name in AGENT_THEMES:
                    model = model_name
                    agent_theme = AGENT_THEMES[model]
                    print_colored(f"\nSwitched to agent: {model} {agent_theme['icon']}", COLORS['WARNING'])
                    print_colored(f"Description: {agent_theme['desc']}", COLORS['BLUE'])
                    if model in ['architect', 'reviewer']:
                        print_colored("NOTE: Switching to 480B model. Loading may take ~30-60 seconds.", COLORS['WARNING'])
                else:
                    print_colored(f"Unknown agent: {model_name}. Available: {', '.join(AGENT_THEMES.keys())}", COLORS['FAIL'])
                continue

            if not (history and history[-1]["role"] == "user" and history[-1].get("auto_send", False)):
                history.append({"role": "user", "content": user_input})

            # Use agent-specific color and prompt
            prompt_text = f"{agent_theme['prompt']} {agent_theme['icon']} > "
            print(f"{agent_theme['color']}{prompt_text}{COLORS['ENDC']}", end="", flush=True)

            payload = {"model": model, "messages": history, "stream": True}
            full_response = ""

            try:
                # Increased timeout for heavy model switching
                response = requests.post(API_URL, json=payload, stream=True, timeout=1200)
                if response.status_code != 200:
                    print_colored(f"\nError: {response.text}", COLORS['FAIL'])
                    continue

                for line in response.iter_lines():
                    if line:
                        line = line.decode('utf-8')
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                break
                            try:
                                data = json.loads(data_str)
                                if "choices" in data:
                                    content = data["choices"][0]["delta"].get("content", "")
                                    if content:
                                        print(content, end="", flush=True)
                                        full_response += content
                            except json.JSONDecodeError:
                                pass

                print()
                history.append({"role": "assistant", "content": full_response})

                tool_output = process_remote_commands(full_response)

                if tool_output:
                    history.append({"role": "user", "content": f"Tool output:\n{tool_output}", "auto_send": True})
                    continue

            except requests.exceptions.ConnectionError:
                print_colored(f"\nConnection failed! Is the server at {LINUX_SERVER_IP} reachable?", COLORS['FAIL'])

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