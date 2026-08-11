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
import textwrap
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from coding_model_server.streaming import strip_thinking as _server_strip_thinking

from ._http import post_chat_completion

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

# DEV-481 fix 1: mechanical testability check on the design's Criterion Seams.
# Its own budget, so a testability bounce never spends the design review's
# single revision. Two rounds: one to add a missing section, one to fix what
# the section then reveals.
TESTABILITY_CHECK_ENABLED = os.getenv(
    "AUTONOMOUS_TESTABILITY_CHECK", "1").lower() not in ("0", "false", "no")
TESTABILITY_CHECK_MAX_ROUNDS = int(
    os.getenv("AUTONOMOUS_TESTABILITY_CHECK_MAX_ROUNDS", "2"))

# Robustness: how many times to re-run the reviewer when its output is
# truncated/unparseable before treating it as a soft FAIL (→ supervisor/retry)
# instead of failing the whole spec. The 122B reviewer's degenerate truncation
# is intermittent, so one re-run often recovers it.
REVIEWER_PARSE_RETRIES = int(os.getenv("AUTONOMOUS_REVIEWER_PARSE_RETRIES", "1"))

# Roles allowed to receive server-side RAG context, comma-separated
# (e.g. "implementer" or "implementer,architect"). EMPTY BY DEFAULT, which
# preserves the long-standing behaviour: _http.post_chat_completion defaults
# skip_memory=True for the whole autonomous package, so no autonomous agent has
# ever seen a retrieved chunk.
#
# Opt in per role rather than globally because the value depends entirely on
# prompt shape. Retrieval uses the LAST USER MESSAGE as its embedding query, so
# it only works when that message reads like a question about an API. A planner
# prompt ("decompose this spec") or a manifest-mode implementer prompt (a list
# of 12 file paths) embeds diffusely and retrieves noise — or, worse, retrieves
# confidently-irrelevant docs that then get appended to the system prompt.
#
# Injection is bounded: get_context_string takes the top 5 hits and truncates to
# ~1000 tokens, and MEMORY_RELEVANCE_THRESHOLD discards anything past the cosine
# cutoff, so an off-topic task should retrieve nothing rather than filler.
def _parse_memory_roles(raw: str) -> set[str]:
    """Split the env value into a normalised role set.

    Separate function so tests can exercise the parsing without reloading this
    module — reloading rebinds module-scope objects that sibling modules and
    other tests already hold references to.
    """
    return {r.strip().lower() for r in (raw or "").split(",") if r.strip()}


AUTONOMOUS_MEMORY_ROLES = _parse_memory_roles(os.getenv("AUTONOMOUS_MEMORY_ROLES", ""))

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

def _strip_thinking(text: str) -> str:
    """Defensive strip before parsing structured output (DEV-153).

    Delegates to streaming.strip_thinking — the one implementation that
    handles all three observed real patterns (full block, orphan close,
    unclosed open) plus <REACT>. The local regex copy only handled full
    blocks, so as a defensive layer it didn't defend against the patterns
    actually seen; if the server-side strip ever regresses, un-stripped
    reasoning would have flowed straight into the YAML/FILE-marker parsers.
    """
    return _server_strip_thinking(text).strip()


def artifact_path(spec_dir: Path, rel_path: str) -> Path:
    """Resolve a spec-relative artifact path, rejecting traversal.

    Strips leading ``/`` so models can't write absolute paths (Python's
    ``Path / "/abs"`` would discard the left operand). Then resolves
    symlinks and checks the result is still under ``spec_dir``.

    Uses Path.is_relative_to (not str.startswith): the latter would treat
    /work/abc-evil/x as nested under /work/abc, so a sibling-prefix dir
    escape would not be caught. is_relative_to compares path components.

    Separate from _write_artifact so a caller that needs to *read* what is
    currently at a path — the DEV-541 pre-repair snapshot — resolves it the
    same way the write will, rather than reimplementing the rules and drifting.
    """
    # Strip leading slashes only — do NOT use lstrip("./") which eats
    # individual chars and would normalize "../../../x" into "x".
    while rel_path.startswith("/"):
        rel_path = rel_path[1:]
    spec_root = spec_dir.resolve()
    abs_path = (spec_dir / rel_path).resolve()
    if not abs_path.is_relative_to(spec_root):
        raise ValueError(f"Path traversal rejected: {rel_path}")
    return abs_path


def _write_artifact(spec_dir: Path, rel_path: str, content: str) -> Path:
    """Write a file to the spec workspace with path-traversal protection."""
    abs_path = artifact_path(spec_dir, rel_path)
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

    ## Criterion Seams
    <ONE entry per checklist item, IN THE SAME ORDER, naming the API a test
    would use. Every entry needs all three steps, and every symbol must be one
    this design declares:>
    - <criterion> | setup: `<call that builds the starting state>` | act:
      `<call that invokes the behaviour>` | assert: `<expression that observes
      the outcome>`
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
    10. Ensure the design AFFORDS the tests its own checklist demands. Rule 9
       governs how a criterion is phrased; this rule governs whether the API you
       specify can actually carry it out. For every checklist item, a test must
       be able to (a) construct the starting state it needs, (b) invoke the
       behaviour, and (c) observe the outcome — using only the API in this
       document. If any of the three is unreachable, the DESIGN is wrong, not
       the criterion. Concretely:
         - state the conformances a comparison needs — ANY type a criterion
           compares for equality must be declared Equatable, not just enums.
           This reaches through the standard library: a criterion asserting two
           collections are identical needs the ELEMENT type Equatable, because
           a dictionary or array is only Equatable when its element is. A design
           that says `cells: [Position: Mushroom]` and asks for "the same seed
           produces an identical field" must declare `Mushroom` Equatable.
           TUPLES NEVER CONFORM TO ANY PROTOCOL IN SWIFT — not Equatable, not
           Hashable, not anything, and no annotation changes that. So an array
           or dictionary OF tuples can never be Equatable, and a type holding
           one cannot synthesise `==` however it is declared. Replace the tuple
           with a named struct that declares the conformance. Check EVERY
           stored property for this, not just the one you were asked about:
           this rule has been broken twice in sibling properties of the same
           type, once in a revision that had just fixed it next door;
         - keep state a criterion must set up reachable — a read-only collection
           whose only initialiser is seeded cannot be positioned for a boundary
           test, so give it a test-visible initialiser or an entry point that
           places the state directly;
         - name every constant the criteria refer to, instead of leaving the
           value in prose, so a test can assert against the same symbol.
       A checklist item you cannot sketch as a three-line test against your own
       API is a design defect. Fix the design before emitting it.
    11. The `## Criterion Seams` section is where rule 10 is made checkable, and
       it is verified mechanically before your design reaches a human. One entry
       per checklist item, same order, all three steps present, and every
       backticked symbol declared somewhere in this document. `Type.member`
       naming a member you never declared is rejected, as is an assert comparing
       a type you never declared Equatable, as is a setup writing state you
       declared read-only. EVERY STEP MUST NAME A CALL IN BACKTICKS — a step
       written as prose ("place a chain at the rightmost column") is rejected,
       and so is one that elides the call (`let snapshot = ...`). Write the
       actual expression a test would run. If there is no call to write, you
       have found a criterion your API cannot reach: that is the defect this
       section exists to surface, so fix the API rather than describing the
       intent. Write the seams as you write the checklist — if you cannot name
       the seam, change the API until you can.
    """)

# Swift value semantics: ~29% of every build diagnostic this pipeline has
# produced (DEV-511), and the same errors recur across EVERY implementer in the
# rotation — which points at missing shared instruction rather than a gap in any
# one model, since a model-specific weakness would produce different failures
# per agent (DEV-431).
#
# It is the corner of Swift with no analogue in the Python/TypeScript these
# models are saturated with, so the training prior actively works against them.
#
# Lives in the SYSTEM prompt, not interpolated into the user message: system
# prompts are the stable cached prefix (DEV-409), and this is prepended to every
# per-file call — 26 of them in one manifest build — so an interpolated version
# would cost a cache miss each time. Deliberately unconditional for the same
# reason; branching on language would break the prefix.
#
# Scoped to the ~29% mutability class only. The larger ~62% cross-file class
# (DEV-467) is an architecture problem and is untouched by this.
SWIFT_VALUE_SEMANTICS = textwrap.dedent("""\

    # Swift value semantics (skip if you are not writing Swift)
    - `struct` and `enum` are VALUE types. Any method that assigns to `self` or
      to a stored property must be marked `mutating`.
    - `mutating` is invalid on a `class`. Classes are reference types; their
      methods mutate freely and must never be marked `mutating`.
    - A `let` stored property cannot be reassigned after init, and cannot be the
      left side of a mutating operator (`+=`, `append`, …).
    - `private(set) var` is readable everywhere but writable ONLY inside the
      declaring type. Writing it from another type does not compile.
    - You cannot call a `mutating` method through a `let` binding, a computed
      property, or a dictionary subscript. Bind to a `var` first, mutate, then
      write back.
    - A struct gets its memberwise `init` only if it declares no `init` of its
      own, and that init's parameter order follows declaration order.
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
    """) + SWIFT_VALUE_SEMANTICS

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
    correct path yourself.

    For JavaScript `node --test` suites: import assertions with
    `import assert from 'node:assert/strict'`. The `node:test` module
    does NOT export `assert` — `import { assert } from 'node:test'`
    makes every test die on a TypeError before it exercises anything.

    Example:

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
    """) + SWIFT_VALUE_SEMANTICS


# ── Agent calling ────────────────────────────────────────────────────────────

def call_agent(
    role: str,
    messages: list[dict[str, str]],
    *,
    agent: str | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
    meta: Optional[dict] = None,
    memory_query: str | None = None,
) -> str:
    """Call an agent via the coding-model-server inference API.

    Returns the raw content string from the model's response.
    Raises on transport or HTTP errors so the caller can decide
    whether to retry or fail.

    If *meta* is provided, it is populated with response metadata the caller
    can inspect after the call:
      - ``meta["agent"]``: the resolved model name, so the caller can attribute
        an event to it without re-deriving the role→agent mapping.
      - ``meta["duration_ms"]``: wall-clock for the call, retry backoff included.
      - ``meta["prompt_tokens"]`` / ``["completion_tokens"]`` / ``["total_tokens"]``:
        usage as reported by the server; absent when it reported none.
      - ``meta["finish_reason"]``: the model's stop reason ("stop", "length", …)
      - ``meta["truncated"]``: True when finish_reason == "length", i.e. the
        output hit ``max_tokens`` and was cut off mid-stream. The implementer
        path uses this to emit an OUTPUT_TRUNCATED event instead of letting a
        half-written file surface as a bogus reviewer "missing file" FAIL.

    ``agent_event_fields(meta)`` packages the first four for an event payload.

    *memory_query* is what RAG retrieves on, for roles that opted in. Without
    it the server embeds the last user message, which for these prompts leads
    with the entire spec and design — and the embedding model truncates at 256
    tokens, so the actual ask at the end never reaches retrieval (DEV-489).
    Ignored when the role has not opted into memory.
    """
    agent = agent or role_to_agent(role)
    max_tokens = max_tokens or ROLE_TO_MAX_TOKENS.get(role, 8000)
    timeout = timeout or ROLE_TO_TIMEOUT.get(role, 1800)

    # Per-role RAG opt-in (default: nothing opts in — see AUTONOMOUS_MEMORY_ROLES).
    use_memory = role.lower() in AUTONOMOUS_MEMORY_ROLES

    logger.info("calling agent=%s, role=%s, msg_count=%d, max_tokens=%d, rag=%s",
                agent, role, len(messages), max_tokens,
                "on" if use_memory else "off")

    # retry_5xx=True: model-swap CUDA OOM is the dominant cause of 500s here
    # (VRAM not fully released between models — see project_model_swap_oom.md):
    # the next process can't allocate its compute buffer until the kernel
    # reaps the previous CUDA context. Without this retry a single transient
    # swap-OOM cancels the whole spec (_start_task catches Exception → task
    # failed → spec FAILED, no rotation). The generous backoff schedule lives
    # in _http._BACKOFFS (Blackwell VRAM release takes 10-30s after exit).
    # Only sent when the role actually retrieves, so the payload doesn't carry
    # a query the server would ignore.
    extra = {}
    if use_memory and memory_query:
        extra["memory_query"] = memory_query
    # Wall-clock over the whole call, backoff included (DEV-528). retry_5xx
    # means a model-swap OOM can spend 10-30s sleeping before the request that
    # finally succeeds, and that is real time the spec sat waiting — so it
    # belongs in the figure a "cost per attempt" query reads. Measured here
    # rather than at the ~20 event call sites so every agent is timed the same
    # way; monotonic because the box runs NTP and a step would give a negative.
    t0 = time.monotonic()
    resp = post_chat_completion(
        agent, messages, timeout=timeout, max_tokens=max_tokens,
        temperature=0.2, retry_5xx=True, skip_memory=not use_memory,
        **extra,
    )
    duration_ms = int((time.monotonic() - t0) * 1000)
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
        meta["duration_ms"] = duration_ms
        # The server returns OpenAI-style `usage` on every non-streaming
        # completion (streaming.build_completion_response). llama_server
        # substitutes zeros when a backend omits it, so a zero total means
        # "not reported" rather than a free call — record nothing in that
        # case, because a missing number and a genuine 0 must not read alike.
        usage = data.get("usage") or {}
        if usage.get("total_tokens"):
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                val = usage.get(key)
                if isinstance(val, int) and val >= 0:
                    meta[key] = val
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


def agent_event_fields(meta: Optional[dict]) -> dict:
    """Telemetry subset of a ``call_agent`` *meta*, for an AGENT_RAN payload.

    Every AGENT_RAN event that records a real model call routes its
    attribution through here, so the fields are spelled identically at all
    of them (DEV-528). A query that groups by agent cannot work when one
    site writes ``agent`` and another writes ``model``, which is how the
    adversarial path drifted.

    Keys absent from *meta* are omitted rather than stored as None: an event
    predating this change and one whose usage went unreported should look the
    same to a reader, and neither should be mistaken for a measured zero.
    """
    if not meta:
        return {}
    out = {
        key: meta[key]
        for key in ("agent", "duration_ms", "prompt_tokens",
                    "completion_tokens", "total_tokens", "calls")
        if meta.get(key) is not None
    }
    # Carried because it changes how an attempt reads: a truncated response is
    # a budget failure, not a model failure, and the two should not pool.
    if meta.get("truncated"):
        out["truncated"] = True
    return out


def accumulate_agent_fields(tally: dict, meta: Optional[dict]) -> dict:
    """Fold one ``call_agent`` *meta* into a running *tally*, and return it.

    Manifest mode builds one implementer attempt out of 1 + N model calls —
    the manifest, then a file at a time. DEV-528 asks for cost "per attempt",
    so those sum: the question "what did this attempt cost" has one answer,
    not thirty.

    The tally uses the same key names as a *meta*, so ``agent_event_fields``
    renders either. ``calls`` is kept alongside because a 3-call attempt and a
    30-call one that happen to total the same are not the same event, and the
    mean per call is only recoverable if the count survives.
    """
    if not meta:
        return tally
    # Last writer wins. Within one attempt every call uses the same agent —
    # rotation happens between attempts, not inside one — so this is stable;
    # it is a fallback for the case where an early call reported nothing.
    if meta.get("agent"):
        tally["agent"] = meta["agent"]
    for key in ("duration_ms", "prompt_tokens", "completion_tokens",
                "total_tokens"):
        val = meta.get(key)
        if val is not None:
            tally[key] = tally.get(key, 0) + val
    tally["calls"] = tally.get("calls", 0) + 1
    # Any truncated call taints the attempt: the file it was writing is
    # half-finished regardless of how the remaining calls went.
    if meta.get("truncated"):
        tally["truncated"] = True
    return tally


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
# DEV-498: the architect corrupts its own opening delimiter, reproducibly. On
# spec_9872c963 it emitted <<<DESINVARIANT>>> on three consecutive calls —
# DESIGN blended with INVARIANT, which the architect prompt shouts throughout
# (rules 1 and 8). Everything else in those responses was correct: a complete
# design body, a well-formed <<<END>>>, and a valid COMPLEXITY block after it.
#
# Discarding 15 KB of correct design over one token is expensive — each retry
# is a full generation, and exhausting the attempts fails the SPEC outright,
# with no synthesis path. So accept an opening delimiter that starts with DES
# and is followed by the usual structure. The closing delimiter is unchanged,
# so there is no ambiguity about where the block ends.
_DESIGN_FUZZY_RE = re.compile(
    r"<{1,3}DES[A-Z_]*>{1,3}\s*(.*?)\s*<{1,3}END>{1,3}", re.DOTALL | re.IGNORECASE,
)
# For the failure message: what delimiters were actually present?
_ANY_DELIMITER_RE = re.compile(r"<{1,3}[A-Z_]{2,}>{1,3}")
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
        # DEV-498: fall back to a near-miss opening delimiter before giving up.
        m = _DESIGN_FUZZY_RE.search(cleaned)
        if m:
            logger.warning(
                "architect opening delimiter was %r, not <<<DESIGN>>> — "
                "recovered the block anyway (DEV-498)",
                m.group(0)[:m.group(0).find(">")+3],
            )
    if not m:
        # Name what was actually there. "no block found" reads as "the model
        # produced nothing usable", which sent me to the artifact to discover
        # a complete design behind one wrong token.
        seen = ", ".join(sorted(set(_ANY_DELIMITER_RE.findall(cleaned)))) or "none"
        return ParseError(
            f"No <<<DESIGN>>>…<<<END>>> block found (delimiters seen: {seen})",
            text)
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

def _render_plan_constraints(plan_yaml: str) -> "str | None":
    """Render the approved plan's binding decisions for the architect prompt.

    The architect used to see only the raw spec, so it could re-derive a
    language/toolchain from an ambiguous spec that contradicted the plan the
    operator already approved (DEV-107: plan said javascript + "no
    TypeScript", spec mentioned TypeScript, architect designed TypeScript
    twice and the spec failed at design). Rejection notes alone did not hold
    — only rewriting spec.md did — so each decision is phrased as an order
    that outranks the spec, not as a fact the architect is free to weigh.

    Returns None when the YAML is unparseable or carries none of the binding
    fields; the caller then omits the section, which is exactly the
    pre-DEV-107 prompt. Planner output is LLM-generated, so a section that
    comes back the wrong shape (`test_strategy: pytest` as a string rather
    than a mapping) drops that one line instead of taking the run down.
    """
    try:
        plan = yaml.safe_load(plan_yaml)
    except yaml.YAMLError:
        return None
    if not isinstance(plan, dict):
        return None

    def _mapping(key: str) -> dict:
        value = plan.get(key)
        return value if isinstance(value, dict) else {}

    test_strategy = _mapping("test_strategy")
    constraints = _mapping("constraints")

    lines: list[str] = []
    if plan.get("language"):
        lines.append(
            f"- **Language: {plan['language']}.** Write the design for this "
            "language and its toolchain. Do not choose another, and do not "
            "offer the implementer a choice."
        )
    if plan.get("target_runtime"):
        lines.append(f"- **Target runtime: {plan['target_runtime']}.**")
    if test_strategy.get("framework"):
        lines.append(
            f"- **Test framework: {test_strategy['framework']}.** The design "
            "must be testable under this framework — keep the logic core free "
            "of anything it cannot exercise."
        )
    if "dependencies_allowed" in constraints:
        if constraints["dependencies_allowed"]:
            lines.append("- **External dependencies: permitted.**")
        else:
            lines.append(
                "- **External dependencies: NOT permitted.** Design against "
                "the standard library only — no third-party packages."
            )
    if constraints.get("notes"):
        lines.append(f"- **Other constraints:** {constraints['notes']}")

    raw_clarifications = plan.get("clarifications")
    clarifications = ([str(c) for c in raw_clarifications if c]
                      if isinstance(raw_clarifications, list) else [])

    # DEV-490: the plan's acceptance_criteria never reached the architect, so a
    # criterion struck at the plan gate came straight back in the design. On
    # spec_837b167f I rejected three criteria the harness cannot evaluate, the
    # planner removed all three, the plan was approved — and the architect
    # reinstated them verbatim, because spec.md still carried the original text
    # (4 occurrences of "pre-fix") and nothing said which document wins. A plan
    # rejection survived exactly one stage. Same shape as the DEV-107 failure
    # this function was written for, one field over.
    raw_criteria = plan.get("acceptance_criteria")
    criteria = ([str(c) for c in raw_criteria if c]
                if isinstance(raw_criteria, list) else [])

    if not lines and not clarifications and not criteria:
        return None

    block = [
        "## Approved plan — binding constraints\n\n"
        "The operator has already approved a plan for this spec. These are "
        "settled decisions, not suggestions: where the specification is "
        "ambiguous, offers a choice, or contradicts them, the plan WINS. The "
        "choice has already been made — design to it.\n"
    ]
    if lines:
        block.append("\n" + "\n".join(lines) + "\n")
    if clarifications:
        block.append(
            "\n### Operator clarifications (verbatim — hard requirements)\n\n"
            "These are the operator's own answers, at the same authority as "
            "the spec. Apply each literally; do not override one with a "
            "default or a more idiomatic alternative.\n\n"
        )
        block.extend(f"{i}. {c}\n" for i, c in enumerate(clarifications, 1))
    if criteria:
        block.append(
            "\n### Approved acceptance criteria — THE definitive list\n\n"
            "Your Acceptance Criteria Checklist must restate exactly these, and "
            "nothing else. The specification below may contain an older set: "
            "criteria were added, reworded or REMOVED when this plan was "
            "approved, and the spec was not rewritten. Any criterion that "
            "appears in the spec but not here was deliberately struck — do not "
            "reinstate it, however reasonable it looks. Where the two lists "
            "disagree, this one is correct.\n\n"
        )
        block.extend(f"{i}. {c}\n" for i, c in enumerate(criteria, 1))
    return "".join(block).rstrip()


# ── RAG retrieval queries ────────────────────────────────────────────────────
#
# The server embeds the last user message by default, which is right for chat
# and wrong here: our user messages lead with the whole spec and design, and
# all-MiniLM-L6-v2 truncates at 256 tokens (~1000 chars). A per-file message is
# many times that, so the actual ask at the end never reaches retrieval and
# every file in a project retrieves on the same spec preamble (DEV-489).
#
# These build short queries that describe the real subject. Keep them well
# under the truncation limit — that is the whole point.
_MEMORY_QUERY_MAX_CHARS = 600

def spec_memory_query(spec_md: str) -> str:
    """The spec's TITLE — the one line that says what this work is about.

    Deliberately NOT the opening paragraph (DEV-494). That paragraph is mostly
    process boilerplate — "targets an existing repository, not a greenfield
    project", "the Mac runner has it registered as", "contains a working SwiftPM
    scaffold with a green test baseline" — and that generic engineering prose is
    what retrieval latches onto. The Centipede spec pulled MTLPipelineOption,
    MTLPipelineOptionNone and addComputePipelineFunctions into a design about
    mushroom fields at distance 0.508, i.e. more confidently than most genuine
    matches score.

    Measured against the live collection: the title alone returns nothing for
    that spec (correct — a corpus of Apple API docs holds nothing about
    centipede movement), while "Make the Stop button actually cancel MLX
    generation" returns cancelAction and cancel(_:action:). Adding the paragraph
    back is what produces the noise; it was never the path or the title.

    Falls back to the opening prose only when a spec has no heading at all.
    """
    title = ""
    body: list[str] = []
    for line in spec_md.splitlines():
        stripped = line.strip()
        if not stripped:
            if body:
                break          # first paragraph is enough
            continue
        if stripped.startswith("#"):
            if not title:
                title = stripped.lstrip("#").strip()
                break
            continue
        body.append(stripped)
    return (title or " ".join(body))[:_MEMORY_QUERY_MAX_CHARS]


def file_memory_query(entry: "ManifestEntry") -> str:
    """The one file being written — path, purpose, and what it exports."""
    return " ".join(filter(None, [
        entry.path, entry.purpose, entry.exports,
    ]))[:_MEMORY_QUERY_MAX_CHARS]


def _render_reference_files(reference_files: list[tuple[str, str]]) -> str:
    """Read-only view of files the spec puts off-limits (DEV-492 / DEV-427).

    Protected files are stripped from the dispatch payload, so neither role has
    ever seen them — yet they are compiled into the build, and everything they
    declare is already in scope. That blind spot is what produced Centipede
    run 5's `invalid redeclaration of 'Field'`: the design created a second
    `Field` because the one in the protected scaffold was invisible to it.

    Visibility here is safe by construction: a protected path can never be
    written back, so showing it cannot widen what the pipeline may change.
    """
    out = [
        "## Existing files you may NOT change (read-only context)\n\n",
        "These files already exist and are already compiled into the target. "
        "The spec puts them off-limits: anything you write to these paths is "
        "discarded, not merged. Do NOT create, modify, emit or re-declare "
        "them.\n\n"
        "They are shown so you can USE what they already provide. Everything "
        "they declare — types, constants, functions — is in scope already. "
        "Declaring any of it a second time is a redeclaration error, not a "
        "new feature; reference the existing declaration instead, or extend "
        "it in a file you are allowed to write.\n\n",
    ]
    budget = EXISTING_FILES_MAX_CHARS
    omitted: list[str] = []
    for path, content in reference_files:
        if len(content) > budget:
            omitted.append(path)
            continue
        budget -= len(content)
        out.append(f"### {path} (read-only)\n\n````\n{content}\n````\n\n")
    if omitted:
        out.append(
            "**Not shown** (over the context budget), but still off-limits and "
            "still compiled in: " + ", ".join(omitted) + "\n\n"
        )
    return "".join(out)


def _render_approval_conditions(notes: str, *, approved: str,
                                author: str) -> str:
    """Operator conditions attached to an APPROVED gate (DEV-546).

    Rejection notes propagated and demonstrably worked; approval notes were
    stored, mirrored to Jira, and read by nobody, while the API accepted them
    identically on both decisions. A reviewer who approves "with these three
    one-liners fixed" was talking to no one.

    The wording matters as much as the plumbing. This is NOT a rejection: the
    artefact was approved and must not be re-litigated or rewritten wholesale.
    It is a short list of conditions attached to that approval.
    """
    return (
        f"## Reviewer conditions on the approved {approved}\n\n"
        f"The {approved} below was **approved** — do not redesign or "
        f"re-litigate it. The {author} attached the following conditions to "
        f"that approval, and they are HARD REQUIREMENTS at the same authority "
        f"as the specification. Apply each one literally.\n\n"
        f"{notes.strip()}\n"
    )


def build_architect_message(spec_md: str,
                            rejection_notes: str | None = None,
                            plan_yaml: str | None = None,
                            reference_files: list[tuple[str, str]] | None = None,
                            approval_conditions: str | None = None,
                            ) -> list[dict[str, str]]:
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
    constraints_block = _render_plan_constraints(plan_yaml) if plan_yaml else None
    if constraints_block:
        user_parts.append(constraints_block + "\n\n---\n\n")
    # DEV-546: alongside the plan's own constraints, since both are the
    # operator speaking about the same approved plan.
    if approval_conditions and approval_conditions.strip():
        user_parts.append(
            _render_approval_conditions(approval_conditions, approved="plan",
                                        author="operator")
            + "\n---\n\n")
    if reference_files:
        user_parts.append(_render_reference_files(reference_files) + "---\n\n")
    user_parts.append(
        "## Specification\n\n"
        f"{spec_md}\n\n---\n\n"
        "Your task: produce a complete architecture design for this project. "
        "Output exactly one <<<DESIGN>>>…<<<END>>> block as instructed."
    )
    if constraints_block:
        # Restated after the spec: the failure this guards against is the
        # architect reading an ambiguous spec *last* and re-opening a question
        # the operator already settled, so the constraint needs recency as much
        # as primacy.
        user_parts.append(
            "\nHonor the approved plan's binding constraints above — where the "
            "specification is ambiguous or suggests an alternative, the plan "
            "is the answer. If the plan listed approved acceptance criteria, "
            "your checklist restates those and only those; a criterion present "
            "in the specification but absent there was struck on purpose "
            "(DEV-490)."
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
    - AFFORDANCE: separately from whether a criterion is well phrased, can a test
      actually carry it out THROUGH THE API THIS DESIGN SPECIFIES? For each
      criterion walk the three steps a test needs — construct the starting state,
      invoke the behaviour, observe the outcome — and name any step that has no
      reachable entry point. A criterion can be perfectly phrased and still be
      impossible: a collection exposed read-only with only a seeded initialiser
      cannot be positioned for a boundary case; an enum compared for equality
      that is not declared Equatable cannot be asserted on; a value the criteria
      refer to only in prose has no symbol to assert against. These are DESIGN
      defects — say which criterion is stranded and which seam is missing.
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


# Ceiling on existing-file context injected into the implementer prompt
# (DEV-492). The single-call implementer runs at 32k tokens total, and the
# spec, design and clarifications all have to fit alongside. ~60k chars is
# roughly 15k tokens — enough for the handful of files a change actually
# touches, small enough that it cannot crowd out the instructions telling the
# model what to do with them. Files past the ceiling are named, never silently
# dropped: "you were not shown X" is actionable, a missing section is not.
EXISTING_FILES_MAX_CHARS = int(
    os.getenv("AUTONOMOUS_EXISTING_FILES_MAX_CHARS", "60000"))


def _render_existing_files(existing_files: list[tuple[str, str]]) -> str:
    """Current repo contents, framed as ground truth the model must preserve.

    Deliberately not wrapped in the <<<FILE:…>>> delimiters the implementer
    emits — those are parsed out of the *response*, and echoing the input
    format invites the model to treat these as already-emitted files.
    """
    out = [
        "## Current contents of files you must modify\n\n",
        "These are the ACTUAL contents in the repository right now. They are "
        "ground truth and outrank any description of these files in the spec "
        "or design. Reproduce every declaration exactly as it appears here "
        "except for the specific changes the design calls for — do not "
        "rewrite, reorder, re-indent, modernise or 'improve' anything you "
        "were not asked to change. Anything you drop is deleted from the "
        "repository.\n\n",
    ]
    budget = EXISTING_FILES_MAX_CHARS
    omitted: list[str] = []
    for path, content in existing_files:
        if len(content) > budget:
            omitted.append(path)
            continue
        budget -= len(content)
        out.append(f"### {path}\n\n````\n{content}\n````\n\n")
    if omitted:
        out.append(
            "**Not shown** (over the context budget): "
            + ", ".join(omitted)
            + ". You have NOT seen these files. Do not emit them — emitting a "
              "file you have not read would replace it with an invention.\n\n"
        )
    return "".join(out)


def build_implementer_message(
    spec_md: str,
    design_md: str,
    rejection_notes: str | None = None,
    clarifications: list[str] | None = None,
    existing_files: list[tuple[str, str]] | None = None,
    reference_files: list[tuple[str, str]] | None = None,
    approval_conditions: str | None = None,
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
    # DEV-546: with the clarifications, and for the same reason — both are the
    # operator speaking at spec authority, and both are read before the design
    # so the model does not treat the design as the last word.
    if approval_conditions and approval_conditions.strip():
        user_parts.append(
            _render_approval_conditions(approval_conditions, approved="design",
                                        author="reviewer") + "\n")
    user_parts.extend([
        "## Specification\n\n",
        spec_md,
        "\n\n## Architecture Design\n\n",
        design_md,
    ])
    # After the design, so the model reads intent first and then the reality it
    # has to preserve — and close to the task instruction, where it is most
    # salient at the moment of writing (DEV-492).
    if existing_files:
        user_parts.append("\n\n")
        user_parts.append(_render_existing_files(existing_files))
    if reference_files:
        user_parts.append("\n\n")
        user_parts.append(_render_reference_files(reference_files))
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
    """) + SWIFT_VALUE_SEMANTICS


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
#
# DEV-467: the original pattern matched a declaration keyword at the start of the
# line, which works for Python/TS/Rust but drops almost everything in Swift,
# because Swift puts modifiers first — `mutating func`, `final class`,
# `private(set) var`, `@discardableResult func`. On spec_cc7dd609 the summary
# handed to World.swift was four lines of `struct X {` with not one initialiser,
# method or property, so every cross-file call was a guess and six of them were
# wrong. Modifiers and attributes may now precede the keyword, and the keyword
# set covers Swift's declaration forms. Additive only: every line the old pattern
# matched still matches.
_DECL_MODIFIER = (
    r"(?:@[\w.]+(?:\([^()]*\))?"
    r"|(?:public|private|internal|fileprivate|open|package|static|final|mutating"
    r"|nonmutating|override|required|convenience|lazy|weak|unowned|indirect"
    r"|dynamic|optional|class|export|default|declare|abstract|readonly|async)"
    r"(?:\([^()]*\))?)"
)
_DECL_KEYWORD = (
    r"(?:export|import|from|def|class|func|function|interface|type|enum|public|pub"
    r"|impl|trait|struct|module|protocol|actor|extension|init|deinit|subscript"
    r"|typealias|associatedtype)"
)
# Deliberately NOT here: `const`. In TS a module's exported constants already
# match via `export`, while a bare `const hidden = 1` is a body line — adding it
# swept those in and broke test_summarize_extracts_interface_lines.
# Stored properties define a Swift type's memberwise initialiser, so callers need
# them — but a bare `var`/`let` also matches locals inside a function body. The
# type annotation is what distinguishes a declared property from `let x = 5`.
_DECL_PROPERTY = r"(?:var|let)\s+[\w`]+\s*:"
# Enum cases are the payload shape callers pattern-match on. Anchored to exclude
# switch cases, which lead with a dot (`case .left:`), a literal, or a binding.
_DECL_ENUM_CASE = r"case\s+[a-z_]\w*\s*(?:[,(=]|$)"
_SIGNATURE_RE = re.compile(
    rf"^\s*(?:{_DECL_MODIFIER}\s+)*"
    rf"(?:{_DECL_KEYWORD}\b|{_DECL_PROPERTY}|{_DECL_ENUM_CASE})"
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
    max_sig_lines: int = 40,
    max_total_chars: int = 12000,
) -> str:
    """Compact interface summary of already-written files for the per-file prompt.

    Emits each file's import/declaration lines (its public surface) rather than
    full bodies, so later files can import real symbols without the prompt
    ballooning. Bounded in both per-file line count and total size.

    The per-file line cap was 24 when a Swift file yielded a single matching
    line; now that properties, initialisers and cases are captured (DEV-467) a
    modest type easily reaches double figures, so a type with many members would
    truncate before its interface was fully described. `max_total_chars` remains
    the real bound on prompt growth.
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
    approval_conditions: str | None = None,
) -> list[dict[str, str]]:
    parts: list[str] = []
    if approval_conditions and approval_conditions.strip():
        # DEV-546: a condition can change the file SET, not just file contents.
        parts.append(_render_approval_conditions(
            approval_conditions, approved="design", author="reviewer") + "\n")
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
    existing_content: str | None = None,
    reference_files: list[tuple[str, str]] | None = None,
    approval_conditions: str | None = None,
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
    # DEV-546: this is where the code is actually written, so a condition the
    # reviewer attached to the approved design has to reach here too.
    if approval_conditions and approval_conditions.strip():
        parts.append(_render_approval_conditions(
            approval_conditions, approved="design", author="reviewer") + "\n")
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
    # Protected files must reach the per-file path too. Each call here is
    # isolated — the model sees the manifest and interface summaries of what has
    # been written, but nothing about files the spec put off-limits, which are
    # nonetheless compiled into the target. Centipede run 8 proved the cost: the
    # architect had this context and correctly used the scaffold's `Field`, then
    # the per-file implementer, which did not, declared `struct Field` in
    # World.swift and reproduced run 5's `invalid redeclaration of 'Field'`.
    if reference_files:
        parts.append("\n" + _render_reference_files(reference_files))
    # Manifest mode writes one file per call, so unlike the single-call path it
    # needs only the target's own current content — which is exactly the file at
    # risk of being reconstructed (DEV-492).
    if existing_content is not None:
        parts.extend([
            f"\n## Current content of {target.path} — this file ALREADY EXISTS\n\n",
            "You are EDITING this file, not writing it from scratch. What "
            "follows is its actual content in the repository. Reproduce every "
            "declaration exactly except for the specific changes asked of you "
            "above; anything you omit is deleted from the repository.\n\n",
            f"````\n{existing_content}\n````\n",
        ])
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


# ── Deterministic Swift `import Foundation` (DEV-540) ───────────────────────
#
# Same principle as the package.json pinning above, for the Swift equivalent:
# a file that names `UUID` and forgets `import Foundation` does not compile,
# there is exactly one correct fix, and no judgement is involved. It was the
# top terminal failure of Centipede runs 7 and 8 — 11 diagnostics in one, 10 in
# the other — and it recurs because manifest mode regenerates files constantly
# and every regeneration is a fresh chance to drop the line.
#
# Adding the import is safe in a way most normalizations are not: a redundant
# `import Foundation` is legal Swift and a no-op, so a false positive costs a
# line of source and nothing else. The two guards below exist only to keep the
# diff honest, not because getting them wrong would break a build.

# Types that live in Foundation and have no Swift-standard-library equivalent.
_FOUNDATION_ONLY_SYMBOLS = (
    "Bundle", "Calendar", "Data", "Date", "DateComponents", "DateFormatter",
    "FileManager", "IndexSet", "JSONDecoder", "JSONEncoder",
    "JSONSerialization", "Locale", "Measurement", "NotificationCenter",
    "NSRegularExpression", "NumberFormatter", "OperationQueue", "ProcessInfo",
    "TimeInterval", "TimeZone", "URL", "URLComponents", "URLRequest",
    "URLSession", "UUID", "UserDefaults",
)

# Importing any of these already brings Foundation in transitively, so adding
# it would be pure noise in the diff.
_FOUNDATION_REEXPORTERS = frozenset({
    "AppKit", "Cocoa", "Foundation", "SwiftUI", "UIKit",
})

_SWIFT_IMPORT_RE = re.compile(
    r"^[ \t]*(?:@testable[ \t]+)?import[ \t]+([A-Za-z_]\w*)", re.MULTILINE)
_SWIFT_DECL_RE = re.compile(
    r"\b(?:struct|class|enum|protocol|actor|typealias)\s+([A-Za-z_]\w*)")
# Strings first: a URL literal contains `//`, and stripping comments before
# strings would eat the rest of that line.
_SWIFT_STRING_RE = re.compile(r'"""(?:.|\n)*?"""|"(?:\\.|[^"\\\n])*"')
_SWIFT_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_SWIFT_LINE_COMMENT_RE = re.compile(r"//[^\n]*")


def _swift_code_only(src: str) -> str:
    """Blank out string literals and comments so symbol matching sees code.

    Without this, a doc comment reading "returns a UUID" would request an
    import the file does not need.
    """
    src = _SWIFT_STRING_RE.sub('""', src)
    src = _SWIFT_BLOCK_COMMENT_RE.sub(" ", src)
    return _SWIFT_LINE_COMMENT_RE.sub("", src)


def _first_swift_code_line(lines: list[str]) -> int:
    """Index of the first line that is neither blank nor part of a header
    comment — i.e. where an import may be inserted without landing inside the
    file's leading comment block."""
    i = 0
    in_block = False
    while i < len(lines):
        stripped = lines[i].strip()
        if in_block:
            if "*/" in stripped:
                in_block = False
        elif not stripped or stripped.startswith("//"):
            pass
        elif stripped.startswith("/*"):
            if "*/" not in stripped[2:]:
                in_block = True
        else:
            return i
        i += 1
    return len(lines)


# DEV-552: a generated file may not redeclare a type a protected file already
# declares. Anchored at column 0 on both sides deliberately — a NESTED type of
# the same name is a different type in a different scope and is perfectly
# legal, so matching indented declarations would delete working files over a
# name collision that the compiler is perfectly happy with. `extension` is
# absent from the alternation for the same reason: extending a protected type
# is the correct way to add to it.
_SWIFT_TOPLEVEL_DECL_RE = re.compile(
    r"^(?:(?:public|internal|fileprivate|private|final|open|indirect)\s+)*"
    r"(?:struct|class|enum|protocol|actor|typealias)\s+([A-Za-z_]\w*)",
    re.MULTILINE)


def declared_top_level_types(content: str) -> "set[str]":
    """Type names declared at file scope in Swift source."""
    return set(_SWIFT_TOPLEVEL_DECL_RE.findall(_swift_code_only(content)))


def protected_type_collisions(
    files: "list[tuple[str, str]]",
    protected_files: "list[tuple[str, str]]",
) -> "list[tuple[str, list[str], bool]]":
    """Generated files that redeclare a type a protected file already declares.

    Returns one entry per offending file: `(path, colliding names, total)`,
    where *total* is True when EVERY top-level type the file declares collides
    — i.e. the file is a pure duplicate and dropping it loses nothing.

    Run 10 of DEV-102 died on exactly this. The synthesis repair invented
    `Sources/CentipedeCore/Field.swift` declaring `public struct Field`, while
    the protected `GameState.swift` already declared `public enum Field`. Every
    diagnostic in that final build came from the one invented file, and the
    pipeline could not have fixed it on a later attempt either: the file it
    collides with is one it is forbidden to edit.

    A protected file is never itself an offender — it is not generated, and it
    is dropped from the dispatch anyway.
    """
    protected_paths = {p for p, _ in protected_files}
    declared: set[str] = set()
    for path, content in protected_files:
        if path.endswith(".swift"):
            declared |= declared_top_level_types(content)
    if not declared:
        return []

    out: list[tuple[str, list[str], bool]] = []
    for path, content in files:
        if not path.endswith(".swift") or path in protected_paths:
            continue
        mine = declared_top_level_types(content)
        hits = sorted(mine & declared)
        if hits:
            out.append((path, hits, mine == set(hits)))
    return out


def _ensure_foundation_import(content: str) -> tuple[str, list[str]]:
    """Add `import Foundation` to Swift source that needs it and lacks it.

    Returns (content, symbols that required it); an empty list means no change.
    A symbol the file declares itself is ignored — a local `struct Timer` is
    not a reference to Foundation's.
    """
    if _FOUNDATION_REEXPORTERS & set(_SWIFT_IMPORT_RE.findall(content)):
        return content, []

    code = _swift_code_only(content)
    declared = set(_SWIFT_DECL_RE.findall(code))
    needed = [
        sym for sym in _FOUNDATION_ONLY_SYMBOLS
        if sym not in declared
        # Not preceded by a word char or a dot: skips `MyUUID` and `x.Date`.
        and re.search(rf"(?<![\w.]){sym}\b", code)
    ]
    if not needed:
        return content, []

    lines = content.splitlines(keepends=True)
    first_import = next(
        (i for i, line in enumerate(lines) if _SWIFT_IMPORT_RE.match(line)),
        None,
    )
    if first_import is not None:
        lines.insert(first_import, "import Foundation\n")
    else:
        at = _first_swift_code_line(lines)
        lines[at:at] = ["import Foundation\n", "\n"]
    return "".join(lines), needed


def normalize_boilerplate(
    files: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[str]]:
    """Deterministically fix boilerplate the reviewer checks. Returns the
    (possibly rewritten) file list and a list of human-readable change notes.

    Currently: exact-pin dependency ranges in every package.json, and add a
    missing `import Foundation` to Swift files that need one (DEV-540).
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
        elif path.endswith(".swift"):
            new_content, needed = _ensure_foundation_import(content)
            if needed:
                notes.append(
                    f"{path}: added `import Foundation` "
                    f"(references {', '.join(needed)})"
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


# Assertions that can never fail (DEV-407 — spec_96d7e07f's synthesized
# tests contained `assert.ok(x || true)`, inflating the pass count). Line-
# based on purpose: cheap, language-tolerant, and a miss only costs one
# undetected vacuous assert — never a false hard-failure.
_TAUTOLOGY_PATTERNS = (
    re.compile(r"\b(?:assert|expect|ok)\w*\s*\(.*\|\|\s*(?:true|1)\b"),
    re.compile(r"\bassert(?:\.ok)?\s*\(\s*true\b"),
    re.compile(r"\bexpect\s*\(\s*(true|1)\s*\)\s*\.\s*toBe\s*\(\s*\1\s*\)"),
    re.compile(r"\bassertTrue\s*\(\s*True\s*\)"),
    re.compile(r"^\s*assert\s+True\b"),
)
_TEST_FILE_PATH_RE = re.compile(
    r"(?:^|/)tests?/|\.test\.|\.spec\.|(?:^|/)test_")


def scan_tautological_asserts(files: list[tuple[str, str]]) -> list[str]:
    """Assertions in TEST files that can never fail, as 'path:line — text'.

    Only test files are scanned — `x or True` in implementation code is
    ordinary logic, not a vacuous test.
    """
    results: list[str] = []
    for path, content in files:
        if not _TEST_FILE_PATH_RE.search(path):
            continue
        for ln, line in enumerate(content.splitlines(), 1):
            if any(pat.search(line) for pat in _TAUTOLOGY_PATTERNS):
                results.append(f"{path}:{ln} — {line.strip()[:120]}")
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

    # Same deterministic-scan pattern as the `any` section: hand the
    # reviewer line-precise facts instead of hoping it notices (DEV-407).
    tautologies = scan_tautological_asserts(code_files)
    tautology_section = ""
    if tautologies:
        tautology_section = (
            "\n## Tautological assertions detected\n\n"
            "These assertions can NEVER fail, so any pass count that includes "
            "them overstates real coverage. Name each in your review and write "
            "a real assertion for the same behavior in your own tests. This "
            "list alone is NOT grounds for a FAIL verdict:\n\n"
            + "\n".join(f"- {t}" for t in tautologies) + "\n"
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
            + tautology_section
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
    """) + SWIFT_VALUE_SEMANTICS


def build_synthesis_message(
    spec_md: str,
    design_md: str,
    attempts: list[dict],
    review_notes: list[str] | None = None,
    reference_files: list[tuple[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Build a single synthesis prompt that gives the model the full
    rotation history (code + per-attempt test outcome) and asks for a
    merged best version.

    Each attempt dict has keys: retry (int), agent (str), test_summary
    (str — last ~1500 chars of pytest output), files (dict of relpath→content).

    review_notes carries the human reviewer's notes from any rejected code
    review gates, oldest first (DEV-433). When the attempts were rejected at
    the gate rather than by a failing test run, these are the only record of
    *which* attempt got *which* part right — a judgement the merge cannot
    recover from the code alone.
    """
    parts = [
        "## Specification\n\n", spec_md,
        "\n\n## Architecture Design\n\n", design_md,
    ]
    # DEV-552: synthesis was the only generation that could not see the files
    # it is forbidden to edit, and it is reached only after every retry is
    # spent — the worst possible place to be missing that context.
    if reference_files:
        parts.append("\n\n" + _render_reference_files(reference_files))
    if review_notes:
        parts.append("\n\n## Reviewer feedback on the attempts below\n\n")
        parts.append(
            "These are the human reviews that rejected the attempts, oldest "
            "first. Where a review says a part is correct and should be kept, "
            "keep it verbatim; where it names a defect, fix it.\n\n"
        )
        for i, note in enumerate(review_notes, 1):
            parts.append(f"### Review {i}\n\n{note}\n\n")
    parts.append("\n\n## Prior Attempts\n\n")
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


def build_synthesis_repair_message(
    spec_md: str,
    design_md: str,
    files: "list[tuple[str, str]]",
    failing_output: str,
    build_diagnostic: str | None = None,
    warning_diagnostic: str | None = None,
    reference_files: list[tuple[str, str]] | None = None,
) -> list[dict[str, str]]:
    """One targeted repair round on a synthesized artifact.

    Unlike synthesis (which merges all attempts), the repair sees only the
    synthesized files plus the failure excerpt, and is told to change the
    minimum — a 15/17 artifact should not be re-imagined (DEV-406).

    ``build_diagnostic`` carries the first compiler error when the synthesis
    failed to BUILD rather than to pass (DEV-522). DEV-469 opened that path
    but left DEV-406's near-miss wording behind it, so the repair was told
    its code "already passes most of its tests" about code that compiled not
    at all, under a "## Failing tests" heading holding compiler diagnostics.
    The damage is not the false flattery but the instruction that follows it:
    "do not restructure or rewrite passing behavior" reads as "edit where the
    error is reported", and a missing conformance is reported at the USE site
    while the fix belongs at the DECLARATION. Run 6 of spec_1ba2db3d died one
    word from compiling because the repair rewrote the test that cited the
    error instead of adding ``: Equatable`` to the type.

    ``warning_diagnostic`` is the third case (DEV-547): the code compiled and
    the run produced no usable test result — run 9 of spec_9ff962b9 trapped at
    runtime with 19 tests started and 0 completed — but the compiler flagged
    generated code as contradicting itself. That is neither of the cases above.
    Saying the build failed would send the model hunting a syntax error that
    does not exist; the near-miss wording is worse still, because nothing here
    is known to pass. Both existing prompts are left byte-identical.
    """
    building = build_diagnostic is not None
    warning_only = not building and warning_diagnostic is not None
    if building:
        state = "\n\n## Current implementation (does NOT compile)\n\n"
    elif warning_only:
        state = "\n\n## Current implementation (compiles; no usable test result)\n\n"
    else:
        state = "\n\n## Current implementation (passes most tests)\n\n"
    parts = [
        "## Specification\n\n", spec_md,
        "\n\n## Architecture Design\n\n", design_md,
    ]
    # DEV-552: the repair is the LAST generation of the run. Run 10 died here
    # because it invented a file redeclaring a type the protected scaffold
    # already had — it had never been shown that scaffold.
    if reference_files:
        parts.append("\n\n" + _render_reference_files(reference_files))
    parts.append(state)
    for relpath, content in files:
        parts.append(f"### {relpath}\n\n```\n{content}\n```\n\n")
    if warning_only:
        parts.append(
            "## Compiler warnings on generated code\n\n```\n"
            + warning_diagnostic + "\n```\n\n"
            "## Test runner output\n\n```\n" + failing_output + "\n```\n\n"
            "---\n\n"
            "This implementation COMPILED, but the test run produced no usable "
            "result — so nothing here is known to pass, and none of it is "
            "protected. Do not go looking for a syntax error; there is none.\n\n"
            "The warnings above are the evidence. Each one is a place where the "
            "compiler proved the code does not do what it reads as doing: a "
            "conditional binding reported as unused means the condition is not "
            "testing what it appears to test; unreachable code and always-true "
            "comparisons mean a branch can never run. A defect of that shape "
            "commonly traps at runtime and takes the whole test process down "
            "before any test can report, which matches what happened here.\n\n"
            "Fix the CAUSE of each warning, then re-check the surrounding logic "
            "for the same mistake — a wrong condition is rarely alone. Change a "
            "test only when the test is itself what is wrong.\n\n"
            "Output <<<FILE: path>>>…<<<END_FILE>>> blocks for every file you "
            "change, each with its complete content."
        )
    elif building:
        parts.append(
            "## Compiler diagnostics\n\n```\n" + failing_output + "\n```\n\n"
            "---\n\n"
            "This implementation FAILED TO COMPILE. No test ran, so no "
            "behavior here is known to pass and none of it is protected — "
            "getting the build correct comes first.\n\n"
            "Fix the CAUSE of each diagnostic. The cause is often NOT in the "
            "file the diagnostic names: a missing protocol or interface "
            "conformance is reported at the line that USES the type and is "
            "fixed at the line that DECLARES it. Add the conformance to the "
            "declaration rather than rewriting the caller to avoid needing "
            "it. Change a test only when the test is itself what is wrong.\n\n"
            "Output <<<FILE: path>>>…<<<END_FILE>>> blocks for every file you "
            "change, each with its complete content."
        )
    else:
        parts.append(
            "## Failing tests\n\n```\n" + failing_output + "\n```\n\n---\n\n"
            "This implementation already passes most of its tests. Fix ONLY "
            "what the failures above require — do not restructure or rewrite "
            "passing behavior. Output <<<FILE: path>>>…<<<END_FILE>>> blocks "
            "for JUST the files you change, each with its complete content."
        )
    return [
        {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
        {"role": "user", "content": "".join(parts)},
    ]
