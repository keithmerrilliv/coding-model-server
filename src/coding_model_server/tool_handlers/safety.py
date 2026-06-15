"""Security gates for tool execution: protected paths, dangerous-command
warnings, hard deny rules, and permission-mode auto-approval.

Pure helpers — they depend only on os/re and the shared permission mode in
``state``. No filesystem mutation, no prompting.
"""
import os
import re

from coding_model_server.tool_state import state

# Paths that require explicit user confirmation regardless of permission mode
PROTECTED_PATHS = [
    '.git/', '.ssh/', '.gnupg/', '/etc/', '/usr/', '/bin/', '/sbin/',
]
PROTECTED_FILES = [
    '.env', '.bashrc', '.zshrc', '.profile', '.bash_profile',
    'id_rsa', 'id_ed25519', 'authorized_keys', 'known_hosts',
]


def _is_protected_path(filepath):
    """Check if a path is protected. Returns (is_protected, reason).

    Uses realpath (not just normpath) so a symlink from a benign-looking
    path to a protected one is still caught. Without realpath,
    `/home/user/Dev/symlink → /etc/passwd` would slip the /etc check.
    """
    expanded = os.path.expanduser(filepath)
    try:
        norm = os.path.realpath(expanded)
    except OSError:
        # If the path doesn't exist yet, fall back to the lexical normpath.
        # Real-world: this happens when WRITE_FILE creates a new file.
        norm = os.path.normpath(expanded)
    # Check directory components
    parts = norm.split(os.sep)
    for pdir in PROTECTED_PATHS:
        pdir_clean = pdir.rstrip('/')
        if pdir_clean in parts:
            return True, f"inside protected directory '{pdir}'"
        # Absolute path prefix (e.g., /etc/)
        if pdir_clean.startswith('/') and norm.startswith(os.path.normpath(pdir_clean)):
            return True, f"inside protected directory '{pdir}'"
    basename = os.path.basename(norm)
    for pfile in PROTECTED_FILES:
        if basename == pfile:
            return True, f"matches protected file '{pfile}'"
    return False, ""


# Shell patterns that get extra warnings even in yolo mode
DANGEROUS_PATTERNS = [
    # rm with a recursive flag, in any cluster position: -r, -rf, -fr, -Rf, etc.
    # The earlier `-[a-zA-Z]*r` only matched clusters ENDING in r, so the most
    # common form `rm -rf` (ends in f) slipped the warning. Mirror the deny
    # rule's `-[a-zA-Z]*r[a-zA-Z]*` so r anywhere in the cluster is caught.
    (re.compile(r'\brm\s+(-[a-zA-Z]*r[a-zA-Z]*|--recursive)\b'), "recursive delete"),
    (re.compile(r'\bsudo\b'), "elevated privileges"),
    (re.compile(r'\bchmod\s+[0-7]*777\b'), "world-writable permissions"),
    (re.compile(r'\bchown\b'), "ownership change"),
    (re.compile(r'\bmkfs\b'), "filesystem format"),
    (re.compile(r'\bdd\s+'), "raw disk write"),
    (re.compile(r'>\s*/dev/(?!null)'), "writing to device"),  # exclude /dev/null
    (re.compile(r'\bgit\s+push\s+.*--force\b'), "force push"),
    (re.compile(r'\bgit\s+reset\s+--hard\b'), "destructive git reset"),
]


def _check_dangerous_command(command):
    """Check if a command matches known dangerous patterns. Returns (is_dangerous, reason)."""
    for pattern, description in DANGEROUS_PATTERNS:
        if pattern.search(command):
            return True, description
    return False, ""


# Operations that are always denied — no prompt, no override.
# No $ anchor — catches compound commands like "rm -rf / && echo done".
DENY_RULES = [
    (re.compile(r'\brm\s+-[a-zA-Z]*r[a-zA-Z]*\s+/\s*(?:[;&|]|$)'), "recursive delete of root"),
    (re.compile(r'\brm\s+-[a-zA-Z]*r[a-zA-Z]*\s+/home\s*(?:[;&|]|$)'), "recursive delete of /home"),
    (re.compile(r':\(\)\s*\{\s*:\|:&\s*\};\s*:'), "fork bomb"),
]


def _check_deny_rules(command):
    """Check if a command is unconditionally denied. Returns (denied, reason)."""
    for pattern, description in DENY_RULES:
        if pattern.search(command):
            return True, description
    return False, ""


def _should_auto_approve(tool_name):
    """Check if a tool operation should be auto-approved based on permission mode.

    Args:
        tool_name: One of 'REMOTE_EXEC', 'WRITE_FILE', 'EDIT_FILE', 'READ_FILE', etc.

    Returns:
        bool: True if the operation should be auto-approved.

    REMOTE_EXEC under yolo additionally requires ALLOW_REMOTE_EXEC_YOLO=1
    in the environment. Defense-in-depth: a prompt-injected memory or
    cross-origin CSRF that lands a malicious REMOTE_EXEC must compromise
    yolo mode AND the env opt-in to silently execute. Without the opt-in
    yolo still prompts for shell, even though it auto-approves edits.
    """
    if state.permission_mode == 'yolo':
        if tool_name == 'REMOTE_EXEC':
            return os.getenv('ALLOW_REMOTE_EXEC_YOLO', '').lower() in ('1', 'true', 'yes')
        return True
    if state.permission_mode == 'acceptEdits':
        # Auto-approve everything EXCEPT shell commands
        return tool_name != 'REMOTE_EXEC'
    return False  # 'default' — prompt for everything
