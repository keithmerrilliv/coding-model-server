"""AgenticContext — aggregates all five agentic RAG features.

This is the single object the orchestrator interacts with.  It
encapsulates classifier, budget, scratchpad, planner, and confidence
behind two methods:

- ``process_response(text)`` — extract agentic markers, return cleaned text
- ``get_pre_completion_injection()`` — build combined injection string
"""
import re
from typing import Optional

from coding_model_client.agentic.classifier import classify_query, QUERY_BUDGETS
from coding_model_client.agentic.budget import RetrievalBudget
from coding_model_client.agentic.scratchpad import Scratchpad
from coding_model_client.agentic.planner import RetrievalPlan
from coding_model_client.agentic.confidence import ConfidenceGate


# Regex to strip agentic marker blocks from the response text before
# passing it to process_remote_commands() (prevents false matches).
_SCRATCHPAD_STRIP_RE = re.compile(
    r'<{1,3}SCRATCHPAD>{1,3}\s*.*?(?=<{1,3}(?!SCRATCHPAD)\w+>{1,3}|\Z)',
    re.DOTALL
)
_PLAN_STRIP_RE = re.compile(
    r'<{1,3}PLAN>{1,3}\s*.*?(?=<{1,3}(?!PLAN)\w+>{1,3}|\Z)',
    re.DOTALL
)
_CONFIDENCE_STRIP_RE = re.compile(
    r'<{0,3}/?CONFIDEN\w*>{0,3}\s*\d*'
)


class AgenticContext:
    """Aggregates all agentic RAG features for a single task."""

    def __init__(self, user_query: str):
        self.query_type = classify_query(user_query)
        budget_limit = QUERY_BUDGETS.get(self.query_type, 15)

        self.scratchpad = Scratchpad()
        self.plan = RetrievalPlan()
        self.budget = RetrievalBudget(max_iterations=budget_limit)
        self.confidence = ConfidenceGate()

    def process_response(self, response_text: str) -> str:
        """Extract agentic markers and return cleaned response text.

        The cleaned text has ``<<<SCRATCHPAD>>>``, ``<<<PLAN>>>``, and
        ``<<<CONFIDENCE>>>`` blocks removed so they don't interfere with
        tool marker parsing in ``process_remote_commands()``.
        """
        self.scratchpad.update_from_response(response_text)
        self.plan.update_from_response(response_text)
        self.confidence.update_from_response(response_text)

        # Strip agentic markers from the text
        cleaned = _SCRATCHPAD_STRIP_RE.sub('', response_text)
        cleaned = _PLAN_STRIP_RE.sub('', cleaned)
        cleaned = _CONFIDENCE_STRIP_RE.sub('', cleaned)

        return cleaned

    def get_pre_completion_injection(self) -> Optional[str]:
        """Build the combined injection string for the next completion.

        Returns None if all features have nothing to inject (full
        backward compatibility with non-agentic behavior).
        """
        parts = []

        # Query classification header
        parts.append(f"## Query Classification: {self.query_type.value}")

        # Scratchpad state
        sp = self.scratchpad.get_injection()
        if sp:
            parts.append(sp)

        # Plan state
        pl = self.plan.get_injection()
        if pl:
            parts.append(pl)

        # Budget warnings
        bw = self.budget.get_injection()
        if bw:
            parts.append(bw)

        # Confidence gating
        cg = self.confidence.get_injection(self.budget.exhausted)
        if cg:
            parts.append(cg)

        # Only the query-type header? Nothing meaningful to inject.
        if len(parts) <= 1:
            return None

        return "\n\n".join(parts)

    def should_force_synthesis(self) -> bool:
        """True when the model must stop retrieving and synthesize."""
        return self.budget.exhausted

    def reset(self):
        """Reset all features for a new task."""
        self.scratchpad.reset()
        self.plan.reset()
        self.budget.reset()
        self.confidence.reset()
