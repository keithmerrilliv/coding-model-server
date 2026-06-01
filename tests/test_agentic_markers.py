"""Tests for the agentic confidence gate.

Models emit ``<<<CONFIDENCE>>>N`` with inconsistent bracket counts
(``<CONFIDENCE>`` … ``<<<CONFIDENCE>>>``); the 1-3 bracket tolerance is
load-bearing. These also pin the gate's retrieve-vs-synthesize decisions, which
steer the agentic RAG loop.
"""
from qwen_client.agentic.confidence import ConfidenceGate


class TestUpdateFromResponse:
    def test_triple_bracket(self):
        g = ConfidenceGate()
        assert g.update_from_response("<<<CONFIDENCE>>>85") is True
        assert g.current_confidence == 85

    def test_single_bracket_tolerated(self):
        g = ConfidenceGate()
        assert g.update_from_response("<CONFIDENCE>42") is True
        assert g.current_confidence == 42

    def test_embedded_in_surrounding_text(self):
        g = ConfidenceGate()
        g.update_from_response("some answer\n<<<CONFIDENCE>>>7\ntrailing")
        assert g.current_confidence == 7

    def test_missing_marker_returns_false(self):
        g = ConfidenceGate()
        assert g.update_from_response("no marker here") is False
        assert g.current_confidence is None

    def test_value_clamped_to_100(self):
        g = ConfidenceGate()
        g.update_from_response("<<<CONFIDENCE>>>150")
        assert g.current_confidence == 100


class TestGetInjection:
    @staticmethod
    def _gate(confidence):
        g = ConfidenceGate()
        if confidence is not None:
            g.update_from_response(f"<<<CONFIDENCE>>>{confidence}")
        return g

    def test_no_confidence_reported_returns_none(self):
        assert self._gate(None).get_injection(budget_exhausted=False) is None

    def test_high_confidence_says_synthesize(self):
        msg = self._gate(80).get_injection(budget_exhausted=False)
        assert msg is not None and "synthesize" in msg.lower()

    def test_low_confidence_asks_for_more_retrieval(self):
        msg = self._gate(30).get_injection(budget_exhausted=False)
        assert msg is not None and "retrieval" in msg.lower()

    def test_neutral_zone_returns_none(self):
        # 50-69 is the neutral band: keep working, no nudge.
        assert self._gate(60).get_injection(budget_exhausted=False) is None

    def test_budget_exhausted_low_confidence_forces_synthesis(self):
        msg = self._gate(40).get_injection(budget_exhausted=True)
        assert msg is not None and "budget" in msg.lower()

    def test_budget_exhausted_high_confidence_no_injection(self):
        assert self._gate(90).get_injection(budget_exhausted=True) is None


class TestReset:
    def test_reset_clears_confidence(self):
        g = ConfidenceGate()
        g.update_from_response("<<<CONFIDENCE>>>80")
        g.reset()
        assert g.current_confidence is None
