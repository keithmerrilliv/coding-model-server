"""Behavioural test net for tool_handlers, written BEFORE splitting the module.

tool_handlers.py has 100+ references to configure()-injected globals and no
prior tests, so this suite is the safety net that lets the upcoming module split
be verified mechanically. It covers:

  * the pure security/parse helpers (protected paths, dangerous/deny rules,
    permission gating, command parsing, marker normalization)
  * the marker dispatch (process_remote_commands / extract_fallback_commands)
  * the file handlers (read/write/edit/list/glob/grep) driven against tmp_path

configure() is wired with test doubles and yolo permission mode so file
operations auto-approve (no interactive prompt) without touching real paths.
"""
import os

import pytest

from qwen_server import tool_handlers as th


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    """Configure tool_handlers with test doubles + yolo auto-approve."""
    class _Cfg:
        CHUNK_THRESHOLD = 1_000_000   # high so handlers return inline, not chunked
        CHUNK_SIZE = 100_000
        CHUNK_OVERLAP = 200
        ALLOW_SHELL_MODE = True

    colors = {k: "" for k in (
        "BLUE", "GREEN", "RED", "FAIL", "WARNING", "CYAN", "YELLOW",
        "BOLD", "ENDC", "DIM", "MAGENTA",
    )}

    th.configure(
        config=_Cfg(),
        colors=colors,
        print_colored_fn=lambda *a, **k: None,
        logger_inst=__import__("logging").getLogger("test"),
        temp_tracker={"add": lambda p: None},
        external_handlers={},
        permission_mode="yolo",
        verbose_mode=False,
    )
    # yolo REMOTE_EXEC also needs the env opt-in; harmless for file ops.
    monkeypatch.setenv("ALLOW_REMOTE_EXEC_YOLO", "1")
    th.reset_write_counts()
    yield


# ── pure security helpers ─────────────────────────────────────────────────────

class TestProtectedPath:
    def test_env_file_protected(self):
        prot, _ = th._is_protected_path("~/.env")
        assert prot

    def test_ssh_dir_protected(self):
        prot, _ = th._is_protected_path("~/.ssh/id_rsa")
        assert prot

    def test_etc_protected(self):
        prot, _ = th._is_protected_path("/etc/passwd")
        assert prot

    def test_ordinary_path_not_protected(self, tmp_path):
        prot, _ = th._is_protected_path(str(tmp_path / "notes.txt"))
        assert not prot


class TestDangerousCommand:
    @pytest.mark.parametrize("cmd", [
        "rm -rf build/",     # the common form; regex must catch r anywhere
        "rm -fr build/",
        "rm -r build/",
        "rm --recursive build/",
        "sudo apt install x", "chmod 777 file",
        "git push origin main --force", "dd if=/dev/zero of=x",
    ])
    def test_flagged(self, cmd):
        flagged, _ = th._check_dangerous_command(cmd)
        assert flagged

    def test_benign_not_flagged(self):
        flagged, _ = th._check_dangerous_command("ls -la && echo hi")
        assert not flagged

    def test_non_recursive_rm_not_flagged(self):
        # A plain file delete shouldn't trip the recursive-delete warning.
        flagged, _ = th._check_dangerous_command("rm file.txt")
        assert not flagged


class TestDenyRules:
    @pytest.mark.parametrize("cmd", [
        "rm -rf /", "rm -rf / && echo done", ":(){ :|:& };:",
    ])
    def test_denied(self, cmd):
        denied, _ = th._check_deny_rules(cmd)
        assert denied

    def test_normal_delete_not_denied(self):
        denied, _ = th._check_deny_rules("rm -rf ./build")
        assert not denied


class TestShouldAutoApprove:
    def test_yolo_approves_edits(self):
        assert th._should_auto_approve("WRITE_FILE") is True

    def test_yolo_remote_exec_needs_env(self, monkeypatch):
        monkeypatch.setenv("ALLOW_REMOTE_EXEC_YOLO", "1")
        assert th._should_auto_approve("REMOTE_EXEC") is True
        monkeypatch.setenv("ALLOW_REMOTE_EXEC_YOLO", "")
        assert th._should_auto_approve("REMOTE_EXEC") is False

    def test_accept_edits_blocks_shell(self):
        th.set_permission_mode("acceptEdits")
        assert th._should_auto_approve("WRITE_FILE") is True
        assert th._should_auto_approve("REMOTE_EXEC") is False
        th.set_permission_mode("yolo")

    def test_default_prompts_everything(self):
        th.set_permission_mode("default")
        assert th._should_auto_approve("WRITE_FILE") is False
        th.set_permission_mode("yolo")


# ── file handlers (tmp_path) ──────────────────────────────────────────────────

class TestReadWrite:
    def test_write_then_read_roundtrip(self, tmp_path):
        f = tmp_path / "hello.txt"
        res = th.write_file_content(f"{f}\nhello world")
        assert "error" not in res.lower()
        assert f.read_text() == "hello world"
        out = th.read_file_content(str(f))
        assert "hello world" in out

    def test_read_missing_file(self, tmp_path):
        out = th.read_file_content(str(tmp_path / "nope.txt"))
        assert "not found" in out.lower()

    def test_write_loop_detection(self, tmp_path):
        f = tmp_path / "loop.txt"
        # MAX_WRITES_PER_FILE writes are allowed; beyond that it refuses.
        for _ in range(th.state.MAX_WRITES_PER_FILE):
            th.write_file_content(f"{f}\nx")
        blocked = th.write_file_content(f"{f}\nx")
        # Refusal returns None (so the orchestrator stops), per the handler.
        assert blocked is None


class TestEdit:
    def test_edit_replaces_text(self, tmp_path):
        f = tmp_path / "e.py"
        f.write_text("a = 1\nb = 2\n")
        res = th.edit_file_content(f"{f}\n<<<OLD>>>\nb = 2\n<<<NEW>>>\nb = 3\n")
        assert "error" not in res.lower()
        assert "b = 3" in f.read_text()
        assert "b = 2" not in f.read_text()


class TestListAndGlob:
    def test_list_directory(self, tmp_path):
        (tmp_path / "a.txt").write_text("x")
        (tmp_path / "sub").mkdir()
        out = th.list_directory(str(tmp_path))
        assert "a.txt" in out
        assert "sub/" in out

    def test_glob_finds_by_extension(self, tmp_path):
        (tmp_path / "one.py").write_text("x")
        (tmp_path / "two.py").write_text("y")
        (tmp_path / "skip.md").write_text("z")
        out = th.glob_files(f"{tmp_path}/*.py")
        assert "one.py" in out and "two.py" in out
        assert "skip.md" not in out


class TestGrep:
    def test_grep_finds_match(self, tmp_path):
        (tmp_path / "code.py").write_text("def foo():\n    return BANANA\n")
        out = th.grep_search(f"BANANA|{tmp_path}")
        assert "BANANA" in out
        assert "code.py" in out


# ── marker parsing / dispatch ─────────────────────────────────────────────────

class TestMarkerDispatch:
    def test_write_marker_executes(self, tmp_path):
        f = tmp_path / "marked.txt"
        resp = f"Sure, writing it.\n<<<WRITE_FILE>>>{f}\nfrom a marker"
        result = th.process_remote_commands(resp)
        assert result is not None          # a tool ran
        assert f.read_text() == "from a marker"

    def test_no_marker_returns_none(self):
        assert th.process_remote_commands("just a normal reply, no tools") is None

    def test_fallback_extracts_bash_block(self):
        resp = "Run this:\n```bash\nls -la\n```\n"
        cmds = th.extract_fallback_commands(resp)
        assert any("ls -la" in c for c in cmds)

    def test_fallback_ignores_non_shell_block(self):
        resp = "Here:\n```python\nprint('hi')\n```\n"
        cmds = th.extract_fallback_commands(resp)
        assert cmds == []
