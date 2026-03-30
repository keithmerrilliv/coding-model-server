"""Chat history persistence — save, load, prune, compress."""
import os
import json
from datetime import datetime

from qwen_client.config import config, COLORS, print_colored

_INTERNAL_KEYS = {"auto_send", "_retried", "_retried_prompt"}


def save_chat_history(history, current_agent="implementer"):
    """Save full chat history and metadata to file, stripping internal keys."""
    try:
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
