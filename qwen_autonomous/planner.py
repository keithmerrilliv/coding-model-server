"""Planner agent for autonomous mode.

Reads a markdown spec (plus any prior clarification Q/A) and either:
  - Returns CLARIFY questions if the spec is missing critical information
  - Returns a normalized YAML plan if the spec is complete enough to execute

Calls the qwen-server /v1/chat/completions endpoint over HTTP. The model is
configurable via the AUTONOMOUS_PLANNER_AGENT env var; default is
``q36_architect`` (Qwen3.6-27B), chosen for reasoning capacity over raw speed.

The planner's only contract is the output format. It is NOT supposed to
write code, make architectural decisions, or expand scope. Its job is
extraction + gap-finding.
"""
from __future__ import annotations

import logging
import os
import re
import textwrap
from dataclasses import dataclass
from typing import Optional

import requests

_SESSION = requests.Session()

logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────────────

PLANNER_AGENT = os.getenv("AUTONOMOUS_PLANNER_AGENT", "q36_architect")
QWEN_SERVER_HOST = os.getenv("QWEN_SERVER_IP", "127.0.0.1")
QWEN_SERVER_PORT = int(os.getenv("QWEN_SERVER_PORT", "5000"))
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
PLANNER_TIMEOUT = float(os.getenv("AUTONOMOUS_PLANNER_TIMEOUT", "900"))  # 15 min
PLANNER_MAX_TOKENS = int(os.getenv("AUTONOMOUS_PLANNER_MAX_TOKENS", "4000"))


# ── System prompt ────────────────────────────────────────────────────────────

PLANNER_SYSTEM_PROMPT = textwrap.dedent("""\
    You are the PLANNER agent for an autonomous software development service.
    Your only job is to read a project specification and produce one of two
    well-formed responses. You DO NOT write code, design architecture, or
    make implementation decisions. You extract structured information and
    identify gaps that would block downstream agents.

    # Input format

    You will receive a markdown specification. It may include a section
    `## Clarifications` containing your previous questions and the human's
    answers. Treat those answers as authoritative — the user has filled in
    the gaps you found.

    # Output contract — CRITICAL

    Respond with EXACTLY ONE of these two formats. No preamble. No commentary.
    No text outside the markers. The downstream parser is strict.

    Format A — when critical information is missing:

    <<<CLARIFY>>>
    1. <First specific, answerable question>
    2. <Second specific, answerable question>
    ...
    <<<END>>>

    Format B — when the spec is complete enough to begin work:

    <<<YAML>>>
    title: "..."
    goal: |
      ...
    language: ...
    ... (full YAML, see schema below)
    <<<END>>>

    # Required information (must be present or asked about)

    For every spec:
      - Goal: what is being built and why
      - Target language and runtime version
      - Acceptance criteria: testable conditions for "done"
      - Output location: where the code should live (existing repo? new project? which directory?)

    For specs that involve code execution:
      - Test strategy: framework and whether tests are required
      - Dependencies: are external packages allowed? any forbidden?

    For platform-specific specs:
      - Runtime constraints (OS version, hardware, deployment target)
      - Whether tooling requires a specific platform (macOS for Xcode, etc.)

    # YAML schema (use this exact structure)

    ```yaml
    title: "Human-readable project title"
    goal: |
      Multi-line description of what is being built and what success
      looks like. Should match what the user wrote, condensed.
    language: python              # or swift, typescript, rust, etc.
    target_runtime: "Python 3.10+"
    output_location:
      repo: "path/to/repo or 'new project'"
      directory: "subdir if applicable, else null"
    acceptance_criteria:
      - "Specific, testable condition"
      - "Another specific testable condition"
    test_strategy:
      framework: pytest            # or xctest, jest, none
      required: true
      notes: "any extra context"
    constraints:
      dependencies_allowed: true   # external packages permitted?
      notes: "any other constraints from the spec"
    phases:
      - name: design
        role: architect
        inputs: ["spec.md"]
        outputs: ["design.md"]
        success: "Architecture document covering components, data model, and APIs"
      - name: implement
        role: implementer
        inputs: ["design.md", "spec.md"]
        outputs: ["<source file paths>"]
        success: "Code matches design; acceptance criteria covered"
        execution_target: server   # or 'client' for macOS-specific tooling
      - name: test
        role: reviewer
        inputs: ["<source files>"]
        outputs: ["test_report.md"]
        success: "All tests pass; acceptance criteria verified"
    risks:
      - "Known unknowns or edge cases worth flagging early"
    ```

    # Hard rules

    1. NEVER invent details. If the language is not stated, ASK. Do not
       default to Python.
    2. NEVER expand scope. If the spec says "TODO list", do not add
       authentication or sync. Stay within what was requested.
    3. ASK rather than assume when the spec is ambiguous, even slightly.
       The cost of one extra clarification round is small; the cost of
       a wrong implementation is hours of wasted compute.
    4. Questions must be SPECIFIC and ANSWERABLE. Bad: "What about the
       architecture?" Good: "Should the database be SQLite or Postgres?"
    5. Ask AT MOST 5 questions per round. Pick the most critical first.
    6. Phases must always be design → implement → test in that order.
       Adjust the success criteria text to match the spec.
    7. If the spec targets macOS-only tooling (Xcode, Swift Package Manager
       on Apple platforms, AppKit, UIKit, CoreML training, etc.), set
       execution_target: client. Otherwise set it to server.
    8. If you are producing YAML, every required field above must be
       present. If you cannot fill any required field from the spec or
       from prior clarifications, switch to CLARIFY format and ask.
    """)


# ── Output models ────────────────────────────────────────────────────────────

@dataclass
class PlannerClarify:
    """Planner needs more information before it can produce a plan."""
    questions: list[str]
    raw_response: str


@dataclass
class PlannerYaml:
    """Planner produced a complete plan ready for human review."""
    yaml_text: str
    raw_response: str


@dataclass
class PlannerError:
    """Planner output was unparseable. Caller should mark spec as failed."""
    reason: str
    raw_response: str


PlannerResult = PlannerClarify | PlannerYaml | PlannerError


# ── Parsing ──────────────────────────────────────────────────────────────────

_CLARIFY_RE = re.compile(
    r"<<<CLARIFY>>>\s*(.*?)\s*<<<END>>>", re.DOTALL | re.IGNORECASE,
)
_YAML_RE = re.compile(
    r"<<<YAML>>>\s*(.*?)\s*<<<END>>>", re.DOTALL | re.IGNORECASE,
)
# Fallback for partial/unclosed CLARIFY blocks: planner is non-deterministic
# and occasionally emits the opener without an explicit <<<END>>>. Accept the
# tail of the response as the questions block. NOT applied to YAML — a missing
# END there means structurally malformed output we shouldn't try to recover.
_CLARIFY_OPEN_ONLY_RE = re.compile(
    r"<<<CLARIFY>>>\s*(.*)", re.DOTALL | re.IGNORECASE,
)


def _strip_thinking(text: str) -> str:
    """Remove `<think>...</think>` blocks if the model emitted any.

    Some Qwen3.5 variants leak thinking tags even with reasoning disabled.
    See the llama-server backend gotchas for the full story.
    """
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def parse_planner_response(text: str) -> PlannerResult:
    """Parse the model's raw output into one of the typed result variants."""
    cleaned = _strip_thinking(text)

    yaml_match = _YAML_RE.search(cleaned)
    clarify_match = _CLARIFY_RE.search(cleaned)

    # Both present is an ambiguous response — refuse to guess.
    if yaml_match and clarify_match:
        return PlannerError(
            reason="Response contained both <<<YAML>>> and <<<CLARIFY>>> blocks",
            raw_response=text,
        )

    if yaml_match:
        yaml_text = yaml_match.group(1).strip()
        # Strip ```yaml fences the model sometimes adds inside the markers.
        if yaml_text.startswith("```"):
            yaml_text = re.sub(r"^```[a-z]*\s*", "", yaml_text)
            yaml_text = re.sub(r"\s*```$", "", yaml_text)
        if not yaml_text:
            return PlannerError(
                reason="<<<YAML>>> block was empty",
                raw_response=text,
            )
        return PlannerYaml(yaml_text=yaml_text, raw_response=text)

    # Fallback: the model opened CLARIFY but never closed with <<<END>>>.
    # Treat the tail as the questions block. Only used when no YAML was
    # found anywhere — if YAML is present, an unclosed CLARIFY suggests the
    # model is confused and we should fail loudly.
    if not clarify_match and not yaml_match:
        open_only = _CLARIFY_OPEN_ONLY_RE.search(cleaned)
        if open_only:
            clarify_match = open_only

    if clarify_match:
        body = clarify_match.group(1).strip()
        # Split numbered lines into individual questions. Tolerate "1.", "1)", "- ".
        questions: list[str] = []
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            stripped = re.sub(r"^(\d+[.)]|\-)\s*", "", stripped)
            if stripped:
                questions.append(stripped)
        if not questions:
            return PlannerError(
                reason="<<<CLARIFY>>> block contained no parseable questions",
                raw_response=text,
            )
        return PlannerClarify(questions=questions, raw_response=text)

    return PlannerError(
        reason="Response did not contain <<<YAML>>> or <<<CLARIFY>>> markers",
        raw_response=text,
    )


# ── User-message construction ────────────────────────────────────────────────

def build_user_message(
    spec_markdown: str,
    clarifications: list[tuple[str, str]] | None = None,
) -> str:
    """Compose the prompt sent to the planner.

    *clarifications* is a list of (questions_block, answers_block) tuples,
    one per prior clarification round. Each tuple is the raw text the
    planner asked and the raw text the human answered with — the planner
    is expected to read them in context, not via rigid Q/A pairing.

    They get appended as a `## Clarifications` section so the planner sees
    its own previous questions and the human's answers in one document.
    """
    parts = [spec_markdown.strip()]

    if clarifications:
        parts.append("\n\n## Clarifications\n")
        for i, (questions, answers) in enumerate(clarifications, start=1):
            parts.append(
                f"\n### Round {i}\n\n"
                f"**Your previous questions:**\n\n{questions.strip()}\n\n"
                f"**Human's answers:**\n\n{answers.strip()}\n"
            )

    return "".join(parts)


# ── Inference ────────────────────────────────────────────────────────────────

def call_planner(
    spec_markdown: str,
    clarifications: list[tuple[str, str]] | None = None,
    *,
    agent: str = PLANNER_AGENT,
    timeout: float = PLANNER_TIMEOUT,
) -> PlannerResult:
    """Send the spec to the planner agent and parse the response.

    Raises requests.RequestException on transport failures so callers can
    distinguish "model said something we couldn't parse" from "couldn't
    reach the server at all". Parse failures return a PlannerError.
    """
    url = f"http://{QWEN_SERVER_HOST}:{QWEN_SERVER_PORT}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if ADMIN_API_KEY:
        headers["X-Admin-Key"] = ADMIN_API_KEY

    user_msg = build_user_message(spec_markdown, clarifications)

    payload = {
        "model": agent,
        "messages": [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": PLANNER_MAX_TOKENS,
        "temperature": 0.2,        # low — we want consistent structured output
        "stream": False,
        # Opt out of server-side RAG (see executor.py call_agent).
        "skip_memory": True,
    }

    logger.info("planner: calling agent=%s, spec_chars=%d, clarifications=%d",
                agent, len(spec_markdown),
                len(clarifications) if clarifications else 0)

    resp = _SESSION.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        return PlannerError(
            reason=f"Server response missing choices/content: {e}",
            raw_response=str(data)[:2000],
        )

    result = parse_planner_response(content)
    if isinstance(result, PlannerYaml):
        logger.info("planner: produced YAML (%d bytes)", len(result.yaml_text))
    elif isinstance(result, PlannerClarify):
        logger.info("planner: needs clarification (%d questions)",
                    len(result.questions))
    else:
        logger.warning("planner: unparseable response (%s)", result.reason)

    return result
