"""Structured retrieval plan the model creates and updates.

The model emits ``<<<PLAN>>>`` blocks with a goal, numbered steps
(with ``[x]``/``[ ]`` checkboxes), and a current-step indicator.
Each emission fully replaces the previous plan.
"""
import re
from typing import Optional


# Matches <<<PLAN>>> content up to next <<<TAG>>> or end of string.
_PLAN_RE = re.compile(
    r'<{1,3}PLAN>{1,3}\s*(.*?)(?=<{1,3}(?!PLAN)\w+>{1,3}|\Z)',
    re.DOTALL
)

_GOAL_RE = re.compile(r'^GOAL\s*:\s*(.+)', re.MULTILINE)
_CURRENT_RE = re.compile(r'^CURRENT\s*:\s*(\d+)', re.MULTILINE)
_STEP_RE = re.compile(r'^\s*\d+\.\s*\[([ xX])\]\s*(.+)$', re.MULTILINE)


class RetrievalPlan:
    """Structured retrieval plan the model creates and updates."""

    def __init__(self):
        self.goal: Optional[str] = None
        self.steps: list[dict] = []        # [{"text": str, "done": bool}]
        self.current_step: int = 0
        self._raw: Optional[str] = None

    def update_from_response(self, response_text: str) -> bool:
        """Parse ``<<<PLAN>>>`` block from model response.

        Returns True if a plan block was found (and state updated).
        """
        match = _PLAN_RE.search(response_text)
        if not match:
            return False

        content = match.group(1).strip()
        if not content:
            return False

        self._raw = content

        goal_match = _GOAL_RE.search(content)
        if goal_match:
            self.goal = goal_match.group(1).strip()

        self.steps = []
        for step_match in _STEP_RE.finditer(content):
            done = step_match.group(1).lower() == 'x'
            text = step_match.group(2).strip()
            self.steps.append({"text": text, "done": done})

        current_match = _CURRENT_RE.search(content)
        if current_match:
            self.current_step = int(current_match.group(1))
        elif self.steps:
            # Default to first unchecked step
            for i, s in enumerate(self.steps, 1):
                if not s["done"]:
                    self.current_step = i
                    break

        return True

    @property
    def is_empty(self) -> bool:
        return self.goal is None and not self.steps

    def get_injection(self) -> Optional[str]:
        """Return formatted plan for injection.

        Returns None if no plan has been created yet.
        """
        if self.is_empty:
            return None

        parts = ["## RETRIEVAL PLAN"]
        if self.goal:
            parts.append(f"GOAL: {self.goal}")
        if self.steps:
            parts.append("STEPS:")
            for i, s in enumerate(self.steps, 1):
                check = "x" if s["done"] else " "
                marker = " <-- CURRENT" if i == self.current_step else ""
                parts.append(f"  {i}. [{check}] {s['text']}{marker}")
        parts.append("")
        parts.append("Update this plan with <<<PLAN>>> as steps are completed or revised.")
        return "\n".join(parts)

    def display(self) -> Optional[str]:
        """Return a user-facing display of the current plan state."""
        if self.is_empty:
            return None

        lines = []
        if self.goal:
            lines.append(f"  Plan: {self.goal}")
        for i, s in enumerate(self.steps, 1):
            check = "x" if s["done"] else " "
            arrow = " <-" if i == self.current_step else ""
            lines.append(f"    {i}. [{check}] {s['text']}{arrow}")
        return "\n".join(lines)

    def reset(self):
        """Reset for a new task."""
        self.goal = None
        self.steps.clear()
        self.current_step = 0
        self._raw = None
