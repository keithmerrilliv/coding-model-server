"""Terminal display helpers — titles, notifications."""
import sys
import subprocess


def set_terminal_title(title):
    """Set the terminal window title using ANSI escape codes."""
    sys.stdout.write(f"\033]0;{title}\007")
    sys.stdout.flush()


def _escape_applescript_string(s: str) -> str:
    """Escape a Python string for safe interpolation into an AppleScript
    double-quoted string literal.

    Order matters: backslash must be escaped first or it doubles up.
    Also strips control chars and Unicode line separators (U+2028/U+2029)
    that AppleScript treats as line breaks and could be used to inject
    additional statements.
    """
    safe = "".join(c for c in s if c not in "  " and (ord(c) >= 32 or c in "\t"))
    return safe.replace("\\", "\\\\").replace('"', '\\"')


def send_macos_notification(text, title="Coding Model Client"):
    """Send a native macOS notification via osascript."""
    if sys.platform != 'darwin':
        return
    try:
        safe_text = _escape_applescript_string(text)
        safe_title = _escape_applescript_string(title)
        cmd = f'display notification "{safe_text}" with title "{safe_title}"'
        subprocess.run(['osascript', '-e', cmd], check=False, stderr=subprocess.DEVNULL)
    except Exception:
        pass
