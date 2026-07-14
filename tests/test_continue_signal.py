"""Tests for the <<<CONTINUE>>> continuation signal (DEV-80).

Config.TOKEN_BUDGET_GUIDANCE §3 tells the model it may stop early on an
oversized task by ending with <<<CONTINUE>>> + a REMAINING list, and promises
the client will ask for the rest. Nothing implemented that: the client only
continued on a HARD cut-off (finish_reason "length"). A voluntary stop came back
as finish_reason "stop", fell through the continuation loop, and the marker was
stripped as degenerate output — the REMAINING items silently dropped.

_split_continue_signal is the parser that makes the promise true; the
orchestrator now feeds its result into the same continuation loop that already
handled cut-offs.
"""
import pytest

from coding_model_client.orchestrator import _split_continue_signal
from coding_model_server.config import Config


# ── no signal → untouched ────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Here is the finished answer.",
    "",
    "Done. All files written.",
])
def test_plain_response_is_not_a_continuation(text):
    out, remaining = _split_continue_signal(text)
    assert out == text
    assert remaining is None, "a normal answer must not trigger a continuation turn"


def test_marker_mid_response_does_not_trigger():
    """Only a TRAILING signal counts. The model quoting the protocol mid-answer
    (or explaining it) must not send us into a continuation loop."""
    text = "If I run out of room I would end with <<<CONTINUE>>>, but I fit. All done."
    out, remaining = _split_continue_signal(text)
    assert out == text
    assert remaining is None


# ── signal → split off, REMAINING captured ───────────────────────────────────

def test_signal_is_stripped_and_remaining_captured():
    text = (
        "Wrote src/a.py and src/b.py.\n"
        "<<<CONTINUE>>>\n"
        "REMAINING: src/c.py, and wire up the CLI"
    )
    out, remaining = _split_continue_signal(text)
    assert out == "Wrote src/a.py and src/b.py."
    assert remaining == "src/c.py, and wire up the CLI"
    assert "<<<CONTINUE>>>" not in out, "the marker must never reach the final answer"


def test_signal_without_a_remaining_list_still_continues():
    """Signalled, but didn't say what's left: continue anyway. '' is not None —
    that distinction is what tells the loop to fire."""
    out, remaining = _split_continue_signal("Partial work here.\n<<<CONTINUE>>>")
    assert out == "Partial work here."
    assert remaining == ""
    assert remaining is not None


@pytest.mark.parametrize("marker", [
    "<<<CONTINUE>>>",
    "<continue>",
    "<<CONTINUE>>",
    "**<<<CONTINUE>>>**",
    "<<<continue>>>",
    "<<<CONTINUE>>>:",
])
def test_sloppy_marker_variants_are_recognised(marker):
    """Models emit the marker imperfectly. Miss the variant and the work is lost,
    so accept the realistic forms rather than only the canonical one."""
    out, remaining = _split_continue_signal(f"Some work.\n{marker}")
    assert remaining is not None, f"{marker!r} was not recognised as a continuation"
    assert "CONTINUE" not in out.upper()
    assert out == "Some work."


def test_multiline_remaining_list_is_captured_whole():
    text = (
        "Did the models.\n"
        "<<<CONTINUE>>>\n"
        "REMAINING:\n"
        "- the views\n"
        "- the tests"
    )
    out, remaining = _split_continue_signal(text)
    assert out == "Did the models."
    assert "- the views" in remaining
    assert "- the tests" in remaining


# ── the prompt contract the handler now honours ──────────────────────────────

def test_guidance_still_documents_the_protocol_the_client_implements():
    """The interactive guidance promises continuation; the client must implement
    the marker it names. If someone renames the marker in the prompt, this fails."""
    guidance = Config.TOKEN_BUDGET_GUIDANCE.format(available_tokens=4096)
    assert "<<<CONTINUE>>>" in guidance
    _, remaining = _split_continue_signal("work\n<<<CONTINUE>>>\nREMAINING: rest")
    assert remaining == "rest"


def test_guidance_does_not_recommend_shell_assembly_the_rules_forbid():
    """§4 used to tell the model to assemble large files with temp files + `cat`,
    while the tool rules in the SAME system prompt say never to modify files via
    shell (no diff preview, breaks checkpoint/undo). Contradictory instructions in
    one prompt — resolved in favour of the rules."""
    guidance = Config.TOKEN_BUDGET_GUIDANCE.format(available_tokens=4096)
    assert "cat" not in guidance.split()
    assert "shell tools" not in guidance
    assert "temporary files" not in guidance
    # ...and it points at the tools that actually exist.
    assert "<<<EDIT_FILE>>>" in guidance
    assert "<<<WRITE_FILE>>>" in guidance


def test_core_guidance_has_no_continuation_protocol():
    """DEV-79 invariant, restated here: the single-shot variant must NOT promise
    continuation — there is no continuation turn on that path."""
    core = Config.TOKEN_BUDGET_GUIDANCE_CORE.format(available_tokens=4096)
    assert "<<<CONTINUE>>>" not in core
