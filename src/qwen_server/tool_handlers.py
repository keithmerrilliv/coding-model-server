"""
Tool handler functions for the Qwen Remote Client.

These functions handle command execution, file operations, search,
and other tool-related operations. Imported by qwen_client.

Must be configured via configure() before use.
"""
import os
import re
import subprocess
import json
import shlex
import shutil
import hashlib
import tempfile
import time
import logging
import difflib
import glob as glob_module

from datetime import datetime
from typing import Optional, List

try:
    from rich.syntax import Syntax
except ImportError:
    Syntax = None


# Module-level references set by configure()
_config = None
_colors = None
_print_colored = None
_logger = None
_temp_tracker = None  # dict with 'add' function (the only key consumed here)
_external_handlers = None  # dict with save_memory, web_search, etc.

# Extended state (set via configure **kwargs)
_rich_console = None
_permission_mode = 'default'  # 'default' | 'acceptEdits' | 'yolo'
_verbose_mode = True  # True=full output, False=compact one-liners

# Checkpoint state for /undo
_CHECKPOINT_DIR = os.path.expanduser("~/.qwen_checkpoints")
_checkpoint_stack = []  # List of (original_path, checkpoint_path, timestamp)
_MAX_CHECKPOINTS = 20

# Write-loop detection: {normalized_path: write_count}
_write_counts = {}
# Separate counter for shell-write redirects so a real path literally named
# "__shell_writes__" can't collide with the global counter.
_shell_write_count = 0
_MAX_WRITES_PER_FILE = 3

# ---------------------------------------------------------------------------
# Safety: protected paths, dangerous commands, deny rules
# ---------------------------------------------------------------------------

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


def reset_write_counts():
    """Clear per-file and shell-write counters between tasks."""
    global _shell_write_count
    _write_counts.clear()
    _shell_write_count = 0


def configure(config, colors, print_colored_fn, logger_inst, temp_tracker, external_handlers, **kwargs):
    """Initialize module-level references needed by all handler functions.

    Args:
        config: Config instance with settings like ALLOW_SHELL_MODE, CHUNK_SIZE, etc.
        colors: COLORS dict mapping color names to ANSI codes.
        print_colored_fn: print_colored(text, color) function.
        logger_inst: logging.Logger instance.
        temp_tracker: dict with key 'add' mapping to a function that registers a temp file path.
        external_handlers: dict with keys 'save_memory', 'web_search',
            'handle_cupertino_search', 'handle_apple_deep_docs', 'ingest_pdf',
            'ingest_url_content' mapping to their respective functions.
        **kwargs: Extended options — rich_console, permission_mode, verbose_mode.
    """
    global _config, _colors, _print_colored, _logger, _temp_tracker, _external_handlers
    global _rich_console, _permission_mode, _verbose_mode
    _config = config
    _colors = colors
    _print_colored = print_colored_fn
    _logger = logger_inst
    _temp_tracker = temp_tracker
    _external_handlers = external_handlers
    _rich_console = kwargs.get('rich_console', None)
    _permission_mode = kwargs.get('permission_mode', getattr(config, 'PERMISSION_MODE', 'default'))
    _verbose_mode = kwargs.get('verbose_mode', getattr(config, 'VERBOSE_MODE', True))


def set_permission_mode(mode):
    """Update permission mode at runtime (called by /permissions command)."""
    global _permission_mode
    _permission_mode = mode


def set_verbose_mode(mode):
    """Update verbose mode at runtime (called by /verbose command)."""
    global _verbose_mode
    _verbose_mode = mode


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
    if _permission_mode == 'yolo':
        if tool_name == 'REMOTE_EXEC':
            return os.getenv('ALLOW_REMOTE_EXEC_YOLO', '').lower() in ('1', 'true', 'yes')
        return True
    if _permission_mode == 'acceptEdits':
        # Auto-approve everything EXCEPT shell commands
        return tool_name != 'REMOTE_EXEC'
    return False  # 'default' — prompt for everything


# ---------------------------------------------------------------------------
# Content sanitizer — single boundary between model output and disk writes
# ---------------------------------------------------------------------------

# Patterns compiled once, applied to every file write and edit replacement.
_SANITIZE_PATTERNS = [
    # Git conflict / Aider / Cursor edit markers
    (re.compile(r'^\s*<{1,7}\s*SEARCH\s*>{0,7}\s*$', re.MULTILINE), ''),
    (re.compile(r'^\s*>{1,7}\s*REPLACE\s*>{0,7}\s*$', re.MULTILINE), ''),
    (re.compile(r'^\s*<{7}(?:\s+\S+)?\s*$', re.MULTILINE), ''),  # <<<<<<< or <<<<<<< HEAD
    (re.compile(r'^\s*={7}\s*$', re.MULTILINE), ''),               # =======
    (re.compile(r'^\s*>{7}(?:\s+\S+)?\s*$', re.MULTILINE), ''),   # >>>>>>> or >>>>>>> branch
    # Agentic markers that escaped process_response stripping
    (re.compile(r'<{0,3}/?CONFIDEN\w*>{0,3}\s*\d*'), ''),
    (re.compile(r'<{1,3}SCRATCHPAD>{1,3}.*?(?=<{1,3}\w+>{1,3}|\Z)', re.DOTALL), ''),
    (re.compile(r'<{1,3}PLAN>{1,3}.*?(?=<{1,3}\w+>{1,3}|\Z)', re.DOTALL), ''),
    # Qwen special tokens
    (re.compile(r'</?tool_call\s*>'), ''),
    (re.compile(r'<REACT>.*?</REACT>\s*', re.DOTALL), ''),
    # Common LLM stop tokens that leak into content
    (re.compile(r'<\|im_end\|>'), ''),
    (re.compile(r'<\|endoftext\|>'), ''),
    (re.compile(r'</s>\s*$'), ''),
    # Our own tool markers that should never appear inside file content
    (re.compile(r'<{1,3}(?:REMOTE_EXEC|SAVE_MEMORY|WEB_SEARCH|CUPERTINO|APPLE_DEEP_DOCS|INGEST_PDF|DEEP_INGEST)>{1,3}'), ''),
]


def _sanitize_generated_content(content: str) -> str:
    """Remove LLM generation artifacts from content before writing to disk.

    This is the single sanitization boundary between model output and the
    filesystem. All file writes (WRITE_FILE, EDIT_FILE) pass through here.
    """
    for pattern, replacement in _SANITIZE_PATTERNS:
        content = pattern.sub(replacement, content)
    # Collapse runs of 3+ blank lines left by stripped blocks
    content = re.sub(r'\n{3,}', '\n\n', content)
    return content


def _display_diff(old_text, new_text, filepath):
    """Display a colored unified diff. Uses rich if available, else ANSI."""
    diff_lines = list(difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"a/{filepath}",
        tofile=f"b/{filepath}",
    ))
    if not diff_lines:
        _print_colored("   (no changes)", _colors['CYAN'])
        return

    diff_text = ''.join(diff_lines)
    if _rich_console:
        _rich_console.print(Syntax(diff_text, "diff", theme="monokai", word_wrap=True))
    else:
        for line in diff_lines:
            line = line.rstrip('\n')
            if line.startswith('+'):
                _print_colored(line, _colors['GREEN'])
            elif line.startswith('-'):
                _print_colored(line, _colors['FAIL'])
            elif line.startswith('@@'):
                _print_colored(line, _colors['CYAN'])
            else:
                print(line)


def _create_checkpoint(filepath):
    """Backup a file before modification for /undo support."""
    if not os.path.exists(filepath):
        return
    os.makedirs(_CHECKPOINT_DIR, exist_ok=True)
    path_hash = hashlib.md5(filepath.encode()).hexdigest()[:8]
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    basename = os.path.basename(filepath)
    checkpoint_name = f"{path_hash}_{ts}_{basename}"
    checkpoint_path = os.path.join(_CHECKPOINT_DIR, checkpoint_name)
    shutil.copy2(filepath, checkpoint_path)
    _checkpoint_stack.append((filepath, checkpoint_path, ts))
    # FIFO cleanup
    while len(_checkpoint_stack) > _MAX_CHECKPOINTS:
        _, old_path, _ = _checkpoint_stack.pop(0)
        try:
            os.remove(old_path)
        except OSError:
            pass
    _logger.info("Checkpoint created: %s", checkpoint_path)


def undo_last_checkpoint():
    """Restore the most recently checkpointed file. Called by /undo command."""
    if not _checkpoint_stack:
        return "No checkpoints available to undo."
    original_path, checkpoint_path, ts = _checkpoint_stack.pop()
    if not os.path.exists(checkpoint_path):
        return f"Checkpoint file missing: {checkpoint_path}"
    shutil.copy2(checkpoint_path, original_path)
    os.remove(checkpoint_path)
    return f"Restored {original_path} from checkpoint ({ts})"


def _compact_summary(tag, arg, result):
    """Generate a one-line summary for compact display mode."""
    if not result:
        return None
    result_len = len(result)
    line_count = result.count('\n') + 1
    if tag == 'READ_FILE':
        path = arg.strip().split('\n')[0]
        return f"  [read] {path} ({line_count} lines)"
    elif tag == 'REMOTE_EXEC':
        cmd = arg.strip().split('\n')[0][:60]
        return f"  [exec] {cmd} ({result_len} chars)"
    elif tag == 'WRITE_FILE':
        path = arg.strip().split('\n')[0]
        return f"  [write] {path} ({result_len} chars)"
    elif tag == 'EDIT_FILE':
        path = arg.strip().split('\n')[0]
        return f"  [edit] {path}"
    elif tag == 'LIST_DIR':
        path = arg.strip()
        return f"  [ls] {path} ({line_count} entries)"
    elif tag == 'GLOB':
        return f"  [glob] {arg.strip()[:40]} ({line_count} matches)"
    elif tag == 'GREP':
        pattern = arg.strip().split('|')[0][:30]
        return f"  [grep] {pattern} ({line_count} results)"
    elif tag == 'SAVE_MEMORY':
        return f"  [memory] saved ({result_len} chars)"
    elif tag == 'WEB_SEARCH':
        return f"  [search] {arg.strip()[:40]}"
    else:
        return f"  [{tag.lower()}] ({result_len} chars)"


def parse_command_safely(command: str) -> List[str]:
    """Parse command string into argument list safely"""
    if not command or not isinstance(command, str):
        raise ValueError("Command must be a non-empty string")

    # Sanitize command string
    command = command.strip()
    if not command:
        raise ValueError("Command cannot be empty after trimming")

    if not _config.ALLOW_SHELL_MODE:
        dangerous_chars = ['|', '&', ';', '$', '`', '\n', '>', '<', '(', ')', '*', '?', '[', ']']
        if any(char in command for char in dangerous_chars):
            raise ValueError(
                f"Command contains shell metacharacters. "
                f"Set ALLOW_SHELL_MODE=true to enable shell features, "
                f"or rewrite command without: {', '.join(dangerous_chars)}"
            )

    try:
        parsed = shlex.split(command)
        # Additional validation: ensure no empty arguments that could cause issues
        if any(arg == '' for arg in parsed):
            raise ValueError("Command contains empty arguments after parsing")
        return parsed
    except ValueError as e:
        raise ValueError(f"Failed to parse command: {e}")
    except Exception as e:
        raise ValueError(f"Unexpected error parsing command: {e}")


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
    if not _config.COMMAND_WHITELIST:
        return True, "No whitelist configured (all commands allowed)"

    if not command_args:
        return False, "Empty command"

    base_command = command_args[0]

    # Additional security check: prevent path traversal attempts
    if '..' in base_command:
        return False, f"Command '{base_command}' contains path traversal ('..') which is not allowed"

    if base_command in _config.COMMAND_WHITELIST:
        return True, f"Command '{base_command}' is whitelisted"

    if '/' in base_command:
        base_name = os.path.basename(base_command)
        if base_name in _config.COMMAND_WHITELIST:
            return True, f"Command '{base_name}' is whitelisted"

    return False, f"Command '{base_command}' not in whitelist: {', '.join(_config.COMMAND_WHITELIST)}"


def chunk_large_output(content, chunk_size=None, overlap=None):
    """
    Split large output into chunks with overlap to preserve context.

    Args:
        content (str): The large output content to chunk
        chunk_size (int): Maximum size of each chunk in characters (uses config if None)
        overlap (int): Number of overlapping characters between chunks (uses config if None)

    Returns:
        dict: Contains 'chunks' list, 'total_chunks', 'chunk_size', and 'original_length'
    """
    # Use config values if not provided
    if chunk_size is None:
        chunk_size = _config.CHUNK_SIZE
    if overlap is None:
        overlap = _config.CHUNK_OVERLAP

    if len(content) <= chunk_size:
        return {
            'chunks': [content],
            'total_chunks': 1,
            'chunk_size': chunk_size,
            'original_length': len(content),
            'needs_chunking': False
        }

    chunks = []
    start = 0
    content_len = len(content)

    while start < content_len:
        end = start + chunk_size

        # If this is the last chunk, include the remainder
        if end >= content_len:
            chunk = content[start:]
        else:
            # Try to break at a line boundary using rfind (simpler than binary search)
            line_break = content.rfind('\n', start, end)

            if line_break != -1 and line_break > start:
                chunk = content[start:line_break + 1]
            else:
                chunk = content[start:end]

        chunks.append(chunk)
        chunk_len = len(chunk)

        # Calculate next start position with overlap
        if chunk_len < chunk_size:
            # Last chunk, no need to continue
            break

        # Move start forward, accounting for overlap
        next_start = start + chunk_len - overlap
        if next_start >= content_len:
            break
        start = next_start

    return {
        'chunks': chunks,
        'total_chunks': len(chunks),
        'chunk_size': chunk_size,
        'original_length': len(content),
        'needs_chunking': True
    }


def get_chunk_for_display(content, chunk_idx=None, chunk_size=None, overlap=None, log_path=None):
    """
    Get a specific chunk or a summary of chunks for display in the context window.

    Args:
        content (str): The full content to chunk
        chunk_idx (int, optional): Specific chunk index to return (-1 for all chunks)
        chunk_size (int): Size of each chunk (uses config if None)
        overlap (int): Overlap between chunks (uses config if None)
        log_path (str, optional): Path to the log file for reference

    Returns:
        str: Formatted output for display
    """
    # Use config values if not provided
    if chunk_size is None:
        chunk_size = _config.CHUNK_SIZE
    if overlap is None:
        overlap = _config.CHUNK_OVERLAP

    chunk_result = chunk_large_output(content, chunk_size, overlap)

    if not chunk_result['needs_chunking']:
        return content

    if chunk_idx is not None and 0 <= chunk_idx < len(chunk_result['chunks']):
        # Return specific chunk
        return f"[CHUNK {chunk_idx + 1}/{chunk_result['total_chunks']}]\n{chunk_result['chunks'][chunk_idx]}"
    elif chunk_idx == -1:
        # Return all chunks (for when space permits)
        result = []
        for i, chunk in enumerate(chunk_result['chunks']):
            result.append(f"[CHUNK {i + 1}/{chunk_result['total_chunks']}]\n{chunk}")
        return "\n--- CHUNK BREAK ---\n".join(result)
    else:
        # Return first chunk + last chunk + error summary (similar to current approach but more structured)
        first_chunk = chunk_result['chunks'][0]
        last_chunk = chunk_result['chunks'][-1]

        # Look for errors in all chunks
        all_error_lines = []
        for i, chunk in enumerate(chunk_result['chunks'][1:-1]):  # Skip first and last which are displayed fully
            for line in chunk.splitlines():
                if 'error' in line.lower() or 'fail' in line.lower() or 'warning' in line.lower():
                    all_error_lines.append(f"(Chunk {i+2}) {line.strip()}")

        # Limit error lines to prevent overflow
        error_summary = ""
        if all_error_lines:
            error_summary = "\n... [ERROR/WARNING SUMMARY] ...\n" + "\n".join(all_error_lines[:20])
            if len(all_error_lines) > 20:
                error_summary += f"\n... ({len(all_error_lines) - 20} more error lines omitted) ..."

        log_ref = f"... [Log: {log_path}] ..." if log_path else ""

        return (
            f"[CHUNK 1/{chunk_result['total_chunks']} - FIRST]\n{first_chunk}\n"
            f"\n... [CHUNKED: {chunk_result['original_length']} chars in {chunk_result['total_chunks']} chunks] ...\n"
            f"{log_ref}\n"
            f"{error_summary}\n"
            f"\n[CHUNK {chunk_result['total_chunks']}/{chunk_result['total_chunks']} - LAST]\n{last_chunk}"
        )


def execute_remote_command(command, chunk_output=True):
    """Internal function to execute a command with security checks"""

    # Validate input
    if not command or not isinstance(command, str):
        _logger.warning("Invalid command received: %s", command)
        return "Error: Invalid command - command must be a non-empty string"

    _logger.info("Executing command: %s", command)

    # Detect and block shell-based file writes that bypass WRITE_FILE loop detection.
    # Agents use 'python3 -c "open(...).write()"' or heredocs to circumvent.
    _shell_write_match = re.search(
        r"open\s*\(.+['\"]w['\"]|>\s*\S+\.(?:swift|py|md|yml|json|txt|h|m|metal)\b|cat\s*<<",
        command
    )
    if _shell_write_match:
        global _shell_write_count
        _shell_write_count += 1
        if _shell_write_count > _MAX_WRITES_PER_FILE:
            over = _shell_write_count - _MAX_WRITES_PER_FILE
            msg = (f"Shell file write BLOCKED ({_shell_write_count - 1} attempts). "
                   f"Do NOT use python open(), cat <<, sed -i, or > to write files. "
                   f"You MUST use <<<WRITE_FILE>>>path\\ncontent or <<<EDIT_FILE>>> instead.")
            _logger.warning(msg)
            _print_colored(f"\n[Shell write blocked] {msg}", _colors['FAIL'])
            if over > 2:
                return None  # Hard stop — agent won't learn, break the loop
            return msg  # Return feedback so agent learns to switch approach
        _print_colored(f"\nAgent is writing files via shell instead of <<<WRITE_FILE>>>.", _colors['WARNING'])
        _print_colored(f"   This bypasses diff preview and loop detection. ({_shell_write_count}/{_MAX_WRITES_PER_FILE})", _colors['WARNING'])

    _print_colored(f"\nAgent wants to run command: {command}", _colors['WARNING'])

    # Deny rules — unconditionally blocked, no prompt
    denied, deny_reason = _check_deny_rules(command)
    if denied:
        _print_colored(f"   DENIED: {deny_reason} — this operation is blocked.", _colors['FAIL'])
        _logger.warning("Command denied by deny rule: %s — %s", command[:100], deny_reason)
        return f"Command denied: {deny_reason}. This operation is not allowed."

    if _config.ALLOW_SHELL_MODE:
        _print_colored(f"   Shell mode enabled (less safe)", _colors['WARNING'])
    else:
        _print_colored(f"   Safe mode (shell=False)", _colors['GREEN'])

    try:
        if not _config.ALLOW_SHELL_MODE:
            command_args = parse_command_safely(command)
            allowed, msg = is_command_allowed(command_args)
            if not allowed:
                _print_colored(f"   {msg}", _colors['FAIL'])
                _logger.warning("Command rejected: %s - %s", command, msg)
                return f"Command rejected: {msg}"
            _print_colored(f"   {msg}", _colors['GREEN'])
    except ValueError as e:
        _print_colored(f"   {str(e)}", _colors['FAIL'])
        _logger.error("Command validation failed: %s - %s", command, str(e))
        return f"Command validation failed: {str(e)}"
    except Exception as e:
        _print_colored(f"   Unexpected error during command validation: {str(e)}", _colors['FAIL'])
        _logger.error("Unexpected error during command validation: %s - %s", command, str(e), exc_info=True)
        return f"Command validation failed with unexpected error: {str(e)}"

    # Dangerous command detection — prompts even in yolo mode
    is_dangerous, danger_reason = _check_dangerous_command(command)
    if is_dangerous:
        _print_colored(f"   DANGEROUS: {danger_reason}", _colors['FAIL'])
        try:
            choice = input(f"{_colors['BOLD']}Allow DANGEROUS command? [y/N] > {_colors['ENDC']}")
        except (EOFError, KeyboardInterrupt):
            return "User cancelled dangerous command."
        if choice.lower() != 'y':
            _logger.info("Dangerous command denied by user: %s — %s", command[:100], danger_reason)
            return f"User denied dangerous command ({danger_reason})."
    elif _should_auto_approve('REMOTE_EXEC'):
        _print_colored(f"   Auto-approved ({_permission_mode} mode)", _colors['GREEN'])
        _logger.info("Auto-approving command (%s mode): %s", _permission_mode, command)
        choice = 'y'
    else:
        try:
            choice = input(f"{_colors['BOLD']}Allow? [y/N] > {_colors['ENDC']}")
        except (EOFError, KeyboardInterrupt):
            _logger.info("Command execution cancelled by user: %s", command)
            return "User cancelled command execution."

        if choice.lower() != 'y':
            _logger.info("Command denied by user: %s", command)
            return "User denied command execution."

    try:
        # Create a temp file to capture output
        # We keep it if output is large so the agent can inspect it later
        with tempfile.NamedTemporaryFile(mode='w+', delete=False, prefix='qwen_cmd_', suffix='.log') as tmp:
            tmp_path = tmp.name
        _temp_tracker['add'](tmp_path)

        # Run process redirecting stdout/stderr to the temp file
        try:
            if _config.ALLOW_SHELL_MODE:
                _logger.debug("Running command in shell mode: %s", command)
                with open(tmp_path, 'w') as f:
                    result = subprocess.run(command, shell=True, stdout=f, stderr=subprocess.STDOUT, text=True, errors='replace', timeout=_config.COMMAND_TIMEOUT)
            else:
                command_args = parse_command_safely(command)
                command_args = expand_paths_in_args(command_args)
                _logger.debug("Running command in safe mode: %s", command_args)
                with open(tmp_path, 'w') as f:
                    result = subprocess.run(command_args, shell=False, stdout=f, stderr=subprocess.STDOUT, text=True, errors='replace', timeout=_config.COMMAND_TIMEOUT)
        except subprocess.TimeoutExpired:
            _logger.error("Command timed out: %s", command[:100])
            return f"Error: Command timed out after {_config.COMMAND_TIMEOUT} seconds. Command: {command[:100]}..."
        except Exception as e:
            _logger.error("Error running command subprocess: %s - %s", command[:100], str(e))
            return f"Error running command subprocess: {str(e)}. Command: {command[:100]}..."

        # Analyze output file
        try:
            with open(tmp_path, 'r', errors='replace') as f:
                content = f.read()
        except IOError as e:
            _logger.error("Error reading command output: %s", str(e))
            return f"Error reading command output: {str(e)}"

        output_len = len(content)
        _logger.info("Command executed successfully, output length: %d", output_len)

        # Use chunked processing if output is large and chunking is enabled
        if chunk_output and output_len > _config.CHUNK_THRESHOLD:  # Same threshold as original
            _logger.debug("Using chunked output for command with %d characters", output_len)
            final_output = get_chunk_for_display(content, chunk_size=_config.CHUNK_SIZE, overlap=_config.CHUNK_OVERLAP, log_path=tmp_path)

            # Store chunk info in a way that can be accessed later if needed
            chunk_info_path = tmp_path + '.chunks'
            _temp_tracker['add'](chunk_info_path)
            chunk_result = chunk_large_output(content, chunk_size=_config.CHUNK_SIZE, overlap=_config.CHUNK_OVERLAP)

            # Save chunk metadata for potential later retrieval
            try:
                with open(chunk_info_path, 'w') as f:
                    json.dump({
                        'total_chunks': chunk_result['total_chunks'],
                        'original_length': chunk_result['original_length'],
                        'chunk_size': chunk_result['chunk_size'],
                        'file_path': tmp_path
                    }, f)
            except IOError as e:
                _print_colored(f"Warning: Could not save chunk metadata: {str(e)}", _colors['WARNING'])
                _logger.warning("Could not save chunk metadata: %s", str(e))

            return final_output

        # If it fits in one go, just return it
        _logger.debug("Returning command output directly, length: %d", len(content) if content else 0)
        return content if content else "(empty output)"

    except Exception as e:
        _logger.error("Error executing command: %s", str(e), exc_info=True)
        return f"Error executing command: {str(e)}"


def read_file_content(path):
    """Read content of a local file safely"""
    try:
        # Expand user path
        full_path = os.path.expanduser(path)
        if not os.path.exists(full_path):
            return f"Error: File not found: {path}"

        # Basic security check - prevent reading outside of home/project if needed
        # For now, we trust the agent as it's running locally under user permissions

        with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()

        size = len(content)
        if size > 100000:
            return f"Error: File too large ({size} bytes). Use head/tail via shell or read specific lines."

        output = f"File: {path}\nContent:\n{content}"
        if len(output) > _config.CHUNK_THRESHOLD:
            return get_chunk_for_display(output, log_path=full_path)
        return output
    except Exception as e:
        return f"Error reading file: {str(e)}"


def write_file_content(payload):
    """Write content to a local file safely.

    Payload format: first line is the path, rest is the content.
    Example:
        /path/to/file.txt
        content goes here
        multiple lines ok
    """
    try:
        lines = payload.split('\n', 1)
        if len(lines) < 2:
            return "Error: WRITE_FILE requires path on first line and content on subsequent lines"

        path = lines[0].strip()
        content = _sanitize_generated_content(lines[1] if len(lines) > 1 else "")

        if not path:
            return "Error: No file path provided"

        # Expand user path
        full_path = os.path.expanduser(path)
        norm_path = os.path.normpath(full_path)

        # Write-loop detection: prevent agent from rewriting the same file endlessly
        _write_counts[norm_path] = _write_counts.get(norm_path, 0) + 1
        if _write_counts[norm_path] > _MAX_WRITES_PER_FILE:
            msg = (f"WRITE_FILE refused: '{os.path.basename(path)}' has been written "
                   f"{_write_counts[norm_path] - 1} times already this session. "
                   f"This looks like a loop. Move on to the next task.")
            _logger.warning(msg)
            _print_colored(f"\n[Loop detected] {msg}", _colors['FAIL'])
            # Return None — NOT a refusal message. Returning a message causes the
            # orchestrator to treat it as tool output and continue the loop forever.
            return None

        # Create parent directories if they don't exist
        parent_dir = os.path.dirname(full_path)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)
            _logger.info("Created directory: %s", parent_dir)

        # Protected path check — always prompts regardless of permission mode
        protected, protect_reason = _is_protected_path(full_path)
        if protected:
            _print_colored(f"\nAgent wants to write file: {full_path}", _colors['FAIL'])
            _print_colored(f"   WARNING: {protect_reason}", _colors['FAIL'])
            _print_colored(f"   Protected path — requires explicit approval.", _colors['FAIL'])
            try:
                choice = input(f"{_colors['BOLD']}Allow write to PROTECTED path? [y/N] > {_colors['ENDC']}")
            except (EOFError, KeyboardInterrupt):
                return "User cancelled write to protected path."
            if choice.lower() != 'y':
                _logger.info("Write to protected path denied: %s — %s", full_path, protect_reason)
                return f"Denied: {protect_reason}"

        _logger.info("Writing file: %s (%s bytes)", full_path, len(content))

        # Show diff if overwriting existing file, else preview
        _print_colored(f"\nAgent wants to write file: {full_path}", _colors['WARNING'])
        _print_colored(f"   Content size: {len(content)} bytes", _colors['WARNING'])

        if os.path.exists(full_path):
            try:
                with open(full_path, 'r', encoding='utf-8', errors='replace') as ef:
                    existing = ef.read()
                _display_diff(existing, content, path)
            except Exception:
                pass  # Fall through to preview if diff fails
        else:
            _print_colored(f"   (new file)", _colors['CYAN'])
            preview_lines = content.split('\n')[:5]
            preview = '\n'.join(preview_lines)
            total_lines = content.count('\n') + 1
            if len(preview_lines) < total_lines:
                preview += f"\n... ({total_lines} total lines)"
            _print_colored(f"   Preview:\n{preview[:500]}", _colors['CYAN'])

        if _should_auto_approve('WRITE_FILE'):
            _print_colored(f"   Auto-approved ({_permission_mode} mode)", _colors['GREEN'])
            _logger.info("Auto-approving file write (%s mode): %s", _permission_mode, full_path)
            choice = 'y'
        else:
            try:
                choice = input(f"{_colors['BOLD']}Allow write? [y/N] > {_colors['ENDC']}")
            except (EOFError, KeyboardInterrupt):
                _logger.info("File write cancelled by user: %s", full_path)
                return "User cancelled file write."

            if choice.lower() != 'y':
                _logger.info("File write denied by user: %s", full_path)
                return "User denied file write."

        # Checkpoint before overwriting
        _create_checkpoint(full_path)

        # Write the file
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)

        _logger.info("File written successfully: %s", full_path)
        return f"Successfully wrote {len(content)} bytes to {path}"

    except PermissionError as e:
        return f"Error: Permission denied writing to {path}: {str(e)}"
    except Exception as e:
        _logger.error("Error writing file %s: %s", path, str(e))
        return f"Error writing file: {str(e)}"


def _normalize_git_style_markers(text):
    """Convert git-style SEARCH/REPLACE markers to <<<OLD>>>/<<<NEW>>> format.

    Qwen models sometimes emit Aider/Cursor-style edit blocks from training data:
        <<<<<<< SEARCH
        old text
        =======
        new text
        >>>>>>> REPLACE
    This normalizes them so edit_file_content can parse them.
    """
    # <<<<<<< SEARCH\n  ->  <<<OLD>>>\n  (preserve the newline after the marker)
    text = re.sub(r'<{1,7}\s*SEARCH\s*>{0,7}\s*\n', '<<<OLD>>>\n', text)
    # =======  (separator between old and new)  ->  <<<NEW>>>
    text = re.sub(r'\n={3,}\s*\n', '\n<<<NEW>>>\n', text)
    # >>>>>>> REPLACE  ->  remove (it's just a closing marker)
    text = re.sub(r'\n>{1,7}\s*REPLACE\s*>{0,7}', '', text)
    return text


def edit_file_content(payload):
    """Edit a file by replacing specific text (search and replace).

    Payload format:
        /path/to/file
        <<<OLD>>>
        text to find (exact match)
        <<<NEW>>>
        replacement text

    Supports multiple replacements in one call by repeating <<<OLD>>>...<<<NEW>>> blocks.
    Also handles git-style <<<<<<< SEARCH / ======= / >>>>>>> REPLACE markers.
    """
    try:
        # Normalize git-style markers before parsing
        payload = _normalize_git_style_markers(payload)

        lines = payload.split('\n', 1)
        if len(lines) < 2:
            return "Error: EDIT_FILE requires path on first line and OLD/NEW blocks"

        path = lines[0].strip()
        edit_content = lines[1] if len(lines) > 1 else ""

        if not path:
            return "Error: No file path provided"

        full_path = os.path.expanduser(path)

        if not os.path.exists(full_path):
            return f"Error: File not found: {path}"

        # Protected path check
        protected, protect_reason = _is_protected_path(full_path)
        if protected:
            _print_colored(f"\nAgent wants to edit file: {full_path}", _colors['FAIL'])
            _print_colored(f"   WARNING: {protect_reason}", _colors['FAIL'])
            try:
                choice = input(f"{_colors['BOLD']}Allow edit of PROTECTED path? [y/N] > {_colors['ENDC']}")
            except (EOFError, KeyboardInterrupt):
                return "User cancelled edit of protected path."
            if choice.lower() != 'y':
                _logger.info("Edit of protected path denied: %s — %s", full_path, protect_reason)
                return f"Denied: {protect_reason}"

        # Read current file content
        with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()

        original_content = content

        # Parse OLD/NEW blocks
        # Pattern: <<<OLD>>>\n...\n<<<NEW>>>\n...
        edit_pattern = r'<<<OLD>>>\n(.*?)\n<<<NEW>>>\n(.*?)(?=\n<<<OLD>>>|$)'
        matches = list(re.finditer(edit_pattern, edit_content, re.DOTALL))

        if not matches:
            # Try alternate format without trailing newline requirement
            edit_pattern = r'<<<OLD>>>(.*?)<<<NEW>>>(.*?)(?=<<<OLD>>>|$)'
            matches = list(re.finditer(edit_pattern, edit_content, re.DOTALL))

        if not matches:
            return "Error: No valid <<<OLD>>>...<<<NEW>>> blocks found. Format:\n<<<OLD>>>\nold text\n<<<NEW>>>\nnew text"

        replacements_made = 0
        for match in matches:
            old_text = match.group(1).strip('\n')
            new_text = _sanitize_generated_content(match.group(2).strip('\n'))

            if old_text not in content:
                _logger.warning("EDIT_FILE: old_text not found in %s: %s...", path, old_text[:100])
                return f"Error: Text to replace not found in file. Searched for:\n{old_text[:200]}"

            # Count occurrences
            occurrences = content.count(old_text)
            if occurrences > 1:
                _logger.warning("EDIT_FILE: Multiple occurrences (%s) of old_text in %s", occurrences, path)
                # Still proceed but warn
                _print_colored(f"   Warning: Found {occurrences} occurrences, replacing all", _colors['WARNING'])

            content = content.replace(old_text, new_text)
            replacements_made += occurrences

        if content == original_content:
            return "Error: No changes made - old text may not match exactly"

        # Show unified diff
        _print_colored(f"\nAgent wants to edit file: {full_path}", _colors['WARNING'])
        _print_colored(f"   Replacements: {replacements_made}", _colors['CYAN'])
        old_lines = original_content.split('\n')
        new_lines = content.split('\n')
        _print_colored(f"   Lines: {len(old_lines)} -> {len(new_lines)}", _colors['CYAN'])
        _display_diff(original_content, content, path)

        if _should_auto_approve('EDIT_FILE'):
            _print_colored(f"   Auto-approved ({_permission_mode} mode)", _colors['GREEN'])
            _logger.info("Auto-approving file edit (%s mode): %s", _permission_mode, full_path)
            choice = 'y'
        else:
            try:
                choice = input(f"{_colors['BOLD']}Allow edit? [y/N] > {_colors['ENDC']}")
            except (EOFError, KeyboardInterrupt):
                _logger.info("File edit cancelled by user: %s", full_path)
                return "User cancelled file edit."

            if choice.lower() != 'y':
                _logger.info("File edit denied by user: %s", full_path)
                return "User denied file edit."

        # Checkpoint before editing
        _create_checkpoint(full_path)

        # Write the edited content
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)

        _logger.info("File edited successfully: %s (%s replacements)", full_path, replacements_made)
        return f"Successfully edited {path}: {replacements_made} replacement(s) made"

    except Exception as e:
        _logger.error("Error editing file %s: %s", path, str(e))
        return f"Error editing file: {str(e)}"


def list_directory(path):
    """List contents of a directory with file info.

    Returns structured listing with file types, sizes, and modification times.
    """
    try:
        full_path = os.path.expanduser(path.strip()) if path else os.getcwd()

        if not os.path.exists(full_path):
            return f"Error: Directory not found: {path}"

        if not os.path.isdir(full_path):
            return f"Error: Not a directory: {path}"

        entries = []
        try:
            items = sorted(os.listdir(full_path))
        except PermissionError:
            return f"Error: Permission denied reading directory: {path}"

        for item in items:
            item_path = os.path.join(full_path, item)
            try:
                stat = os.stat(item_path)
                is_dir = os.path.isdir(item_path)
                size = stat.st_size if not is_dir else 0
                mtime = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M')

                if is_dir:
                    entries.append(f"  {item}/")
                else:
                    # Human-readable size
                    if size < 1024:
                        size_str = f"{size}B"
                    elif size < 1024 * 1024:
                        size_str = f"{size // 1024}K"
                    else:
                        size_str = f"{size // (1024 * 1024)}M"
                    entries.append(f"  {item:<40} {size_str:>8}  {mtime}")
            except (OSError, PermissionError):
                entries.append(f"  {item} (inaccessible)")

        result = f"Directory: {full_path}\n"
        result += f"Total: {len(items)} items\n\n"
        result += "\n".join(entries) if entries else "(empty)"

        return result

    except Exception as e:
        _logger.error("Error listing directory %s: %s", path, str(e))
        return f"Error listing directory: {str(e)}"


def glob_files(pattern):
    """Find files matching a glob pattern.

    Supports patterns like:
        *.swift           - Swift files in current dir
        **/*.swift        - Swift files recursively
        src/**/*.py       - Python files under src/
        /absolute/path/*  - Files in absolute path
    """
    try:
        pattern = pattern.strip()
        if not pattern:
            return "Error: No glob pattern provided"

        # If pattern doesn't start with / or ., assume current directory
        if not pattern.startswith('/') and not pattern.startswith('.'):
            # Check if it's a relative path or just a pattern
            if '/' not in pattern or pattern.startswith('**'):
                pattern = os.path.join(os.getcwd(), pattern)

        # Expand user home if present
        pattern = os.path.expanduser(pattern)

        # Use recursive glob
        matches = sorted(glob_module.glob(pattern, recursive=True))

        if not matches:
            return f"No files found matching: {pattern}"

        # Limit output to prevent overwhelming responses
        max_results = 200
        truncated = len(matches) > max_results

        result_lines = [f"Found {len(matches)} file(s) matching: {pattern}"]
        if truncated:
            result_lines.append(f"(showing first {max_results})")
        result_lines.append("")

        for match in matches[:max_results]:
            # Show relative path if possible
            try:
                rel_path = os.path.relpath(match)
                if not rel_path.startswith('..'):
                    result_lines.append(f"  {rel_path}")
                else:
                    result_lines.append(f"  {match}")
            except ValueError:
                result_lines.append(f"  {match}")

        if truncated:
            result_lines.append(f"\n... and {len(matches) - max_results} more")

        return "\n".join(result_lines)

    except Exception as e:
        _logger.error("Error in glob %s: %s", pattern, str(e))
        return f"Error in glob: {str(e)}"


def grep_search(payload):
    """Search for pattern in files.

    Payload format (one of):
        pattern                     - Search pattern in current directory
        pattern|path                - Search pattern in specific path
        pattern|path|options        - With options like -i (case insensitive), -n (line numbers)

    Examples:
        TODO                        - Find TODO in current dir
        def.*init|*.py              - Find 'def.*init' in Python files
        import|src/|i               - Case-insensitive search for 'import' in src/
    """
    try:
        parts = payload.strip().split('|')
        pattern = parts[0].strip() if parts else ""
        path = parts[1].strip() if len(parts) > 1 else "."
        options = parts[2].strip().lower() if len(parts) > 2 else ""

        if not pattern:
            return "Error: No search pattern provided"

        # Expand path
        search_path = os.path.expanduser(path)
        if not os.path.exists(search_path):
            return f"Error: Path not found: {path}"

        # Build regex flags
        flags = re.MULTILINE
        if 'i' in options:
            flags |= re.IGNORECASE

        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return f"Error: Invalid regex pattern: {e}"

        results = []
        files_searched = 0
        files_matched = 0
        max_results = 100
        max_files = 500

        # Determine files to search
        if os.path.isfile(search_path):
            files_to_search = [search_path]
        else:
            # Walk directory
            files_to_search = []
            for root, dirs, files in os.walk(search_path):
                # Skip hidden and common non-code directories
                dirs[:] = [d for d in dirs if not d.startswith('.') and d not in
                          ['node_modules', '__pycache__', 'venv', 'env', '.git', 'build', 'dist', 'DerivedData']]

                for filename in files:
                    if filename.startswith('.'):
                        continue
                    files_to_search.append(os.path.join(root, filename))

                    if len(files_to_search) >= max_files:
                        break
                if len(files_to_search) >= max_files:
                    break

        for filepath in files_to_search:
            files_searched += 1
            try:
                # Skip very large files to avoid memory issues
                if os.path.getsize(filepath) > 5 * 1024 * 1024:
                    continue
                with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()

                matches = list(regex.finditer(content))
                if matches:
                    files_matched += 1
                    lines = content.split('\n')

                    # Get relative path
                    try:
                        rel_path = os.path.relpath(filepath)
                    except ValueError:
                        rel_path = filepath

                    # Precompute newline offsets ONCE so per-match line lookup
                    # is O(log lines) via bisect instead of O(file_size) per
                    # match via content[:match.start()].count('\n').
                    import bisect
                    line_starts = [0]
                    for i, ch in enumerate(content):
                        if ch == '\n':
                            line_starts.append(i + 1)

                    for match in matches[:10]:  # Limit matches per file
                        if len(results) >= max_results:
                            break

                        # bisect_right gives 1-indexed line number directly
                        line_num = bisect.bisect_right(line_starts, match.start())
                        line_content = lines[line_num - 1] if line_num <= len(lines) else ""

                        # Truncate long lines
                        if len(line_content) > 200:
                            line_content = line_content[:200] + "..."

                        results.append(f"{rel_path}:{line_num}: {line_content.strip()}")

                    if len(results) >= max_results:
                        break

            except (IOError, OSError, UnicodeDecodeError):
                continue  # Skip unreadable files

        # Build result
        header = f"Search: '{pattern}' in {path}\n"
        header += f"Files searched: {files_searched}, Files matched: {files_matched}\n"

        if not results:
            return header + "\nNo matches found."

        truncated = len(results) >= max_results
        if truncated:
            header += f"Showing first {max_results} results:\n"

        return header + "\n" + "\n".join(results)

    except Exception as e:
        _logger.error("Error in grep search: %s", str(e))
        return f"Error in search: {str(e)}"


def _normalize_standalone_search_replace(text):
    """Convert standalone git-style SEARCH/REPLACE blocks into <<<EDIT_FILE>>> blocks.

    Catches the case where the model emits a bare search/replace block without
    wrapping it in <<<EDIT_FILE>>>:
        /path/to/file
        <<<<<<< SEARCH
        old text
        =======
        new text
        >>>>>>> REPLACE

    Converts to:
        <<<EDIT_FILE>>>/path/to/file
        <<<OLD>>>
        old text
        <<<NEW>>>
        new text
    """
    # Match: filepath line, then <<<<<<< SEARCH block(s)
    # The filepath is on the line before <<<<<<< SEARCH
    pattern = (
        r'^([^\n<>]+?)\s*\n'                  # filepath (line without angle brackets)
        r'<{1,7}\s*SEARCH\s*>{0,7}\s*\n'      # <<<<<<< SEARCH
        r'(.*?)'                                # old text (multi-line, lazy)
        r'\n={3,}\s*\n'                         # =======
        r'(.*?)'                                # new text (multi-line, lazy)
        r'\n>{1,7}\s*REPLACE\s*>{0,7}'         # >>>>>>> REPLACE
    )
    def _replace(m):
        path = m.group(1).strip()
        old = m.group(2)
        new = m.group(3)
        return f'<<<EDIT_FILE>>>{path}\n<<<OLD>>>\n{old}\n<<<NEW>>>\n{new}\n'

    return re.sub(pattern, _replace, text, flags=re.DOTALL | re.MULTILINE)


def process_remote_commands(response_text: str) -> Optional[str]:
    """Process ALL remote command markers in agent response.

    Finds and executes every tool marker in the response, in order of appearance.
    Returns aggregated output from all commands, or None if no markers found.
    """
    # Pre-process: strip Qwen3.5 native formatting that breaks marker parsing.
    # <tool_call> special token turns <<<TAG>>> into <tool_call>TAG>>> — strip it.
    response_text = re.sub(r'</?tool_call\s*>', '', response_text)
    # <REACT>Thought:...</REACT> reasoning blocks are noise — strip them.
    response_text = re.sub(r'<REACT>.*?</REACT>', '', response_text, flags=re.DOTALL)

    # Strip agentic markers (SCRATCHPAD, PLAN, CONFIDENCE) so they don't leak
    # into file content. These aren't tool commands but the command regex doesn't
    # know about them, so <<<CONFIDENCE>>>95 ends up inside WRITE_FILE payloads.
    response_text = re.sub(
        r'<{1,3}SCRATCHPAD>{1,3}\s*.*?(?=<{1,3}\w+>{1,3}|\Z)', '', response_text, flags=re.DOTALL
    )
    response_text = re.sub(
        r'<{1,3}PLAN>{1,3}\s*.*?(?=<{1,3}\w+>{1,3}|\Z)', '', response_text, flags=re.DOTALL
    )
    response_text = re.sub(r'<{0,3}/?CONFIDEN\w*>{0,3}\s*\d*', '', response_text)

    # Pre-process: convert standalone git-style SEARCH/REPLACE into EDIT_FILE blocks
    response_text = _normalize_standalone_search_replace(response_text)

    # Dispatch table: opening tag name -> handler
    command_handlers = {
        'REMOTE_EXEC':    lambda arg: execute_remote_command(arg.strip()),
        'READ_FILE':      lambda arg: read_file_content(arg.strip()),
        'WRITE_FILE':     lambda arg: write_file_content(arg),
        'EDIT_FILE':      lambda arg: edit_file_content(arg),
        'LIST_DIR':       lambda arg: list_directory(arg),
        'GLOB':           lambda arg: glob_files(arg),
        'GREP':           lambda arg: grep_search(arg),
        'SAVE_MEMORY':    lambda arg: _external_handlers['save_memory'](arg.strip()),
        'WEB_SEARCH':     lambda arg: _external_handlers['web_search'](arg.strip()),
        'CUPERTINO':      lambda arg: _external_handlers['handle_cupertino_search'](arg.strip()),
        'APPLE_DEEP_DOCS': lambda arg: _external_handlers['handle_apple_deep_docs'](arg.strip()),
        'INGEST_PDF':     lambda arg: ingest_pdf_content(arg),
        'DEEP_INGEST':    lambda arg: _external_handlers['ingest_url_content'](arg.strip()),
    }

    # Build regex from known tags so internal markers (<<<OLD>>>, <<<NEW>>>) are not
    # mistaken for command boundaries.  Each command block runs from its opening tag
    # to the next known opening tag (or end of string).  No closing tags required.
    #
    # The regex matches liberally (1-3 brackets on each side) so we can detect
    # malformed markers as content-boundary anchors, but a post-match validation
    # step rejects any bracket combination that isn't one of the documented forms:
    #   <<<TAG>>>  -- standard triple-bracket format
    #   <TAG>>>    -- after <tool_call> special token strip (1 open, 3 close)
    #   <TAG>      -- XML-style (1 open, 1 close; seen with EDIT_FILE)
    # Asymmetric or partial markers like <<<TAG> or <<TAG>>> are rejected, which
    # forces the model to emit a properly-formed marker on its next turn instead
    # of silently executing a tool from a malformed call.
    _TAG_NAMES = '|'.join(command_handlers.keys())
    _COMMAND_RE = (
        rf'(<{{1,3}})({_TAG_NAMES})(>{{1,3}})\s*'
        rf'(.*?)'
        rf'(?=<{{1,3}}(?:{_TAG_NAMES})>{{1,3}}|\Z)'
    )
    _VALID_BRACKETS = frozenset({('<<<', '>>>'), ('<', '>>>'), ('<', '>')})

    all_matches = []
    for match in re.finditer(_COMMAND_RE, response_text, re.DOTALL | re.IGNORECASE):
        open_brackets = match.group(1)
        tag = match.group(2).upper()
        close_brackets = match.group(3)
        content = match.group(4).strip()

        if (open_brackets, close_brackets) not in _VALID_BRACKETS:
            if _logger:
                _logger.warning(
                    "Rejecting malformed tool marker: %s%s%s — expected one of "
                    "<<<TAG>>>, <TAG>>>, or <TAG>",
                    open_brackets, tag, close_brackets
                )
            continue

        if content:  # skip empty blocks (e.g. stale closing tags parsed as openers)
            all_matches.append((match.start(), match, tag, command_handlers[tag]))

    if not all_matches:
        return None

    # Execute all commands and aggregate results
    results = []
    total_len = 0
    GLOBAL_MAX_LEN = 40000 # ~10k tokens global cap for tool outputs in one turn

    for i, (_, match, tag, handler) in enumerate(all_matches):
        if total_len > GLOBAL_MAX_LEN:
            results.append(f"\n... [OMITTED {len(all_matches) - i} ADDITIONAL COMMANDS TO PREVENT CONTEXT OVERFLOW] ...")
            break

        arg = match.group(4)
        # Strip closing tags the model generates (e.g. </REMOTE_EXEC>, </list_dir>).
        # These get captured as part of the content and corrupt shell commands
        # (the shell interprets </TAG> as input redirection < /TAG>).
        arg = re.sub(r'</\w+>\s*$', '', arg)
        try:
            result = handler(arg)
            if result is None:
                continue  # Handler silently refused (e.g., write-loop detection)
            if result:
                # In compact mode, show one-liner to user but keep full result for history
                if not _verbose_mode:
                    summary = _compact_summary(tag, arg, result)
                    if summary:
                        _print_colored(summary, _colors['CYAN'])
                res_str = f"[Command {i+1}] {result}"
                results.append(res_str)
                total_len += len(res_str)
        except Exception as e:
            err_str = f"[Command {i+1}] Error: {str(e)}"
            results.append(err_str)
            total_len += len(err_str)

    return "\n\n".join(results) if results else None


def extract_fallback_commands(response_text: str) -> List[str]:
    """Extract shell commands from markdown code blocks when <<<REMOTE_EXEC>>> markers are missing.

    Catches the common failure mode where the model writes a correct command
    inside a fenced code block instead of using the marker protocol.
    Only extracts blocks explicitly tagged as shell languages (bash/shell/sh/zsh).
    Plain or non-shell code blocks (python, swift, etc.) are ignored to prevent
    catastrophic misexecution of code snippets as shell commands.
    """
    commands = []

    # REQUIRE a shell language hint -- the ? was removed to prevent matching
    # python/swift/unlabeled blocks which caused catastrophic misexecution.
    # Allow matching to end-of-string (```|\Z) for unclosed code blocks --
    # the model sometimes emits a stop token before closing backticks.
    for m in re.finditer(r'```(?:bash|shell|sh|zsh)\s*\n(.+?)(?:```|\Z)', response_text, re.DOTALL):
        block = m.group(1).strip()
        if block:
            commands.append(block)

    return commands


def ingest_pdf_content(payload):
    """Ingest a PDF file into memory. Can handle both local client files and server files.

    Payload format:
        /path/to/document.pdf       - Path to PDF file (on client or server)
        local:/path/to/document.pdf - Explicitly indicates client-side file to upload first

    Examples:
        /home/user/docs/manual.pdf
        local:/Users/me/Documents/report.pdf
    """
    try:
        import base64
        import requests

        path = payload.strip()
        if not path:
            return "Error: No PDF path provided"

        # Check if this is a local file that needs to be uploaded
        if path.startswith('local:') or not path.startswith('/'):
            # This is a local file path - we need to upload it first
            if path.startswith('local:'):
                local_path = path[6:]  # Remove 'local:' prefix
            else:
                local_path = path

            # Expand user path if needed
            local_path = os.path.expanduser(local_path)

            # Check if file exists locally
            if not os.path.exists(local_path):
                return f"Error: Local file not found: {local_path}"

            # Read the file content
            with open(local_path, 'rb') as f:
                file_content = f.read()

            # Encode as base64
            encoded_content = base64.b64encode(file_content).decode('utf-8')

            # Upload the file to the server
            upload_payload = {
                "filename": os.path.basename(local_path),
                "content": encoded_content
            }

            upload_url = f"http://{_config.LINUX_SERVER_IP}:5000/v1/files/upload"
            _print_colored(f"Uploading {local_path} to server...", _colors['CYAN'])

            response = requests.post(upload_url, json=upload_payload, timeout=_config.LONG_REQUEST_TIMEOUT)
            if response.status_code != 200:
                return f"Error uploading file: {response.text}"

            upload_result = response.json()
            server_path = upload_result.get("path", "")

            if not server_path:
                return "Error: Upload succeeded but no path returned"

            _print_colored(f"File uploaded to server: {server_path}", _colors['GREEN'])

            # Now ingest the uploaded file
            result = _external_handlers['ingest_pdf'](server_path)
            return result
        else:
            # This is a server-side file path - ingest directly
            result = _external_handlers['ingest_pdf'](path)
            return result
    except Exception as e:
        _logger.error("Error ingesting PDF %s: %s", path, str(e))
        return f"Error ingesting PDF: {str(e)}"
