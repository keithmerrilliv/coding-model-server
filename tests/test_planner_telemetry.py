"""DEV-734: the planner's telemetry — the one role that emitted none.

Before this, a plan that died on a 4,000-token budget and a plan the model
simply rushed produced the same record: `result_kind` and `rounds_provided`.
`--reasoning-format none` puts thinking IN-BAND against that budget, so
"is the planner weak" and "has the planner ever been allowed to finish" were
not separable — which is why DEV-735 could not be decided, and why a swap
could not have been evaluated even after the fact.
"""
from __future__ import annotations

import pytest
import requests

from coding_model_autonomous import planner
from coding_model_autonomous.executor import agent_event_fields

GOOD_YAML = (
    "<<<YAML>>>\n"
    'title: "x"\n'
    "goal: y\n"
    "<<<END>>>\n"
)
UNPARSEABLE = (
    "<<<YAML>>>\n"
    'title: "x\n'
    "  : : {{{{\n"
    "<<<END>>>\n"
)


class _Resp:
    """A server response with the parts telemetry reads."""

    def __init__(self, content, *, finish_reason="stop", usage=None):
        self._body = {
            "choices": [{"message": {"content": content},
                         "finish_reason": finish_reason}],
        }
        if usage is not None:
            self._body["usage"] = usage

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def _serve(monkeypatch, responses):
    """Serve *responses* in order, clamping to the last."""
    state = {"n": 0}

    def fake(*args, **kwargs):
        i = state["n"]
        state["n"] += 1
        return responses[min(i, len(responses) - 1)]

    monkeypatch.setattr(planner, "post_chat_completion", fake)
    return state


USAGE = {"prompt_tokens": 3000, "completion_tokens": 900, "total_tokens": 3900}


class TestOneCall:
    def test_meta_carries_what_the_call_cost(self, monkeypatch):
        _serve(monkeypatch, [_Resp(GOOD_YAML, usage=USAGE)])
        result, meta = planner._call_planner_once(
            "spec", agent="dense_architect", timeout=1)
        assert isinstance(result, planner.PlannerYaml)
        assert meta["agent"] == "dense_architect"
        assert meta["prompt_tokens"] == 3000
        assert meta["completion_tokens"] == 900
        assert meta["total_tokens"] == 3900
        assert meta["finish_reason"] == "stop"
        assert meta["truncated"] is False
        assert isinstance(meta["duration_ms"], int) and meta["duration_ms"] >= 0

    def test_a_truncated_plan_says_so(self, monkeypatch):
        # The case the ticket exists for: the budget ran out mid-plan.
        _serve(monkeypatch, [_Resp(UNPARSEABLE, finish_reason="length",
                                   usage=USAGE)])
        result, meta = planner._call_planner_once(
            "spec", agent="a", timeout=1, )
        assert isinstance(result, planner.PlannerError)
        assert meta["finish_reason"] == "length"
        assert meta["truncated"] is True

    def test_unreported_usage_is_absent_not_zero(self, monkeypatch):
        # A missing number and a measured zero must not read alike — a zero
        # total means the backend omitted usage, not that the call was free.
        _serve(monkeypatch, [_Resp(GOOD_YAML, usage={"total_tokens": 0})])
        _, meta = planner._call_planner_once("spec", agent="a", timeout=1)
        assert "total_tokens" not in meta
        assert "prompt_tokens" not in meta

    def test_meta_exists_even_when_the_body_is_malformed(self, monkeypatch):
        _serve(monkeypatch, [_Resp(GOOD_YAML, usage=USAGE)])
        monkeypatch.setattr(
            planner, "post_chat_completion",
            lambda *a, **k: type("R", (), {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"no": "choices"},
            })())
        result, meta = planner._call_planner_once("spec", agent="a", timeout=1)
        assert isinstance(result, planner.PlannerError)
        assert meta["agent"] == "a" and "duration_ms" in meta


class TestTally:
    def test_absent_tally_keeps_the_old_contract(self, monkeypatch):
        # Every existing caller passes no tally and must be unaffected.
        _serve(monkeypatch, [_Resp(GOOD_YAML, usage=USAGE)])
        result = planner.call_planner("spec")
        assert isinstance(result, planner.PlannerYaml)

    def test_one_call_one_attempt(self, monkeypatch):
        _serve(monkeypatch, [_Resp(GOOD_YAML, usage=USAGE)])
        tally: dict = {}
        planner.call_planner("spec", tally=tally)
        assert tally["calls"] == 1
        assert tally["total_tokens"] == 3900
        assert tally["finish_reason"] == "stop"
        assert tally["agent"] == planner.PLANNER_AGENT

    def test_a_re_roll_is_counted_and_summed(self, monkeypatch):
        # The re-roll spent the same GPU time as the call that worked; a cost
        # that omits it understates exactly the specs worth studying.
        _serve(monkeypatch, [_Resp(UNPARSEABLE, usage=USAGE),
                             _Resp(GOOD_YAML, usage=USAGE)])
        tally: dict = {}
        result = planner.call_planner("spec", tally=tally, parse_retries=1)
        assert isinstance(result, planner.PlannerYaml)
        assert tally["calls"] == 2
        assert tally["total_tokens"] == 7800

    def test_truncation_anywhere_taints_the_plan(self, monkeypatch):
        _serve(monkeypatch, [_Resp(UNPARSEABLE, finish_reason="length",
                                   usage=USAGE),
                             _Resp(GOOD_YAML, finish_reason="stop",
                                   usage=USAGE)])
        tally: dict = {}
        planner.call_planner("spec", tally=tally, parse_retries=1)
        assert tally["truncated"] is True
        # ...while finish_reason is the reason of the call that produced the
        # plan we actually kept.
        assert tally["finish_reason"] == "stop"

    def test_the_tally_survives_a_transport_failure(self, monkeypatch):
        # An out-parameter, so a plan that died after burning a generation
        # still records what it burned — previously the emptiest record of all.
        calls = {"n": 0}

        def fake(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _Resp(UNPARSEABLE, usage=USAGE)
            raise requests.ConnectionError("runner went away")

        monkeypatch.setattr(planner, "post_chat_completion", fake)
        tally: dict = {}
        with pytest.raises(requests.RequestException):
            planner.call_planner("spec", tally=tally, parse_retries=1)
        assert tally["calls"] == 1 and tally["total_tokens"] == 3900


class TestEventPayload:
    def test_the_tally_renders_with_every_other_role_s_spelling(self, monkeypatch):
        # The point of reusing agent_event_fields: a query that groups by
        # agent cannot work if one site writes `agent` and another `model`.
        _serve(monkeypatch, [_Resp(GOOD_YAML, finish_reason="length",
                                   usage=USAGE)])
        tally: dict = {}
        planner.call_planner("spec", tally=tally)
        fields = agent_event_fields(tally)
        assert fields["agent"] == planner.PLANNER_AGENT
        assert fields["total_tokens"] == 3900
        assert fields["finish_reason"] == "length"
        assert fields["truncated"] is True
        assert "duration_ms" in fields and "calls" in fields

    def test_an_empty_tally_adds_nothing(self):
        # The daemon spreads this into the payload unconditionally; a planner
        # that never reached the model must not invent a row of zeroes.
        assert agent_event_fields({}) == {}


class TestTruncationIsLoud:
    """Acceptance item 2: a truncated plan must never be silent, and a normal
    one must not cry wolf."""

    def test_a_truncated_plan_warns_and_names_the_knob(self, monkeypatch, caplog):
        _serve(monkeypatch, [_Resp(GOOD_YAML, finish_reason="length",
                                   usage=USAGE)])
        with caplog.at_level("WARNING", logger=planner.logger.name):
            planner.call_planner("spec", tally={})
        msgs = [r.getMessage() for r in caplog.records]
        assert any("TRUNCATED" in m for m in msgs), msgs
        # The message has to say what to do, not just that it happened.
        assert any("AUTONOMOUS_PLANNER_MAX_TOKENS" in m for m in msgs), msgs

    def test_a_normal_plan_does_not_warn(self, monkeypatch, caplog):
        _serve(monkeypatch, [_Resp(GOOD_YAML, finish_reason="stop",
                                   usage=USAGE)])
        with caplog.at_level("WARNING", logger=planner.logger.name):
            planner.call_planner("spec", tally={})
        assert not [r for r in caplog.records if "TRUNCATED" in r.getMessage()]

    def test_it_warns_even_with_no_tally(self, monkeypatch, caplog):
        # The warning is about the run, not about the telemetry; a caller that
        # asked for no tally still needs to be told.
        _serve(monkeypatch, [_Resp(GOOD_YAML, finish_reason="length",
                                   usage=USAGE)])
        with caplog.at_level("WARNING", logger=planner.logger.name):
            planner.call_planner("spec")
        assert any("TRUNCATED" in r.getMessage() for r in caplog.records)
