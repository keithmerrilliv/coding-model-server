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

# Shared runtime dependencies + counters. configure() populates the injected
# fields; handlers read/write state.* (see tool_state.ToolState).
from qwen_server.tool_state import state

# ---------------------------------------------------------------------------
# Safety: protected paths, dangerous commands, deny rules
# ---------------------------------------------------------------------------

# Security gates (protected paths, dangerous-command warnings, deny rules,
# permission gating) live in .safety; re-exported for the public API + the
# handlers below that call them.
from qwen_server.tool_handlers.safety import (  # noqa: E402
    DANGEROUS_PATTERNS,
    DENY_RULES,
    PROTECTED_FILES,
    PROTECTED_PATHS,
    _check_dangerous_command,
    _check_deny_rules,
    _is_protected_path,
    _should_auto_approve,
)


def reset_write_counts():
    """Clear per-file and shell-write counters between tasks."""
    state.write_counts.clear()
    state.shell_write_count = 0


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
    state.config = config
    state.colors = colors
    state.print_colored = print_colored_fn
    state.logger = logger_inst
    state.temp_tracker = temp_tracker
    state.external_handlers = external_handlers
    state.rich_console = kwargs.get('rich_console', None)
    state.permission_mode = kwargs.get('permission_mode', getattr(config, 'PERMISSION_MODE', 'default'))
    state.verbose_mode = kwargs.get('verbose_mode', getattr(config, 'VERBOSE_MODE', True))


def set_permission_mode(mode):
    """Update permission mode at runtime (called by /permissions command)."""
    state.permission_mode = mode


def set_verbose_mode(mode):
    """Update verbose mode at runtime (called by /verbose command)."""
    state.verbose_mode = mode


# Disk-write helpers (sanitizer, diff display, checkpoints, compact summary)
# live in .editing; re-exported for the public API + the handlers that use them.
from qwen_server.tool_handlers.editing import (  # noqa: E402
    _SANITIZE_PATTERNS,
    _compact_summary,
    _create_checkpoint,
    _display_diff,
    _sanitize_generated_content,
    undo_last_checkpoint,
)


# Shell exec (parsing, whitelist, REMOTE_EXEC handler) lives in .shell.
from qwen_server.tool_handlers.shell import (  # noqa: E402
    execute_remote_command,
    expand_paths_in_args,
    is_command_allowed,
    parse_command_safely,
)


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
        if len(output) > state.config.CHUNK_THRESHOLD:
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
        state.write_counts[norm_path] = state.write_counts.get(norm_path, 0) + 1
        if state.write_counts[norm_path] > state.MAX_WRITES_PER_FILE:
            msg = (f"WRITE_FILE refused: '{os.path.basename(path)}' has been written "
                   f"{state.write_counts[norm_path] - 1} times already this session. "
                   f"This looks like a loop. Move on to the next task.")
            state.logger.warning(msg)
            state.print_colored(f"\n[Loop detected] {msg}", state.colors['FAIL'])
            # Return None — NOT a refusal message. Returning a message causes the
            # orchestrator to treat it as tool output and continue the loop forever.
            return None

        # Create parent directories if they don't exist
        parent_dir = os.path.dirname(full_path)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)
            state.logger.info("Created directory: %s", parent_dir)

        # Protected path check — always prompts regardless of permission mode
        protected, protect_reason = _is_protected_path(full_path)
        if protected:
            state.print_colored(f"\nAgent wants to write file: {full_path}", state.colors['FAIL'])
            state.print_colored(f"   WARNING: {protect_reason}", state.colors['FAIL'])
            state.print_colored(f"   Protected path — requires explicit approval.", state.colors['FAIL'])
            try:
                choice = input(f"{state.colors['BOLD']}Allow write to PROTECTED path? [y/N] > {state.colors['ENDC']}")
            except (EOFError, KeyboardInterrupt):
                return "User cancelled write to protected path."
            if choice.lower() != 'y':
                state.logger.info("Write to protected path denied: %s — %s", full_path, protect_reason)
                return f"Denied: {protect_reason}"

        state.logger.info("Writing file: %s (%s bytes)", full_path, len(content))

        # Show diff if overwriting existing file, else preview
        state.print_colored(f"\nAgent wants to write file: {full_path}", state.colors['WARNING'])
        state.print_colored(f"   Content size: {len(content)} bytes", state.colors['WARNING'])

        if os.path.exists(full_path):
            try:
                with open(full_path, 'r', encoding='utf-8', errors='replace') as ef:
                    existing = ef.read()
                _display_diff(existing, content, path)
            except Exception:
                pass  # Fall through to preview if diff fails
        else:
            state.print_colored(f"   (new file)", state.colors['CYAN'])
            preview_lines = content.split('\n')[:5]
            preview = '\n'.join(preview_lines)
            total_lines = content.count('\n') + 1
            if len(preview_lines) < total_lines:
                preview += f"\n... ({total_lines} total lines)"
            state.print_colored(f"   Preview:\n{preview[:500]}", state.colors['CYAN'])

        if _should_auto_approve('WRITE_FILE'):
            state.print_colored(f"   Auto-approved ({state.permission_mode} mode)", state.colors['GREEN'])
            state.logger.info("Auto-approving file write (%s mode): %s", state.permission_mode, full_path)
            choice = 'y'
        else:
            try:
                choice = input(f"{state.colors['BOLD']}Allow write? [y/N] > {state.colors['ENDC']}")
            except (EOFError, KeyboardInterrupt):
                state.logger.info("File write cancelled by user: %s", full_path)
                return "User cancelled file write."

            if choice.lower() != 'y':
                state.logger.info("File write denied by user: %s", full_path)
                return "User denied file write."

        # Checkpoint before overwriting
        _create_checkpoint(full_path)

        # Write the file
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)

        state.logger.info("File written successfully: %s", full_path)
        return f"Successfully wrote {len(content)} bytes to {path}"

    except PermissionError as e:
        return f"Error: Permission denied writing to {path}: {str(e)}"
    except Exception as e:
        state.logger.error("Error writing file %s: %s", path, str(e))
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
            state.print_colored(f"\nAgent wants to edit file: {full_path}", state.colors['FAIL'])
            state.print_colored(f"   WARNING: {protect_reason}", state.colors['FAIL'])
            try:
                choice = input(f"{state.colors['BOLD']}Allow edit of PROTECTED path? [y/N] > {state.colors['ENDC']}")
            except (EOFError, KeyboardInterrupt):
                return "User cancelled edit of protected path."
            if choice.lower() != 'y':
                state.logger.info("Edit of protected path denied: %s — %s", full_path, protect_reason)
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
                state.logger.warning("EDIT_FILE: old_text not found in %s: %s...", path, old_text[:100])
                return f"Error: Text to replace not found in file. Searched for:\n{old_text[:200]}"

            # Count occurrences
            occurrences = content.count(old_text)
            if occurrences > 1:
                state.logger.warning("EDIT_FILE: Multiple occurrences (%s) of old_text in %s", occurrences, path)
                # Still proceed but warn
                state.print_colored(f"   Warning: Found {occurrences} occurrences, replacing all", state.colors['WARNING'])

            content = content.replace(old_text, new_text)
            replacements_made += occurrences

        if content == original_content:
            return "Error: No changes made - old text may not match exactly"

        # Show unified diff
        state.print_colored(f"\nAgent wants to edit file: {full_path}", state.colors['WARNING'])
        state.print_colored(f"   Replacements: {replacements_made}", state.colors['CYAN'])
        old_lines = original_content.split('\n')
        new_lines = content.split('\n')
        state.print_colored(f"   Lines: {len(old_lines)} -> {len(new_lines)}", state.colors['CYAN'])
        _display_diff(original_content, content, path)

        if _should_auto_approve('EDIT_FILE'):
            state.print_colored(f"   Auto-approved ({state.permission_mode} mode)", state.colors['GREEN'])
            state.logger.info("Auto-approving file edit (%s mode): %s", state.permission_mode, full_path)
            choice = 'y'
        else:
            try:
                choice = input(f"{state.colors['BOLD']}Allow edit? [y/N] > {state.colors['ENDC']}")
            except (EOFError, KeyboardInterrupt):
                state.logger.info("File edit cancelled by user: %s", full_path)
                return "User cancelled file edit."

            if choice.lower() != 'y':
                state.logger.info("File edit denied by user: %s", full_path)
                return "User denied file edit."

        # Checkpoint before editing
        _create_checkpoint(full_path)

        # Write the edited content
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)

        state.logger.info("File edited successfully: %s (%s replacements)", full_path, replacements_made)
        return f"Successfully edited {path}: {replacements_made} replacement(s) made"

    except Exception as e:
        state.logger.error("Error editing file %s: %s", path, str(e))
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
        state.logger.error("Error listing directory %s: %s", path, str(e))
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
        state.logger.error("Error in glob %s: %s", pattern, str(e))
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
        state.logger.error("Error in grep search: %s", str(e))
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
        'SAVE_MEMORY':    lambda arg: state.external_handlers['save_memory'](arg.strip()),
        'WEB_SEARCH':     lambda arg: state.external_handlers['web_search'](arg.strip()),
        'CUPERTINO':      lambda arg: state.external_handlers['handle_cupertino_search'](arg.strip()),
        'APPLE_DEEP_DOCS': lambda arg: state.external_handlers['handle_apple_deep_docs'](arg.strip()),
        'INGEST_PDF':     lambda arg: ingest_pdf_content(arg),
        'DEEP_INGEST':    lambda arg: state.external_handlers['ingest_url_content'](arg.strip()),
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
            if state.logger:
                state.logger.warning(
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
                if not state.verbose_mode:
                    summary = _compact_summary(tag, arg, result)
                    if summary:
                        state.print_colored(summary, state.colors['CYAN'])
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

            upload_url = f"http://{state.config.LINUX_SERVER_IP}:5000/v1/files/upload"
            state.print_colored(f"Uploading {local_path} to server...", state.colors['CYAN'])

            response = requests.post(upload_url, json=upload_payload, timeout=state.config.LONG_REQUEST_TIMEOUT)
            if response.status_code != 200:
                return f"Error uploading file: {response.text}"

            upload_result = response.json()
            server_path = upload_result.get("path", "")

            if not server_path:
                return "Error: Upload succeeded but no path returned"

            state.print_colored(f"File uploaded to server: {server_path}", state.colors['GREEN'])

            # Now ingest the uploaded file
            result = state.external_handlers['ingest_pdf'](server_path)
            return result
        else:
            # This is a server-side file path - ingest directly
            result = state.external_handlers['ingest_pdf'](path)
            return result
    except Exception as e:
        state.logger.error("Error ingesting PDF %s: %s", path, str(e))
        return f"Error ingesting PDF: {str(e)}"
