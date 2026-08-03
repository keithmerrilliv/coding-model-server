"""The <<<CONTINUE>>> handler, driven through the real orchestrator loop (DEV-80).

test_continue_signal.py covers the parser. This covers the thing that was
actually broken: the loop only continued on a HARD cut-off (finish_reason
"length"), so a model that took the guidance's offer — stop early, emit
<<<CONTINUE>>> + REMAINING, be asked for the rest — was never asked for the rest.
Its leftovers were stripped and dropped.

These tests script get_completion and assert the orchestrator issues the
follow-up turn, feeds the model's own REMAINING list back to it, and stitches
the halves into one clean answer.
"""
import pytest

from coding_model_client import orchestrator
from coding_model_client.orchestrator import process_agent_tasks, set_tool_functions


@pytest.fixture(autouse=True)
def _restore_tool_functions():
    # set_tool_functions swaps module globals in orchestrator with no teardown;
    # without this, the last-installed stubs leak into every later-collected
    # module and pass/fail depends on execution order (DEV-384).
    prev = (orchestrator._process_remote_commands,
            orchestrator._extract_fallback_commands,
            orchestrator._execute_remote_command)
    yield
    set_tool_functions(*prev)


@pytest.fixture
def scripted(monkeypatch):
    """Drive the loop with a canned list of (text, finish_reason) replies.

    Returns a recorder holding every history snapshot get_completion was called
    with, so a test can assert what the model was actually asked.
    """
    class _Recorder:
        def __init__(self):
            self.replies = []
            self.prompts = []       # history (deep-ish copy) per call
            self.final_history = None

        def install(self, replies):
            self.replies = list(replies)

            def fake_get_completion(history, model, agent_theme, **kwargs):
                self.prompts.append([dict(m) for m in history])
                if not self.replies:
                    # Nothing scripted left: end the task cleanly.
                    return "The task is complete.", "stop", None
                text, finish = self.replies.pop(0)
                return text, finish, None

            monkeypatch.setattr(orchestrator, "get_completion", fake_get_completion)

    rec = _Recorder()

    # AGENT_THEMES is populated at runtime from the server's agent list, so it's
    # empty under test — give the loop the one agent these tests drive.
    monkeypatch.setattr(
        orchestrator, "AGENT_THEMES",
        {"implementer": {"icon": "*", "color": "", "name": "implementer"}},
    )
    # Silence/no-op the side effects the loop reaches for.
    monkeypatch.setattr(orchestrator, "save_chat_history", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "set_terminal_title", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "send_macos_notification", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "reset_write_counts", lambda *a, **k: None)
    # No tools in play: every response is plain prose.
    set_tool_functions(lambda _resp: "", lambda _resp: [], lambda _cmd: "")

    return rec


def _run(replies, scripted):
    scripted.install(replies)
    history = []
    process_agent_tasks(
        [("implementer", "Build the thing.")], history, "implementer", None
    )
    return history


def _assistant_texts(history):
    return [m["content"] for m in history if m["role"] == "assistant"]


def test_voluntary_continue_triggers_a_follow_up_turn(scripted):
    """The bug, end to end: finish_reason is 'stop', not 'length'. Pre-fix the
    loop ignored it, the marker was stripped, and 'part two' was never written."""
    history = _run([
        ("Wrote part one.\n<<<CONTINUE>>>\nREMAINING: write part two", "stop"),
        ("Wrote part two. The task is complete.", "stop"),
    ], scripted)

    assert len(scripted.prompts) == 2, "the model was never asked to continue"

    # The follow-up turn must carry the model's OWN remaining list back to it.
    nudge = scripted.prompts[1][-1]
    assert nudge["role"] == "user"
    assert "write part two" in nudge["content"]
    assert "<<<CONTINUE>>>" in nudge["content"]

    # The two halves are stitched into ONE assistant message, marker removed.
    final = _assistant_texts(history)[-1]
    assert "Wrote part one." in final
    assert "Wrote part two." in final
    assert "<<<CONTINUE>>>" not in final
    assert "REMAINING" not in final


def test_no_signal_means_no_continuation(scripted):
    """Control: a normal finished answer must not cost an extra inference call."""
    _run([("All done. The task is complete.", "stop")], scripted)
    assert len(scripted.prompts) == 1


def test_hard_truncation_still_continues(scripted):
    """The pre-existing behaviour must survive: finish_reason 'length' continues,
    and its halves are spliced without an inserted newline (cut mid-token)."""
    history = _run([
        ("The answer is fourty-", "length"),
        ("two. The task is complete.", "stop"),
    ], scripted)

    assert len(scripted.prompts) == 2
    nudge = scripted.prompts[1][-1]
    assert "cut off" in nudge["content"]
    assert "fourty-two." in _assistant_texts(history)[-1], "halves were not spliced cleanly"


def test_continuation_is_bounded(scripted):
    """A model that signals CONTINUE forever must not loop forever — the existing
    max_continuations cap (5) applies to the voluntary path too."""
    forever = [("more...\n<<<CONTINUE>>>", "stop")] * 12
    _run(forever, scripted)
    # 1 initial + at most 5 continuations before the loop gives up on continuing.
    assert len(scripted.prompts) <= 6, "voluntary continuation is not bounded"


def test_signal_without_remaining_list_still_continues(scripted):
    """'' is not None: the model signalled but didn't say what's left. Continue."""
    history = _run([
        ("Half the work.\n<<<CONTINUE>>>", "stop"),
        ("The rest. The task is complete.", "stop"),
    ], scripted)

    assert len(scripted.prompts) == 2
    final = _assistant_texts(history)[-1]
    assert "Half the work." in final and "The rest." in final
    assert "<<<CONTINUE>>>" not in final
