"""
Tool handlers for the Coding Model client — package facade.

This package's submodules implement command execution, file operations, marker
parsing/dispatch, and their shared helpers:

    tool_state  — the shared ToolState (configure() populates it)
    safety      — protected paths, dangerous-command/deny rules, permission gating
    chunking    — large-output chunking for the context window
    editing     — sanitizer, diff display, checkpoints, compact-summary formatter
    shell       — command parsing/whitelist + the REMOTE_EXEC handler
    files       — read/write/edit/list/glob/grep handlers
    dispatch    — <<<TAG>>> marker parsing + tool dispatch (top layer)

This __init__ keeps the configure()/mode lifecycle functions and re-exports the
public API, so existing callers (`from coding_model_server import tool_handlers`) and
`tool_handlers.<fn>` access are unchanged.
"""
# Shared runtime dependencies + counters. configure() populates the injected
# fields; handlers read/write state.* (see tool_state.ToolState).
from coding_model_server.tool_state import state

# Security gates (protected paths, dangerous-command warnings, deny rules,
# permission gating) live in .safety; re-exported for the public API + the
# handlers below that call them.
from coding_model_server.tool_handlers.safety import (  # noqa: E402
    DANGEROUS_PATTERNS,
    DENY_RULES,
    PROTECTED_FILES,
    PROTECTED_PATHS,
    _check_dangerous_command,
    _check_deny_rules,
    _is_protected_path,
    _should_auto_approve,
)

# Workspace containment: relative paths resolve against the workspace root (a
# temp dir unless set), and writes may not escape it. Re-exported so the client's
# /workspace command can drive it.
from coding_model_server.tool_handlers.workspace import (  # noqa: E402
    REPO_ROOT,
    WORKSPACE_ENV,
    get_workspace,
    is_inside,
    is_temp_workspace,
    resolve_for_read,
    resolve_for_write,
    set_workspace,
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
from coding_model_server.tool_handlers.editing import (  # noqa: E402
    _SANITIZE_PATTERNS,
    _compact_summary,
    _create_checkpoint,
    _display_diff,
    _sanitize_generated_content,
    undo_last_checkpoint,
)


# Shell exec (parsing, whitelist, REMOTE_EXEC handler) lives in .shell.
from coding_model_server.tool_handlers.shell import (  # noqa: E402
    execute_remote_command,
    expand_paths_in_args,
    is_command_allowed,
    parse_command_safely,
)


# File handlers (read/write/edit/list/glob/grep) live in .files.
from coding_model_server.tool_handlers.files import (  # noqa: E402
    edit_file_content,
    glob_files,
    grep_search,
    list_directory,
    read_file_content,
    write_file_content,
)


# Marker parsing + dispatch (the top layer) lives in .dispatch.
from coding_model_server.tool_handlers.dispatch import (  # noqa: E402
    extract_fallback_commands,
    ingest_pdf_content,
    process_remote_commands,
)
