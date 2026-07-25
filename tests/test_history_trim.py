"""DEV-135 — the context-limit retry must not mutate stored history.

get_completion's docstring promises "the actual history list is never
modified", but _trim_history_for_context did `history[:] = ...`: a
context-limit retry permanently dropped ~25% of the session (then saved to
disk), and a trim firing mid-continuation broke the orchestrator's
`del history[-(continuation_count*2):]` arithmetic. The retry now gets a
sanitized copy; deliberate shrinking goes through trim_history_in_place.
"""
from coding_model_client.completion import (
    _trim_history_for_context,
    trim_history_in_place,
)


def _history(n_turns=8):
    h = [{"role": "system", "content": "you are the implementer"}]
    for i in range(n_turns):
        h.append({"role": "user", "content": f"user turn {i}"})
        h.append({"role": "assistant", "content": f"assistant turn {i}"})
    return h


def test_retry_copy_does_not_mutate_the_original():
    h = _history()
    snapshot = [dict(m) for m in h]

    trimmed = _trim_history_for_context(h)

    assert h == snapshot, "stored history must be untouched by the retry path"
    assert trimmed is not None
    assert len(trimmed) < len(h), "the retry payload really is trimmed"


def test_retry_copy_preserves_system_prompt_and_last_message():
    h = _history()
    trimmed = _trim_history_for_context(h)
    assert trimmed[0]["role"] == "system"
    assert trimmed[-1]["content"] == h[-1]["content"]


def test_in_place_trim_is_explicit_and_keeps_original_dicts():
    h = _history()
    # Give one surviving message a non-standard field that the sanitized
    # retry copy would drop — the in-place trim must keep it.
    h[-2]["tool_calls"] = [{"id": "t1"}]
    before = len(h)

    trim_history_in_place(h)

    assert len(h) < before
    assert h[0]["role"] == "system"
    surviving = [m for m in h if m.get("tool_calls")]
    assert surviving, "in-place trim keeps original message dicts intact"


def test_short_history_untouched():
    h = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    assert _trim_history_for_context(h) is None
    trim_history_in_place(h)
    assert len(h) == 2
