"""DEV-714: a bounded, read-only tool loop for the architect.

The architect has always been handed a fixed context assembled from someone
else's guess about what it needs, and the guess has been wrong expensively:

  DEV-698  it called `spawnWaveChain()`, could not see the definition, burned
           five attempts, and the spec was cancelled. Two Electric Sheep specs
           died the same way — every compiler error naming a type that was
           never served.
  DEV-648  a per-section char knob dropped a file the window could hold 2.3x
           over.
  DEV-544  a transient outage stripped protected context and the design was
           generated blind.

Telling a role what it cannot see (DEV-698's fix) is the cheap half. Letting
it ask is the other half, and it attacks the class rather than the instances.

## The protocol

Deliberately the same marker vocabulary DEV-618 gave the eval harness, so a
model that learned it there behaves identically here and DEV-702's result
transfers instead of needing re-measurement:

    <<<READ_FILE>>>path/to/File.swift

## What is deliberately NOT offered

`LIST_DIR`, `GLOB` and `GREP` are REFUSED with an explanation rather than
ignored. The runner exposes no listing endpoint, so they cannot be answered
honestly — and silence is the worst response, because a model that asks and
hears nothing asks again and spends the round. Saying "this tool does not
exist here, name the path" converts a wasted round into a useful one.

Writing tools are refused for the obvious reason: the architect produces a
design, not code.

## Why it is bounded twice

A round cap alone does not bound cost — one round can request forty files.
The character budget is the real ceiling and it counts against the same
allocator as the served sections (DEV-633), because a tool result is prompt
text like any other. Blowing the window mid-inspection would turn a helpful
model into a failed one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("orchestrator")

# Tools answered against the repository.
READ_TOOLS = ("READ_FILE",)
# Tools a model may reasonably try that this surface cannot serve. Refused by
# name so the model stops asking rather than burning rounds on silence.
UNAVAILABLE_TOOLS = ("LIST_DIR", "GLOB", "GREP", "FIND")
# Tools refused on principle. The architect writes a design, not code.
REFUSED_TOOLS = ("WRITE_FILE", "EDIT_FILE", "REMOTE_EXEC", "SAVE_MEMORY")

_ALL = READ_TOOLS + UNAVAILABLE_TOOLS + REFUSED_TOOLS
_MARKER_RE = re.compile(
    r"<<<(" + "|".join(_ALL) + r")>>>[ \t]*([^\n]*)", re.IGNORECASE)

# Defaults. Small on purpose: this is a look-up aid, not a research budget,
# and every round is a full completion at architect cost.
DEFAULT_MAX_ROUNDS = 3
DEFAULT_MAX_CHARS = 40_000
# Below this there is no point offering the tool: one ordinary Swift file does
# not fit, so every read would come back truncated mid-declaration — worse than
# not reading, because a half-signature reads like a whole one.
MIN_USEFUL_CHARS = 4_000


@dataclass
class ToolBudget:
    """What the loop may still spend. Never negative, never silent when spent."""
    max_rounds: int = DEFAULT_MAX_ROUNDS
    max_chars: int = DEFAULT_MAX_CHARS
    rounds_used: int = 0
    chars_used: int = 0
    calls: list = field(default_factory=list)

    @property
    def chars_left(self) -> int:
        return max(0, self.max_chars - self.chars_used)

    @property
    def rounds_left(self) -> int:
        return max(0, self.max_rounds - self.rounds_used)

    def exhausted(self) -> bool:
        return self.rounds_left <= 0 or self.chars_left <= 0


def parse_tool_markers(text: str) -> list[tuple[str, str]]:
    """[(TAG, argument)] for every well-formed marker, in order, upper-cased."""
    return [(m.group(1).upper(), m.group(2).strip())
            for m in _MARKER_RE.finditer(text or "")]


def strip_tool_markers(text: str) -> str:
    """The design text with every marker line removed.

    A design that reaches the gate must not carry protocol chatter — the
    testability check reads backticked spans and would score a marker as
    content.
    """
    return _MARKER_RE.sub("", text or "")


# The architect's answer format. A reply that carries one is an ANSWER, and is
# never re-read as a request however it is worded.
_DESIGN_BLOCK = "<<<DESIGN>>>"


def wants_tools(text: str) -> bool:
    """True when the completion is asking for something rather than answering."""
    return bool(parse_tool_markers(text))


def is_tool_request(text: str) -> bool:
    """True when this completion should be answered with tool results.

    Markers alone are not enough. A design document may legitimately contain
    the string `<<<WRITE_FILE>>>` — describing the implementer's protocol, or
    quoting a prior artefact — and treating that as a request would loop a
    finished design back through the tool stage and spend the attempt. The
    presence of the answer format settles it: a reply with a design block is
    an answer, and the prompt says so in the same words.
    """
    return bool(text) and _DESIGN_BLOCK not in text and wants_tools(text)


def resolve_round(markers: list[tuple[str, str]], *, read, budget: ToolBudget
                  ) -> str:
    """Answer one round of markers. Returns the text to feed back.

    *read* is a bound reader — `read(paths) -> (files, problems)` — with the
    repository and the ref already fixed by the context stage (DEV-632 keeps
    one door to the runner's read path, so this module does not open another).
    None means the spec has no repository to read from.
    """
    sections: list[str] = []
    wanted: list[str] = []
    for tag, arg in markers:
        budget.calls.append(f"{tag} {arg}".strip())
        if tag in REFUSED_TOOLS:
            sections.append(
                f"<<<{tag}>>>{arg}\nREFUSED. You are producing a DESIGN, not "
                f"code. Describe the change instead of making it.")
        elif tag in UNAVAILABLE_TOOLS:
            sections.append(
                f"<<<{tag}>>>{arg}\nUNAVAILABLE. This context has no listing "
                f"or search tool — only READ_FILE, by exact repository path. "
                f"Name the path you want. Do not ask for this again.")
        elif tag in READ_TOOLS and arg:
            wanted.append(arg)

    if wanted and read is not None:
        # One batched call per round: the runner takes a path list, and N
        # separate round-trips for N markers would multiply an already slow
        # step by the model's appetite.
        try:
            files, problems = read(wanted)
        except Exception as exc:                       # noqa: BLE001
            logger.warning("architect tools: read failed (%s)", exc)
            files, problems = [], [f"the read failed: {exc}"]
        for path, content in files:
            if budget.chars_left <= 0:
                sections.append(
                    f"<<<READ_FILE>>>{path}\nBUDGET SPENT before this file "
                    f"could be read. Design from what you already have.")
                continue
            body = content or ""
            if len(body) > budget.chars_left:
                body = (body[:budget.chars_left]
                        + f"\n… TRUNCATED at the {budget.max_chars}-character "
                          f"inspection budget.")
            budget.chars_used += len(body)
            sections.append(f"<<<READ_FILE>>>{path}\n{body}")
        for problem in problems:
            sections.append(f"COULD NOT READ: {problem}")
    elif wanted:
        sections.append(
            "COULD NOT READ: this spec names no repository, so there is "
            "nothing to read. Design from the specification.")

    budget.rounds_used += 1
    header = (f"# TOOL RESULTS (round {budget.rounds_used}/"
              f"{budget.max_rounds}, {budget.chars_left} characters of "
              f"inspection budget left)")
    footer = (
        "\n\nContinue. When you have what you need, write the complete final "
        "design with NO tool markers in it."
        if not budget.exhausted() else
        "\n\nThe inspection budget is now SPENT. Write the complete final "
        "design from what you have, with NO tool markers in it. Do not ask "
        "for more files.")
    return header + "\n\n" + "\n\n".join(sections) + footer


def summary(budget: ToolBudget) -> dict:
    """Event payload: what the architect fetched for itself (DEV-657 pattern)."""
    return {
        "tool_rounds": budget.rounds_used,
        "tool_calls": list(budget.calls),
        "tool_chars": budget.chars_used,
        "tool_budget_spent": budget.exhausted(),
    }
