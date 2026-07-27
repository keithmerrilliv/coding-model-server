"""Planner agent for autonomous mode.

Reads a markdown spec (plus any prior clarification Q/A) and either:
  - Returns CLARIFY questions if the spec is missing critical information
  - Returns a normalized YAML plan if the spec is complete enough to execute

Calls the coding-model-server /v1/chat/completions endpoint over HTTP. The model is
configurable via the AUTONOMOUS_PLANNER_AGENT env var; default is
``dense_architect`` (Qwen3.6-27B), chosen for reasoning capacity over raw speed.

The planner's only contract is the output format. It is NOT supposed to
write code, make architectural decisions, or expand scope. Its job is
extraction + gap-finding.
"""
from __future__ import annotations

import logging
import os
import re
import textwrap

import yaml
from dataclasses import dataclass

from coding_model_autonomous._http import post_chat_completion
from coding_model_server.streaming import strip_thinking as _server_strip_thinking

logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────────────

PLANNER_AGENT = os.getenv("AUTONOMOUS_PLANNER_AGENT", "dense_architect")
PLANNER_TIMEOUT = float(os.getenv("AUTONOMOUS_PLANNER_TIMEOUT", "900"))  # 15 min
PLANNER_MAX_TOKENS = int(os.getenv("AUTONOMOUS_PLANNER_MAX_TOKENS", "4000"))

# How many times to RE-ISSUE the planner request when the model's output is
# unparseable (bad YAML, missing/duplicate markers) before giving up. Mirrors
# executor.REVIEWER_PARSE_RETRIES: structure errors are intermittent at temp 0.2,
# so a re-roll usually recovers — and the orchestrator turns a PlannerError into
# spec FAILED, so a single bad token used to hard-kill the whole spec. Real
# observed kill: dense_architect emitted `notes: Out-of-scope: CLI wrapper...`,
# where the unquoted `: ` parsed as a nested mapping ("mapping values are not
# allowed here"). Only PlannerError re-rolls; PlannerClarify/PlannerYaml are
# valid outcomes and are never retried.
PLANNER_PARSE_RETRIES = int(os.getenv("AUTONOMOUS_PLANNER_PARSE_RETRIES", "1"))


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
    clarifications:                # ONLY include if the spec has a ## Clarifications section
      - "Verbatim text of the operator's answer to question 1"
      - "Verbatim text of the operator's answer to question 2"
    test_strategy:
      framework: pytest            # pytest | node_test | jest | swift_test | xcodebuild_test | none
      required: true
      notes: "any extra context"
      # Apple targets ONLY (framework: xcodebuild_test or swift_test) — these are
      # forwarded verbatim as arguments to the Mac runner, so they must be real
      # YAML keys here, never prose inside `notes`:
      repo: electric-sheep         # REQUIRED. Symbolic name from the Mac runner's repos.yml
      scheme: ElectricSheep        # REQUIRED for xcodebuild_test
      destination: "platform=macOS"
      # REQUIRED for xcodebuild_test: without it xcodebuild runs EVERY target in
      # the scheme, including the UI-test target every Xcode app template ships.
      # A UI test must launch the app against a window server, which the runner
      # cannot provide, so it crashes and fails the run however good the unit
      # tests are (DEV-394). Name the unit-test target, or a narrower
      # Target/Class/testMethod path.
      filter: ElectricSheepTests
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
    9. If the input contains a `## Clarifications` section with
       operator answers, copy EACH answer VERBATIM into a top-level
       `clarifications:` list in the YAML. The implementer reads this
       block directly and treats it as authoritative — same authority
       as the spec itself. Do NOT paraphrase, summarize, merge, or
       fold clarifications into other fields. Keep the operator's
       original wording. Omit the `clarifications:` field entirely
       when there are no clarifications to record.
       EXCEPTION: a round whose question text begins with
       "## Plan rejection feedback" is NOT an answer about the spec — it is
       a reviewer telling you to revise THIS YAML. Apply it to the plan and
       do NOT copy it into `clarifications:`. Copying it there hands
       plan-editing instructions to the implementer as if they were product
       requirements, which has caused implementers to act on them (DEV-391).
    10. For JavaScript/TypeScript specs whose tests run on THIS server
       (execution_target: server — e.g. web/WebGPU/Node projects), set
       test_strategy.framework: node_test. The sandbox has NO network and
       NO dependency-install step, so tests MUST use Node's built-in runner
       (`node:test` + `node:assert`) with ZERO external packages — no jest,
       vitest, ts-jest, or any `package.json` dependency. Test files must be
       plain `*.test.js` (or pre-compiled to JS) discoverable by `node --test`.
       Design the spec so the testable logic core is pure JS with no DOM/GPU/
       browser globals. Reserve the `jest` framework for repos that already
       vendor their own node_modules.
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

# Marker regexes accept 1-3 brackets on both sides because Coding Model-family
# models output `<<<TAG>>>` / `<<TAG>>` / `<TAG>` non-deterministically
# (see executor.py for the same fragility on FILE/REVIEW/etc).
_CLARIFY_RE = re.compile(
    r"<{1,3}CLARIFY>{1,3}\s*(.*?)\s*<{1,3}END>{1,3}", re.DOTALL | re.IGNORECASE,
)
_YAML_RE = re.compile(
    r"<{1,3}YAML>{1,3}\s*(.*?)\s*<{1,3}END>{1,3}", re.DOTALL | re.IGNORECASE,
)
# Fallback for partial/unclosed CLARIFY blocks: planner is non-deterministic
# and occasionally emits the opener without an explicit <<<END>>>. Accept the
# tail of the response as the questions block. NOT applied to YAML — a missing
# END there means structurally malformed output we shouldn't try to recover.
_CLARIFY_OPEN_ONLY_RE = re.compile(
    r"<{1,3}CLARIFY>{1,3}\s*(.*)", re.DOTALL | re.IGNORECASE,
)


def _strip_thinking(text: str) -> str:
    """Defensive strip before parsing planner output (DEV-153): delegates to
    streaming.strip_thinking, the single implementation that handles all
    observed patterns (full block, orphan close, unclosed open, <REACT>) —
    the local regex copy only covered full blocks."""
    return _server_strip_thinking(text).strip()


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
        # Validate before persist. The orchestrator's _process_executing path
        # calls yaml.safe_load on every tick; a malformed plan turns that into
        # a tight retry loop (every 5s, indefinitely). Catching here surfaces
        # the parse error once, on the planner-output boundary, instead. Real
        # observed failure: Qwen3.6-27B emitted a risk bullet starting with
        # a literal backtick (- `ares-cli` ...) which is invalid YAML.
        try:
            parsed = yaml.safe_load(yaml_text)
        except yaml.YAMLError as exc:
            return PlannerError(
                reason=f"<<<YAML>>> block is not valid YAML: {exc}",
                raw_response=text,
            )
        if not isinstance(parsed, dict):
            return PlannerError(
                reason=f"<<<YAML>>> block is not a mapping (got {type(parsed).__name__})",
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

def _call_planner_once(
    user_msg: str,
    *,
    agent: str,
    timeout: float,
) -> PlannerResult:
    """One planner inference + parse. Raises requests.RequestException on
    transport failure (so the retry loop in call_planner does NOT swallow
    transport errors — only parse failures re-roll)."""
    resp = post_chat_completion(
        agent,
        [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        timeout=timeout,
        max_tokens=PLANNER_MAX_TOKENS,
        temperature=0.2,           # low — we want consistent structured output
        retry_5xx=True,            # transient 5xx (e.g. mid model-swap) shouldn't kill planning
    )
    resp.raise_for_status()
    data = resp.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        return PlannerError(
            reason=f"Server response missing choices/content: {e}",
            raw_response=str(data)[:2000],
        )

    return parse_planner_response(content)


def call_planner(
    spec_markdown: str,
    clarifications: list[tuple[str, str]] | None = None,
    *,
    agent: str = PLANNER_AGENT,
    timeout: float = PLANNER_TIMEOUT,
    parse_retries: int | None = None,
) -> PlannerResult:
    """Send the spec to the planner agent and parse the response.

    Raises requests.RequestException on transport failures so callers can
    distinguish "model said something we couldn't parse" from "couldn't
    reach the server at all".

    Parse failures return a PlannerError — but only after re-issuing the
    request up to ``parse_retries`` times (default PLANNER_PARSE_RETRIES). LLM
    structure errors (invalid YAML, missing/duplicate markers) are intermittent
    at temp 0.2, and the orchestrator turns a PlannerError into spec FAILED, so
    a single garbled emission would otherwise hard-kill the spec. Valid results
    (PlannerYaml / PlannerClarify) short-circuit the loop immediately.
    """
    user_msg = build_user_message(spec_markdown, clarifications)
    if parse_retries is None:
        parse_retries = PLANNER_PARSE_RETRIES

    logger.info("planner: calling agent=%s, spec_chars=%d, clarifications=%d",
                agent, len(spec_markdown),
                len(clarifications) if clarifications else 0)

    result: PlannerResult = PlannerError(reason="planner not called", raw_response="")
    for attempt in range(parse_retries + 1):
        result = _call_planner_once(user_msg, agent=agent, timeout=timeout)
        if not isinstance(result, PlannerError):
            break
        if attempt < parse_retries:
            logger.warning(
                "planner: unparseable output (%s) — re-rolling (attempt %d/%d)",
                result.reason, attempt + 2, parse_retries + 1)

    if isinstance(result, PlannerYaml):
        logger.info("planner: produced YAML (%d bytes)", len(result.yaml_text))
    elif isinstance(result, PlannerClarify):
        logger.info("planner: needs clarification (%d questions)",
                    len(result.questions))
    else:
        logger.warning("planner: unparseable response after %d attempt(s) (%s)",
                       parse_retries + 1, result.reason)

    return result
