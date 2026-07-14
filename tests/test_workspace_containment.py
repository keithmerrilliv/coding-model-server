"""Workspace containment for the agent's file tools.

Regression: the client's CWD is the repo checkout (bin/start-client.sh cds
there), and the file tools resolved relative paths against the CWD. A model
whose WRITE_FILE payload began with the literal word "path" therefore created
a file named `path` at the repo root. Writes are now confined to a workspace
that defaults to a temp dir.

Every test here runs in yolo — the mode that auto-approves every prompt — because
containment must be a hard refusal, not a permission gate.
"""
import logging
import os

import pytest

from coding_model_server import tool_handlers as th
from coding_model_server.tool_handlers import workspace as ws
from coding_model_server.tool_state import state

COLORS = {k: '' for k in ('BOLD', 'CYAN', 'ENDC', 'FAIL', 'GREEN', 'WARNING')}


class _Config:
    ALLOW_SHELL_MODE = True
    CHUNK_THRESHOLD = 10 ** 9
    COMMAND_TIMEOUT = 10
    PERMISSION_MODE = 'yolo'


@pytest.fixture
def workspace(tmp_path):
    """A configured tool-handler stack whose workspace is an empty tmp dir."""
    th.configure(
        _Config(), COLORS, lambda *a, **k: None, logging.getLogger('test'),
        {'add': lambda p: None}, {}, permission_mode='yolo',
    )
    th.set_permission_mode('yolo')
    state.write_counts.clear()

    root = tmp_path / "ws"
    root.mkdir()
    ok, _ = ws.set_workspace(str(root))
    assert ok
    yield os.path.realpath(str(root))
    state.workspace_root = None


def test_bare_relative_path_lands_in_workspace_not_cwd(workspace):
    """The original bug: a bare filename must not follow the process CWD."""
    result = th.write_file_content("path\n// swift-tools-version:5.9\n")

    assert "Successfully wrote" in result
    assert os.path.exists(os.path.join(workspace, "path"))
    assert not os.path.exists(os.path.join(ws.REPO_ROOT, "path"))


def test_write_into_repo_is_refused(workspace):
    target = os.path.join(ws.REPO_ROOT, "evil.txt")

    result = th.write_file_content(f"{target}\npwned\n")

    assert result.startswith("Refused:")
    assert not os.path.exists(target)


def test_traversal_out_of_workspace_is_refused(workspace):
    result = th.write_file_content("../../escape.txt\npwned\n")

    assert result.startswith("Refused:")
    assert not os.path.exists(os.path.join(os.path.dirname(workspace), "escape.txt"))


def test_symlink_out_of_workspace_is_refused(workspace, tmp_path):
    """A symlink planted inside the workspace must not become an exit."""
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(str(outside), os.path.join(workspace, "sneaky"))

    result = th.write_file_content("sneaky/pwned.txt\npwned\n")

    assert result.startswith("Refused:")
    assert not os.path.exists(str(outside / "pwned.txt"))


def test_edit_of_repo_file_is_refused(workspace):
    """EDIT_FILE is a write and gets the same gate as WRITE_FILE."""
    readme = os.path.join(ws.REPO_ROOT, "README.md")
    before = open(readme, encoding='utf-8').read()

    result = th.edit_file_content(f"{readme}\n<<<OLD>>>\n#\n<<<NEW>>>\nPWNED\n")

    assert result.startswith("Refused:")
    assert open(readme, encoding='utf-8').read() == before


def test_nested_write_creates_parents(workspace):
    """A fresh workspace is empty, so nested paths must still work."""
    result = th.write_file_content("src/deep/mod.py\nx = 1\n")

    assert "Successfully wrote" in result
    assert os.path.exists(os.path.join(workspace, "src", "deep", "mod.py"))


def test_repo_cannot_be_chosen_as_workspace():
    """The checkout is permanently protected, even if explicitly requested."""
    ok, msg = ws.set_workspace(ws.REPO_ROOT)

    assert not ok
    assert "permanently protected" in msg


def test_default_workspace_is_a_temp_dir(monkeypatch):
    """With nothing configured, the workspace must not be the CWD."""
    monkeypatch.delenv(ws.WORKSPACE_ENV, raising=False)
    state.workspace_root = None
    try:
        root = ws.get_workspace()

        assert ws.is_temp_workspace()
        assert not ws.is_inside(root, ws.REPO_ROOT)
        assert not ws.is_inside(os.getcwd(), root)
    finally:
        state.workspace_root = None


def test_bad_workspace_env_does_not_fall_back_to_cwd(monkeypatch):
    """A bogus CODING_MODEL_WORKSPACE must degrade to a temp dir, not the repo."""
    monkeypatch.setenv(ws.WORKSPACE_ENV, "/nonexistent/nope")
    state.workspace_root = None
    th.configure(
        _Config(), COLORS, lambda *a, **k: None, logging.getLogger('test'),
        {'add': lambda p: None}, {}, permission_mode='yolo',
    )
    try:
        root = ws.get_workspace()

        assert ws.is_temp_workspace()
        assert not ws.is_inside(root, ws.REPO_ROOT)
    finally:
        state.workspace_root = None


def test_reads_outside_the_workspace_still_work(workspace):
    """Containment is write-only; the agent still needs to read source elsewhere."""
    resolved = ws.resolve_for_read(os.path.join(ws.REPO_ROOT, "README.md"))

    assert resolved == os.path.join(ws.REPO_ROOT, "README.md")
    assert "File:" in th.read_file_content(resolved)
