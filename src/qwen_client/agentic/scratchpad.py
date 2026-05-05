"""Working memory (scratchpad) the model writes/reads across iterations.

The model emits ``<<<SCRATCHPAD>>>`` blocks with structured content.
Each emission fully replaces the previous scratchpad state — the model
is the source of truth.
"""
import re
from typing import Optional


# Matches <<<SCRATCHPAD>>> content up to next <<<TAG>>> or end of string.
# Tolerates 1-3 brackets (Qwen bracket style variation).
_SCRATCHPAD_RE = re.compile(
    r'<{1,3}SCRATCHPAD>{1,3}\s*(.*?)(?=<{1,3}(?!SCRATCHPAD)\w+>{1,3}|\Z)',
    re.DOTALL
)

# Section headers inside scratchpad content
_SECTION_RE = re.compile(r'^(FACTS|OPEN_QUESTIONS|DEAD_ENDS)\s*:', re.MULTILINE)


class Scratchpad:
    """Working memory the model writes/reads across iterations within a task."""

    def __init__(self):
        self.facts: list[str] = []
        self.open_questions: list[str] = []
        self.dead_ends: list[str] = []
        self._raw: Optional[str] = None

    def update_from_response(self, response_text: str) -> bool:
        """Parse ``<<<SCRATCHPAD>>>`` block from model response.

        Returns True if a scratchpad block was found (and state updated).
        Each emission fully replaces the previous state.
        """
        match = _SCRATCHPAD_RE.search(response_text)
        if not match:
            return False

        content = match.group(1).strip()
        if not content:
            return False

        self._raw = content
        self.facts = []
        self.open_questions = []
        self.dead_ends = []

        current_section = None
        for line in content.splitlines():
            line = line.strip()
            header = _SECTION_RE.match(line)
            if header:
                current_section = header.group(1)
                continue
            if not line or line.startswith('#'):
                continue
            # Strip leading bullet
            item = re.sub(r'^[-*]\s*', '', line)
            if not item:
                continue
            if current_section == 'FACTS':
                self.facts.append(item)
            elif current_section == 'OPEN_QUESTIONS':
                self.open_questions.append(item)
            elif current_section == 'DEAD_ENDS':
                self.dead_ends.append(item)
            elif current_section is None:
                # Free-form text before any section header — treat as facts
                self.facts.append(item)

        return True

    @property
    def is_empty(self) -> bool:
        return not self.facts and not self.open_questions and not self.dead_ends

    def get_injection(self) -> Optional[str]:
        """Return formatted scratchpad state for injection.

        Returns None if scratchpad is empty (no data yet).
        """
        if self.is_empty:
            return None

        parts = ["## WORKING MEMORY (Scratchpad)"]
        if self.facts:
            parts.append("FACTS:")
            for f in self.facts:
                parts.append(f"- {f}")
        if self.open_questions:
            parts.append("OPEN_QUESTIONS:")
            for q in self.open_questions:
                parts.append(f"- {q}")
        if self.dead_ends:
            parts.append("DEAD_ENDS:")
            for d in self.dead_ends:
                parts.append(f"- {d}")
        parts.append("")
        parts.append("Update this scratchpad with <<<SCRATCHPAD>>> as you learn new information.")
        return "\n".join(parts)

    def reset(self):
        """Reset for a new task."""
        self.facts.clear()
        self.open_questions.clear()
        self.dead_ends.clear()
        self._raw = None
