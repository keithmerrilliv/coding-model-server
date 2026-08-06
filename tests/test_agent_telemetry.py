"""Per-call telemetry on AGENT_RAN events (DEV-528).

Before this, an `agent_ran` payload carried a role, sometimes an agent, and
nothing about what the call cost. 188 of 413 recorded events named no agent
at all, so the only per-agent table the pipeline could build covered barely
half its own attempts — and with no duration or token figures there was no
second axis to fall back on.

Two distinct causes hid behind that one number, and they need different fixes:

  * 151 were real model calls whose event simply never read `meta["agent"]`
    back — the architect and the reviewer between them. Those now route their
    payload through `executor.agent_event_fields`.
  * 37 were anomaly and routing records that piggyback on AGENT_RAN without
    any model having run. Those legitimately have no agent, and are marked
    `model_call: False` so a cost query drops them instead of counting an
    attempt that never happened.

The structural guard at the bottom is the one that matters long-term: it fails
on a NEW call site that does neither, which is how this drifted in the first
place.
"""
import ast
from pathlib import Path
from unittest import mock

import pytest

import coding_model_autonomous.executor as ex


def _resp(content="ok", finish_reason="stop", usage=None):
    r = mock.Mock()
    r.raise_for_status.return_value = None
    body = {"choices": [{"message": {"content": content},
                         "finish_reason": finish_reason}]}
    if usage is not None:
        body["usage"] = usage
    r.json.return_value = body
    return r


def _call(meta, *, usage=None, finish_reason="stop", clock=None):
    """Run call_agent against a stubbed server, filling *meta*."""
    if clock is not None:
        # Deterministic elapsed time — a real sleep would be flaky and slow.
        ticks = iter(clock)
        with mock.patch.object(ex.time, "monotonic", lambda: next(ticks)), \
                mock.patch.object(ex, "post_chat_completion",
                                  return_value=_resp(usage=usage,
                                                     finish_reason=finish_reason)):
            return ex.call_agent("implementer", [{"role": "user", "content": "hi"}],
                                 meta=meta)
    with mock.patch.object(ex, "post_chat_completion",
                           return_value=_resp(usage=usage,
                                              finish_reason=finish_reason)):
        return ex.call_agent("implementer", [{"role": "user", "content": "hi"}],
                             meta=meta)


class TestCallAgentPopulatesMeta:
    def test_duration_is_measured_across_the_request(self):
        meta = {}
        _call(meta, clock=[100.0, 102.5])
        assert meta["duration_ms"] == 2500

    def test_agent_is_always_resolved(self):
        meta = {}
        _call(meta)
        assert meta["agent"]  # the resolved model, not the role

    def test_token_usage_is_recorded_when_reported(self):
        meta = {}
        _call(meta, usage={"prompt_tokens": 1200, "completion_tokens": 340,
                           "total_tokens": 1540})
        assert meta["prompt_tokens"] == 1200
        assert meta["completion_tokens"] == 340
        assert meta["total_tokens"] == 1540

    def test_absent_usage_records_no_token_keys(self):
        """A backend that reports nothing must not look like a free call."""
        meta = {}
        _call(meta, usage=None)
        assert "total_tokens" not in meta
        assert "prompt_tokens" not in meta

    def test_zeroed_usage_records_no_token_keys(self):
        """llama_server substitutes zeros when the backend omits usage, so an
        all-zero block means 'not reported' — recording 0 would assert a
        measurement that was never taken."""
        meta = {}
        _call(meta, usage={"prompt_tokens": 0, "completion_tokens": 0,
                           "total_tokens": 0})
        assert "total_tokens" not in meta

    def test_duration_recorded_even_when_truncated(self):
        meta = {}
        _call(meta, finish_reason="length", clock=[10.0, 11.0])
        assert meta["truncated"] is True
        assert meta["duration_ms"] == 1000


class TestAgentEventFields:
    def test_absent_keys_are_omitted_not_none(self):
        """An old event and one whose usage went unreported should read alike,
        and neither should be mistaken for a measured zero."""
        assert ex.agent_event_fields({"agent": "impl"}) == {"agent": "impl"}

    def test_empty_and_none_meta_are_safe(self):
        assert ex.agent_event_fields({}) == {}
        assert ex.agent_event_fields(None) == {}

    def test_full_meta_round_trips(self):
        out = ex.agent_event_fields({
            "agent": "deep_implementer", "duration_ms": 900,
            "prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
            "calls": 4, "finish_reason": "stop",
        })
        assert out == {"agent": "deep_implementer", "duration_ms": 900,
                       "prompt_tokens": 10, "completion_tokens": 20,
                       "total_tokens": 30, "calls": 4}
        assert "finish_reason" not in out  # not a telemetry axis

    def test_truncated_is_carried_only_when_true(self):
        assert ex.agent_event_fields({"truncated": True})["truncated"] is True
        assert "truncated" not in ex.agent_event_fields({"truncated": False})


class TestAccumulate:
    def test_sums_duration_and_tokens_across_calls(self):
        """Manifest mode makes one attempt out of many calls; 'what did this
        attempt cost' has to have one answer."""
        tally = {}
        ex.accumulate_agent_fields(tally, {"agent": "a", "duration_ms": 100,
                                           "total_tokens": 5})
        ex.accumulate_agent_fields(tally, {"agent": "a", "duration_ms": 250,
                                           "total_tokens": 7})
        assert tally["duration_ms"] == 350
        assert tally["total_tokens"] == 12
        assert tally["calls"] == 2
        assert tally["agent"] == "a"

    def test_counts_calls_so_the_per_call_mean_survives(self):
        tally = {}
        for _ in range(30):
            ex.accumulate_agent_fields(tally, {"duration_ms": 10})
        assert tally == {"duration_ms": 300, "calls": 30}

    def test_partial_meta_still_counts_the_call(self):
        """A call that reported no usage is still a call that happened."""
        tally = {}
        ex.accumulate_agent_fields(tally, {"duration_ms": 50})
        assert tally["calls"] == 1
        assert "total_tokens" not in tally

    def test_one_truncated_call_taints_the_attempt(self):
        tally = {}
        ex.accumulate_agent_fields(tally, {"duration_ms": 1})
        ex.accumulate_agent_fields(tally, {"duration_ms": 1, "truncated": True})
        ex.accumulate_agent_fields(tally, {"duration_ms": 1})
        assert tally["truncated"] is True

    def test_none_meta_is_a_noop(self):
        tally = {"calls": 3}
        assert ex.accumulate_agent_fields(tally, None) == {"calls": 3}

    def test_renders_through_agent_event_fields(self):
        """The tally deliberately reuses meta's key names."""
        tally = {}
        ex.accumulate_agent_fields(tally, {"agent": "x", "duration_ms": 5})
        assert ex.agent_event_fields(tally) == {"agent": "x", "duration_ms": 5,
                                                "calls": 1}


# ── Structural guard ─────────────────────────────────────────────────────────

_DAEMON = (Path(__file__).resolve().parents[1]
           / "src" / "coding_model_server" / "orchestrator_daemon.py")


def _agent_ran_payloads():
    """Every `record_event(EventKind.AGENT_RAN, ..., payload={...})` literal.

    Yields (lineno, ast.Dict). Call sites that build the payload some other
    way would be invisible here, so the guard also asserts the count it found
    against a floor — a refactor that hides them all must not pass silently.
    """
    tree = ast.parse(_DAEMON.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Attribute) and first.attr == "AGENT_RAN"):
            continue
        for kw in node.keywords:
            if kw.arg == "payload" and isinstance(kw.value, ast.Dict):
                yield node.lineno, kw.value


def _describes_a_model_call(payload: ast.Dict) -> bool:
    """True when the payload names its agent, directly or via the helper."""
    for key in payload.keys:
        if key is None:
            return True  # **executor.agent_event_fields(...)
        if isinstance(key, ast.Constant) and key.value == "agent":
            return True
    return False


def _marked_as_bookkeeping(payload: ast.Dict) -> bool:
    for key, val in zip(payload.keys, payload.values):
        if (isinstance(key, ast.Constant) and key.value == "model_call"
                and isinstance(val, ast.Constant) and val.value is False):
            return True
    return False


class TestEveryCallSiteIsAttributable:
    def test_the_guard_can_see_the_call_sites(self):
        """Guard against the guard: if a refactor moves these off dict
        literals this test starts passing vacuously, which is how DEV-502
        survived."""
        assert len(list(_agent_ran_payloads())) >= 18

    def test_every_agent_ran_payload_is_one_or_the_other(self):
        """Either it names the agent that ran, or it declares no model ran.

        A payload that does neither is exactly the 46%-unattributable defect
        this ticket exists to fix, and it is invisible until someone tries to
        build a per-agent table months later.
        """
        offenders = [
            lineno for lineno, payload in _agent_ran_payloads()
            if not _describes_a_model_call(payload)
            and not _marked_as_bookkeeping(payload)
        ]
        assert not offenders, (
            f"{_DAEMON.name} lines {offenders}: AGENT_RAN payload neither "
            "carries an agent (splat **executor.agent_event_fields(meta), or "
            "set 'agent') nor declares 'model_call': False"
        )

    @pytest.mark.parametrize("role", ["architect", "reviewer"])
    def test_the_two_biggest_offenders_now_use_the_helper(self, role):
        """Between them these were 146 of the 151 genuine omissions. Both
        already built a `meta` and passed it to call_agent — they just never
        read it back."""
        src = _DAEMON.read_text()
        marker = f'"role": "{role}",'
        idx = src.index(marker)
        window = src[idx:idx + 400]
        assert "agent_event_fields(meta)" in window, (
            f"the {role} AGENT_RAN payload stopped using the helper")
