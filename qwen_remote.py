#!/usr/bin/env python3
"""
Qwen Remote Client
Interactive CLI for connecting to the Qwen Multi-Agent Server
"""
import sys
import os
import json
import argparse
import re
import subprocess
import shlex
import threading
import urllib3
import atexit
import time
import tempfile
import select
import logging

# Thread lock for managing pending tasks safely
_pending_tasks_lock = threading.Lock()

from datetime import datetime
from typing import Optional, List
import requests
import difflib

# Rich terminal rendering (optional — graceful fallback)
try:
    from rich.console import Console
    from rich.syntax import Syntax
    from rich.panel import Panel
    RICH_AVAILABLE = True
    rich_console = Console()
except ImportError:
    RICH_AVAILABLE = False
    rich_console = None

# Readline for command history and CLI editing
try:
    import readline
    READLINE_AVAILABLE = True
except ImportError:
    READLINE_AVAILABLE = False

# Configuration class
class Config:
    LINUX_SERVER_IP = os.getenv("QWEN_SERVER_IP", "192.168.50.101")
    API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/chat/completions"
    MEMORY_API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/memory"
    INGEST_API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/memory/ingest"
    SEARCH_API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/tools/search"
    DEEP_DOCS_API_URL = f"http://{LINUX_SERVER_IP}:5000/v1/tools/apple_deep_docs"
    HEALTH_URL = f"http://{LINUX_SERVER_IP}:5000/health"
    MODELS_URL = f"http://{LINUX_SERVER_IP}:5000/v1/models"
    ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

    # Command execution security settings
    ALLOW_SHELL_MODE = os.getenv('ALLOW_SHELL_MODE', 'true').lower() == 'true'
    COMMAND_WHITELIST = os.getenv('COMMAND_WHITELIST', '').split(',') if os.getenv('COMMAND_WHITELIST') else None

    # Permission mode: 'default' (prompt all), 'acceptEdits' (auto-approve file ops), 'yolo' (auto-approve all)
    # Backward compat: ALLOW_ALL=true maps to 'yolo' if PERMISSION_MODE not explicitly set
    _explicit_perm = os.getenv('PERMISSION_MODE')
    if _explicit_perm:
        PERMISSION_MODE = _explicit_perm
    elif os.getenv('ALLOW_ALL', 'false').lower() == 'true':
        PERMISSION_MODE = 'yolo'
    else:
        PERMISSION_MODE = 'default'
    ALLOW_ALL = PERMISSION_MODE == 'yolo'  # backward compat for any direct checks

    # Display settings
    VERBOSE_MODE = True  # True=full tool output, False=compact one-liners

    # Session settings
    SESSION_NAME = None

    # Temporary file settings
    HISTORY_FILE = os.path.expanduser("~/.qwen_client_history")
    CHAT_HISTORY_FILE = os.path.expanduser("~/.qwen_chat_history.json")
    HISTORY_MAX_LENGTH = 1000

    # Chunking settings
    CHUNK_SIZE = 6000
    CHUNK_OVERLAP = 500
    CHUNK_THRESHOLD = 8000
    MAX_DISPLAY_CHARS = 8000

    # Timeout settings
    COMMAND_TIMEOUT = 240  # seconds
    REQUEST_TIMEOUT = 30   # seconds for API requests
    LONG_REQUEST_TIMEOUT = 60  # seconds for longer operations like PDF ingestion

# Initialize configuration
config = Config()

# Setup structured logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.expanduser("~/.qwen_client.log")),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

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

# Colors with readline non-printing escape codes (ONLY for input prompts)
PROMPT_COLORS = {
    k: f"\001{v}\002" for k, v in COLORS.items()
}

# Theme definitions (Visuals only)
THEME_STYLES = {
    "implementer": {"color": COLORS['GREEN'], "icon": "💻", "prompt": "Implementer"},
    "fast_implementer": {"color": COLORS['GREEN'], "icon": "⏩", "prompt": "Fast Implementer"},
    "architect":   {"color": COLORS['HEADER'], "icon": "🏗️", "prompt": "Architect"},
    "reviewer":    {"color": COLORS['CYAN'], "icon": "🔍", "prompt": "Reviewer"},
    "debugger":    {"color": COLORS['FAIL'], "icon": "🐞", "prompt": "Debugger"},
    "metal_implementer": {"color": COLORS['BLUE'], "icon": "🤘", "prompt": "Metal"},
    "m25_implementer": {"color": COLORS['GREEN'], "icon": "🌙", "prompt": "M2.5 Impl"},
    "m25_architect": {"color": COLORS['HEADER'], "icon": "🌙", "prompt": "M2.5 Arch"},
    "default":     {"color": COLORS['WARNING'], "icon": "🤖", "prompt": "Agent"},
}

# This will be populated from the server
AGENT_THEMES = {}

def set_terminal_title(title):
    """Set the terminal window title using ANSI escape codes"""
    sys.stdout.write(f"\033]0;{title}\007")
    sys.stdout.flush()

def send_macos_notification(text, title="Qwen Client"):
    """Send a native macOS notification via osascript"""
    if sys.platform != 'darwin':
        return
    try:
        # Escape quotes for AppleScript
        safe_text = text.replace('"', '\\"')
        safe_title = title.replace('"', '\\"')
        cmd = f'display notification "{safe_text}" with title "{safe_title}"'
        subprocess.run(['osascript', '-e', cmd], check=False, stderr=subprocess.DEVNULL)
    except Exception:
        pass # Fail silently

def fetch_available_models():
    """Fetch available models from the server and populate AGENT_THEMES"""
    global AGENT_THEMES
    try:
        response = requests.get(config.MODELS_URL, timeout=config.REQUEST_TIMEOUT)
        if response.status_code == 200:
            data = response.json().get("data", [])
            AGENT_THEMES.clear()
            for model in data:
                mid = model["id"]
                # Get style or fallback to default
                style = THEME_STYLES.get(mid, THEME_STYLES["default"])

                AGENT_THEMES[mid] = {
                    "color": style["color"],
                    "icon": style["icon"],
                    "prompt": style["prompt"],
                    "desc": model["description"]  # Description from server
                }
            # print_colored(f"Loaded {len(AGENT_THEMES)} agents from server.", COLORS['GREEN'])
        else:
            print_colored(f"Failed to fetch models: {response.status_code}", COLORS['FAIL'])
            _load_fallback_themes()
    except Exception as e:
        # Fallback to hardcoded defaults if server is unreachable
        _load_fallback_themes()

def _load_fallback_themes():
    """Load default themes if server is unreachable"""
    global AGENT_THEMES
    AGENT_THEMES.clear()
    defaults = {
        "implementer": "Code Agent (Offline)",
        "fast_implementer": "Fast Code Agent (Offline)",
        "architect": "System Design (Offline)",
        "reviewer": "Code Review (Offline)",
        "debugger": "Debugging (Offline)",
        "metal_implementer": "Metal & Graphics (Offline)",
        "m25_implementer": "MiniMax M2.5 Implementer (Offline)",
        "m25_architect": "MiniMax M2.5 Architect (Offline)"
    }
    for mid, desc in defaults.items():
        style = THEME_STYLES.get(mid, THEME_STYLES["default"])
        AGENT_THEMES[mid] = {
            "color": style["color"],
            "icon": style["icon"],
            "prompt": style["prompt"],
            "desc": desc
        }

# Register cleanup on exit (DISABLED: Server now handles auto-unload)
# atexit.register(cleanup_server_resources)

_INTERNAL_KEYS = {"auto_send", "_retried", "_retried_prompt"}

def save_chat_history(history, current_agent="implementer"):
    """Save full chat history and metadata to file, stripping internal keys"""
    try:
        # Proactively prune history to prevent excessive growth
        pruned_history = _prune_history(history)

        cleaned = []
        for msg in pruned_history:
            clean_msg = {k: v for k, v in msg.items() if k not in _INTERNAL_KEYS}
            cleaned.append(clean_msg)
        data = {
            "messages": cleaned,
            "last_agent": current_agent,
            "session_name": config.SESSION_NAME,
            "timestamp": datetime.now().isoformat()
        }
        with open(config.CHAT_HISTORY_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print_colored(f"Warning: Failed to save chat history: {e}", COLORS['WARNING'])


def _prune_history(history, max_messages=100):
    """Prune history to prevent excessive growth, keeping important context"""
    if len(history) <= max_messages:
        return history

    # Keep the first few messages (initial context) and the last max_messages-n messages
    n_keep_start = 10  # Keep first 10 messages
    n_keep_end = max_messages - n_keep_start  # Keep last n messages

    pruned = history[:n_keep_start] + history[-n_keep_end:]

    print_colored(f"Pruned chat history from {len(history)} to {len(pruned)} messages", COLORS['WARNING'])
    return pruned

def load_chat_history():
    """Load chat history from file if it exists. Returns (history, last_agent, session_name)"""
    if os.path.exists(config.CHAT_HISTORY_FILE):
        try:
            with open(config.CHAT_HISTORY_FILE, 'r') as f:
                data = json.load(f)

            # Handle both old format (list) and new format (dict)
            if isinstance(data, list):
                history = data
                last_agent = "implementer"
            else:
                history = data.get("messages", [])
                last_agent = data.get("last_agent", "implementer")
                # Restore session name
                saved_name = data.get("session_name")
                if saved_name and not config.SESSION_NAME:
                    config.SESSION_NAME = saved_name

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
    print_colored(f"\nConnection lost. Waiting for server at {config.LINUX_SERVER_IP}...", COLORS['WARNING'])
    while True:
        try:
            response = requests.get(config.HEALTH_URL, timeout=config.REQUEST_TIMEOUT)
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
        if os.path.exists(config.HISTORY_FILE):
            readline.read_history_file(config.HISTORY_FILE)
    except (IOError, OSError):
        pass  # History file doesn't exist or isn't readable

    # Set history length
    readline.set_history_length(config.HISTORY_MAX_LENGTH)

    # Register save on exit
    atexit.register(save_readline_history)

    # Configure readline behavior
    # Enable auto-complete on tab (basic filename completion)
    if (readline.__doc__ and 'libedit' in readline.__doc__) or sys.platform == 'darwin':
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
        readline.write_history_file(config.HISTORY_FILE)
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




# Register cleanup on exit

# Track temporary files with creation time for cleanup
_temp_files = {}  # Maps path -> creation_time

def _cleanup_temp_files():
    """Remove all tracked temporary files on exit"""
    for path in list(_temp_files.keys()):
        try:
            if os.path.exists(path):
                os.remove(path)
        except PermissionError as e:
            print_colored(f"Permission denied removing temp file {path}: {e}", COLORS["WARNING"])
        except OSError as e:
            print_colored(f"OS error removing temp file {path}: {e}", COLORS["WARNING"])
        except Exception as e:
            print_colored(f"Unexpected error removing temp file {path}: {e}", COLORS["FAIL"])
    _temp_files.clear()


def _cleanup_old_temp_files(max_age_minutes=60):
    """Remove temporary files older than max_age_minutes"""
    current_time = time.time()
    expired_files = []

    for path, creation_time in _temp_files.items():
        if current_time - creation_time > max_age_minutes * 60:
            expired_files.append(path)

    for path in expired_files:
        try:
            if os.path.exists(path):
                os.remove(path)
                print_colored(f"Cleaned up old temp file: {path}", COLORS['WARNING'])
        except Exception as e:
            print_colored(f"Failed to clean up temp file {path}: {e}", COLORS['FAIL'])
        finally:
            _temp_files.pop(path, None)


def _add_temp_file(path):
    """Add a temporary file to tracking with timestamp"""
    _temp_files[path] = time.time()

    # Clean up old files periodically
    if len(_temp_files) % 10 == 0:  # Every 10 new files
        _cleanup_old_temp_files()


def _remove_temp_file(path):
    """Remove a temporary file from tracking"""
    _temp_files.pop(path, None)


atexit.register(_cleanup_temp_files)


def print_colored(text, color):
    print(f"{color}{text}{COLORS['ENDC']}")


def ingest_url_content(url):
    """Fetch URL content, strip HTML, and send to server memory."""
    try:
        print_colored(f"[Client] Deep-ingesting content from {url}...", COLORS['BLUE'])
        response = requests.get(url, timeout=20)
        if response.status_code != 200:
            return f"Error: Failed to fetch URL (Status {response.status_code})"

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(response.text, 'html.parser')
        for script in soup(["script", "style"]):
            script.extract()
        text = soup.get_text(separator=' ', strip=True)

        payload = {"text": f"Source URL: {url}\n\n{text}"}
        mem_resp = requests.post(config.MEMORY_API_URL, json=payload, timeout=30)

        if mem_resp.status_code == 200:
            return f"Successfully ingested full content from {url} into RAG database."
        else:
            return f"Error: Server rejected memory ingestion ({mem_resp.status_code})"
    except ImportError:
        return "Error: BeautifulSoup4 is required on the client for deep-ingestion. Run 'pip install beautifulsoup4'."
    except Exception as e:
        return f"Error during deep-ingestion: {str(e)}"


def save_memory(text):
    """Send a memory/fact to the server to be saved"""
    try:
        response = requests.post(config.MEMORY_API_URL, json={"text": text}, timeout=config.REQUEST_TIMEOUT)
        if response.status_code == 200:
            print_colored(f"Memory Saved: {text[:60]}...", COLORS['GREEN'])
            return f"Memory saved successfully."
        else:
            return f"Failed to save memory: {response.text}"
    except Exception as e:
        return f"Error saving memory: {str(e)}"


def ingest_pdf(path):
    """Tell the server to ingest a local PDF file"""
    try:
        print_colored(f"Requesting server to ingest PDF: {path}", COLORS['CYAN'])
        response = requests.post(config.INGEST_API_URL, json={"path": path}, timeout=config.LONG_REQUEST_TIMEOUT)
        if response.status_code == 200:
            result = response.json()
            msg = f"Successfully ingested {result['filename']}: {result['chunks']} chunks from {result['pages']} pages."
            print_colored(msg, COLORS['GREEN'])
            return msg
        else:
            error_msg = f"Failed to ingest PDF: {response.text}"
            print_colored(error_msg, COLORS['FAIL'])
            return error_msg
    except Exception as e:
        return f"Error ingesting PDF: {str(e)}"




def web_search(query):
    """Send a search query to the server"""
    try:
        print_colored(f"Searching web for: {query}", COLORS['CYAN'])
        response = requests.post(config.SEARCH_API_URL, json={"query": query}, timeout=config.REQUEST_TIMEOUT)
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
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1
            )
            return True
        except Exception as e:
            print_colored(f"Error starting Cupertino MCP: {e}", COLORS['FAIL'])
            return False

    def stop(self):
        """Stop the Cupertino MCP server process"""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None

    def _readline_with_timeout(self, timeout: float = 30) -> Optional[str]:
        """Read a line from the subprocess stdout with a timeout using select.

        Returns the line string, or None on timeout / EOF.
        """
        if not self.process or not self.process.stdout:
            return None

        # Poll stdout for data
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        if ready:
            return self.process.stdout.readline()

        print_colored(f"Cupertino MCP readline timed out after {timeout:.1f} seconds", COLORS['WARNING'])
        return None

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

                # Read lines until we get the matching response ID,
                # skipping notifications and non-matching messages
                for _ in range(50):  # safety cap
                    line = self._readline_with_timeout(timeout=30)
                    if not line:
                        if self.process.poll() is not None:
                            return {"error": "Cupertino MCP server process exited unexpectedly."}
                        return {"error": "No response from Cupertino MCP (timed out)."}

                    line = line.strip()
                    if not line:
                        continue

                    try:
                        response = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if response.get("id") == req_id:
                        return response.get("result", {})
                    # else: skip notifications / mismatched IDs

                return {"error": "Cupertino MCP sent too many non-matching responses"}
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
atexit.register(cupertino_client.stop)


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


def handle_apple_deep_docs(payload_str):
    """Handle Apple Deep Docs command with proper error handling"""
    try:
        # Parse the payload string as JSON
        payload = json.loads(payload_str)
        tool = payload.get("tool")
        args = payload.get("arguments", {})

        if not tool:
            return "Error: Missing 'tool' in Apple Deep Docs payload"

        return apple_deep_docs_search(tool, args)
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in Apple Deep Docs payload: %s", payload_str)
        return f"Error: Invalid JSON in Apple Deep Docs payload: {str(e)}"
    except Exception as e:
        logger.error("Error handling Apple Deep Docs: %s", str(e))
        return f"Error handling Apple Deep Docs: {str(e)}"


def apple_deep_docs_search(tool, args):
    """Send a deep doc search query to the server"""
    try:
        print_colored(f"Calling Apple Deep Docs ({tool}): {args}", COLORS['CYAN'])

        payload = {"tool": tool, "arguments": args}
        response = requests.post(config.DEEP_DOCS_API_URL, json=payload, timeout=config.LONG_REQUEST_TIMEOUT)

        if response.status_code == 200:
            result = response.json().get("result", "No results")

            # Format result for display and memory
            formatted_result = ""
            if isinstance(result, (dict, list)):
                formatted_result = json.dumps(result, indent=2)
            elif isinstance(result, str):
                try:
                    # Try to parse string as JSON for pretty printing
                    parsed = json.loads(result)
                    formatted_result = json.dumps(parsed, indent=2)
                except Exception:
                    formatted_result = result
            else:
                formatted_result = str(result)

            # Save to memory for grounding (limit size)
            save_memory(f"Apple Deep Doc ({tool}): {str(args)}\n{formatted_result[:10000]}")
            return f"Apple Deep Docs Result ({tool}):\n{formatted_result}"
        else:
            return f"Failed to call Deep Docs: {response.text}"
    except Exception as e:
        return f"Error in Deep Docs call: {str(e)}"


# ---------------------------------------------------------------------------
# Tool handlers (extracted to tool_handlers.py)
# ---------------------------------------------------------------------------
import tool_handlers

tool_handlers.configure(
    config=config,
    colors=COLORS,
    print_colored_fn=print_colored,
    logger_inst=logger,
    temp_tracker={'add': _add_temp_file, 'remove': _remove_temp_file, 'cleanup': _cleanup_old_temp_files},
    external_handlers={
        'save_memory': save_memory,
        'web_search': web_search,
        'handle_cupertino_search': handle_cupertino_search,
        'handle_apple_deep_docs': handle_apple_deep_docs,
        'ingest_pdf': ingest_pdf,
        'ingest_url_content': ingest_url_content,
    },
    rich_console=rich_console if RICH_AVAILABLE else None,
    permission_mode=config.PERMISSION_MODE,
    verbose_mode=config.VERBOSE_MODE,
)

from tool_handlers import (
    parse_command_safely, expand_paths_in_args, is_command_allowed,
    chunk_large_output, get_chunk_for_display, execute_remote_command,
    read_file_content, write_file_content, edit_file_content,
    list_directory, glob_files, grep_search, ingest_pdf_content,
    process_remote_commands, extract_fallback_commands,
    undo_last_checkpoint, reset_write_counts,
)

# Global queue for interrupted multi-agent chains
PENDING_TASKS = []



def _trim_history_for_context(history):
    """Trim history when context limit is reached"""
    if len(history) > 2:
        trim_index = max(1, int(len(history) * 0.25))
        # Walk forward until we land on a "user" message to keep valid conversation structure
        while trim_index < len(history) - 1 and history[trim_index]["role"] != "user":
            trim_index += 1
        trimmed_past = history[trim_index:-1]
        history[:] = trimmed_past + [history[-1]]
        
        # Re-sanitize history for the retry payload
        sanitized_retry = []
        valid_roles = {"system", "user", "assistant"}
        for m in history:
            content = m.get("content", "").strip()
            role = m.get("role", "")
            if content and role in valid_roles:
                sanitized_retry.append({"role": role, "content": content})
        if not sanitized_retry:
            sanitized_retry.append({"role": "user", "content": "Hello"})
        return sanitized_retry
    return None


def _compress_history(messages, keep_recent=6, summary_len=200):
    """Compress older messages to reduce context usage.

    Keeps the last `keep_recent` messages at full fidelity.
    Older tool-output messages (> 500 chars) and large assistant messages
    (> 2000 chars) are truncated to head + tail summaries.
    """
    if len(messages) <= keep_recent:
        return messages

    compressed = []
    cutoff = len(messages) - keep_recent

    for i, msg in enumerate(messages):
        if i < cutoff:
            content = msg["content"]
            is_tool_output = content.startswith("Tool output:\n")
            threshold = 500 if is_tool_output else 2000

            if len(content) > threshold:
                head = content[:summary_len]
                tail = content[-summary_len:]
                msg = {**msg, "content": f"{head}\n... [truncated, was {len(content)} chars] ...\n{tail}"}
        compressed.append(msg)

    return compressed


def get_completion(history, model, agent_theme):
    """Internal helper to get a completion from the server with full error handling and streaming.

    Returns (response_text, finish_reason) on success, or (None, None) on failure.
    finish_reason is "stop" for normal completion, "length" for truncation.
    """
    full_response = ""
    finish_reason = "stop"
    server_error_occurred = False
    
    # Sanitize history to remove internal flags like 'auto_send'
    # AND filter out invalid messages that cause 422 errors on server
    sanitized_history = []
    valid_roles = {"system", "user", "assistant"}
    
    for msg in history:
        content = msg.get("content", "").strip()
        role = msg.get("role", "")
        
        # skip empty content or invalid roles
        if content and role in valid_roles:
            sanitized_history.append({"role": role, "content": content})
            
    # If history is empty after sanitization (e.g. only had empty messages), add a fallback prompt
    if not sanitized_history:
        sanitized_history.append({"role": "user", "content": "Hello"})

    # Compress older messages to save context
    sanitized_history = _compress_history(sanitized_history)

    payload = {
        "model": model, 
        "messages": sanitized_history, 
        "stream": True,
        "max_tokens": 30000 
    }

    # Progress tracking
    start_time = time.time()
    first_token_time = None
    chunk_count = 0
    stop_progress = threading.Event()

    def show_progress():
        last_heartbeat = time.time()
        while not stop_progress.is_set():
            now = time.time()
            elapsed = now - start_time
            if now - last_heartbeat > 30:
                try: requests.get(config.HEALTH_URL, timeout=2); last_heartbeat = now
                except Exception: pass
            sys.stdout.write(f"\r{COLORS['BLUE']}Waiting for {model}... ({elapsed:.1f}s){COLORS['ENDC']}")
            sys.stdout.flush()
            time.sleep(0.1)
        sys.stdout.write("\r" + " " * 50 + "\r")
        sys.stdout.flush()

    progress_thread = threading.Thread(target=show_progress, daemon=True)
    progress_thread.start()

    context_retries = 0
    MAX_CONTEXT_RETRIES = 3
    TTFT_TIMEOUT = 600  # seconds — bail if no first token within 10 minutes

    def _is_context_error(text):
        return any(phrase in text for phrase in (
            "exceed context window", "context_length_exceeded",
            "fills the entire context window",
        ))

    def _try_trim_and_retry():
        nonlocal context_retries
        if context_retries < MAX_CONTEXT_RETRIES:
            context_retries += 1
            print_colored(
                f"\n[Client] Context limit likely reached. Trimming history and retrying "
                f"({context_retries}/{MAX_CONTEXT_RETRIES})...",
                COLORS['WARNING']
            )
            sanitized_retry = _trim_history_for_context(history)
            if sanitized_retry:
                payload["messages"] = sanitized_retry
                return True
        return False

    while True:
        try:
            response = requests.post(config.API_URL, json=payload, stream=True, timeout=7200)

            if response.status_code != 200:
                stop_progress.set()
                error_text = response.text
                if _is_context_error(error_text) and _try_trim_and_retry():
                    stop_progress = threading.Event(); progress_thread = threading.Thread(target=show_progress, daemon=True); progress_thread.start()
                    continue
                print_colored(f"\nError: {error_text}", COLORS['FAIL'])
                return None, None

            try:
                for line in response.iter_lines():
                    # TTFT deadline: if we haven't received a first token yet,
                    # check if we've exceeded the timeout.  The server is likely
                    # stuck tokenizing an oversized prompt.
                    if not first_token_time and (time.time() - start_time) > TTFT_TIMEOUT:
                        stop_progress.set()
                        response.close()
                        if _try_trim_and_retry():
                            server_error_occurred = True
                            break
                        print_colored(f"\n[Client] TTFT timeout ({TTFT_TIMEOUT}s) — server not responding.", COLORS['FAIL'])
                        return None, None

                    if line:
                        line = line.decode('utf-8')
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str == "[DONE]": break
                            try:
                                data = json.loads(data_str)
                                if "choices" in data and len(data["choices"]) > 0:
                                    choice = data["choices"][0]
                                    delta = choice.get("delta", {})
                                    content = delta.get("content", "")
                                    if content:
                                        if not first_token_time:
                                            first_token_time = time.time()
                                            stop_progress.set()
                                        print(content, end="", flush=True)
                                        full_response += content
                                        chunk_count += 1
                                    # Capture finish_reason from the final chunk
                                    if choice.get("finish_reason"):
                                        finish_reason = choice["finish_reason"]
                                elif "error" in data:
                                    stop_progress.set()
                                    error_msg = data['error'].get('message', 'Unknown error')
                                    if _is_context_error(error_msg) and _try_trim_and_retry():
                                        server_error_occurred = True
                                        break
                                    print_colored(f"\nServer Error: {error_msg}", COLORS['FAIL'])
                                    finish_reason = "error"
                                    break
                            except json.JSONDecodeError:
                                pass
                            except Exception as e:
                                logger.debug("SSE parse error: %s", e)
            except KeyboardInterrupt:
                stop_progress.set()
                try:
                    response.close()
                except Exception:
                    pass
                if full_response:
                    print_colored("\n[Interrupted] Keeping partial response.", COLORS['WARNING'])
                    finish_reason = "interrupted"
                    break
                else:
                    return None, None

            if server_error_occurred and context_retries <= MAX_CONTEXT_RETRIES:
                 server_error_occurred = False; full_response = ""; chunk_count = 0
                 start_time = time.time(); first_token_time = None
                 stop_progress = threading.Event(); progress_thread = threading.Thread(target=show_progress, daemon=True); progress_thread.start()
                 continue
            break

        except requests.exceptions.ConnectionError:
            stop_progress.set()
            if chunk_count > 0: break
            if wait_for_server():
                start_time = time.time(); stop_progress = threading.Event(); progress_thread = threading.Thread(target=show_progress, daemon=True); progress_thread.start()
                continue
            return None, None
        except (requests.exceptions.ChunkedEncodingError, urllib3.exceptions.ProtocolError) as e:
            stop_progress.set()
            print_colored(f"\n[Client] Connection interrupted: {e}. Retrying...", COLORS['WARNING'])
            if chunk_count > 0: 
                # If we already got some data, we can't easily resume seamlessly without server support for offsets
                # For now, we return what we have or treat it as a failure depending on needs.
                # Let's try to just return None to trigger a full retry if possible, or break if that's risky.
                # Given the user wants recovery, a full retry of the generation is usually safest.
                full_response = ""; chunk_count = 0
                start_time = time.time(); stop_progress = threading.Event(); progress_thread = threading.Thread(target=show_progress, daemon=True); progress_thread.start()
                continue
            # If no data yet, definitely retry
            start_time = time.time(); stop_progress = threading.Event(); progress_thread = threading.Thread(target=show_progress, daemon=True); progress_thread.start()
            continue
        except Exception as e:
            stop_progress.set(); print_colored(f"\nUnexpected error: {e}", COLORS['FAIL'])
            return None, None

    stop_progress.set()
    print()
    if chunk_count > 0:
        end_time = time.time(); total_duration = end_time - start_time
        ttft = first_token_time - start_time; gen_duration = end_time - first_token_time
        cps = chunk_count / gen_duration if gen_duration > 0 else 0
        print_colored(f"[Stats] {model}: TTFT: {ttft:.2f}s | Total: {total_duration:.2f}s | {chunk_count} chunks | {cps:.2f} chunks/s", COLORS['CYAN'])

    if finish_reason == "length":
        print_colored("[Truncated] Response hit token limit — continuation needed.", COLORS['WARNING'])

    return full_response, finish_reason


def handle_user_command(user_input, history, model, agent_theme):
    """Handle special slash commands. Returns (should_continue, updated_model)"""
    # Help Command
    if user_input.lower() == '/help':
        print_colored("\n--- Qwen Remote CLI Help ---", COLORS['HEADER'])
        print_colored(f"{COLORS['BOLD']}GENERAL COMMANDS:{COLORS['ENDC']}", COLORS['BLUE'])
        print(f"  /help                - Show this help menu")
        print(f"  /exit, /quit         - Exit the CLI and cleanup resources")
        print(f"  /model <name>        - Switch the active agent (e.g. /model architect)")
        print(f"  /clear               - Clear conversation history and start fresh")
        print(f"  /resume              - Resume interrupted multi-agent tasks")
        print(f"  /history             - Show recent command history")
        print(f"  /history clear       - Clear command history")

        print_colored(f"\n{COLORS['BOLD']}SESSION & DISPLAY:{COLORS['ENDC']}", COLORS['BLUE'])
        print(f"  /permissions         - Cycle permission mode (default -> acceptEdits -> yolo)")
        print(f"  /verbose             - Toggle verbose/compact tool output display")
        print(f"  /context             - Show context window usage (tokens, budget)")
        print(f"  /compact             - Manually compress conversation history")
        print(f"  /undo                - Revert the last file modification")
        print(f"  /rename <name>       - Name or rename the current session")
        print(f"  \\ + Enter            - Multiline input (backslash continuation)")
        print(f"  Ctrl+C               - Interrupt generation (keeps partial response)")

        print_colored(f"\n{COLORS['BOLD']}DOCUMENTATION TOOLS:{COLORS['ENDC']}", COLORS['BLUE'])
        print(f"  /cupertino <query>   - Search local Apple documentation on macOS")
        print(f"                         Example: /cupertino MTLMeshRenderPipelineDescriptor")
        print(f"  /apple <tool> <args> - Search Apple Deep Docs on the Linux server")
        print(f"                         Example: /apple search_swift_evolution {{\"feature\": \"actors\"}}")
        print(f"  /ingest <path>       - Ingest a PDF into memory (supports server files or local: prefix for client files)")
        print(f"                         Examples: /ingest /home/user/Metal4_Specs.pdf")
        print(f"                                   /ingest local:/Users/me/Reports/annual.pdf")
        print(f"  /scrape [framework]  - Run the documentation scraper (default: Metal)")
        print(f"                         Example: /scrape MetalFX")
        
        print_colored(f"\n{COLORS['BOLD']}AGENT SHORTCUTS:{COLORS['ENDC']}", COLORS['BLUE'])
        print(f"  @<agent_name> [msg]  - Switch agent and optionally send message in one go")
        print(f"                         Example: @architect Design a Metal 4 renderer")
        print(f"                         Example: @debugger Why is this kernel crashing?")
        print(f"  MULTI-AGENT:         - You can use multiple @ mentions in one prompt!")
        print(f"                         Example: @architect Design X then @implementer build it.")
        
        print_colored(f"\n{COLORS['BOLD']}AVAILABLE AGENTS:{COLORS['ENDC']}", COLORS['BLUE'])
        for name, theme in AGENT_THEMES.items():
            print(f"  {name.ljust(18)} - {theme['desc']}")
        print_colored("----------------------------\n", COLORS['HEADER'])
        return True, model

    # Ingest PDF Command
    if user_input.lower().startswith('/ingest '):
        parts = user_input.split(' ', 1)
        if len(parts) < 2 or not parts[1].strip():
            print_colored("Usage: /ingest <path> (path can be on server or local: prefix for client files)", COLORS['FAIL'])
            print_colored("  Examples:", COLORS['BLUE'])
            print_colored("    /ingest /path/on/server.pdf", COLORS['BLUE'])
            print_colored("    /ingest local:/path/on/client.pdf", COLORS['BLUE'])
            return True, model
        path = parts[1].strip()
        
        # Use the same logic as the tool version to handle local files
        result = ingest_pdf_content(path)
        print_colored(f"\n{result}\n", COLORS['GREEN'])
        return True, model

    # Scrape Documentation Command
    if user_input.lower().startswith('/scrape'):
        parts = user_input.split(' ', 1)
        framework_arg = ""
        
        if len(parts) > 1 and parts[1].strip():
            framework_arg = f" {parts[1].strip()}"
            print_colored(f"Starting documentation scraper for '{parts[1].strip()}' on the server...", COLORS['CYAN'])
        else:
            print_colored("Starting Metal documentation scraper on the server...", COLORS['CYAN'])
            
        # Run the main.py scraper orchestrator with optional argument
        scrape_cmd = f"cd scraping && python3 main.py{framework_arg}"
        result = execute_remote_command(scrape_cmd)
        print_colored(f"\n{result}\n", COLORS['GREEN'])
        return True, model

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
                # Modifying user_input here won't affect the caller directly unless returned
                # This specific logic for @ handling is complex to extract cleanly without changing flow
                # So we return the new model but return False to indicate "don't continue loop, process as task"
                return False, model 
            else:
                # Just a switch, don't send anything
                return True, model
        else:
            # If it looks like a mention but isn't a valid agent, warn but don't crash
            # Alternatively, we could just treat it as text. Let's warn.
            print_colored(f"Unknown agent '{potential_agent}'. Available: {', '.join(AGENT_THEMES.keys())}", COLORS['FAIL'])
            print_colored("Treating as normal text...", COLORS['BLUE'])
            return False, model

    # Apple Documentation Search (Cupertino MCP)
    if user_input.lower().startswith('/cupertino '):
        parts = user_input.split(' ', 1)
        if len(parts) < 2 or not parts[1].strip():
            print_colored("Usage: /cupertino <query>", COLORS['FAIL'])
            return True, model
        query = parts[1].strip()
        result = handle_cupertino_search(query)
        print_colored(f"\n{result}\n", COLORS['GREEN'])
        return True, model

    # Apple Deep Docs Search (Server-side MCP)
    if user_input.lower().startswith('/apple '):
        parts = user_input.split(' ', 2)
        if len(parts) < 2:
            print_colored("Usage: /apple <tool_name> [args_json]", COLORS['FAIL'])
            print_colored("Example: /apple search_swift_evolution {\"feature\": \"actors\"}", COLORS['BLUE'])
            return True, model
        
        tool = parts[1]
        args_str = parts[2].strip() if len(parts) > 2 else "{}"
        if not args_str: args_str = "{}"
        
        try:
            args = json.loads(args_str)
            if not isinstance(args, dict):
                print_colored("Error: Arguments must be a JSON object (dictionary).", COLORS['FAIL'])
                print_colored("Example: /apple tool {\"key\": \"value\"}", COLORS['BLUE'])
                return True, model
                
            result = apple_deep_docs_search(tool, args)
            print_colored(f"\n{result}\n", COLORS['GREEN'])
        except json.JSONDecodeError as e:
            print_colored(f"Error: Invalid JSON arguments: {e}", COLORS['FAIL'])
            print_colored("Hint: Ensure keys and values are in double quotes.", COLORS['BLUE'])
            print_colored("Example: /apple tool {\"query\": \"something\"}", COLORS['CYAN'])
        return True, model

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
            # Fallback: show recent chat history when readline is unavailable
            if history:
                recent = history[-20:]
                print_colored(f"\nChat History ({len(history)} messages, showing last {len(recent)}):", COLORS['HEADER'])
                for msg in recent:
                    role = msg["role"].capitalize()
                    content = msg["content"][:120]
                    color = COLORS['CYAN'] if msg["role"] == "user" else COLORS['GREEN']
                    print_colored(f"  {role}: {content}", color)
            else:
                print_colored("No chat history available.", COLORS['WARNING'])
        return True, model

    if user_input.lower() == '/history clear':
        if READLINE_AVAILABLE:
            readline.clear_history()
            print_colored("Command history cleared.", COLORS['GREEN'])
        else:
            print_colored("Readline not available - cannot clear command history", COLORS['WARNING'])
        return True, model

    # Model switch command
    if user_input.lower().startswith('/model '):
        parts = user_input.split(' ', 1)
        if len(parts) < 2 or not parts[1].strip():
            print_colored("Usage: /model <name>", COLORS['FAIL'])
            print_colored(f"Available models: {', '.join(AGENT_THEMES.keys())}", COLORS['BLUE'])
            return True, model

        requested_model = parts[1].strip().lower()
        if requested_model in AGENT_THEMES:
            model = requested_model
            agent_theme = AGENT_THEMES[model]
            print_colored(f"\nSwitched to agent: {model} {agent_theme['icon']}", COLORS['WARNING'])
            print_colored(f"Description: {agent_theme['desc']}", COLORS['BLUE'])
        else:
            print_colored(f"Unknown agent '{requested_model}'. Available: {', '.join(AGENT_THEMES.keys())}", COLORS['FAIL'])
        return True, model

    # Clear conversation history
    if user_input.lower() == '/clear':
        history.clear()
        save_chat_history(history, model)
        print_colored("Conversation history cleared. Starting fresh.", COLORS['GREEN'])
        return True, model

    # Resume interrupted tasks
    if user_input.lower() == '/resume':
        with _pending_tasks_lock:
            if not PENDING_TASKS:
                print_colored("No interrupted tasks to resume.", COLORS['WARNING'])
                return True, model
        # Return False so the main loop picks up the /resume and processes tasks
        return False, model

    # Permission mode toggle
    if user_input.lower() == '/permissions':
        modes = ['default', 'acceptEdits', 'yolo']
        labels = {
            'default': 'Prompt for all operations',
            'acceptEdits': 'Auto-approve file ops, prompt for shell commands',
            'yolo': 'Auto-approve everything (dangerous!)',
        }
        current_idx = modes.index(config.PERMISSION_MODE) if config.PERMISSION_MODE in modes else 0
        config.PERMISSION_MODE = modes[(current_idx + 1) % len(modes)]
        config.ALLOW_ALL = config.PERMISSION_MODE == 'yolo'
        tool_handlers.set_permission_mode(config.PERMISSION_MODE)
        color = COLORS['FAIL'] if config.PERMISSION_MODE == 'yolo' else COLORS['GREEN']
        print_colored(f"Permission mode: {config.PERMISSION_MODE} — {labels[config.PERMISSION_MODE]}", color)
        return True, model

    # Verbose/compact toggle
    if user_input.lower() == '/verbose':
        config.VERBOSE_MODE = not config.VERBOSE_MODE
        tool_handlers.set_verbose_mode(config.VERBOSE_MODE)
        mode_str = "verbose (full tool output)" if config.VERBOSE_MODE else "compact (one-line summaries)"
        print_colored(f"Display mode: {mode_str}", COLORS['GREEN'])
        return True, model

    # Context usage
    if user_input.lower() == '/context':
        total_chars = sum(len(m.get("content", "")) for m in history)
        est_tokens = total_chars // 4
        msg_count = len(history)
        budget = HISTORY_CHAR_BUDGET
        pct = (total_chars / budget) * 100 if budget > 0 else 0
        bar_len = 30
        filled = int(bar_len * pct / 100)
        bar = f"[{'#' * filled}{'.' * (bar_len - filled)}]"
        color = COLORS['FAIL'] if pct > 80 else COLORS['WARNING'] if pct > 50 else COLORS['GREEN']
        print_colored(f"\nContext Usage:", COLORS['HEADER'])
        print_colored(f"  Messages:  {msg_count}", COLORS['CYAN'])
        print_colored(f"  Chars:     {total_chars:,} / {budget:,}", COLORS['CYAN'])
        print_colored(f"  Est tokens: ~{est_tokens:,}", COLORS['CYAN'])
        print_colored(f"  Budget:    {bar} {pct:.1f}%", color)
        return True, model

    # Manual context compression
    if user_input.lower() == '/compact':
        before_count = len(history)
        before_chars = sum(len(m.get("content", "")) for m in history)
        system_msgs = [m for m in history if m['role'] == 'system']
        non_system = [m for m in history if m['role'] != 'system']
        if len(non_system) <= 12:
            print_colored("History is already compact.", COLORS['GREEN'])
            return True, model
        keep_start = non_system[:4]
        keep_end = non_system[-8:]
        compressed_count = len(non_system) - 12
        summary_msg = {"role": "system", "content": f"[{compressed_count} earlier messages compressed to save context]"}
        history[:] = system_msgs + keep_start + [summary_msg] + keep_end
        after_chars = sum(len(m.get("content", "")) for m in history)
        saved = before_chars - after_chars
        print_colored(f"Compacted: {before_count} -> {len(history)} messages, freed ~{saved // 4:,} tokens", COLORS['GREEN'])
        save_chat_history(history, model)
        return True, model

    # Undo last file modification
    if user_input.lower() == '/undo':
        result = undo_last_checkpoint()
        color = COLORS['GREEN'] if 'Restored' in result else COLORS['WARNING']
        print_colored(result, color)
        return True, model

    # Session rename
    if user_input.lower().startswith('/rename '):
        new_name = user_input.split(' ', 1)[1].strip()
        if not new_name:
            print_colored("Usage: /rename <session_name>", COLORS['FAIL'])
            return True, model
        config.SESSION_NAME = new_name
        set_terminal_title(f"Qwen - {new_name}")
        save_chat_history(history, model)
        print_colored(f"Session renamed to: {new_name}", COLORS['GREEN'])
        return True, model

    return False, model


HISTORY_CHAR_BUDGET = 150000  # ~37.5k tokens, leaves room for system prompt + response

MAX_STALL_NUDGES = 2  # Max times we'll nudge a stalled agent before accepting its response

# Patterns that suggest the agent is planning/summarizing rather than delivering a final answer
_STALL_PATTERNS = re.compile(
    r'(?:'
    r'(?:let me|I need to|I should|I will|I\'ll|now I\'ll|next I\'ll)\s'
    r'|(?:^|\n)\s*(?:let me (?:now |also )?(?:check|read|search|write|implement|create|update|fix|modify|look|open|see|try))'
    r'|(?:^|\n)\s*(?:Now (?:let me|I\'ll|I need to|I should))'
    r'|(?:here\'s (?:my|the) (?:plan|approach|strategy))'
    r'|(?:steps? (?:to|I\'ll|we\'ll|needed))'
    r')',
    re.IGNORECASE | re.MULTILINE
)

# Patterns that suggest the agent has genuinely finished
_DONE_PATTERNS = re.compile(
    r'(?:'
    r'(?:implementation is (?:now )?complete|I\'ve (?:finished|completed|implemented|done))'
    r'|(?:the (?:changes|implementation|fix|update|code) (?:is|are) (?:now )?(?:complete|done|ready|in place))'
    r'|(?:all (?:changes|tasks|work) (?:have been|are) (?:completed|done|applied))'
    r'|(?:(?:task|work) (?:is )?(?:complete|done|finished))'
    r'|(?:successfully (?:implemented|completed|updated|created|fixed|written))'
    r')',
    re.IGNORECASE
)


def _looks_like_stall(response_text: str) -> bool:
    """Detect if the agent is stalling (planning/summarizing) rather than delivering a final answer.

    Returns True if the response appears to describe future work rather than
    presenting a completed result.
    """
    # If the agent explicitly says it's done, trust it
    if _DONE_PATTERNS.search(response_text):
        return False
    # If the response contains stall patterns, it's likely planning
    if _STALL_PATTERNS.search(response_text):
        return True
    return False


def _check_history_budget(history):
    """Proactively trim history if total size approaches context limit."""
    total_chars = sum(len(m.get("content", "")) for m in history)
    if total_chars > HISTORY_CHAR_BUDGET:
        print_colored(
            f"\n[Client] History size ({total_chars // 1000}k chars) exceeds budget. Trimming...",
            COLORS['WARNING']
        )
        _trim_history_for_context(history)


def process_agent_tasks(tasks, history, initial_model, agent_theme):
    """Execute a list of agent tasks sequentially"""
    model = initial_model
    task_idx = 0

    try:
        for task_idx, (task_agent, task_content) in enumerate(tasks):
            if not task_content.strip(): continue
            
            # Switch active context for this specific task
            task_theme = AGENT_THEMES[task_agent]
            print_colored(f"\n>>> Executing task with @{task_agent} {task_theme['icon']}", COLORS['BLUE'])
            
            # Update terminal title
            set_terminal_title(f"Qwen - @{task_agent} Working...")

            # Permanently update the active agent if it was explicitly mentioned
            if task_agent != model:
                model = task_agent
                agent_theme = AGENT_THEMES[model]

            # Append user prompt to history
            history.append({"role": "user", "content": task_content})
            save_chat_history(history, model)

            # Reset per-task state
            reset_write_counts()

            # Inner loop: keep sending tool output back until the agent
            # produces a response with no actionable commands.
            task_aborted = False
            task_commands_executed = False # Track if any commands were run for this task
            nudge_count = 0  # Track how many times we've nudged a stalling agent
            max_continuations = 5  # safety cap for truncation retries
            while True:
                _check_history_budget(history)
                response_text, finish_reason = get_completion(history, model, agent_theme)
                if response_text is None:
                    task_aborted = True
                    break

                # ── Handle interrupted responses (Ctrl+C) ──
                if finish_reason == "interrupted":
                    history.append({"role": "assistant", "content": response_text})
                    save_chat_history(history, model)
                    break

                # ── Handle truncated responses (finish_reason == "length") ──
                # Automatically request continuation so the agent can finish.
                # We track partial segments so we can append them to history
                # without duplicating content in the final merged response.
                continuation_count = 0
                aggregated_response = response_text
                while finish_reason == "length" and continuation_count < max_continuations:
                    continuation_count += 1
                    print_colored(
                        f"\n[Continuation {continuation_count}/{max_continuations}] "
                        "Response was truncated. Requesting continuation...",
                        COLORS['WARNING']
                    )
                    # Append the latest segment and ask the agent to continue
                    history.append({"role": "assistant", "content": response_text})
                    history.append({
                        "role": "user",
                        "content": "Your previous response was cut off. Continue exactly where you left off.",
                        "auto_send": True,
                    })
                    save_chat_history(history, model)

                    cont_text, finish_reason = get_completion(
                        history, model, agent_theme
                    )
                    if cont_text is None:
                        task_aborted = True
                        break
                    # The continuation becomes the new response_text for the
                    # next iteration (the partial is already in history above).
                    response_text = cont_text

                    # If the previous segment ended mid-tag (e.g. "<<<WRITE")
                    # and the continuation starts with whitespace + rest of tag
                    # (e.g. "\n_FILE>>>"), the newline would corrupt the tag
                    # name making it unmatchable.  Strip leading whitespace
                    # from the continuation when we detect a partial tag at the
                    # boundary so the aggregated text reconstructs correctly.
                    if re.search(r'<{1,3}\w*$', aggregated_response):
                        cont_text = cont_text.lstrip()
                    aggregated_response += cont_text

                if task_aborted:
                    break

                # Append the (possibly merged) assistant response to history
                # If we aggregated, we already put segments in history, 
                # but the final tool processing should use the full combined text.
                if continuation_count > 0:
                    # Update response_text to the full aggregated version for tool parsing
                    response_text = aggregated_response
                    # Remove fragmented entries: each continuation added 2 (assistant + user prompt)
                    del history[-(continuation_count * 2):]
                    history.append({"role": "assistant", "content": aggregated_response})
                    save_chat_history(history, model)
                else:
                    history.append({"role": "assistant", "content": response_text})
                    save_chat_history(history, model)

                # ── Execute commands found in the response ──
                tool_output = process_remote_commands(response_text)

                if not tool_output and finish_reason == "stop":
                    # Fallback: model wrote commands in code blocks instead of markers.
                    # Only on clean completions — truncated/errored responses contain
                    # code examples that are NOT intended as executable commands.
                    fallback_cmds = extract_fallback_commands(response_text)
                    if fallback_cmds:
                        print_colored("\nAgent used code blocks instead of markers. Extracting commands...", COLORS['WARNING'])
                        results = []
                        total_len = 0
                        global_max_len = 40000

                        for i, cmd in enumerate(fallback_cmds):
                            if total_len > global_max_len:
                                results.append(f"\n... [OMITTED {len(fallback_cmds) - i} FALLBACK COMMANDS TO PREVENT CONTEXT OVERFLOW] ...")
                                break

                            result = execute_remote_command(cmd.strip())
                            if result:
                                res_str = f"[Command {i+1}] {result}"
                                results.append(res_str)
                                total_len += len(res_str)

                        if results:
                            tool_output = "\n\n".join(results)

                if tool_output:
                    task_commands_executed = True
                    nudge_count = 0  # Reset nudge counter after successful tool use
                    cmd_count = tool_output.count("[Command ")
                    label = f"{cmd_count} command(s) executed" if cmd_count > 1 else "Tool result"
                    print_colored(f"\n{label}. Sending output back to agent...", COLORS['CYAN'])
                    history.append({"role": "user", "content": f"Tool output:\n{tool_output}"})
                    save_chat_history(history, model)
                    continue  # loop back to get the agent's next response

                # No commands found — check if the agent is stalling
                # If it was previously executing tools and now produced a
                # plan/summary without acting, nudge it to continue.
                if (task_commands_executed
                        and nudge_count < MAX_STALL_NUDGES
                        and _looks_like_stall(response_text)):
                    nudge_count += 1
                    print_colored(
                        f"\n[Nudge {nudge_count}/{MAX_STALL_NUDGES}] "
                        "Agent produced a summary instead of acting. Nudging to continue...",
                        COLORS['WARNING']
                    )
                    history.append({
                        "role": "user",
                        "content": (
                            "You described what needs to be done but didn't execute any commands. "
                            "Stop summarizing and proceed with the implementation now. "
                            "Use your tools (<<<WRITE_FILE>>>, <<<EDIT_FILE>>>, <<<REMOTE_EXEC>>>, etc.) "
                            "to make the actual changes."
                        ),
                    })
                    save_chat_history(history, model)
                    continue  # loop back for the agent's next response

                # Agent is genuinely done with this task
                break

            if task_aborted:
                remaining_tasks = tasks[task_idx+1:]
                if remaining_tasks:
                    with _pending_tasks_lock:
                            PENDING_TASKS.extend(remaining_tasks)
                    print_colored(f"\n⚠️  Task aborted. Saved {len(remaining_tasks)} pending tasks.", COLORS['WARNING'])
                    print_colored("   Type '/resume' to retry/continue.", COLORS['BLUE'])
                break  # stop processing remaining tasks
                
    except KeyboardInterrupt:
        print_colored("\nInterrupt received.", COLORS['WARNING'])
        remaining_tasks = tasks[task_idx+1:]
        if remaining_tasks:
            with _pending_tasks_lock:
                            PENDING_TASKS.extend(remaining_tasks)
            remaining_agents = [t[0] for t in remaining_tasks]
            print_colored(f"⚠️  Skipped remaining tasks for: {', '.join(remaining_agents)}", COLORS['WARNING'])
            print_colored("   Type '/resume' to continue later.", COLORS['BLUE'])
    except Exception as e:
        print_colored(f"\nMain Loop Error: {e}", COLORS['FAIL'])
    
    set_terminal_title("Qwen - Idle")
    with _pending_tasks_lock:
        if not PENDING_TASKS:
            send_macos_notification("All tasks completed.", title="Qwen Client")
        
    return model

def chat(model="implementer", session_name=None):
    """Main interactive chat loop"""
    # Apply session name
    if session_name:
        config.SESSION_NAME = session_name
        # Use a session-specific history file
        safe_name = re.sub(r'[^\w\-]', '_', session_name)
        config.CHAT_HISTORY_FILE = os.path.expanduser(f"~/.qwen_chat_history_{safe_name}.json")

    # Initialize readline for command history and editing
    setup_readline()

    # Load available models from server
    fetch_available_models()

    print_colored(f"\nQwen Remote CLI (Connected to {config.LINUX_SERVER_IP})", COLORS['HEADER'])
    title = f"Qwen - {config.SESSION_NAME}" if config.SESSION_NAME else "Qwen - Idle"
    set_terminal_title(title)
    if config.SESSION_NAME:
        print_colored(f"Session: {config.SESSION_NAME}", COLORS['CYAN'])
    
    # Get initial theme
    if model not in AGENT_THEMES:
        if "implementer" in AGENT_THEMES:
            model = "implementer"
        elif AGENT_THEMES:
            model = list(AGENT_THEMES.keys())[0]
        else:
            # Absolute fallback if no agents loaded
            AGENT_THEMES["default"] = {
                "color": COLORS['WARNING'], 
                "icon": "❓", 
                "prompt": "Agent", 
                "desc": "Unknown Agent"
            }
            model = "default"
            
    agent_theme = AGENT_THEMES[model]
    print_colored(f"Agent: {model} {agent_theme['icon']}", COLORS['WARNING'])
    print_colored(f"({agent_theme['desc']})", COLORS['BLUE'])

    perm_labels = {
        'default': 'Prompt for all operations',
        'acceptEdits': 'Auto-approve file ops, prompt for shell',
        'yolo': 'Auto-approve ALL (dangerous!)',
    }
    perm_color = COLORS['FAIL'] if config.PERMISSION_MODE == 'yolo' else COLORS['GREEN']
    print_colored(f"\nPermissions: {config.PERMISSION_MODE} — {perm_labels.get(config.PERMISSION_MODE, '?')}", perm_color)
    if config.ALLOW_SHELL_MODE:
        print_colored("  Shell mode: ENABLED", COLORS['WARNING'])
    print_colored(f"  Display: {'verbose' if config.VERBOSE_MODE else 'compact'} | Multiline: \\ + Enter", COLORS['BLUE'])

    print_colored("\nCommands: /help /exit /model /clear /resume /permissions /verbose /context /compact /undo /rename", COLORS['BLUE'])
    if READLINE_AVAILABLE:
        print_colored("Use arrow keys for history. Ctrl+C to interrupt generation.\n", COLORS['BLUE'])
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
            # Wrap ANSI codes with \001/\002 so readline correctly computes prompt width
            raw_color = agent_theme['color']
            prompt_text = f"{agent_theme['prompt']} {agent_theme['icon']} > "
            full_prompt = f"\001{raw_color}\002{prompt_text}\001{COLORS['ENDC']}\002"
            
            # Pass the prompt directly to input() so readline handles it correctly
            try:
                user_input = input(full_prompt)
                # Multiline input: backslash + Enter continues on next line
                while user_input.endswith('\\'):
                    user_input = user_input[:-1] + '\n'
                    try:
                        user_input += input('... ')
                    except EOFError:
                        break
                # Add user input to readline history (for up/down arrow navigation)
                if user_input.strip():
                    add_to_history(user_input)
            except EOFError:
                break # Handle Ctrl+D gracefully

            if not user_input.strip():
                continue
            
            if user_input.lower() in ['/exit', '/quit']:
                break

            # Handle commands
            should_continue, new_model = handle_user_command(user_input, history, model, agent_theme)
            if new_model != model:
                model = new_model
                agent_theme = AGENT_THEMES[model]
            
            if should_continue:
                continue

            # Resume Interrupted Chain (handled by handle_user_command returning False)
            if user_input.lower() == '/resume':
                with _pending_tasks_lock:
                    print_colored(f"Resuming {len(PENDING_TASKS)} pending tasks...", COLORS['GREEN'])
                    tasks = list(PENDING_TASKS)
                    PENDING_TASKS.clear()
            else:
                # Normal Multi-Agent Orchestration Logic
                try:
                    # Split input by @mentions while keeping the mentions
                    # segments example: ['Initial ', '@architect', ' design ', '@implementer', ' code']
                    segments = re.split(r'(@\w+)', user_input)
                    
                    # Tasks: list of (agent_name, message_content)
                    tasks = []
                    current_task_agent = model
                    
                    # Initial segment before any @ (assigned to current active agent)
                    first_segment = segments[0].strip()
                    if first_segment:
                        tasks.append((current_task_agent, first_segment))
                    
                    # Process pairs of (@agent, following_text)
                    for i in range(1, len(segments), 2):
                        agent_name = segments[i][1:].lower()
                        message_content = segments[i+1].strip() if i+1 < len(segments) else ""
                        
                        if agent_name in AGENT_THEMES:
                            tasks.append((agent_name, message_content))
                        else:
                            # Not a valid agent, treat as literal text for the last active task
                            if tasks:
                                tasks[-1] = (tasks[-1][0], tasks[-1][1] + " " + segments[i] + message_content)
                            else:
                                tasks.append((current_task_agent, segments[i] + message_content))
                except Exception as e:
                    print_colored(f"Error parsing tasks: {e}", COLORS['FAIL'])
                    tasks = []

            # Process Tasks
            model = process_agent_tasks(tasks, history, model, agent_theme)
            # Update theme if model changed during tasks
            if model in AGENT_THEMES:
                agent_theme = AGENT_THEMES[model]

        except KeyboardInterrupt:
            # First Ctrl+C at prompt: warn. Second exits.
            print_colored("\n(Press Ctrl+C again to exit, or type /exit)", COLORS['WARNING'])
            try:
                # Wait briefly for a second Ctrl+C
                time.sleep(0.3)
            except KeyboardInterrupt:
                break
            continue

    print_colored("\nGoodbye!", COLORS['HEADER'])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Qwen Remote Client")
    parser.add_argument("--model", type=str, default="implementer", help="Agent model to use")
    parser.add_argument("--name", type=str, default=None, help="Name this session")
    args = parser.parse_args()

    try:
        chat(args.model, session_name=args.name)
    except Exception as e:
        print_colored(f"\nCRITICAL CLIENT ERROR: {e}", COLORS['FAIL'])
        import traceback
        traceback.print_exc()
        sys.exit(1)