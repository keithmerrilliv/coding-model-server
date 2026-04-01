"""Chat history persistence — save, load, prune, compress."""
import os
import re
import glob
import json
import shutil
from datetime import datetime

from qwen_client.config import config, COLORS, print_colored

_INTERNAL_KEYS = {"auto_send", "_retried", "_retried_prompt"}


# ---------------------------------------------------------------------------
# Session directory helpers
# ---------------------------------------------------------------------------

def ensure_session_dir():
    """Create session directory if it doesn't exist."""
    os.makedirs(config.SESSION_DIR, exist_ok=True)


def session_path(name):
    """Get file path for a named session."""
    safe_name = re.sub(r'[^\w\-]', '_', name) if name else "default"
    return os.path.join(config.SESSION_DIR, f"{safe_name}.json")


def migrate_legacy_sessions():
    """One-time migration from flat files in ~/ to ~/.qwen_sessions/."""
    ensure_session_dir()
    migrated = 0
    # Default session
    legacy_default = os.path.expanduser("~/.qwen_chat_history.json")
    if os.path.exists(legacy_default):
        new_path = session_path("default")
        if not os.path.exists(new_path):
            try:
                shutil.move(legacy_default, new_path)
                migrated += 1
            except Exception as e:
                print_colored(f"Warning: Could not migrate default session: {e}", COLORS['WARNING'])
    # Named sessions
    for legacy in glob.glob(os.path.expanduser("~/.qwen_chat_history_*.json")):
        name = os.path.basename(legacy).replace(".qwen_chat_history_", "").replace(".json", "")
        new_path = session_path(name)
        if not os.path.exists(new_path):
            try:
                shutil.move(legacy, new_path)
                migrated += 1
            except Exception as e:
                print_colored(f"Warning: Could not migrate session {name}: {e}", COLORS['WARNING'])
    if migrated:
        print_colored(f"Migrated {migrated} session(s) to {config.SESSION_DIR}/", COLORS['CYAN'])


def list_sessions():
    """List all saved sessions with metadata."""
    ensure_session_dir()
    sessions = []
    for filepath in sorted(glob.glob(os.path.join(config.SESSION_DIR, "*.json"))):
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
            name = data.get("session_name") or os.path.splitext(os.path.basename(filepath))[0]
            sessions.append({
                "name": name,
                "file": filepath,
                "messages": len(data.get("messages", [])),
                "timestamp": data.get("timestamp", "unknown"),
                "last_agent": data.get("last_agent", "unknown"),
            })
        except Exception:
            continue
    return sessions


def load_chat_history_from_file(filepath):
    """Load history from a specific file. Returns (history, last_agent)."""
    try:
        with open(filepath, 'r') as f:
            data = json.load(f)
        if isinstance(data, list):
            return data, "implementer"
        return data.get("messages", []), data.get("last_agent", "implementer")
    except Exception as e:
        print_colored(f"Warning: Failed to load session: {e}", COLORS['WARNING'])
        return [], "implementer"


def save_chat_history(history, current_agent="implementer"):
    """Save full chat history and metadata to file, stripping internal keys."""
    try:
        ensure_session_dir()
        pruned_history = _prune_history(history)
        cleaned = []
        for msg in pruned_history:
            clean_msg = {k: v for k, v in msg.items() if k not in _INTERNAL_KEYS}
            cleaned.append(clean_msg)
        data = {
            "messages": cleaned,
            "last_agent": current_agent,
            "session_name": config.SESSION_NAME,
            "timestamp": datetime.now().isoformat(),
        }
        with open(config.CHAT_HISTORY_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print_colored(f"Warning: Failed to save chat history: {e}", COLORS['WARNING'])


def _prune_history(history, max_messages=100):
    """Prune history to prevent excessive growth, keeping important context."""
    if len(history) <= max_messages:
        return history
    n_keep_start = 10
    n_keep_end = max_messages - n_keep_start
    pruned = history[:n_keep_start] + history[-n_keep_end:]
    print_colored(f"Pruned chat history from {len(history)} to {len(pruned)} messages", COLORS['WARNING'])
    return pruned


def load_chat_history():
    """Load chat history from file if it exists. Returns (history, last_agent)."""
    if os.path.exists(config.CHAT_HISTORY_FILE):
        try:
            with open(config.CHAT_HISTORY_FILE, 'r') as f:
                data = json.load(f)

            if isinstance(data, list):
                history = data
                last_agent = "implementer"
            else:
                history = data.get("messages", [])
                last_agent = data.get("last_agent", "implementer")
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
