"""DEV-131 / DEV-136 — marker parsing must not execute quoted content.

DEV-131: each command block used to run from its opening tag to the next
known opening tag anywhere in the text. A WRITE_FILE whose content contained
any of the 13 markers (docs quoting <<<REMOTE_EXEC>>>, tool references, test
fixtures) was truncated at the inner marker and the tail DISPATCHED — a
corrupt file plus execution of quoted examples. The scanner now masks fenced
code blocks and explicit <<<END_FILE>>>-terminated payloads before matching.

DEV-136: the no-marker fallback extracted and ran every ```bash fence, so an
explanatory answer ("to build you'd run: ...") had its illustrative commands
executed. Extraction now requires the response to be essentially just the
fence; the client additionally confirms before running what it returns.
"""
import pytest

from coding_model_server import tool_handlers as th
from coding_model_server.tool_state import state


@pytest.fixture(autouse=True)
def configured(monkeypatch, tmp_path):
    class _Cfg:
        CHUNK_THRESHOLD = 1_000_000
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
    monkeypatch.setenv("ALLOW_REMOTE_EXEC_YOLO", "1")
    th.reset_write_counts()
    ok, _ = th.set_workspace(str(tmp_path))
    assert ok
    yield
    state.workspace_root = None


class TestInnerMarkerSafety:
    def test_fenced_marker_inside_write_is_content_not_command(self, tmp_path):
        # Docs that QUOTE a marker inside a fence: the whole fence must land
        # in the file, and nothing after the quoted marker may execute.
        content = (
            "# Tool docs\n"
            "```\n"
            "<<<REMOTE_EXEC>>>ls -la\n"
            "```\n"
            "That is how you run a command.\n"
        )
        resp = f"<<<WRITE_FILE>>>docs.md\n{content}"
        out = th.process_remote_commands(resp)

        written = (tmp_path / "docs.md").read_text()
        assert "<<<REMOTE_EXEC>>>ls -la" in written, "quoted marker must be file content"
        assert "That is how you run a command." in written
        assert out is not None and out.count("[Command") == 1, (
            "exactly one command (the write) — the quoted exec must not run"
        )

    def test_end_file_terminator_protects_unfenced_markers(self, tmp_path):
        # Unfenced live markers in content, protected by the explicit
        # terminator. Nothing between the opener and END_FILE may dispatch,
        # and the file must not be truncated at the inner marker. (The
        # sanitize boundary still strips the UNFENCED marker token itself —
        # a live marker loose in file content is never intentional; quote
        # inside ``` fences to keep markers verbatim.)
        resp = (
            "<<<WRITE_FILE>>>ref.txt\n"
            "line one\n"
            "<<<REMOTE_EXEC>>>echo pwned\n"
            "line three\n"
            "<<<END_FILE>>>\n"
        )
        out = th.process_remote_commands(resp)

        written = (tmp_path / "ref.txt").read_text()
        assert "line one" in written
        assert "echo pwned" in written, "payload must not be truncated"
        assert "line three" in written, "payload must not be truncated"
        assert "END_FILE" not in written, "terminator is protocol, not content"
        assert out.count("[Command") == 1, "the quoted exec must never dispatch"

    def test_commands_after_end_file_still_run(self, tmp_path):
        (tmp_path / "other.txt").write_text("hello")
        resp = (
            "<<<WRITE_FILE>>>a.txt\n"
            "content with <<<GLOB>>>*.md inside\n"
            "<<<END_FILE>>>\n"
            f"<<<READ_FILE>>>{tmp_path}/other.txt"
        )
        out = th.process_remote_commands(resp)
        assert (tmp_path / "a.txt").read_text().strip().endswith("inside")
        assert "hello" in out, "the real follow-up command still executes"
        assert out.count("[Command") == 2

    def test_sequential_unfenced_commands_still_split(self, tmp_path):
        # The pre-existing contract: two real commands in one response.
        (tmp_path / "x.txt").write_text("XX")
        resp = (
            f"<<<WRITE_FILE>>>y.txt\nY content\n"
            f"<<<READ_FILE>>>{tmp_path}/x.txt"
        )
        out = th.process_remote_commands(resp)
        assert (tmp_path / "y.txt").read_text().strip() == "Y content"
        assert "XX" in out
        assert out.count("[Command") == 2

    def test_quoted_end_file_inside_fence_does_not_truncate(self, tmp_path):
        # A fenced, QUOTED terminator must not end the payload early; the
        # real terminator after it does.
        resp = (
            "<<<WRITE_FILE>>>t.md\n"
            "```\n<<<END_FILE>>>\n```\n"
            "after the quoted terminator\n"
            "<<<END_FILE>>>\n"
        )
        th.process_remote_commands(resp)
        written = (tmp_path / "t.md").read_text()
        assert "after the quoted terminator" in written
        assert written.count("END_FILE") == 1, "only the quoted copy remains"


class TestFallbackExtractionGate:
    def test_explanatory_answer_yields_no_commands(self):
        resp = (
            "To build this project from a clean tree you would normally run "
            "the full distclean cycle, which removes every generated artifact "
            "including the configure cache, and then rebuild from scratch. "
            "On this codebase that looks like the following, though you "
            "rarely need it in day-to-day work:\n"
            "```bash\nmake distclean && make -j\n```\n"
        )
        assert th.extract_fallback_commands(resp) == []

    def test_bare_action_response_is_extracted(self):
        resp = "Running the tests:\n```bash\npytest -q\n```\n"
        assert th.extract_fallback_commands(resp) == ["pytest -q"]

    def test_non_shell_fences_still_ignored(self):
        resp = "```python\nprint('hi')\n```\n"
        assert th.extract_fallback_commands(resp) == []
