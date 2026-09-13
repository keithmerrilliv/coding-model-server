"""DEV-618 — the eval harness's opt-in tool loop.

The harness is single-turn by default and must stay byte-for-byte that way
(every prior eval was scored on it). With --tool-loop N an agentic model's
inspection markers are answered from an empty, read-only sandbox and the
model is asked to continue; the judged text is the final answer with its
markers stripped, and the answer records how many rounds it used.
"""
import importlib.util
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(_HERE, "..", "scripts", "eval_agents.py")


@pytest.fixture(scope="module")
def ea():
    spec = importlib.util.spec_from_file_location("eval_agents_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    def __init__(self, text, tokens=7):
        self._body = {"choices": [{"message": {"content": text}}],
                      "usage": {"completion_tokens": tokens}}

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def _server(replies):
    """A fake requests.post that answers from `replies` in order and records
    every request body it saw."""
    seen = []

    def post(url, headers=None, timeout=None, json=None):
        seen.append(json)
        return _Resp(replies[min(len(seen) - 1, len(replies) - 1)])
    post.seen = seen
    return post


class TestMarkers:
    def test_parses_the_dev616_inspection_pivot(self, ea):
        text = ("I'll start by checking the working directory.\n"
                "<<<LIST_DIR>>>.\n<<<GLOB>>>**/*.swift\n<<<PLAN>>>GOAL: inspect\nSTEPS:\n- [ ] look\n")
        assert ea.parse_markers(text) == [
            ("LIST_DIR", "."), ("GLOB", "**/*.swift"), ("PLAN", "GOAL: inspect\nSTEPS:\n- [ ] look")]

    def test_the_two_other_bracket_forms_the_server_accepts(self, ea):
        assert ea.parse_markers("<LIST_DIR>>>src") == [("LIST_DIR", "src")]      # after a <tool_call> strip
        assert ea.parse_markers("<READ_FILE>a.py") == [("READ_FILE", "a.py")]    # XML-style

    def test_malformed_brackets_are_not_markers(self, ea):
        assert ea.parse_markers("<<LIST_DIR>>> . and <<<GLOB> x") == []

    def test_strip_keeps_the_answer_around_a_confidence_line(self, ea):
        text = "The bug is the unclosed think block.\n<<<CONFIDENCE>>>90\nFix: close it on EOS."
        assert ea.strip_markers(text) == "The bug is the unclosed think block.\n\nFix: close it on EOS."

    def test_strip_removes_a_block_marker_to_the_next_marker(self, ea):
        text = "<<<PLAN>>>GOAL: x\n- [ ] a\n<<<LIST_DIR>>>.\nDone."
        assert ea.strip_markers(text) == "Done."


class TestSandbox:
    def test_is_empty_read_only_and_contained(self, ea, tmp_path):
        sb = ea.EvalSandbox(str(tmp_path))
        assert sb.run("LIST_DIR", ".") == "(empty directory)"
        assert sb.run("GLOB", "**/*.py") == "(no matches)"
        assert sb.run("READ_FILE", "Package.swift").startswith("(no such file")
        assert sb.run("READ_FILE", "../../etc/passwd").startswith("(refused")
        assert sb.run("LIST_DIR", "/etc").startswith("(refused")
        assert sb.run("REMOTE_EXEC", "ls -la").startswith("(not available")
        assert sb.run("WRITE_FILE", "x.py\nprint(1)").startswith("(not available")
        assert sb.run("PLAN", "GOAL: y") == "(noted)"
        assert list(tmp_path.iterdir()) == []

    def test_grep_and_read_see_only_what_is_inside(self, ea, tmp_path):
        (tmp_path / "a.txt").write_text("needle here\n")
        sb = ea.EvalSandbox(str(tmp_path))
        assert sb.run("GREP", "needle|.|i") == "a.txt:1: needle here"
        assert sb.run("READ_FILE", "a.txt") == "needle here\n"


class TestAskAgent:
    def test_default_is_exactly_one_completion_untouched(self, ea, monkeypatch):
        post = _server(["<<<LIST_DIR>>>.\nlooking"])
        monkeypatch.setattr(ea.requests, "post", post)
        a = ea.ask_agent("http://s", {}, "arch", "task", 100)
        assert len(post.seen) == 1
        assert a["text"] == "<<<LIST_DIR>>>.\nlooking"       # not stripped: single-turn baseline
        assert a["tool_rounds"] == 0 and a["tool_calls"] == [] and not a["tool_exhausted"]

    def test_markers_are_answered_and_the_final_answer_is_judged_clean(self, ea, monkeypatch, tmp_path):
        post = _server(["Let me look.\n<<<LIST_DIR>>>.\n<<<GLOB>>>**/*.swift",
                        "Nothing to inspect. The answer is 42."])
        monkeypatch.setattr(ea.requests, "post", post)
        a = ea.ask_agent("http://s", {}, "arch", "task", 100, tool_loop=3,
                         sandbox=ea.EvalSandbox(str(tmp_path)))
        assert len(post.seen) == 2
        follow_up = post.seen[1]["messages"]
        assert follow_up[-2] == {"role": "assistant", "content": "Let me look.\n<<<LIST_DIR>>>.\n<<<GLOB>>>**/*.swift"}
        assert follow_up[-1]["role"] == "user"
        assert "(empty directory)" in follow_up[-1]["content"]
        assert "(no matches)" in follow_up[-1]["content"]
        assert "round 1/3" in follow_up[-1]["content"]
        assert a["text"] == "Nothing to inspect. The answer is 42."
        assert a["tool_rounds"] == 1
        assert a["tool_calls"] == ["LIST_DIR .", "GLOB **/*.swift"]
        assert a["completion_tokens"] == 14
        assert not a["tool_exhausted"]

    def test_a_model_that_never_stops_inspecting_is_cut_at_n_and_flagged(self, ea, monkeypatch, tmp_path):
        post = _server(["<<<LIST_DIR>>>.\nstill looking"])   # every reply inspects again
        monkeypatch.setattr(ea.requests, "post", post)
        a = ea.ask_agent("http://s", {}, "arch", "task", 100, tool_loop=2,
                         sandbox=ea.EvalSandbox(str(tmp_path)))
        assert len(post.seen) == 3                 # first + 2 rounds
        assert a["tool_rounds"] == 2 and a["tool_exhausted"]
        assert a["text"] == "still looking"        # judged on what is visible, markers gone

    def test_a_clean_first_answer_costs_no_extra_call(self, ea, monkeypatch, tmp_path):
        post = _server(["Direct answer."])
        monkeypatch.setattr(ea.requests, "post", post)
        a = ea.ask_agent("http://s", {}, "arch", "task", 100, tool_loop=3,
                         sandbox=ea.EvalSandbox(str(tmp_path)))
        assert len(post.seen) == 1 and a["tool_rounds"] == 0 and a["text"] == "Direct answer."
