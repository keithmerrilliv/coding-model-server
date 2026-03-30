"""Terminal display helpers — titles, notifications."""
import sys
import subprocess


def set_terminal_title(title):
    """Set the terminal window title using ANSI escape codes."""
    sys.stdout.write(f"\033]0;{title}\007")
    sys.stdout.flush()


def send_macos_notification(text, title="Qwen Client"):
    """Send a native macOS notification via osascript."""
    if sys.platform != 'darwin':
        return
    try:
        safe_text = text.replace('"', '\\"')
        safe_title = title.replace('"', '\\"')
        cmd = f'display notification "{safe_text}" with title "{safe_title}"'
        subprocess.run(['osascript', '-e', cmd], check=False, stderr=subprocess.DEVNULL)
    except Exception:
        pass
