"""Execution engine for autonomous mode (Phase 2).

Drives specs through architect → implementer → reviewer, calling each
agent via the coding-model-server inference API, parsing structured responses,
writing artifacts to the spec workspace, running tests, and managing
the review-gate / retry loop.

The daemon calls into this module from its ``_process_executing`` handler.
Every function here is *synchronous* — it blocks the daemon's tick thread
for the full duration of an inference call. That's intentional: the
hardware only has one GPU and a sequential inference lock, so there's
nothing else the daemon could usefully do in parallel.

Response formats use ``<<<MARKER>>>…<<<END>>>``-style delimiters that
match the pattern established by the planner. The models' chat templates
are compatible with these markers — they treat them as user-defined
structured output, not as built-in tool calls.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

from coding_model_server import external_judges

from . import seccomp_filter
from ._http import post_chat_completion

# Module-level session: reuses TCP+TLS connections across the
# architect → implementer → reviewer → supervisor sequence, saving
# 5-30 ms per call. Keepalive matters most over real network; even
# on localhost the socket-setup elimination is measurable.
_SESSION = requests.Session()

logger = logging.getLogger("orchestrator.executor")


# ── Configuration ────────────────────────────────────────────────────────────

ARCHITECT_AGENT = os.getenv("AUTONOMOUS_ARCHITECT_AGENT", "dense_architect")
IMPLEMENTER_AGENT = os.getenv("AUTONOMOUS_IMPLEMENTER_AGENT", "implementer")
REVIEWER_AGENT = os.getenv("AUTONOMOUS_REVIEWER_AGENT", "reviewer")

# Design review (#3): a pre-implementation LLM critique of the architect's design.
# Uses the light/fast `reviewer` (Coder-30B) by default — design input is small,
# and turnaround matters. Bounded + fail-open in the orchestrator.
DESIGN_REVIEW_ENABLED = os.getenv("AUTONOMOUS_DESIGN_REVIEW", "1").lower() not in ("0", "false", "no")
DESIGN_REVIEW_AGENT = os.getenv("AUTONOMOUS_DESIGN_REVIEW_AGENT", "reviewer")
DESIGN_REVIEW_MAX_TOKENS = int(os.getenv("AUTONOMOUS_DESIGN_REVIEW_MAX_TOKENS", "8000"))
DESIGN_REVIEW_MAX_REVISIONS = int(os.getenv("AUTONOMOUS_DESIGN_REVIEW_MAX_REVISIONS", "1"))

# Robustness: how many times to re-run the reviewer when its output is
# truncated/unparseable before treating it as a soft FAIL (→ supervisor/retry)
# instead of failing the whole spec. The 122B reviewer's degenerate truncation
# is intermittent, so one re-run often recovers it.
REVIEWER_PARSE_RETRIES = int(os.getenv("AUTONOMOUS_REVIEWER_PARSE_RETRIES", "1"))

ARCHITECT_TIMEOUT = float(os.getenv("AUTONOMOUS_ARCHITECT_TIMEOUT", "2700"))
IMPLEMENTER_TIMEOUT = float(os.getenv("AUTONOMOUS_IMPLEMENTER_TIMEOUT", "1800"))
REVIEWER_TIMEOUT = float(os.getenv("AUTONOMOUS_REVIEWER_TIMEOUT", "2700"))

ARCHITECT_MAX_TOKENS = int(os.getenv("AUTONOMOUS_ARCHITECT_MAX_TOKENS", "8000"))
IMPLEMENTER_MAX_TOKENS = int(os.getenv("AUTONOMOUS_IMPLEMENTER_MAX_TOKENS", "16000"))
REVIEWER_MAX_TOKENS = int(os.getenv("AUTONOMOUS_REVIEWER_MAX_TOKENS", "16000"))

# Dynamic implementer output budget. The single-call implementer must emit
# EVERY file in one response, so a large multi-file spec needs far more output
# headroom than a small one. A fixed cap is wrong both ways: it truncates big
# specs (the trailing file loses its <<<END_FILE>>>, the reviewer reports it as
# a false "missing file" FAIL, and the retry truncates at the same spot) and
# wastes latency budgeting 16k for a two-file job. We scale max_tokens by the
# number of files the design enumerates, clamped to
# [IMPLEMENTER_MAX_TOKENS (floor), IMPLEMENTER_MAX_TOKENS_CEILING].
IMPLEMENTER_MAX_TOKENS_CEILING = int(os.getenv("AUTONOMOUS_IMPLEMENTER_MAX_TOKENS_CEILING", "48000"))
IMPLEMENTER_TOKENS_PER_FILE = int(os.getenv("AUTONOMOUS_IMPLEMENTER_TOKENS_PER_FILE", "1500"))
IMPLEMENTER_TOKENS_BASE = int(os.getenv("AUTONOMOUS_IMPLEMENTER_TOKENS_BASE", "4000"))

MAX_RETRIES = int(os.getenv("AUTONOMOUS_MAX_RETRIES", "5"))

# Manifest mode (#4): for large multi-file specs, generate a file MANIFEST first,
# then one bounded call per file — removing the single-call output ceiling that
# truncates big repos. (A 29-file design overran even a 47,500-token budget; see
# project_coding_model_autonomous_output_token_fix.) Mode selection:
#   auto     — manifest mode when the design enumerates >= threshold files (default)
#   manifest — always manifest mode
#   single   — always the legacy one-shot call
IMPLEMENTER_MODE = os.getenv("AUTONOMOUS_IMPLEMENTER_MODE", "auto").lower()
MANIFEST_FILE_THRESHOLD = int(os.getenv("AUTONOMOUS_MANIFEST_FILE_THRESHOLD", "8"))
# Defaults raised from 4000/8000 after validation runs: a 24-file manifest
# overran 4000, and substantial single files (resolver/probe/player/ui) overran
# 8000 on retries that thread rejection notes. See project_coding_model_autonomous_output_token_fix.
MANIFEST_MAX_TOKENS = int(os.getenv("AUTONOMOUS_MANIFEST_MAX_TOKENS", "8000"))
PER_FILE_MAX_TOKENS = int(os.getenv("AUTONOMOUS_PER_FILE_MAX_TOKENS", "16000"))
PER_FILE_PARSE_RETRIES = int(os.getenv("AUTONOMOUS_PER_FILE_PARSE_RETRIES", "2"))

# Architect parse-retry: how many times to re-call the architect when its
# response can't be parsed for the <<<DESIGN>>> / <<<COMPLEXITY>>> blocks.
# Total attempts = ARCHITECT_PARSE_RETRIES + 1. Default 2 retries
# (3 attempts) is enough for stochastic markdown drift; deterministic
# failures still surface after the cap. Each failed response is
# persisted to spec_dir as architect_failed_response_attempt<N>.txt
# for post-mortem.
ARCHITECT_PARSE_RETRIES = int(os.getenv("AUTONOMOUS_ARCHITECT_PARSE_RETRIES", "2"))

ROLE_TO_AGENT = {
    "architect": ARCHITECT_AGENT,
    "implementer": IMPLEMENTER_AGENT,
    "reviewer": REVIEWER_AGENT,
}
ROLE_TO_TIMEOUT = {
    "architect": ARCHITECT_TIMEOUT,
    "implementer": IMPLEMENTER_TIMEOUT,
    "reviewer": REVIEWER_TIMEOUT,
}
ROLE_TO_MAX_TOKENS = {
    "architect": ARCHITECT_MAX_TOKENS,
    "implementer": IMPLEMENTER_MAX_TOKENS,
    "reviewer": REVIEWER_MAX_TOKENS,
}


# ── Helpers ──────────────────────────────────────────────────────────────────

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _strip_thinking(text: str) -> str:
    """Remove ``<think>…</think>`` blocks the model emits before its answer.

    Qwen3.x family ships with thinking enabled in their chat templates.
    Server-side ThinkingStripper handles the streaming case; this is the
    sync-parse fallback (compiled at import for repeated calls).
    """
    return _THINK_BLOCK_RE.sub("", text).strip()


def _write_artifact(spec_dir: Path, rel_path: str, content: str) -> Path:
    """Write a file to the spec workspace with path-traversal protection.

    Strips leading ``/`` so models can't write absolute paths (Python's
    ``Path / "/abs"`` would discard the left operand). Then resolves
    symlinks and checks the result is still under ``spec_dir``.

    Uses Path.is_relative_to (not str.startswith): the latter would treat
    /work/abc-evil/x as nested under /work/abc, so a sibling-prefix dir
    escape would not be caught. is_relative_to compares path components.
    """
    # Strip leading slashes only — do NOT use lstrip("./") which eats
    # individual chars and would normalize "../../../x" into "x".
    while rel_path.startswith("/"):
        rel_path = rel_path[1:]
    spec_root = spec_dir.resolve()
    abs_path = (spec_dir / rel_path).resolve()
    if not abs_path.is_relative_to(spec_root):
        raise ValueError(f"Path traversal rejected: {rel_path}")
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content)
    return abs_path


def role_to_agent(role: str) -> str:
    return ROLE_TO_AGENT.get(role, IMPLEMENTER_AGENT)


# ── Dynamic implementer output budget ────────────────────────────────────────

_DESIGN_FILE_PATH_RE = re.compile(
    r"[\w./-]+\.(?:ts|tsx|js|jsx|mjs|cjs|json|py|rs|go|java|kt|swift|"
    r"c|h|cc|cpp|hpp|css|scss|html|md|toml|yaml|yml|sh|sql|proto)\b"
)


def estimate_design_file_count(design_md: str) -> int:
    """Count the distinct file paths a design document enumerates.

    A cheap proxy for how much code the implementer must emit. Matches tokens
    that look like a path with a known code/config extension — in the File
    Structure tree, the outputs list, or prose — and de-dupes them. Used only
    to size the implementer's output budget (see implementer_max_tokens_for),
    so an over- or under-count just nudges the clamp, never breaks correctness.
    """
    paths = set()
    for m in _DESIGN_FILE_PATH_RE.finditer(design_md or ""):
        p = m.group(0).strip("./")
        if p:
            paths.add(p)
    return len(paths)


def implementer_max_tokens_for(design_md: str) -> int:
    """Output-token budget for the implementer, scaled to the design's size.

    ``clamp(BASE + files * PER_FILE, IMPLEMENTER_MAX_TOKENS, CEILING)``. The
    floor is the legacy fixed value, so small specs behave exactly as before;
    large multi-file specs get the headroom that prevents single-call
    truncation.
    """
    n = estimate_design_file_count(design_md)
    est = IMPLEMENTER_TOKENS_BASE + n * IMPLEMENTER_TOKENS_PER_FILE
    return max(IMPLEMENTER_MAX_TOKENS, min(IMPLEMENTER_MAX_TOKENS_CEILING, est))


# ── Complexity → implementer tier mapping ────────────────────────────────────
#
# The architect emits a COMPLEXITY block (parsed by parse_architect_response).
# `_select_implementer_agent` consults the architect's recommendation if it's
# in the whitelist; otherwise it falls back to the tier default; otherwise
# the env-default IMPLEMENTER_AGENT. Telemetry is deferred — see
# ~/.claude/projects/.../memory/project_implementer_telemetry.md.

TIER_TO_IMPLEMENTER = {
    "low": "fast_implementer",
    "medium": "implementer",
    "high": "deep_implementer",
    "extreme": "moe_implementer",
}

ALLOWED_IMPLEMENTER_AGENTS = {
    "fast_implementer", "implementer", "deep_implementer", "moe_implementer",
}


# ── System prompts ───────────────────────────────────────────────────────────

ARCHITECT_SYSTEM_PROMPT = textwrap.dedent("""\
    You are the ARCHITECT agent for an autonomous software development service.
    Your job: read the specification and produce a design document that the
    IMPLEMENTER can execute exactly, without ambiguity. You also assess the
    project's complexity and recommend which implementer agent should build it.

    # Output format

    Respond with EXACTLY TWO blocks, in this order. No preamble. No text
    outside the markers.

    <<<DESIGN>>>
    # Architecture: <project title>

    ## Overview
    <2-3 sentences summarizing what this project is and the key design decisions>

    ## Components
    <list each component/module with its responsibility>

    ## File Structure
    <tree showing every file to be created, with a one-line purpose each>

    ## Data Models
    <key types, schemas, or data structures>

    ## Implementation Notes
    <constraints the implementer must follow, edge cases to handle, etc.>

    ## Acceptance Criteria Checklist
    - [ ] <each criterion from the spec, restated in testable form>
    <<<END>>>

    <<<COMPLEXITY>>>
    Tier: <low|medium|high|extreme>
    Recommended agent: <fast_implementer|implementer|deep_implementer|moe_implementer>
    Justification: <one or two sentences citing concrete signals — file count,
    language count, algorithmic depth, integration surface, edge-case density,
    test surface — that drove the tier and agent choice>
    <<<END_COMPLEXITY>>>

    # Tier guide

    - low:     1-2 small files, single language, no external integrations,
               trivial logic, < 10 test cases. Suits `fast_implementer`.
    - medium:  3-6 files, single primary language, modest algorithmic content,
               10-30 test cases. Suits `implementer` (the default).
    - high:    7+ files OR cross-language OR non-trivial algorithms (parsers,
               schedulers, custom data structures) OR external integrations
               (HTTP clients, DB schemas) OR 30+ test cases. Suits
               `deep_implementer`.
    - extreme: production system with concurrency, persistence, or security
               surface; multi-module refactor of an existing codebase; or
               anything where correctness depends on subtle invariants.
               Suits `moe_implementer`.

    # Rules

    1. Be prescriptive about interfaces, file structure, data shapes, and
       INVARIANTS — the implementer follows your design exactly. (But see rule 8
       on formulas: "exactly" is a liability when the thing you prescribe is
       wrong.)
    2. Specify exact file paths relative to the project workspace root.
    3. Keep it concise — the implementer needs clarity, not prose.
    4. Do NOT write implementation code. That is the implementer's job.
    5. Do NOT add scope beyond what the spec requests.
    6. Every acceptance criterion from the spec must appear in your checklist.
    7. Pick the LOWEST tier that fits — do not over-allocate compute. A `high`
       agent for a `low` job wastes time and VRAM; the inverse causes failures.
    8. Specify INVARIANTS, not unverified formulas. When a component has
       algorithmic content, state the PROPERTIES the implementation must satisfy
       (e.g. "all N items map to distinct positions", "output(t) != output(t+1)",
       "result is sorted ascending") rather than a concrete formula you have not
       verified. A wrong formula in the design propagates verbatim into the code
       AND the tests — the whole pipeline then faithfully builds the bug. A
       stated invariant, by contrast, gets independently tested. Only prescribe
       an exact formula when you are certain it is correct and it is the
       simplest way to convey the requirement.
    9. Make the Acceptance Criteria Checklist TESTABLE: restate each criterion
       as a concrete assertion (specific inputs → expected output / property)
       so the reviewer can turn it directly into a test.
    """)

IMPLEMENTER_SYSTEM_PROMPT = textwrap.dedent("""\
    You are the IMPLEMENTER agent for an autonomous software development service.
    You receive a specification and an architecture design, and you produce
    working code files.

    # Output format

    Respond with one or more file blocks. Every file you create MUST appear
    in its own block. The daemon only reads these blocks — prose outside
    them is ignored for file extraction.

    <<<FILE: relative/path/to/file.py>>>
    <complete file content — NOT a diff, the ENTIRE file>
    <<<END_FILE>>>

    <<<FILE: another/file.py>>>
    <complete file content>
    <<<END_FILE>>>

    # Rules

    1. EVERY file must appear in a <<<FILE: path>>>…<<<END_FILE>>> block.
    2. Paths are relative to the project workspace root (no leading /).
    3. Implement ALL components from the design. Do not skip "trivial" parts.
    4. Do NOT create test files — the REVIEWER handles those.
    5. If the spec requires a requirements.txt, setup.py, or similar, include it.
    6. Write COMPLETE files, not snippets or diffs.
    7. Inside a <<<FILE>>> block, write the file's RAW content. Do NOT wrap
       the content in markdown ```language fences — the daemon strips them
       defensively, but a missing close fence (e.g. on truncation) corrupts
       the file. Just emit the content directly.
    8. On a retry attempt (when previous code was rejected), you will see
       the reviewer's feedback. Edit ONLY the files named in that feedback
       to fix the cited issues. For every other file, output it BYTE-FOR-BYTE
       identical to your previous attempt — do not rewrite, refactor, or
       "improve" untouched files. Output ALL files again (the daemon
       overwrites previous versions; an omitted file is a deleted file).
    9. If the plan or spec includes a `clarifications:` block (or an
       "Operator clarifications" section), every item there is a hard
       requirement at the same authority as the spec itself. Apply each
       clarification literally. Do not override a clarification with a
       default or a "more idiomatic" alternative.
    10. TypeScript: `any` is forbidden. Use `unknown` and narrow with type
        guards, or define a proper type. The reviewer rejects `any` on sight.
    11. Pinned dependencies: in package.json, pyproject.toml, requirements.txt,
        Cargo.toml, etc., pin to an exact version. No `^`, no `~`, no `>=`
        ranges. The reviewer rejects unpinned dependencies on sight.
    """)

REVIEWER_SYSTEM_PROMPT = textwrap.dedent("""\
    You are the REVIEWER for an autonomous software service. You receive the
    spec, the design, and the implementer's source files. You (1) write test
    files that exercise the acceptance criteria, and (2) review the code.

    Your verdict reflects STATIC CODE REVIEW only — you do not see test
    execution output, and the orchestrator runs your tests separately. Phrase
    findings as "test X is designed to check Y", never "tests passed".

    # Output format

    Test files first (one block per file). ALWAYS place test files
    under a `tests/` subdirectory — pytest discovers them recursively
    so the path doesn't affect execution, but a `tests/` prefix keeps
    the spec workspace tidy and separates them from implementer
    deliverables. The orchestrator will rewrite a bare `test_*.py`
    path to `tests/test_*.py` defensively, but you should emit the
    correct path yourself:

        <<<FILE: tests/test_something.py>>>
        <complete test file content>
        <<<END_FILE>>>

    Then exactly one review block:

        <<<REVIEW>>>
        ## Test Files Written
        - <file>: <which acceptance criteria it exercises>

        ## Code Review
        ### Issues Found
        - <severity: critical/major/minor> <file>:<line> — <description>
        (Or: "No issues found.")

        ### Verdict
        PASS
        (Or FAIL if you found a critical code defect.)

        ### Verdict Evidence
        <REQUIRED — see below>

        ### Notes
        <anything the implementer should know on retry>
        <<<END_REVIEW>>>

    # Verdict Evidence (REQUIRED, parsed by the orchestrator)

    Empty or missing evidence forces FAIL — an unanchored verdict is rejected.

    For PASS: one line per acceptance criterion mapped to its test:
        - <criterion text> → <test_file.py::test_function_name>

    For FAIL: one line per blocking defect:
        - <severity> <file>:<line> — <description>

    No prose in this field. No test-result claims.

    # Rules

    1. Use the framework named in the plan (default: pytest).
    2. Tests must be runnable: real imports, real paths, no spec-violating mocks.
    3. PASS requires no critical/major defects you can cite by file:line.
    4. Do not expand scope beyond the spec.
    5. File-existence claims must be GROUNDED in the implementer's file list.
       Before writing "file X is missing" or "no Y was created", scan the
       `## Implementation Files` section of your input — every file the
       implementer wrote appears there as a `### path` heading. If the path
       is in that list, the file exists; do NOT report it as missing. If you
       cannot find a file you expected, name what you DID find and ask
       whether the implementer used a different path, rather than asserting
       absence. Hallucinated "missing file" findings are the #1 source of
       false-FAIL verdicts and waste a full retry cycle.
    6. Enforce IMPLEMENTER hard rules and FAIL on violations:
       - TypeScript `any` TYPE (use `unknown` + narrowing or a proper type).
         Use the "Authoritative `any`-type scan" in your input as the ONLY
         source of truth for this rule — it already strips comments, string
         literals, and identifiers. The word "any" in a comment
         (`// handle any error`) or a string (`'hdr.any'`) is NOT a violation,
         and you must NOT substring-search the source for "any". If that scan
         lists zero usages, the no-`any` rule PASSES — do not FAIL it.
       - Unpinned deps in package.json / pyproject.toml / requirements.txt /
         Cargo.toml — `^`, `~`, `>=` ranges all fail. (package.json dep ranges
         are auto-pinned before you see the code, so they should already be exact.)
       Cite the exact file:line in `### Verdict Evidence`.
    """)


# ── Agent calling ────────────────────────────────────────────────────────────

def call_agent(
    role: str,
    messages: list[dict[str, str]],
    *,
    agent: str | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
    meta: Optional[dict] = None,
) -> str:
    """Call an agent via the coding-model-server inference API.

    Returns the raw content string from the model's response.
    Raises on transport or HTTP errors so the caller can decide
    whether to retry or fail.

    If *meta* is provided, it is populated with response metadata the caller
    can inspect after the call:
      - ``meta["finish_reason"]``: the model's stop reason ("stop", "length", …)
      - ``meta["truncated"]``: True when finish_reason == "length", i.e. the
        output hit ``max_tokens`` and was cut off mid-stream. The implementer
        path uses this to emit an OUTPUT_TRUNCATED event instead of letting a
        half-written file surface as a bogus reviewer "missing file" FAIL.
    """
    agent = agent or role_to_agent(role)
    max_tokens = max_tokens or ROLE_TO_MAX_TOKENS.get(role, 8000)
    timeout = timeout or ROLE_TO_TIMEOUT.get(role, 1800)

    logger.info("calling agent=%s, role=%s, msg_count=%d, max_tokens=%d",
                agent, role, len(messages), max_tokens)

    # retry_5xx=True: model-swap CUDA OOM is the dominant cause of 500s here
    # (VRAM not fully released between models — see project_model_swap_oom.md):
    # the next process can't allocate its compute buffer until the kernel
    # reaps the previous CUDA context. Without this retry a single transient
    # swap-OOM cancels the whole spec (_start_task catches Exception → task
    # failed → spec FAILED, no rotation). The generous backoff schedule lives
    # in _http._BACKOFFS (Blackwell VRAM release takes 10-30s after exit).
    resp = post_chat_completion(
        agent, messages, timeout=timeout, max_tokens=max_tokens,
        temperature=0.2, retry_5xx=True,
    )
    resp.raise_for_status()
    data = resp.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(
            f"Server response missing choices/content: {e}"
        ) from e

    if meta is not None:
        meta["agent"] = agent  # resolved model — lets callers attribute events
        try:
            fr = (data["choices"][0].get("finish_reason") or "").lower()
        except (KeyError, IndexError, AttributeError):
            fr = ""
        meta["finish_reason"] = fr or None
        meta["truncated"] = fr == "length"
        if meta["truncated"]:
            logger.warning(
                "agent=%s role=%s OUTPUT TRUNCATED (finish_reason=length) at "
                "max_tokens=%d — response was cut off mid-stream",
                agent, role, max_tokens,
            )

    return content


# ── Response parsers ─────────────────────────────────────────────────────────
#
# Marker regexes accept 1-3 brackets on both sides because Coding Model-family
# models output `<<<TAG>>>` / `<<TAG>>` / `<TAG>` non-deterministically
# (same fragility that the chat client documented for tool tags). A
# single misplaced bracket on a closing tag would otherwise cause the
# regex to swallow the entire response into one "file", which is what
# triggered the spec_51b1baee retry-1 misparse on 2026-05-01.

_DESIGN_RE = re.compile(
    r"<{1,3}DESIGN>{1,3}\s*(.*?)\s*<{1,3}END>{1,3}", re.DOTALL | re.IGNORECASE,
)
_COMPLEXITY_RE = re.compile(
    r"<{1,3}COMPLEXITY>{1,3}\s*(.*?)\s*<{1,3}END_COMPLEXITY>{1,3}",
    re.DOTALL | re.IGNORECASE,
)
_FILE_RE = re.compile(
    r"<{1,3}FILE:\s*([^\n>]+?)>{1,3}\s*(.*?)\s*<{1,3}END_FILE>{1,3}",
    re.DOTALL | re.IGNORECASE,
)
_REVIEW_RE = re.compile(
    r"<{1,3}REVIEW>{1,3}\s*(.*?)\s*<{1,3}END_REVIEW>{1,3}",
    re.DOTALL | re.IGNORECASE,
)
_VERDICT_RE = re.compile(
    r"###\s*Verdict\s*\n+\s*(PASS|FAIL)", re.IGNORECASE,
)
# Captures the body of the Verdict Evidence block, terminated by the next
# `### Heading` or end-of-text. Anchors the LLM's verdict to specific evidence
# (acceptance criteria → test for PASS, file:line for FAIL); the parser
# downgrades a missing/empty body to FAIL regardless of stated verdict.
_VERDICT_EVIDENCE_RE = re.compile(
    r"###\s*Verdict\s+Evidence\s*\n+(.*?)(?=\n###\s|\Z)",
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class ArchitectResult:
    design_md: str
    raw: str
    # Optional — parsed from the COMPLEXITY block. Older architect outputs and
    # malformed blocks leave this None; orchestrator falls back to the env
    # default implementer in that case.
    complexity: Optional[dict] = None


@dataclass
class ImplementerResult:
    files: list[tuple[str, str]]  # (relative_path, content) — deduped last-wins
    raw: str
    # Paths that appeared more than once in the response. We honor the
    # last occurrence (treating earlier ones as drafts the model
    # corrected itself on) and surface the list so the orchestrator can
    # record a diagnostic event — useful when debugging "why did my file
    # have content I didn't expect" reports.
    duplicate_paths: list[str] = field(default_factory=list)


@dataclass
class ReviewerResult:
    test_files: list[tuple[str, str]]  # (relative_path, content) — deduped last-wins
    review_md: str
    verdict: str  # "PASS" or "FAIL"
    raw: str
    duplicate_paths: list[str] = field(default_factory=list)


@dataclass
class ParseError:
    reason: str
    raw: str


def parse_architect_response(text: str) -> ArchitectResult | ParseError:
    cleaned = _strip_thinking(text)
    m = _DESIGN_RE.search(cleaned)
    if not m:
        return ParseError("No <<<DESIGN>>>…<<<END>>> block found", text)
    design = m.group(1).strip()
    if not design:
        return ParseError("<<<DESIGN>>> block was empty", text)
    complexity = _parse_complexity_block(cleaned)
    return ArchitectResult(design_md=design, raw=text, complexity=complexity)


def _parse_complexity_block(cleaned_text: str) -> Optional[dict]:
    """Extract and validate the <<<COMPLEXITY>>> block.

    Returns None on missing/malformed input — the orchestrator handles the
    None case by falling back to the env-default implementer. We deliberately
    do NOT raise ParseError for a missing complexity block: backwards-compat
    matters more than enforcing a brand-new field on the architect, and the
    fallback is safe.
    """
    m = _COMPLEXITY_RE.search(cleaned_text)
    if not m:
        return None
    body = m.group(1).strip()
    if not body:
        return None

    fields = {}
    for line in body.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip().lower()] = value.strip()

    tier = (fields.get("tier") or "").lower()
    if tier not in TIER_TO_IMPLEMENTER:
        # Unknown tier — return what we got but flag for telemetry
        tier = ""
    rec = (fields.get("recommended agent") or fields.get("recommended_agent") or "").strip()
    if rec and rec not in ALLOWED_IMPLEMENTER_AGENTS:
        rec = ""  # silently drop — _select_implementer_agent will fall back
    justification = (fields.get("justification") or "").strip()

    return {
        "tier": tier or None,
        "recommended_agent": rec or None,
        "justification": justification or None,
    }


_KNOWN_CODE_LANG_TAGS = frozenset({
    "python", "py",
    "ts", "typescript", "tsx",
    "js", "javascript", "jsx", "mjs", "cjs",
    "json", "yaml", "yml", "toml", "ini", "xml",
    "html", "css", "scss", "sass",
    "rust", "rs", "go", "java", "kotlin", "kt",
    "swift", "c", "cpp", "cc", "h", "hpp", "objc", "m",
    "ruby", "rb", "php", "perl", "pl",
    "sh", "bash", "zsh", "fish",
    "sql", "graphql", "proto",
    "dockerfile", "makefile", "cmake",
    "lua", "r", "scala", "haskell", "hs", "elixir", "ex", "erlang", "erl",
})


def _strip_markdown_fence(content: str) -> str:
    """Remove a markdown ``` code fence wrapping the file body.

    The implementer is told to write raw file content, but it sometimes
    formats responses as a fenced code block — leaving the fence in
    causes SyntaxError on import.

    Three cases handled:
      1. Fully wrapped: open fence on first line, close fence on last
         line. Both stripped.
      2. Leading fence only (model forgot to close, or output truncated
         by max_tokens). Stripped only when the language tag is a known
         code lang — a bare ``` with no tag is left alone because a
         markdown doc could legitimately open with one.
      3. No fences anywhere: content returned unchanged.

    Trailing-fence-only (close without open) is left alone — that
    pattern shows up in legitimate `.md` files and shouldn't be
    truncated.
    """
    s = content.strip()
    if not s.startswith("```"):
        return content

    first_nl = s.find("\n")
    if first_nl == -1:
        return content  # single line starting with ``` — too ambiguous

    fully_wrapped = s.endswith("```") and first_nl < len(s) - 3
    if fully_wrapped:
        body = s[first_nl + 1 : -3].rstrip()
        return body + "\n" if not body.endswith("\n") else body

    # Leading fence only. Strip when the language tag clearly identifies
    # this as a code wrap, not legitimate markdown content.
    opener = s[:first_nl].strip()       # e.g. "```python"
    lang = opener[3:].strip().lower()   # everything after the backticks
    if lang in _KNOWN_CODE_LANG_TAGS:
        body = s[first_nl + 1 :].rstrip()
        return body + "\n" if not body.endswith("\n") else body

    # Bare ``` with no language, or unrecognized tag — preserve as-is.
    return content


def _dedupe_files_last_wins(
    pairs: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[str]]:
    """Collapse duplicate paths to last-write-wins; return (deduped, dup_paths).

    Implementers occasionally emit multiple <<<FILE: path>>> blocks for the
    same path — usually a self-correction where the second block is the
    "real" output. Picking the last occurrence matches that intent, but we
    surface the duplicates so the orchestrator can log them for debugging
    "the file's content surprises me" reports.
    """
    indexed: dict[str, int] = {}
    for i, (path, _content) in enumerate(pairs):
        indexed[path] = i  # last occurrence wins
    deduped = [pairs[i] for i in sorted(indexed.values())]
    counts = Counter(path for path, _ in pairs)
    duplicates = sorted(path for path, n in counts.items() if n > 1)
    return deduped, duplicates


def parse_implementer_response(text: str) -> ImplementerResult | ParseError:
    cleaned = _strip_thinking(text)
    matches = _FILE_RE.findall(cleaned)
    if not matches:
        return ParseError("No <<<FILE: path>>>…<<<END_FILE>>> blocks found", text)
    raw_files = [(path.strip(), _strip_markdown_fence(content)) for path, content in matches]
    files, duplicates = _dedupe_files_last_wins(raw_files)
    if duplicates:
        logger.warning(
            "implementer response contained duplicate file paths "
            "(last-write-wins applied): %s",
            duplicates,
        )
    return ImplementerResult(files=files, raw=text, duplicate_paths=duplicates)


def parse_reviewer_response(text: str) -> ReviewerResult | ParseError:
    cleaned = _strip_thinking(text)

    # Extract test files (same FILE marker as implementer)
    raw_test_files = [(p.strip(), c) for p, c in _FILE_RE.findall(cleaned)]
    test_files, dup_paths = _dedupe_files_last_wins(raw_test_files)
    if dup_paths:
        logger.warning(
            "reviewer response contained duplicate test paths "
            "(last-write-wins applied): %s",
            dup_paths,
        )

    # Extract review report
    review_match = _REVIEW_RE.search(cleaned)
    if not review_match:
        return ParseError("No <<<REVIEW>>>…<<<END_REVIEW>>> block found", text)
    review_md = review_match.group(1).strip()

    # Extract verdict
    verdict_match = _VERDICT_RE.search(review_md)
    verdict = verdict_match.group(1).upper() if verdict_match else "FAIL"

    # Layer 3 (anti-hallucination guard): the reviewer must back its verdict
    # with structured evidence — acceptance-criterion → test mapping for PASS,
    # or file:line defects for FAIL. Empty/missing evidence downgrades the
    # verdict to FAIL with a diagnostic appended to the review_md so the
    # implementer retry sees why.
    evidence_match = _VERDICT_EVIDENCE_RE.search(review_md)
    evidence_body = evidence_match.group(1).strip() if evidence_match else ""
    if verdict == "PASS" and not evidence_body:
        verdict = "FAIL"
        review_md += (
            "\n\n---\n\n"
            "**[orchestrator guard]** Verdict downgraded to FAIL: the reviewer "
            "stated PASS but did not provide the required `### Verdict Evidence` "
            "block (acceptance-criterion → test-function mapping). An unanchored "
            "verdict is treated as a hallucination and rejected."
        )

    return ReviewerResult(
        test_files=test_files,
        review_md=review_md,
        verdict=verdict,
        raw=text,
        duplicate_paths=dup_paths,
    )


# ── User message builders ───────────────────────────────────────────────────

def build_architect_message(spec_md: str,
                            rejection_notes: str | None = None) -> list[dict[str, str]]:
    user_parts: list[str] = []
    # On a re-run (design-review rejection or supervisor design-revision), the
    # prior design led to the failure below. The implementer builds the design
    # faithfully, so a recurring failure means the DESIGN is wrong/under-specified
    # — fix it here rather than regenerating the same document.
    if rejection_notes:
        user_parts.append(
            "## Your prior design was rejected — revise it\n\n"
            "A previous version of THIS design produced the problem below. The "
            "implementer follows your design exactly, so the defect is in the "
            "DESIGN, not the code. Find the flawed or under-specified part, fix "
            "it, and where the failure encodes a missing INVARIANT, state that "
            "invariant explicitly (rule 8). Do not simply re-emit the same design.\n\n"
            f"{rejection_notes.strip()}\n\n---\n\n"
        )
    user_parts.append(
        "## Specification\n\n"
        f"{spec_md}\n\n---\n\n"
        "Your task: produce a complete architecture design for this project. "
        "Output exactly one <<<DESIGN>>>…<<<END>>> block as instructed."
    )
    return [
        {"role": "system", "content": ARCHITECT_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


DESIGN_REVIEW_SYSTEM_PROMPT = textwrap.dedent("""\
    You are a DESIGN REVIEWER. You are given a specification and a proposed
    design document (produced by an architect). Find defects in the DESIGN
    before any code is written: the implementer follows the design exactly, so a
    flaw here becomes a bug in every implementation and even in the tests.

    Check, in priority order:
    - CORRECTNESS: are any prescribed algorithms or formulas wrong, or stated so
      concretely that a faithful implementation would fail the spec's own
      acceptance criteria? Actively look for a counterexample — pick boundary
      inputs (max sizes, zero, repetition, collisions, wrap-around) and trace
      whether the design still satisfies each criterion. If you can construct an
      input where a prescribed formula violates a stated criterion, that is a
      FAIL, and you must give that input.
    - COVERAGE: does the design address EVERY acceptance criterion in the spec?
      Name any criterion with no corresponding design element.
    - TESTABILITY: are the acceptance criteria stated as concrete, testable
      assertions? Flag vague ones.
    - SCOPE: does the design add or drop scope versus the spec?

    Respond with EXACTLY this format and nothing else:

    <<<DESIGN_REVIEW>>>
    VERDICT: PASS or FAIL
    <if FAIL: a concrete, numbered list of defects. For each, name the flaw, the
    criterion/algorithm it affects, a counterexample input where relevant, and
    what the architect must change. Be specific enough that the architect can fix
    the design from your notes alone.>
    <<<END_DESIGN_REVIEW>>>

    Default to PASS if the design is sound. Do NOT fail for style or wording —
    only for defects that would cause the implementation to miss the spec.
    """)


def build_design_review_message(spec_md: str, design_md: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": DESIGN_REVIEW_SYSTEM_PROMPT},
        {"role": "user", "content": (
            "## Specification\n\n"
            f"{spec_md}\n\n---\n\n"
            "## Proposed design\n\n"
            f"{design_md}\n\n---\n\n"
            "Review the design for defects that would make the implementation "
            "miss the spec. Output exactly one "
            "<<<DESIGN_REVIEW>>>…<<<END_DESIGN_REVIEW>>> block."
        )},
    ]


def parse_design_review(raw: str) -> tuple[str, str]:
    """Parse a design-review response into ('PASS'|'FAIL', notes).

    Fail-OPEN: if the verdict can't be found, return ('PASS', '') so a flaky or
    malformed review never blocks the pipeline. Brackets are matched 1-3 wide to
    tolerate the same marker drift the other parsers handle.
    """
    m = re.search(r"<{1,3}DESIGN_REVIEW>{1,3}(.*?)<{1,3}(?:END_DESIGN_REVIEW|END)>{1,3}",
                  raw, re.DOTALL | re.IGNORECASE)
    body = m.group(1) if m else raw
    vm = re.search(r"VERDICT:\s*(PASS|FAIL)", body, re.IGNORECASE)
    if not vm:
        return "PASS", ""
    if vm.group(1).upper() == "PASS":
        return "PASS", ""
    return "FAIL", body[vm.end():].strip()


def build_implementer_message(
    spec_md: str,
    design_md: str,
    rejection_notes: str | None = None,
    clarifications: list[str] | None = None,
) -> list[dict[str, str]]:
    user_parts: list[str] = []
    # Clarifications go BEFORE the spec so the implementer reads them as the
    # first operator-authored content. They're hard requirements at the same
    # authority as the spec — see IMPLEMENTER_SYSTEM_PROMPT rule 9. Defensive
    # injection here is the floor: even if the planner forgets to embed them
    # in plan.yaml, the orchestrator-supplied list still surfaces them.
    if clarifications:
        user_parts.append("## Operator clarifications\n\n")
        user_parts.append(
            "These answers from the operator are HARD REQUIREMENTS — same "
            "authority as the spec itself. Apply each item literally; do "
            "not override with a default or 'more idiomatic' alternative.\n\n"
        )
        for i, item in enumerate(clarifications, start=1):
            user_parts.append(f"{i}. {item}\n")
        user_parts.append("\n")
    user_parts.extend([
        "## Specification\n\n",
        spec_md,
        "\n\n## Architecture Design\n\n",
        design_md,
    ])
    if rejection_notes:
        user_parts.extend([
            "\n\n## Previous Attempt — Review Feedback\n\n",
            rejection_notes,
            "\n\n---\n\n"
            "Your task: fix the issues identified above and re-implement. "
            "Output <<<FILE: path>>>…<<<END_FILE>>> blocks for EVERY file. "
            "You must output ALL files again (complete files, not diffs).",
        ])
    else:
        user_parts.extend([
            "\n\n---\n\n"
            "Your task: implement ALL components described in the design. "
            "Output <<<FILE: path>>>…<<<END_FILE>>> blocks for every file. "
            "Paths are relative to the project workspace.",
        ])
    return [
        {"role": "system", "content": IMPLEMENTER_SYSTEM_PROMPT},
        {"role": "user", "content": "".join(user_parts)},
    ]


# ── Manifest mode (#4): manifest → per-file generation ───────────────────────
#
# The single-call implementer must emit the whole repo in one response, capped by
# max_tokens. For large designs that truncates. Manifest mode splits it:
#   1. one cheap call returns the FILE MANIFEST (paths + purpose + exports, in
#      dependency order) — bounded output, no code;
#   2. one bounded call per file emits just that file, given the design, the full
#      manifest, and summaries of the already-written files (for cross-file
#      consistency). Total output is unbounded; each call stays small.

MANIFEST_SYSTEM_PROMPT = textwrap.dedent("""\
    You are the IMPLEMENTER in its planning step. Given the specification and the
    architecture design, output the FILE MANIFEST: the exact list of files you
    will create. You write NO code in this step.

    # Output format

    Respond with EXACTLY ONE block. No preamble, no prose outside the markers.

    <<<MANIFEST>>>
    relative/path/one.ext | one-line purpose | key exports / types / functions
    relative/path/two.ext | one-line purpose | key exports
    ...
    <<<END_MANIFEST>>>

    # Rules
    1. One file per line, fields separated by ` | ` (space-pipe-space):
       `path | purpose | exports`. The exports field may be empty but keep the
       two separators.
    2. DEPENDENCY ORDER: list shared contracts / type modules FIRST, then code
       that imports them, then entrypoints, then config and docs LAST. A file
       must come after everything it imports.
    3. List EVERY file the design calls for — source, config (package.json,
       tsconfig, etc.) and docs (README). Do not skip "trivial" files.
    4. Paths are relative to the project workspace root (no leading /).
    5. No code, no markdown fences, no commentary — only the manifest block.
    """)

PER_FILE_SYSTEM_PROMPT = textwrap.dedent("""\
    You are the IMPLEMENTER writing ONE file of a larger project. You are given
    the spec, the architecture design, the full file manifest, and summaries of
    the files already written. Produce the COMPLETE content of the single
    requested file, consistent with the design and the existing files.

    # Output format

    Respond with EXACTLY ONE file block, nothing else:

    <<<FILE: relative/path/to/file.ext>>>
    <complete file content — the ENTIRE file, not a diff or snippet>
    <<<END_FILE>>>

    # Rules
    1. Output ONLY the one requested file, at the exact path given. Never emit
       another file or wrap the content in markdown ``` fences.
    2. Stay consistent with the already-written files: import the exports they
       actually provide; do not invent symbols or alternate signatures.
    3. TypeScript: `any` is forbidden — use `unknown` + narrowing or a real type.
    4. Pin dependencies to exact versions (no `^`, `~`, `>=`) in any manifest file.
    5. Write complete, working content — no TODO placeholders, no elisions.
    """)


@dataclass
class ManifestEntry:
    path: str
    purpose: str
    exports: str = ""


@dataclass
class ManifestResult:
    entries: list[ManifestEntry]
    raw: str


_MANIFEST_RE = re.compile(
    r"<{1,3}MANIFEST>{1,3}\s*(.*?)\s*<{1,3}END_MANIFEST>{1,3}",
    re.DOTALL | re.IGNORECASE,
)
# Leading list markers ("1.", "- ", "* ") the model sometimes prefixes.
_LIST_MARKER_RE = re.compile(r"^\s*(?:\d+[.)]|[-*])\s*")
# Interface-bearing lines (imports + declarations) for the written-file summary.
_SIGNATURE_RE = re.compile(
    r"^\s*(?:export|import|from|def|class|func|function|interface|type|enum|"
    r"public|pub|impl|trait|struct|module)\b"
)


def parse_manifest_response(text: str) -> ManifestResult | ParseError:
    cleaned = _strip_thinking(text)
    m = _MANIFEST_RE.search(cleaned)
    if not m:
        return ParseError("No <<<MANIFEST>>>…<<<END_MANIFEST>>> block found", text)
    entries: list[ManifestEntry] = []
    seen: set[str] = set()
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        path = _LIST_MARKER_RE.sub("", parts[0]).strip().strip("`").lstrip("/").strip()
        if not path:
            continue
        purpose = parts[1] if len(parts) > 1 else ""
        exports = parts[2] if len(parts) > 2 else ""
        if path in seen:
            continue
        seen.add(path)
        entries.append(ManifestEntry(path=path, purpose=purpose, exports=exports))
    if not entries:
        return ParseError("<<<MANIFEST>>> block contained no file entries", text)
    return ManifestResult(entries=entries, raw=text)


# Mode-selection signals, kept SEPARATE from estimate_design_file_count (which
# sizes the output budget and whose exact counts are pinned by tests). The file
# counter only matches paths with a known code extension, so it scores 0 for
# designs written in an unlisted language, as an extension-less file tree, or in
# prose — and those then wrongly took the single-call path and truncated. These
# add the missing signals. The failure is asymmetric: a missed large design
# truncates a whole repo into one capped call, while a small design wrongly sent
# to manifest mode just does a little more orchestration and still emits correct
# output — so mode selection leans toward manifest, bounded by the threshold.

# Broad superset of the budget regex's extension list — many more languages and
# config formats, used ONLY for the mode decision.
_DESIGN_UNIT_PATH_RE = re.compile(
    r"[\w./-]+\.(?:ts|tsx|js|jsx|mjs|cjs|json|py|pyi|rs|go|java|kt|kts|swift|"
    r"c|h|cc|cpp|hpp|cs|css|scss|sass|less|html|htm|vue|svelte|astro|toml|yaml|"
    r"yml|sh|bash|zsh|sql|proto|rb|erb|rake|php|dart|ex|exs|lua|tf|tfvars|xml|"
    r"graphql|gql|prisma|ini|cfg|conf|env|txt|md|mdx|gradle|groovy|scala|clj|"
    r"r|jl|pl|pm|hs|ml|fs|vb|ipynb|dockerfile|makefile|cmake)\b",
    re.IGNORECASE,
)

# Files that carry no extension but are unmistakably build units.
_BARE_FILENAME_RE = re.compile(
    r"(?:^|[\s`/])("
    r"Dockerfile|Makefile|Gemfile|Rakefile|Procfile|Jenkinsfile|Vagrantfile|"
    r"Brewfile|Caddyfile|Justfile|Containerfile|CMakeLists\.txt|"
    r"\.gitignore|\.dockerignore|\.env(?:\.\w+)?"
    r")(?=$|[\s`,)])",
    re.MULTILINE,
)

# A drawn file tree: lines carrying a box-drawing/ASCII tree connector.
_TREE_NODE_RE = re.compile(r"^\s*(?:[│|]\s*)*[├└][─-]{1,2}\s*\S", re.MULTILINE)

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}
# "ten modules", "12 services", "eight components" — an explicit count of build
# units stated in prose, the one signal that survives when no paths are drawn.
_PROSE_COUNT_RE = re.compile(
    r"\b(\d{1,3}|" + "|".join(_NUMBER_WORDS) + r")\s+"
    r"(?:distinct\s+|separate\s+|small\s+)?"
    r"(?:modules?|files?|components?|services?|classes?|endpoints?|handlers?|"
    r"adapters?|packages?|screens?|pages?|routes?|controllers?|models?|widgets?)\b",
    re.IGNORECASE,
)


def _prose_unit_count(design_md: str) -> int:
    """Largest explicit '<n> <build-noun>' count stated in the design's prose."""
    best = 0
    for m in _PROSE_COUNT_RE.finditer(design_md or ""):
        tok = m.group(1).lower()
        n = int(tok) if tok.isdigit() else _NUMBER_WORDS.get(tok, 0)
        best = max(best, n)
    return best


def estimate_design_unit_count(design_md: str) -> int:
    """Estimate how many build units a design describes, for mode selection only.

    Union of several signals so a design the file-path regex scores 0 (unlisted
    language, extension-less tree, prose) is still recognised as large:
      * named files — broad-extension paths plus bare build files (Dockerfile…),
      * file-tree nodes — lines drawn with tree connectors,
      * an explicit prose count — "ten modules", "12 services".
    Takes the max of the structural and prose signals rather than summing, so a
    tree of .ts files isn't double-counted. NOT used for budget sizing.
    """
    text = design_md or ""
    named = {m.group(0).strip("./").lower() for m in _DESIGN_UNIT_PATH_RE.finditer(text)}
    named |= {m.group(1).lower() for m in _BARE_FILENAME_RE.finditer(text)}
    tree_nodes = len(_TREE_NODE_RE.findall(text))
    structural = max(len(named), tree_nodes)
    return max(structural, _prose_unit_count(text))


def use_manifest_mode(design_md: str) -> bool:
    """Whether to generate this design file-by-file rather than in one call."""
    if IMPLEMENTER_MODE == "manifest":
        return True
    if IMPLEMENTER_MODE == "single":
        return False
    # OR the budget file-count with the broader unit estimate: either crossing
    # the threshold means "too big for one capped call". The unit estimate is a
    # superset for path-based designs, so this never lowers the count the tests
    # pin for .ts-file designs — it only adds coverage for the shapes the file
    # regex misses.
    return (
        estimate_design_file_count(design_md) >= MANIFEST_FILE_THRESHOLD
        or estimate_design_unit_count(design_md) >= MANIFEST_FILE_THRESHOLD
    )


def summarize_written_files(
    files: list[tuple[str, str]],
    *,
    max_sig_lines: int = 24,
    max_total_chars: int = 12000,
) -> str:
    """Compact interface summary of already-written files for the per-file prompt.

    Emits each file's import/declaration lines (its public surface) rather than
    full bodies, so later files can import real symbols without the prompt
    ballooning. Bounded in both per-file line count and total size.
    """
    if not files:
        return "(none yet — this is the first file)"
    sections: list[str] = []
    total = 0
    for path, content in files:
        sig_lines: list[str] = []
        for ln in content.splitlines():
            if _SIGNATURE_RE.match(ln):
                sig_lines.append(ln.rstrip())
                if len(sig_lines) >= max_sig_lines:
                    break
        body = "\n".join(sig_lines) if sig_lines else "(no exported symbols detected)"
        section = f"### {path}\n{body}"
        if total + len(section) > max_total_chars:
            sections.append("… (further files omitted from summary)")
            break
        sections.append(section)
        total += len(section)
    return "\n\n".join(sections)


def build_manifest_message(
    spec_md: str,
    design_md: str,
    clarifications: list[str] | None = None,
) -> list[dict[str, str]]:
    parts: list[str] = []
    if clarifications:
        parts.append("## Operator clarifications (hard requirements)\n\n")
        for i, item in enumerate(clarifications, start=1):
            parts.append(f"{i}. {item}\n")
        parts.append("\n")
    parts.extend([
        "## Specification\n\n", spec_md,
        "\n\n## Architecture Design\n\n", design_md,
        "\n\n---\n\nProduce the FILE MANIFEST for this project. Output exactly one "
        "<<<MANIFEST>>>…<<<END_MANIFEST>>> block, files in dependency order.",
    ])
    return [
        {"role": "system", "content": MANIFEST_SYSTEM_PROMPT},
        {"role": "user", "content": "".join(parts)},
    ]


def build_per_file_message(
    spec_md: str,
    design_md: str,
    manifest: list[ManifestEntry],
    target: ManifestEntry,
    written_summary: str,
    clarifications: list[str] | None = None,
    rejection_notes: str | None = None,
) -> list[dict[str, str]]:
    manifest_block = "\n".join(
        f"- {e.path} — {e.purpose}" + (f"  [exports: {e.exports}]" if e.exports else "")
        for e in manifest
    )
    parts: list[str] = []
    if clarifications:
        parts.append("## Operator clarifications (hard requirements)\n\n")
        for i, item in enumerate(clarifications, start=1):
            parts.append(f"{i}. {item}\n")
        parts.append("\n")
    parts.extend([
        "## Specification\n\n", spec_md,
        "\n\n## Architecture Design\n\n", design_md,
        "\n\n## File Manifest (the full project)\n\n", manifest_block,
        "\n\n## Files already written (their interfaces)\n\n", written_summary,
        f"\n\n---\n\nWrite ONLY this one file now:\n\n"
        f"**{target.path}** — {target.purpose}\n",
    ])
    if target.exports:
        parts.append(f"\nExpected exports / contents: {target.exports}\n")
    if rejection_notes:
        parts.extend([
            "\n## Reviewer feedback to address in this file\n\n",
            rejection_notes, "\n",
        ])
    parts.append(
        f"\nOutput exactly one <<<FILE: {target.path}>>> … <<<END_FILE>>> block."
    )
    return [
        {"role": "system", "content": PER_FILE_SYSTEM_PROMPT},
        {"role": "user", "content": "".join(parts)},
    ]


# ── Deterministic boilerplate normalization (#3) ─────────────────────────────
#
# Some boilerplate the reviewer checks is fully deterministic — there is exactly
# one correct form — so it should not ride on stochastic generation. The reviewer
# rejects unpinned dependencies (`^`, `~`, `>=` ranges) on sight, and that is the
# single most common, most mechanical FAIL. We pin them ourselves after
# generation rather than hoping every implementer model gets it right every time.

def pin_version(spec: str) -> str:
    """Pin a single dependency version spec to an exact version.

    `^1.2.3` / `~1.2.3` / `>=1.2.3` → `1.2.3`. Leaves already-exact versions,
    and non-semver specs (URLs, `workspace:*`, `*`, git refs) untouched.
    """
    s = spec.strip()
    if s[:1] in "^~><=":
        stripped = s.lstrip("^~><= ")
        if stripped[:1].isdigit():  # only pin when a concrete version remains
            return stripped
    return s


def _pin_package_json(content: str) -> tuple[str, int]:
    try:
        data = json.loads(content)
    except ValueError:
        return content, 0  # not valid JSON — leave it for the reviewer to flag
    if not isinstance(data, dict):
        return content, 0
    changed = 0
    for key in ("dependencies", "devDependencies",
                "peerDependencies", "optionalDependencies"):
        deps = data.get(key)
        if not isinstance(deps, dict):
            continue
        for name, ver in list(deps.items()):
            if isinstance(ver, str):
                pinned = pin_version(ver)
                if pinned != ver:
                    deps[name] = pinned
                    changed += 1
    if changed == 0:
        return content, 0
    return json.dumps(data, indent=2) + "\n", changed


def normalize_boilerplate(
    files: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[str]]:
    """Deterministically fix boilerplate the reviewer checks. Returns the
    (possibly rewritten) file list and a list of human-readable change notes.

    Currently: exact-pin dependency ranges in every package.json.
    """
    out: list[tuple[str, str]] = []
    notes: list[str] = []
    for path, content in files:
        if os.path.basename(path) == "package.json":
            new_content, changed = _pin_package_json(content)
            if changed:
                notes.append(
                    f"{path}: pinned {changed} dependency range(s) to exact versions"
                )
                out.append((path, new_content))
                continue
        out.append((path, content))
    return out, notes


# ── Deterministic TypeScript `any`-type scan (for the reviewer) ──────────────
#
# The reviewer's model-written no-`any` test was a naive substring grep that
# false-FAILed on the word "any" in comments and string literals (e.g.
# `// handle any error`, `id: 'hdr.any'`) — killing otherwise-good runs. We scan
# the source ourselves — stripping comments and string/template literals, then
# matching `any` only in TYPE position — and hand the reviewer the authoritative
# list so its verdict rests on real violations, not text coincidences.

def _blank_ts_comments_and_strings(src: str) -> str:
    """Replace comment and string/template-literal CONTENT with spaces, keeping
    newlines so line numbers are preserved. A small hand-rolled scanner — enough
    to stop `any` inside comments/strings from matching the type regex."""
    out: list[str] = []
    i, n = 0, len(src)
    state: Optional[str] = None  # 'line' | 'block' | "'" | '"' | '`'
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if state is None:
            if c == "/" and nxt == "/":
                state = "line"; out.append("  "); i += 2; continue
            if c == "/" and nxt == "*":
                state = "block"; out.append("  "); i += 2; continue
            if c in "'\"`":
                state = c; out.append(" "); i += 1; continue
            out.append(c); i += 1; continue
        if state == "line":
            if c == "\n":
                state = None; out.append("\n")
            else:
                out.append(" ")
            i += 1; continue
        if state == "block":
            if c == "*" and nxt == "/":
                state = None; out.append("  "); i += 2; continue
            out.append("\n" if c == "\n" else " "); i += 1; continue
        # inside a string / template literal
        if c == "\\":
            out.append("  "); i += 2; continue  # skip the escaped char
        if c == state:
            state = None; out.append(" "); i += 1; continue
        out.append("\n" if c == "\n" else " "); i += 1; continue
    return "".join(out)


# `any` used as a TYPE: annotation, cast, generic arg, array, union, intersection.
_ANY_TYPE_RE = re.compile(
    r":\s*any\b"                 # : any
    r"|\bas\s+any\b"             # as any
    r"|<\s*any\b"                # <any  (generic / cast open)
    r"|\bany\s*\[\]"             # any[]
    r"|\bany\s*>"                # any>  (generic close)
    r"|,\s*any\b"                # , any (generic arg)
    r"|\|\s*any\b|\bany\s*\|"    # union with any
    r"|&\s*any\b|\bany\s*&"      # intersection with any
)


def find_any_type_violations(source: str) -> list[tuple[int, str]]:
    """Real TypeScript `any`-TYPE usages in *source*, as (line_no, line_text).

    Comments and string/template literals are excluded first, so "any" inside a
    comment, a string, or an identifier (e.g. 'hdr.any', `// handle any error`)
    is NOT reported — only the `any` type itself.
    """
    blanked = _blank_ts_comments_and_strings(source)
    orig = source.splitlines()
    out: list[tuple[int, str]] = []
    for idx, line in enumerate(blanked.splitlines()):
        if _ANY_TYPE_RE.search(line):
            out.append((idx + 1, orig[idx] if idx < len(orig) else ""))
    return out


def scan_any_violations(files: list[tuple[str, str]]) -> list[str]:
    """`any`-type violations across the TS source files, as 'path:line: text'."""
    results: list[str] = []
    for path, content in files:
        if path.endswith((".ts", ".tsx")) and not path.endswith(".d.ts"):
            for ln, text in find_any_type_violations(content):
                results.append(f"{path}:{ln}: {text.strip()[:120]}")
    return results


def build_reviewer_message(
    spec_md: str,
    design_md: str,
    code_files: list[tuple[str, str]],
    test_framework: str = "pytest",
    rejection_notes: Optional[str] = None,
) -> list[dict[str, str]]:
    file_sections = []
    for path, content in code_files:
        file_sections.append(f"### {path}\n```\n{content}\n```\n")

    # Hand the reviewer a deterministic, comment/string-aware `any`-type scan so
    # its no-`any` verdict can't false-FAIL on the word "any" in a comment or
    # string literal (the spec_5a87fd64 failure).
    any_scan = scan_any_violations(code_files)
    if any_scan:
        any_section = (
            "\n## Authoritative `any`-type scan\n\n"
            "These are the ONLY real TypeScript `any` TYPE usages in the source — "
            "comments, string literals, and identifiers are already excluded. Base "
            "your no-`any` verdict (and any test you write) strictly on THIS list; "
            "do NOT substring-search the source for the word \"any\":\n\n"
            + "\n".join(f"- {v}" for v in any_scan) + "\n"
        )
    else:
        any_section = (
            "\n## Authoritative `any`-type scan\n\n"
            "No real `any` TYPE usages found (comments, strings, and identifiers "
            "excluded). Do NOT FAIL the no-`any` rule: the word \"any\" inside a "
            "comment or string literal (e.g. `// handle any error`, `'hdr.any'`) "
            "is NOT a violation.\n"
        )

    retry_block = ""
    if rejection_notes:
        retry_block = (
            "\n\n## Prior reviewer attempt failed — fix this before resubmitting\n\n"
            f"{rejection_notes}\n\n"
            "Apply this feedback in your next test file. Do not repeat the same "
            "defect. If the feedback names a missing import, add it; if it names "
            "a broken assertion, fix it.\n"
        )

    return [
        {"role": "system", "content": REVIEWER_SYSTEM_PROMPT},
        {"role": "user", "content": (
            "## Specification\n\n"
            f"{spec_md}\n\n"
            "## Architecture Design\n\n"
            f"{design_md}\n\n"
            "## Implementation Files\n\n"
            + "\n".join(file_sections)
            + any_section
            + retry_block
            + f"\n\n---\n\n"
            f"Test framework: {test_framework}\n\n"
            "Your task: write test files and review the implementation. "
            "Output <<<FILE: test_*.py>>>…<<<END_FILE>>> blocks for test files, "
            "then a <<<REVIEW>>>…<<<END_REVIEW>>> block with your verdict."
        )},
    ]


SYNTHESIS_SYSTEM_PROMPT = textwrap.dedent("""\
    You are a code-synthesis agent. Your job is to merge the correct
    behaviors of multiple prior attempts at the same specification into a
    single final implementation.

    The rotation chain is designed to elicit DIFFERENT bug profiles from
    different models — e.g. one attempt nails the type checks but misses
    edge-case ordering; another nails the structure but misses an
    isinstance check. Your task is to take the union of the correct parts.

    ## Inputs you'll receive
    - The original specification + architecture design
    - Each prior attempt's source code
    - Each prior attempt's test result summary (which tests passed, which
      failed). Trust these; they are pytest output, not LLM judgment.

    ## Output rules
    - Produce a single complete implementation as <<<FILE: path>>>…
      <<<END_FILE>>> blocks for EVERY file required by the spec. No diffs.
    - Prefer behaviors that PASSED across multiple attempts.
    - Where attempts disagree on edge-case handling, choose the version
      whose tests for that edge case actually passed. If none passed,
      synthesize a correct version yourself.
    - Do not introduce new bugs. If you can't decide between two
      approaches, pick the one with the better overall test pass rate.
    - Maintain the file/directory layout the architecture design called
      for; do not invent new structure.
    - Keep test files (`tests/test_*.py`) — write them yourself if the
      attempts disagree on which tests should exist. Tests are part of
      the deliverable.
    """)


def build_synthesis_message(
    spec_md: str,
    design_md: str,
    attempts: list[dict],
) -> list[dict[str, str]]:
    """Build a single synthesis prompt that gives the model the full
    rotation history (code + per-attempt test outcome) and asks for a
    merged best version.

    Each attempt dict has keys: retry (int), agent (str), test_summary
    (str — last ~1500 chars of pytest output), files (dict of relpath→content).
    """
    parts = [
        "## Specification\n\n", spec_md,
        "\n\n## Architecture Design\n\n", design_md,
        "\n\n## Prior Attempts\n\n",
    ]
    for att in attempts:
        parts.append(
            f"### Attempt retry={att['retry']} (agent={att['agent']})\n\n"
        )
        if att.get("test_summary"):
            parts.append("#### Test result\n\n```\n")
            parts.append(att["test_summary"])
            parts.append("\n```\n\n")
        for relpath, content in att.get("files", {}).items():
            parts.append(f"#### {relpath}\n\n```\n{content}\n```\n\n")
    parts.append(
        "---\n\n"
        "Synthesize a single correct implementation by taking the union of "
        "behaviors that passed tests across the attempts above. Output "
        "<<<FILE: path>>>…<<<END_FILE>>> blocks for every file required "
        "by the design. Include test files."
    )
    return [
        {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
        {"role": "user", "content": "".join(parts)},
    ]


# ── Test runner ──────────────────────────────────────────────────────────────

def _sandbox_available() -> bool:
    """True if we can sandbox test execution with bubblewrap on this host."""
    return sys.platform.startswith("linux") and shutil.which("bwrap") is not None


def seccomp_preflight() -> "tuple[bool, str]":
    """(fully_sandboxed, detail) — for a LOUD one-time daemon-startup check.

    A missing libseccomp used to surface as one log line per test run,
    buried mid-spec: bwrap kept running but the kernel-CVE surface the
    filter targets (io_uring, userfaultfd, ptrace) was silently re-exposed
    (DEV-155). The daemon logs this prominently at startup and can be made
    to refuse via CODING_MODEL_REQUIRE_SECCOMP=1.
    """
    if not _sandbox_available():
        return False, ("bwrap (bubblewrap) not found — local test runs will "
                       "refuse unless CODING_MODEL_ALLOW_UNSANDBOXED_TESTS=1")
    fd = seccomp_filter.build_seccomp_bpf_fd()
    if fd is None:
        return False, ("libseccomp unavailable (install python3-seccomp) — "
                       "bwrap will run WITHOUT the kernel-syscall denylist")
    try:
        os.close(fd)
    except OSError:
        pass
    return True, "bwrap+seccomp"


# Mountpoint (inside the sandbox) where the Node toolchain is bound for the
# `node_test` framework. Must be TOP-LEVEL: the baseline binds mount /opt
# read-only, so bwrap cannot mkdir a bind target nested under it.
_SANDBOX_NODE_MOUNT = "/coding-model-node"


def _resolve_sandbox_node_root() -> Optional[Path]:
    """Locate a Node install root to bind into the sandbox for `node --test`.

    Prefers the explicit CODING_MODEL_SANDBOX_NODE_ROOT (required in practice:
    the orchestrator's systemd PATH usually has no Node, and nvm installs it
    under /home, which the sandbox masks with tmpfs). Falls back to the Node the
    orchestrator process itself can see on PATH. Returns None if no usable Node
    root is found — `node_test` then fails with a clear diagnostic.
    """
    explicit = os.getenv("CODING_MODEL_SANDBOX_NODE_ROOT", "").strip()
    if explicit:
        root = Path(explicit).expanduser()
        return root if (root / "bin" / "node").exists() else None
    node = shutil.which("node")
    if node:
        # <root>/bin/node  ->  <root>
        return Path(node).resolve().parent.parent
    return None


SANDBOX_NODE_ROOT = _resolve_sandbox_node_root()


def _wrap_in_sandbox(
    cmd: list[str],
    spec_dir: Path,
    seccomp_fd: Optional[int] = None,
) -> list[str]:
    """Wrap `cmd` in a bubblewrap sandbox.

    The sandbox denies the LLM-generated tests access to anything outside the
    spec workspace:

      - `--unshare-all` creates fresh user/ipc/pid/uts/cgroup/net namespaces,
        so the tests cannot see host processes and have no network (not even
        loopback).
      - `/home` and `/root` are masked with tmpfs so secrets (`.env`, `.ssh`,
        API tokens, browser profiles, etc.) are invisible.
      - `/usr`, `/etc`, `/bin`, `/lib*`, `/opt` are bound read-only so Python
        and pytest can still import system libraries.
      - The venv holding the running Python + pytest is bound read-only.
      - `spec_dir` is bound read-write so pytest can create `.pytest_cache`
        and tests can write their own fixtures.
      - `--clearenv` strips inherited env vars — tests see a minimal,
            predictable environment.
      - When *seccomp_fd* is provided, bwrap loads a libseccomp BPF denylist
        from that fd just before exec(), blocking ~50 dangerous syscalls
        (mount/unshare/setns, ptrace, bpf, io_uring, kexec, keyctl, time
        manipulation, etc.). See ``seccomp_filter.DENYLIST``.

    Tests that legitimately need network or host access won't work under this
    sandbox; set CODING_MODEL_ALLOW_UNSANDBOXED_TESTS=1 to opt out at your own risk.
    """
    # Walk up from sys.executable WITHOUT resolving symlinks — venv pythons
    # are typically a symlink chain (`venv/bin/python -> python3 -> /usr/bin/python3`)
    # and .resolve() follows it all the way to /usr, so `--ro-bind /usr /usr`
    # would replace the venv bind and `sys.executable`'s own path would be
    # invisible inside the sandbox.
    venv_root = Path(sys.executable).absolute().parent.parent
    spec_abs = spec_dir.resolve()

    # Optionally bind a Node toolchain into the sandbox so `node_test` specs can
    # run `node --test`. The bind SOURCE is resolved on the host here, before
    # the `/home` tmpfs mask is applied inside the sandbox, so an nvm path under
    # /home works as the source. The mountpoint is top-level (see
    # _SANDBOX_NODE_MOUNT) and prepended to PATH.
    node_bind: list[str] = []
    sandbox_path = "/usr/local/bin:/usr/bin:/bin"
    if SANDBOX_NODE_ROOT is not None:
        node_bin = SANDBOX_NODE_ROOT / "bin"
        if str(node_bin) in ("/usr/bin", "/usr/local/bin", "/bin"):
            # System Node already lives on a bound, on-PATH directory.
            pass
        else:
            node_bind = ["--ro-bind", str(SANDBOX_NODE_ROOT), _SANDBOX_NODE_MOUNT]
            sandbox_path = f"{_SANDBOX_NODE_MOUNT}/bin:{sandbox_path}"

    args = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--clearenv",
        "--setenv", "PATH", sandbox_path,
        "--setenv", "HOME", "/tmp",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "PYTHONUNBUFFERED", "1",
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        # Baseline filesystem — read-only
        "--ro-bind", "/usr", "/usr",
        "--ro-bind-try", "/lib", "/lib",
        "--ro-bind-try", "/lib64", "/lib64",
        "--ro-bind-try", "/lib32", "/lib32",
        "--ro-bind-try", "/bin", "/bin",
        "--ro-bind-try", "/sbin", "/sbin",
        "--ro-bind-try", "/etc", "/etc",
        "--ro-bind-try", "/opt", "/opt",
        # Fresh kernel interfaces and writable tmp
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", "/var/tmp",
        # Hide every user home, then re-expose just what we need
        "--tmpfs", "/home",
        "--tmpfs", "/root",
        "--ro-bind", str(venv_root), str(venv_root),
        *node_bind,
        "--bind", str(spec_abs), str(spec_abs),
        "--chdir", str(spec_abs),
    ]
    if seccomp_fd is not None:
        args.extend(["--seccomp", str(seccomp_fd)])
    args.append("--")
    args.extend(cmd)
    return args


# Per-framework default timeouts (seconds). Swift/Xcode builds are slow,
# especially cold, so their defaults are generous.
DEFAULT_TIMEOUTS: dict[str, int] = {
    "pytest": 120,
    "python": 120,
    "jest": 120,
    "node_test": 120,
    "swift_test": 300,
    "xcodebuild_test": 900,
}

MAC_RUNNER_URL = os.getenv("MAC_RUNNER_URL", "http://127.0.0.1:5050")
MAC_RUNNER_API_KEY = os.getenv("MAC_RUNNER_API_KEY", "")

# Relative paths inside spec_dir that should never be shipped to the Mac
# runner as patch content (they're not part of the LLM's diff).
# retry_history holds a full snapshot of every prior attempt
# (_snapshot_retry): shipping it made mac-runner patch payloads grow
# linearly with retries and materialized N stale copies of every source
# file in the git worktree — duplicate-source compilation in glob-based
# SPM targets (DEV-196).
_SPEC_SKIP_PATTERNS = (".pytest_cache", "__pycache__", ".DS_Store",
                       "test_output.txt", "retry_history")


def _run_local_tests(spec_dir: Path, framework: str, timeout: int) -> tuple[bool, str]:
    """Run pytest/jest/node_test locally (bwrap sandbox on Linux).

    LLM-generated test code runs inside a bubblewrap sandbox by default. If
    bwrap is unavailable, the test run fails with a clear diagnostic unless
    CODING_MODEL_ALLOW_UNSANDBOXED_TESTS is explicitly set.
    """
    if framework == "jest":
        raw_cmd = ["npx", "jest", "--no-coverage", "--roots", str(spec_dir)]
    elif framework == "node_test":
        # Node's built-in test runner (node:test). Zero external deps and no
        # network: the sandbox provides `node` on PATH via the bound Node
        # toolchain (see _wrap_in_sandbox / SANDBOX_NODE_ROOT).
        #
        # We enumerate the test files EXPLICITLY rather than letting `node --test`
        # auto-discover from the cwd, so we can exclude `retry_history/` — the
        # snapshots of prior retries. This mirrors the pytest path's
        # `--ignore retry_history`. Without it, every retry's stale snapshot is
        # re-run and its historical failures poison the result, so a JS spec
        # could never pass once it had retried (killed spec_54b2c1b3 on
        # 2026-07-15: a fixed `node:assert` typo kept failing from retry_0's
        # snapshot). Fall back to auto-discovery only if we find no test files.
        test_files = sorted(
            p.relative_to(spec_dir).as_posix()
            for ext in ("js", "mjs", "cjs")
            for p in spec_dir.rglob(f"*.test.{ext}")
            if "retry_history" not in p.relative_to(spec_dir).parts
        )
        raw_cmd = ["node", "--test", *test_files] if test_files else ["node", "--test"]
    else:
        # `--import-mode=importlib`: import each test module by its full path
        # instead of pytest's default 'prepend' mode, which keys modules by
        # basename and inserts the test's dir on sys.path. Under 'prepend',
        # two test files that share a basename in different dirs (e.g.
        # `tests/test_spec.py` + `ParamountDemo/tests/test_spec.py`, or flat
        # vs nested across retries) collide and abort collection with
        # `import file mismatch` BEFORE any test runs — every retry then
        # FAILs on a harness artefact, not the code (killed spec_031e0aaa on
        # 2026-06-02). importlib imports by path, so duplicate basenames
        # coexist; it supersedes the fragile reviewer-test path normalization
        # as the real guard against this class of failure.
        #
        # `--ignore retry_history` still keeps pytest out of the
        # `_snapshot_retry` dirs so a retry's tests aren't double-collected
        # against the live ones (the snapshots feed the synthesis pass, not
        # a re-run).
        raw_cmd = [
            sys.executable, "-m", "pytest", "-v", "--tb=short",
            "--import-mode=importlib",
            "--ignore", str(spec_dir / "retry_history"),
            str(spec_dir),
        ]

    allow_unsandboxed = os.getenv("CODING_MODEL_ALLOW_UNSANDBOXED_TESTS", "").lower() in ("1", "true", "yes")

    # The env var takes priority over bwrap detection: if the user explicitly
    # opted out, honor it — even when bwrap is installed but broken (e.g.
    # AppArmor restricting unprivileged user namespaces, which silently
    # makes every bwrap invocation fail with "Operation not permitted"
    # before pytest gets a chance to run).
    bpf_fd: Optional[int] = None
    if allow_unsandboxed:
        cmd = raw_cmd
        sandbox_mode = "UNSANDBOXED (CODING_MODEL_ALLOW_UNSANDBOXED_TESTS=1)"
        logger.warning(
            "running LLM-generated tests WITHOUT a sandbox — tests have full "
            "access to this user's environment"
        )
    elif _sandbox_available():
        bpf_fd = seccomp_filter.build_seccomp_bpf_fd()
        cmd = _wrap_in_sandbox(raw_cmd, spec_dir, seccomp_fd=bpf_fd)
        if bpf_fd is None:
            sandbox_mode = "bwrap (no seccomp — libseccomp unavailable)"
            logger.warning(
                "seccomp filter unavailable; bwrap will run without --seccomp. "
                "Install python3-seccomp on the host to enable kernel-syscall "
                "filtering for LLM-generated tests."
            )
        else:
            sandbox_mode = "bwrap+seccomp"
    else:
        msg = (
            "Refusing to run LLM-generated tests: bwrap (bubblewrap) is not "
            "available and CODING_MODEL_ALLOW_UNSANDBOXED_TESTS is not set. Install "
            "bubblewrap (e.g. `apt install bubblewrap` on Debian/Ubuntu) on "
            "the Linux server, or set CODING_MODEL_ALLOW_UNSANDBOXED_TESTS=1 to opt "
            "out (not recommended — tests run with the orchestrator's own "
            "privileges)."
        )
        logger.error(msg)
        return False, msg

    logger.info("running tests via %s: %s (timeout=%ds)",
                sandbox_mode, " ".join(raw_cmd), timeout)

    # Cap on captured output. Without this, a runaway test that prints a
    # tight loop of MB/s straight to stdout buffers everything in memory
    # and OOMs the orchestrator. 4 MiB is plenty for real test traces.
    MAX_OUTPUT_BYTES = 4 * 1024 * 1024

    def _truncate(s: str) -> str:
        b = s.encode("utf-8", errors="replace")
        if len(b) <= MAX_OUTPUT_BYTES:
            return s
        return (
            b[: MAX_OUTPUT_BYTES // 2].decode("utf-8", errors="replace")
            + f"\n... [truncated {len(b) - MAX_OUTPUT_BYTES} bytes of output] ...\n"
            + b[-MAX_OUTPUT_BYTES // 2 :].decode("utf-8", errors="replace")
        )

    try:
        # Popen + communicate instead of subprocess.run: run()'s timeout
        # kills only the DIRECT child, then blocks draining stdout — an
        # LLM-written test that spawned a background process leaves an
        # orphan holding the pipe, and the drain hangs the tick thread
        # forever with no heartbeat (DEV-155). Own session + killpg takes
        # the whole group down, after which the drain returns immediately.
        proc = subprocess.Popen(
            cmd, cwd=spec_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
            pass_fds=(bpf_fd,) if bpf_fd is not None else (),
        )
        try:
            out, err = proc.communicate(timeout=timeout)
            output = _truncate(out or "") + "\n" + _truncate(err or "")
            passed = proc.returncode == 0
        except subprocess.TimeoutExpired as e:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            # Group is dead; the drain returns. Surface partial output so
            # the supervisor / reviewer can diagnose the hang.
            try:
                out, err = proc.communicate(timeout=10)
            except Exception:
                out = e.stdout if isinstance(e.stdout, str) else ""
                err = e.stderr if isinstance(e.stderr, str) else ""
            output = (
                f"Tests timed out after {timeout}s\n"
                f"--- partial stdout ---\n{_truncate(out or '')}\n"
                f"--- partial stderr ---\n{_truncate(err or '')}"
            )
            passed = False
    except Exception as e:
        output = f"Test runner failed: {type(e).__name__}: {e}"
        passed = False
    finally:
        if bpf_fd is not None:
            try:
                os.close(bpf_fd)
            except OSError:
                pass

    return passed, output.strip()


def _collect_patch_files(spec_dir: Path) -> tuple[list[dict], Optional[str]]:
    """Enumerate spec_dir as UTF-8 patch files for the Mac runner.

    Returns (patch_files, error). On binary-content encounter, returns
    ([], error_message) so the caller can fail fast.
    """
    patch_files: list[dict] = []
    spec_root = spec_dir.resolve()
    for p in sorted(spec_root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(spec_root).as_posix()
        if any(skip in rel.split("/") for skip in _SPEC_SKIP_PATTERNS):
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return [], f"non-UTF8 file in spec: {rel} (binary patches not supported)"
        patch_files.append({"path": rel, "content": content})
    return patch_files, None


def _run_mac_runner_tests(
    spec_dir: Path,
    framework: str,
    timeout: int,
    *,
    repo: Optional[str],
    base_ref: str = "HEAD",
    scheme: Optional[str] = None,
    destination: Optional[str] = None,
    configuration: Optional[str] = None,
    workspace: Optional[str] = None,
    project: Optional[str] = None,
    filter: Optional[str] = None,
) -> tuple[bool, str]:
    """Dispatch swift_test / xcodebuild_test to the Mac runner over HTTP."""
    if not MAC_RUNNER_API_KEY:
        return False, (
            "MAC_RUNNER_API_KEY is not set on the orchestrator. Configure "
            "MAC_RUNNER_URL and MAC_RUNNER_API_KEY in ~/.config/coding-model-server/.env "
            "to dispatch Swift/Xcode tests to the Mac runner."
        )
    if not repo:
        return False, (
            f"{framework} requires a 'repo' (symbolic name registered in the "
            f"Mac runner's repos.yml). Add it to the spec's test_strategy block."
        )

    patch_files, err = _collect_patch_files(spec_dir)
    if err:
        return False, err

    payload: dict = {
        "spec_id": spec_dir.name,
        "repo": repo,
        "base_ref": base_ref,
        "patch_files": patch_files,
        "framework": framework,
        "timeout": timeout,
    }
    for key, val in (("scheme", scheme), ("destination", destination),
                     ("configuration", configuration), ("workspace", workspace),
                     ("project", project), ("filter", filter)):
        if val is not None:
            payload[key] = val

    url = f"{MAC_RUNNER_URL.rstrip('/')}/v1/run_tests"
    headers = {"X-Runner-Key": MAC_RUNNER_API_KEY}
    # Give the HTTP call headroom beyond the test timeout so the runner can
    # finish packaging the response even on a long run.
    http_timeout = timeout + 30

    logger.info("dispatching %s to mac-runner %s (timeout=%ds, %d files)",
                framework, url, timeout, len(patch_files))
    try:
        resp = _SESSION.post(url, json=payload, headers=headers, timeout=http_timeout)
    except requests.RequestException as e:
        return False, f"mac-runner unreachable at {url}: {e}"

    if resp.status_code != 200:
        return False, f"mac-runner HTTP {resp.status_code}: {resp.text[:2000]}"

    try:
        data = resp.json()
    except ValueError:
        return False, f"mac-runner returned non-JSON response: {resp.text[:2000]}"

    return bool(data.get("passed")), str(data.get("output", ""))


def run_tests(
    spec_dir: Path,
    framework: str = "pytest",
    timeout: Optional[int] = None,
    **framework_opts,
) -> tuple[bool, str]:
    """Run tests for a spec.

    Dispatches by framework:
      - pytest / python / jest / node_test → local (bwrap sandbox on Linux)
      - swift_test               → Mac runner HTTP dispatch; requires `repo`
      - xcodebuild_test          → Mac runner HTTP dispatch; requires `repo` + `scheme`

    framework_opts carries the framework-specific configuration from the
    planner's test_strategy block (repo, base_ref, scheme, destination,
    configuration, workspace, project, filter) — unknown keys are ignored.

    Returns (passed, combined_output).
    """
    effective_timeout = timeout if timeout is not None else DEFAULT_TIMEOUTS.get(framework, 120)

    if framework in ("swift_test", "xcodebuild_test"):
        passed, output = _run_mac_runner_tests(
            spec_dir, framework, effective_timeout,
            repo=framework_opts.get("repo"),
            base_ref=framework_opts.get("base_ref", "HEAD"),
            scheme=framework_opts.get("scheme"),
            destination=framework_opts.get("destination"),
            configuration=framework_opts.get("configuration"),
            workspace=framework_opts.get("workspace"),
            project=framework_opts.get("project"),
            filter=framework_opts.get("filter"),
        )
    else:
        passed, output = _run_local_tests(spec_dir, framework, effective_timeout)

    logger.info("test result: %s (%d chars output)",
                "PASS" if passed else "FAIL", len(output))
    return passed, (output or "").strip()


# ── Phase b: Adversarial test generation (Gemini and/or Claude) ──────────────
#
# Fires once per spec, after the local Coding Model reviewer's tests pass on retry-0,
# before creating the release_approval gate. Each configured provider is given
# the spec, design, code, and Coding Model's existing tests, and asked to write
# additional pytest tests targeting edge cases Coding Model missed. Files land in
# spec_dir under the `adversarial_test_*.py` namespace.
#
# Provider modes (AUTONOMOUS_ADVERSARIAL_PROVIDER):
#   - gemini (default): single-provider, files named `adversarial_test_*.py`.
#   - claude:           single-provider, same namespace.
#   - both:             both providers fire sequentially. Each gets a
#                       provider-tagged filename prefix
#                       (`adversarial_test_claude_*.py` /
#                       `adversarial_test_gemini_*.py`) so they can't
#                       overwrite each other's files.
#
# Disabled by default. Set AUTONOMOUS_ADVERSARIAL_TESTS_ENABLED=1 to opt in.
# Per-provider failure (key/SDK/network/timeout) is caught inside the loop —
# the next provider still runs and the orchestrator's outer try/except keeps
# the original PASS standing if everything fails.

ADVERSARIAL_TESTS_ENABLED = os.getenv(
    "AUTONOMOUS_ADVERSARIAL_TESTS_ENABLED", "0"
).lower() in ("1", "true", "yes")
ADVERSARIAL_MAX_TOKENS = int(os.getenv("AUTONOMOUS_ADVERSARIAL_MAX_TOKENS", "8000"))
ADVERSARIAL_TIMEOUT = float(os.getenv("AUTONOMOUS_ADVERSARIAL_TIMEOUT", "300"))

ADVERSARIAL_PROVIDER = os.getenv("AUTONOMOUS_ADVERSARIAL_PROVIDER", "gemini").lower()
ADVERSARIAL_GEMINI_MODEL = os.getenv(
    "AUTONOMOUS_ADVERSARIAL_GEMINI_MODEL", external_judges.DEFAULT_GEMINI_MODEL
)
ADVERSARIAL_CLAUDE_MODEL = os.getenv(
    "AUTONOMOUS_ADVERSARIAL_CLAUDE_MODEL", external_judges.DEFAULT_CLAUDE_MODEL
)

ADVERSARIAL_FILENAME_PREFIX = "adversarial_test_"


@dataclass
class AdversarialProviderResult:
    """Per-provider outcome from one Phase b firing.

    The orchestrator records one AGENT_RAN event per result so the stats
    script can attribute false-FAILs to a specific provider.
    """
    provider: str                          # "claude" | "gemini"
    model: str                             # the actual model name used
    files_written: list[tuple[str, str]]   # (basename, content)
    error: Optional[str] = None            # "<ExcType>: <message>" if call raised
    skipped: bool = False                  # True when provider returned 0 blocks


def _resolve_providers() -> list[str]:
    """Map the env var to an ordered provider list.

    Order matters only for telemetry/log readability. Tests re-run reads
    everything from disk regardless of order.
    """
    p = ADVERSARIAL_PROVIDER
    if p == "gemini":
        return ["gemini"]
    if p == "claude":
        return ["claude"]
    if p == "both":
        return ["claude", "gemini"]
    logger.warning(
        "AUTONOMOUS_ADVERSARIAL_PROVIDER=%r is invalid (expected "
        "gemini|claude|both); falling back to gemini",
        p,
    )
    return ["gemini"]


def _provider_model(provider: str) -> str:
    if provider == "claude":
        return ADVERSARIAL_CLAUDE_MODEL
    if provider == "gemini":
        return ADVERSARIAL_GEMINI_MODEL
    raise ValueError(f"unknown provider: {provider}")


def _provider_filename_prefix(provider: str, multi_provider: bool) -> str:
    """Return the filename prefix this provider must use.

    Single-provider mode keeps the original `adversarial_test_` namespace
    (backwards-compatible with the v1 design). 'both' mode uses
    `adversarial_test_<provider>_` so concurrent files can't collide.
    """
    if not multi_provider:
        return ADVERSARIAL_FILENAME_PREFIX
    return f"{ADVERSARIAL_FILENAME_PREFIX}{provider}_"


def _build_adversarial_system_prompt(filename_prefix: str) -> str:
    """Build the per-provider system prompt with its filename prefix
    interpolated into rule 2 and the output-format footer."""
    return textwrap.dedent(f"""\
        You are an adversarial test-writer. The local reviewer has already
        written tests and approved this code. Your job: write 3 to 7 additional
        pytest tests that target edge cases the reviewer's tests miss.

        # What to target
        - Boundary values: empty inputs, single-element inputs, max-size inputs.
        - Type edge cases: None, NaN, negative numbers where positive expected.
        - Concurrent / ordering edge cases when the spec implies order.
        - Error paths: malformed inputs, missing files, timeout/retry.
        - Off-by-one in indices, slices, ranges.

        # Hard rules

        1. Test ONLY behaviors the spec requires. If the spec is silent on an
           edge case, do NOT invent a requirement. Over-specification is the
           failure mode you must avoid.

           Concrete example of what NOT to test: if the spec asks for a
           function that returns the sum of a list of ints and is silent on
           what to do with floats, do NOT add a test that asserts a specific
           behavior for floats. Skip it.

        2. Output one or more `<<<FILE: {filename_prefix}<name>.py>>>` blocks
           terminated by `<<<END_FILE>>>`. Filenames MUST start with
           `{filename_prefix}` so they don't collide with the reviewer's
           tests or with another adversarial provider's tests. Any file
           whose name doesn't start with `{filename_prefix}` will be
           silently dropped by the orchestrator.

        3. Tests must pass on a CORRECT implementation. If you cannot tell
           whether the spec requires the behavior you want to test, skip it.

        4. Use pytest. Real imports, no mocks beyond what the existing tests
           already use.

        5. At the top of each test function, add a one-line docstring naming
           the spec acceptance criterion the test exercises:
               def test_negative_count():
                   \"\"\"Acceptance: 'rejects negative counts with ValueError'.\"\"\"

        6. If, after careful reading, you conclude the reviewer's tests
           already cover every edge case the spec requires, output NO file
           blocks at all. An empty response is the correct answer in that
           case — do not invent tests for the sake of producing output.

        # Output format

        Either zero file blocks (if no useful adversarial tests exist), or
        1 to 3 files containing 3-7 test functions total. No prose outside
        the file blocks.
        """)


def _call_provider(provider: str, system_prompt: str, user_content: str) -> str:
    if provider == "claude":
        return external_judges.call_claude(
            system_prompt, user_content,
            max_tokens=ADVERSARIAL_MAX_TOKENS,
            timeout=ADVERSARIAL_TIMEOUT,
            model=ADVERSARIAL_CLAUDE_MODEL,
        )
    if provider == "gemini":
        return external_judges.call_gemini(
            system_prompt, user_content,
            max_tokens=ADVERSARIAL_MAX_TOKENS,
            timeout=ADVERSARIAL_TIMEOUT,
            model=ADVERSARIAL_GEMINI_MODEL,
        )
    raise ValueError(f"unknown provider: {provider}")


def adversarial_tests_available() -> tuple[bool, Optional[str]]:
    """Pre-flight: (ok, reason_if_not). Used by orchestrator startup logging.

    Returns the FIRST configured-provider failure encountered. ok=True
    means every configured provider has both its key and SDK present.
    """
    if not ADVERSARIAL_TESTS_ENABLED:
        return False, "AUTONOMOUS_ADVERSARIAL_TESTS_ENABLED is not set"
    for provider in _resolve_providers():
        if provider == "claude":
            ok, reason = external_judges.claude_available()
        elif provider == "gemini":
            ok, reason = external_judges.gemini_available()
        else:
            return False, f"unknown provider: {provider}"
        if not ok:
            return False, f"provider={provider}: {reason}"
    return True, None


def build_adversarial_test_message(
    spec_md: str,
    design_md: str,
    code_files: list[tuple[str, str]],
    reviewer_tests: list[tuple[str, str]],
    reviewer_test_output: str,
) -> str:
    """Assemble the user-content payload for the adversarial test-writer.

    Mirrors the structure of build_reviewer_message but adds the
    reviewer's tests and the pytest output proving they passed — that's
    the adversarial framing ("here's what already passes; break it").
    The same user-content is sent to every configured provider.
    """
    code_sections = [f"### {p}\n```\n{c}\n```\n" for p, c in code_files]
    reviewer_test_sections = [
        f"### {p}\n```\n{c}\n```\n" for p, c in reviewer_tests
    ]
    truncated_test_output = reviewer_test_output[-4000:] if reviewer_test_output else ""

    return (
        "## Specification\n\n"
        f"{spec_md}\n\n"
        "## Architecture Design\n\n"
        f"{design_md}\n\n"
        "## Implementation Files (the code under test)\n\n"
        + "\n".join(code_sections)
        + "\n## Reviewer's Existing Tests (already pass — do not duplicate)\n\n"
        + ("\n".join(reviewer_test_sections) if reviewer_test_sections
           else "(no reviewer tests on disk)\n")
        + "\n## Reviewer's Test-Run Output (last 4 KB)\n\n"
        + f"```\n{truncated_test_output}\n```\n\n"
        + "---\n\n"
        "Your task: write 3-7 adversarial pytest tests across 1-3 files. "
        "Each test must exercise a spec-required behavior the reviewer's "
        "tests miss. If you find no missing coverage, output zero file "
        "blocks. Filename rules are in the system prompt — follow them "
        "exactly or your files will be silently dropped."
    )


def _generate_for_provider(
    provider: str,
    expected_prefix: str,
    user_content: str,
    spec_dir: Path,
) -> list[tuple[str, str]]:
    """Call one provider, parse + validate + write its files.

    Returns the list of (basename, content) successfully written. An
    empty list either means the model emitted no blocks (rule 6) or
    every block was rejected for namespace/extension/path-traversal.

    Raises any exception from the SDK call so the outer loop can record
    the failure for telemetry; parse/validation rejects are logged and
    suppressed.
    """
    system_prompt = _build_adversarial_system_prompt(expected_prefix)
    raw = _call_provider(provider, system_prompt, user_content)
    cleaned = _strip_thinking(raw)
    matches = _FILE_RE.findall(cleaned)
    if not matches:
        return []

    written: list[tuple[str, str]] = []
    for rel_path, content in matches:
        rel_path = rel_path.strip()
        # Take just the basename to defang `dir/adversarial_test_x.py`
        # attempts that might escape the namespace check below.
        basename = os.path.basename(rel_path)
        if not basename.startswith(expected_prefix):
            logger.warning(
                "phase-b: rejecting %s adversarial test with non-conforming "
                "filename %r (must start with %r)",
                provider, rel_path, expected_prefix,
            )
            continue
        if not basename.endswith(".py"):
            logger.warning(
                "phase-b: rejecting %s adversarial test with non-.py "
                "extension %r", provider, rel_path,
            )
            continue
        cleaned_content = _strip_markdown_fence(content)
        try:
            _write_artifact(spec_dir, basename, cleaned_content)
        except ValueError as e:
            logger.warning("phase-b: rejecting %s adversarial test %r: %s",
                           provider, rel_path, e)
            continue
        written.append((basename, cleaned_content))
    return written


def generate_adversarial_tests(
    spec_dir: Path,
    spec_md: str,
    design_md: str,
    code_files: list[tuple[str, str]],
    reviewer_tests: list[tuple[str, str]],
    reviewer_test_output: str,
) -> list[AdversarialProviderResult]:
    """Run every configured provider in sequence, return per-provider results.

    Per-provider exceptions are caught and recorded as ``error`` on the
    result; the next provider still runs. The orchestrator records one
    AGENT_RAN event per AdversarialProviderResult and re-runs tests if
    any provider wrote files.
    """
    user_content = build_adversarial_test_message(
        spec_md, design_md, code_files, reviewer_tests, reviewer_test_output,
    )
    providers = _resolve_providers()
    multi = len(providers) > 1
    results: list[AdversarialProviderResult] = []

    for provider in providers:
        prefix = _provider_filename_prefix(provider, multi)
        model = _provider_model(provider)
        logger.info(
            "phase-b: calling %s adversarial test-writer "
            "(model=%s, max_tokens=%d, timeout=%.0fs, prefix=%r, %d KiB user)",
            provider, model, ADVERSARIAL_MAX_TOKENS, ADVERSARIAL_TIMEOUT,
            prefix, len(user_content) // 1024,
        )
        try:
            files = _generate_for_provider(provider, prefix, user_content, spec_dir)
        except Exception as e:  # noqa: BLE001 — fail-open per provider
            err = f"{type(e).__name__}: {e}"
            logger.warning(
                "phase-b: %s adversarial call failed (%s); "
                "remaining providers (if any) will continue",
                provider, err,
            )
            results.append(AdversarialProviderResult(
                provider=provider, model=model, files_written=[], error=err,
            ))
            continue

        if not files:
            logger.info(
                "phase-b: %s returned no adversarial test blocks "
                "(rule 6 — reviewer coverage deemed sufficient, or "
                "every block rejected at validation)",
                provider,
            )
            results.append(AdversarialProviderResult(
                provider=provider, model=model, files_written=[], skipped=True,
            ))
            continue

        results.append(AdversarialProviderResult(
            provider=provider, model=model, files_written=files,
        ))
        logger.info("phase-b: %s wrote %d adversarial test file(s)",
                    provider, len(files))

    total = sum(len(r.files_written) for r in results)
    logger.info("phase-b: %d total adversarial file(s) across %s",
                total, ", ".join(providers))
    return results
