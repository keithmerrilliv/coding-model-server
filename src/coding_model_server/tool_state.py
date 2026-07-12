"""Shared mutable state for the tool-handler functions.

tool_handlers historically kept ~9 ``configure()``-injected module globals plus
checkpoint/write-count state as loose module-level names. Consolidating them
into one ``state`` object (a) removes the scattered ``global`` declarations, and
(b) gives the soon-to-be-split handler submodules a single shared object to
import instead of reaching back into the package for each loose name.

There is exactly one instance (``state``); handlers read/write its attributes.
The interactive client drives tools one at a time, so this is not contended —
the object is for cohesion and clarity, not concurrency control.
"""
import os
from typing import Optional


class ToolState:
    """Runtime dependencies + counters shared across tool handlers."""

    # ── configure()-injected dependencies ───────────────────────────────────
    config = None
    colors = None
    print_colored = None        # print_colored(text, color) callable
    logger = None
    temp_tracker = None          # dict with an 'add' callable
    external_handlers = None     # dict: save_memory, web_search, ingest_pdf, ...
    rich_console = None
    permission_mode = 'default'  # 'default' | 'acceptEdits' | 'yolo'
    verbose_mode = True          # True=full output, False=compact one-liners

    # ── checkpoint state for /undo ──────────────────────────────────────────
    CHECKPOINT_DIR = os.path.expanduser("~/.coding_model_checkpoints")
    MAX_CHECKPOINTS = 20

    # ── write-loop detection ─────────────────────────────────────────────────
    MAX_WRITES_PER_FILE = 3

    def __init__(self):
        # Instance-level mutable containers (never share across instances).
        self.checkpoint_stack: list = []   # [(original_path, checkpoint_path, ts)]
        self.write_counts: dict = {}        # {normalized_path: count}
        self.shell_write_count: int = 0


state = ToolState()
