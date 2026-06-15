"""Disk-write helpers shared by the file and shell handlers.

The single content-sanitization boundary between model output and disk, colored
diff display, checkpoint create/undo for the /undo command, and the compact
one-line summary formatter. Depends only on stdlib + the shared `state`.
"""
import difflib
import hashlib
import os
import re
import shutil
from datetime import datetime

try:
    from rich.syntax import Syntax
except ImportError:
    Syntax = None

from coding_model_server.tool_state import state


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
    # Coding Model special tokens
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
        state.print_colored("   (no changes)", state.colors['CYAN'])
        return

    diff_text = ''.join(diff_lines)
    if state.rich_console:
        state.rich_console.print(Syntax(diff_text, "diff", theme="monokai", word_wrap=True))
    else:
        for line in diff_lines:
            line = line.rstrip('\n')
            if line.startswith('+'):
                state.print_colored(line, state.colors['GREEN'])
            elif line.startswith('-'):
                state.print_colored(line, state.colors['FAIL'])
            elif line.startswith('@@'):
                state.print_colored(line, state.colors['CYAN'])
            else:
                print(line)


def _create_checkpoint(filepath):
    """Backup a file before modification for /undo support."""
    if not os.path.exists(filepath):
        return
    os.makedirs(state.CHECKPOINT_DIR, exist_ok=True)
    path_hash = hashlib.md5(filepath.encode()).hexdigest()[:8]
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    basename = os.path.basename(filepath)
    checkpoint_name = f"{path_hash}_{ts}_{basename}"
    checkpoint_path = os.path.join(state.CHECKPOINT_DIR, checkpoint_name)
    shutil.copy2(filepath, checkpoint_path)
    state.checkpoint_stack.append((filepath, checkpoint_path, ts))
    # FIFO cleanup
    while len(state.checkpoint_stack) > state.MAX_CHECKPOINTS:
        _, old_path, _ = state.checkpoint_stack.pop(0)
        try:
            os.remove(old_path)
        except OSError:
            pass
    state.logger.info("Checkpoint created: %s", checkpoint_path)


def undo_last_checkpoint():
    """Restore the most recently checkpointed file. Called by /undo command."""
    if not state.checkpoint_stack:
        return "No checkpoints available to undo."
    original_path, checkpoint_path, ts = state.checkpoint_stack.pop()
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
