"""Readline integration — command history and CLI editing."""
import os
import sys
import atexit

from qwen_client.config import config

# Readline for command history and CLI editing
try:
    import readline
    READLINE_AVAILABLE = True
except ImportError:
    READLINE_AVAILABLE = False


def setup_readline():
    """Configure readline for command history and editing."""
    if not READLINE_AVAILABLE:
        return
    try:
        if os.path.exists(config.HISTORY_FILE):
            readline.read_history_file(config.HISTORY_FILE)
    except (IOError, OSError):
        pass

    readline.set_history_length(config.HISTORY_MAX_LENGTH)
    atexit.register(save_readline_history)

    if (readline.__doc__ and 'libedit' in readline.__doc__) or sys.platform == 'darwin':
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind('tab: complete')


def save_readline_history():
    """Save command history to file."""
    if not READLINE_AVAILABLE:
        return
    try:
        readline.write_history_file(config.HISTORY_FILE)
    except (IOError, OSError):
        pass


def add_to_history(line):
    """Add a line to readline history (avoiding duplicates of the last entry)."""
    if not READLINE_AVAILABLE:
        return
    if not line or not line.strip():
        return
    history_len = readline.get_current_history_length()
    if history_len > 0:
        last_item = readline.get_history_item(history_len)
        if last_item == line:
            return
    readline.add_history(line)
