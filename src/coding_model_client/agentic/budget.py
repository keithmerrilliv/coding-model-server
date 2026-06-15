"""Retrieval budget — tracks tool iterations and enforces limits."""
from typing import Optional


class RetrievalBudget:
    """Tracks tool call iterations and enforces retrieval limits.

    At 75% budget a warning is injected. At 100% the orchestrator
    forces synthesis.
    """

    def __init__(self, max_iterations: int = 20):
        self.max_iterations = max_iterations
        self.current = 0
        self._warning_issued = False

    def increment(self) -> None:
        """Record one tool iteration."""
        self.current += 1

    @property
    def remaining(self) -> int:
        return max(0, self.max_iterations - self.current)

    @property
    def exhausted(self) -> bool:
        return self.current >= self.max_iterations

    @property
    def at_warning_threshold(self) -> bool:
        """True when at or past 75% budget, warning not yet issued."""
        return (not self._warning_issued
                and self.current >= int(self.max_iterations * 0.75))

    def get_injection(self) -> Optional[str]:
        """Return a system-level injection string, or None.

        Called before each ``get_completion()``.
        """
        if self.exhausted:
            return (
                f"RETRIEVAL BUDGET EXHAUSTED ({self.current}/{self.max_iterations} "
                "iterations used). You MUST synthesize your answer now from the "
                "information you have already gathered. Do NOT attempt further "
                "tool calls."
            )
        if self.at_warning_threshold:
            self._warning_issued = True
            return (
                f"BUDGET WARNING: You have used {self.current}/{self.max_iterations} "
                f"retrieval steps. Only {self.remaining} steps remain. Focus on "
                "synthesizing an answer from what you have gathered so far. Only "
                "use remaining steps for critical missing information."
            )
        return None

    def reset(self):
        """Reset for a new task."""
        self.current = 0
        self._warning_issued = False
