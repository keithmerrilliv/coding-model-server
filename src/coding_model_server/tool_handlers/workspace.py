"""Workspace containment: where the agent's file tools are allowed to write.

The file tools used to resolve relative paths against the *process* CWD.
bin/start-client.sh cds to the repo root, so a model emitting a bare filename
wrote straight into the checkout — that is how a stray `path` file (whose
payload's first line was the literal word "path") landed at the repo root.

Two rules now hold, in every permission mode including yolo:

  1. Relative paths resolve against the workspace root, not the CWD.
  2. Writes must land inside the workspace root. Anything outside is refused.

The workspace defaults to a fresh temp directory, so an unconfigured session
cannot touch a real project at all. Reads are deliberately *not* confined: the
agent still needs to read source outside the workspace for context, and reading
is already gated separately by the protected-path check.
"""
import os
import tempfile

from coding_model_server.tool_state import state

# The coding-model-server checkout itself, derived from this file's location
# (tool_handlers/ -> coding_model_server/ -> src/ -> repo root).
#
# Permanently unwritable by the agent's file tools, even if explicitly set as
# the workspace: the client runs *from* this directory, which is exactly what
# made stray writes land here. Develop this repo with a real editor.
REPO_ROOT = os.path.realpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)

WORKSPACE_ENV = "CODING_MODEL_WORKSPACE"


def _real(path):
    return os.path.realpath(os.path.expanduser(path))


def is_inside(path, root):
    """True if `path` is `root` or lives under it, after symlink resolution.

    realpath (not normpath) so a symlink planted inside the workspace that
    points out of it — the obvious way to defeat a containment check — resolves
    to its target and fails the test.
    """
    path_r, root_r = _real(path), _real(root)
    return path_r == root_r or path_r.startswith(root_r + os.sep)


def get_workspace():
    """The workspace root. Creates a temp dir on first use if none is set."""
    if getattr(state, "workspace_root", None):
        return state.workspace_root

    configured = (os.getenv(WORKSPACE_ENV) or "").strip()
    if configured:
        ok, err = set_workspace(configured)
        if ok:
            return state.workspace_root
        # A bad value must not silently fall back to the CWD — that is the repo.
        if state.logger:
            state.logger.warning("%s ignored (%s); using a temp workspace", WORKSPACE_ENV, err)

    state.workspace_root = tempfile.mkdtemp(prefix="coding-model-work-")
    if state.logger:
        state.logger.info("workspace defaulted to temp dir: %s", state.workspace_root)
    return state.workspace_root


def is_temp_workspace():
    """True if the workspace is an auto-created throwaway rather than a chosen dir."""
    root = get_workspace()
    return is_inside(root, tempfile.gettempdir()) and os.path.basename(root).startswith(
        "coding-model-work-"
    )


def set_workspace(path):
    """Point the session's workspace at `path`. Returns (ok, message)."""
    if not path or not path.strip():
        return False, "no path given"

    target = _real(path)
    if not os.path.isdir(target):
        return False, f"not a directory: {target}"
    if is_inside(target, REPO_ROOT):
        return False, (
            f"{target} is inside the coding-model-server checkout ({REPO_ROOT}), "
            "which is permanently protected"
        )

    state.workspace_root = target
    return True, target


def resolve_for_write(path):
    """Resolve a tool-supplied path for writing. Returns (abs_path, error).

    Relative paths join the workspace; the result must stay inside it. Callers
    return `error` to the model verbatim when it is non-None.
    """
    if not path or not path.strip():
        return None, "Error: No file path provided"

    root = get_workspace()
    expanded = os.path.expanduser(path.strip())
    candidate = expanded if os.path.isabs(expanded) else os.path.join(root, expanded)

    if is_inside(candidate, REPO_ROOT):
        return None, (
            f"Refused: '{path}' resolves inside the coding-model-server checkout "
            f"({REPO_ROOT}), which is permanently protected. Write to the workspace "
            f"instead: {root}"
        )
    if not is_inside(candidate, root):
        return None, (
            f"Refused: '{path}' resolves to {_real(candidate)}, which is outside the "
            f"workspace root ({root}). Relative paths resolve against the workspace. "
            "Ask the user to run /workspace <dir> to point this session elsewhere."
        )
    return os.path.normpath(candidate), None


def resolve_for_read(path):
    """Resolve a path for reading: relative joins the workspace, no containment."""
    expanded = os.path.expanduser((path or "").strip())
    if not expanded:
        return get_workspace()
    if os.path.isabs(expanded):
        return expanded
    return os.path.normpath(os.path.join(get_workspace(), expanded))
