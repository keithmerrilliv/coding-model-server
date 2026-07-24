"""Parsing and budget tests for the supervisor decision layer (DEV-129).

supervisor.decide() drives every failure-recovery transition when
SUPERVISOR_ENABLED; a mis-parsed tool call must surface as SupervisorError
(the daemon's cue to fall back to the legacy deterministic path), never as
a silently wrong decision. All network I/O is mocked.
"""
import json
from unittest import mock

import pytest

import coding_model_autonomous.supervisor as sup


def _resp(arguments, *, name="decide", tool_calls=...):
    """A minimal chat-completion payload with one forced tool call."""
    if tool_calls is ...:
        tool_calls = [{"function": {"name": name, "arguments": arguments}}]
    return {"choices": [{"message": {"content": "", "tool_calls": tool_calls}}]}


def _args(**kw):
    return json.dumps(kw)


class TestParseDecision:
    def test_valid_advance(self):
        decision = sup._parse_decision(
            _resp(_args(action="advance", reason="tests pass")))
        assert decision.action == "advance"
        assert decision.target_role is None
        assert decision.reason == "tests pass"

    def test_valid_retry_carries_role_and_feedback(self):
        decision = sup._parse_decision(_resp(_args(
            action="retry", target_role="implementer",
            feedback_to_inject="fix the off-by-one", reason="tests fail")))
        assert decision.action == "retry"
        assert decision.target_role == "implementer"
        assert decision.feedback_to_inject == "fix the off-by-one"

    def test_retry_without_feedback_rejected(self):
        with pytest.raises(sup.SupervisorError, match="feedback_to_inject"):
            sup._parse_decision(_resp(_args(
                action="retry", target_role="implementer", reason="r")))

    def test_retry_without_valid_role_rejected(self):
        with pytest.raises(sup.SupervisorError, match="target_role"):
            sup._parse_decision(_resp(_args(
                action="retry", feedback_to_inject="f", reason="r")))

    def test_clarification_requires_question(self):
        with pytest.raises(sup.SupervisorError, match="feedback_to_inject"):
            sup._parse_decision(_resp(_args(
                action="request_clarification", reason="r")))

    def test_invalid_action_rejected(self):
        with pytest.raises(sup.SupervisorError, match="invalid action"):
            sup._parse_decision(_resp(_args(action="explode", reason="r")))

    def test_missing_reason_rejected(self):
        with pytest.raises(sup.SupervisorError, match="reason"):
            sup._parse_decision(_resp(_args(action="advance")))

    def test_no_tool_call_rejected(self):
        with pytest.raises(sup.SupervisorError, match="no tool_call"):
            sup._parse_decision(_resp("", tool_calls=[]))

    def test_wrong_tool_name_rejected(self):
        with pytest.raises(sup.SupervisorError, match="unexpected tool name"):
            sup._parse_decision(
                _resp(_args(action="advance", reason="r"), name="detonate"))

    def test_malformed_arguments_json_rejected(self):
        with pytest.raises(sup.SupervisorError, match="malformed"):
            sup._parse_decision(_resp("{not json"))

    def test_missing_choices_rejected(self):
        with pytest.raises(sup.SupervisorError, match="choices"):
            sup._parse_decision({"choices": []})


class TestDecide:
    def test_budget_exhausted_forces_abort_without_network(self):
        ctx = {"spec_id": "spec_x",
               "transitions_used": sup.MAX_SUPERVISOR_TRANSITIONS}
        with mock.patch.object(sup, "_post_completion",
                               side_effect=AssertionError("no network")):
            decision = sup.decide(ctx)
        assert decision.action == "abort"
        assert "budget exhausted" in decision.reason

    def test_happy_path_parses_model_response(self):
        payload = _resp(_args(action="advance", reason="looks done"))
        ctx = {"spec_id": "spec_x", "transitions_used": 0,
               "phase": "EXECUTING", "role": "reviewer",
               "outcome": "tests_passed"}
        with mock.patch.object(sup, "_post_completion", return_value=payload):
            decision = sup.decide(ctx)
        assert decision.action == "advance"
        assert decision.reason == "looks done"
