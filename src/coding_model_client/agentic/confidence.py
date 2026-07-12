"""Confidence gating — model reports confidence, system gates synthesis.

The model emits ``<<<CONFIDENCE>>>N`` (0-100) to self-assess whether
it has enough information to answer.  The gate interacts with the
retrieval budget to decide: keep retrieving, synthesize, or force
synthesis with caveats.
"""
import re
from typing import Optional


_CONFIDENCE_RE = re.compile(r'<{1,3}CONFIDENCE>{1,3}\s*(\d+)')


class ConfidenceGate:
    """Tracks model self-assessed confidence and triggers retrieval or synthesis."""

    SYNTHESIS_THRESHOLD = 70   # >= 70: sufficient to synthesize
    LOW_THRESHOLD = 50         # < 50: trigger more retrieval

    def __init__(self):
        self.current_confidence: Optional[int] = None
        self._history: list[int] = []

    def update_from_response(self, response_text: str) -> bool:
        """Parse ``<<<CONFIDENCE>>>N`` from model response.

        Returns True if found.
        """
        match = _CONFIDENCE_RE.search(response_text)
        if match:
            value = int(match.group(1))
            self.current_confidence = max(0, min(100, value))
            self._history.append(self.current_confidence)
            return True
        return False

    def get_injection(self, budget_exhausted: bool) -> Optional[str]:
        """Return injection based on confidence level and budget state.

        Returns None if no confidence has been reported (graceful degradation)
        or if confidence is in the neutral zone (50-69).
        """
        if self.current_confidence is None:
            return None

        if budget_exhausted:
            if self.current_confidence < self.SYNTHESIS_THRESHOLD:
                return (
                    f"Note: Your confidence is {self.current_confidence}% but the "
                    "retrieval budget is exhausted. Synthesize the best answer you "
                    "can from available information. Clearly note areas of uncertainty."
                )
            return None

        if self.current_confidence < self.LOW_THRESHOLD:
            return (
                f"Your reported confidence is {self.current_confidence}%. This is "
                "below the synthesis threshold. What specific information would "
                "raise your confidence? Use targeted retrieval to fill the gap."
            )

        if self.current_confidence >= self.SYNTHESIS_THRESHOLD:
            return (
                f"Your confidence is {self.current_confidence}%. You have enough "
                "information to synthesize a comprehensive answer."
            )

        return None  # 50-69: neutral zone, keep working

    def reset(self):
        """Reset for a new task."""
        self.current_confidence = None
        self._history.clear()
