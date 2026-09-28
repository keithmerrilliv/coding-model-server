"""Env-derived settings for the agent layer: which agent plays each role,
the token and time budgets, the parse-retry counts and the mode switches.

The one home for every AUTONOMOUS_* knob the agent layer reads. Callers and
tests read and patch these here; executor re-exports them for old importers
only, and a patch on executor does not reach this module.
"""
from __future__ import annotations

import logging
import os



logger = logging.getLogger("orchestrator.executor")


# ── Configuration ────────────────────────────────────────────────────────────

ARCHITECT_AGENT = os.getenv("AUTONOMOUS_ARCHITECT_AGENT", "dense_architect")
IMPLEMENTER_AGENT = os.getenv("AUTONOMOUS_IMPLEMENTER_AGENT", "implementer")
REVIEWER_AGENT = os.getenv("AUTONOMOUS_REVIEWER_AGENT", "deep_reviewer")

# Design review (#3): a pre-implementation LLM critique of the architect's design.
# Uses the light/fast `reviewer` (Coder-30B) by default — design input is small,
# and turnaround matters. Bounded + fail-open in the orchestrator.
#
# DEFAULT OFF since DEV-440 (2026-09-17). Over its entire life this stage
# returned **37 FAIL and 1 PASS** across 38 runs (2026-07-12 to 2026-09-16):
#   2026-07  15 FAIL /  0 PASS
#   2026-08  13 FAIL /  1 PASS
#   2026-09   9 FAIL /  0 PASS
# A verdict that is FAIL 97.4% of the time carries no information. It is not a
# reviewer that is sometimes wrong; as a signal it is a constant, and the
# pipeline was paying an architect revision for a constant. The calls are cheap
# (38 runs, median 47s) but each FAIL bounced the design back, so ~37 architect
# re-runs at a median 268s — roughly 2.8 hours — plus the regression risk
# DEV-440 documents: revising a CORRECT design invites the architect to change
# things that were right (it silently dropped an acceptance criterion doing
# exactly that on spec_cc7dd609).
#
# Deliberately disabled rather than deleted: the idea is sound and the ticket
# lists the fixes that would make it earn its place (a FAIL must name inputs and
# a wrong output; explicitness requests are advisory, not blocking). Re-enable
# with AUTONOMOUS_DESIGN_REVIEW=1 once there is a replay corpus on which the
# PASS rate is not zero. `testability_check` and the human design_approval gate
# cover this ground in the meantime.
DESIGN_REVIEW_ENABLED = os.getenv("AUTONOMOUS_DESIGN_REVIEW", "0").lower() not in ("0", "false", "no")
DESIGN_REVIEW_AGENT = os.getenv("AUTONOMOUS_DESIGN_REVIEW_AGENT", "deep_reviewer")
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
# truncated/unparseable before treating it as a soft FAIL (→ retry)
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

# DEV-657 part 1: the OTHER half of the opt-in. Retrieval was conditioned on
# the role and nothing else, so an all-Apple corpus was queried for every
# opted-in call whatever the spec was written in — on run 29, on every
# implementer call of a Python self-target spec. Across the 82 stored plans,
# 32 (39%) are Python or JavaScript: two in five retrievals were asking a
# Swift documentation corpus about code it has nothing to say on.
#
# This is config, not a hardcoded "swift", because it is a statement about
# what the CORPUS covers rather than about the pipeline. The day a Python
# corpus is indexed, this is the line that changes.
# DEV-781: one spelling per language, so the planner's choice of "objc",
# "Obj-C" or "Objective-C" cannot switch retrieval off. Unknown names pass
# through lowercased — the gate then says "language_not_covered" for them.
_LANGUAGE_ALIASES = {
    "objc": "objective-c", "obj-c": "objective-c", "objectivec": "objective-c",
    "objective c": "objective-c", "objective-c": "objective-c",
    "objc++": "objective-c++", "obj-c++": "objective-c++",
    "objcpp": "objective-c++", "objective c++": "objective-c++",
    "objective-c++": "objective-c++", "objectivec++": "objective-c++",
    "py": "python", "swiftui": "swift",
}


def normalize_language(name: "str | None") -> "str | None":
    """Canonical lowercase language name, or None for empty input."""
    if name is None:
        return None
    key = str(name).strip().lower()
    if not key:
        return None
    return _LANGUAGE_ALIASES.get(key, key)


# Extension -> language, in precedence order: Objective-C++ is the most
# specific claim a change surface can make, then Objective-C, then Swift. A
# `.h` alone decides nothing (it could be any of the three).
_EXTENSION_LANGUAGES = (
    (".mm", "objective-c++"),
    (".m", "objective-c"),
    (".swift", "swift"),
    (".py", "python"),
    (".ts", "typescript"), (".tsx", "typescript"),
    (".js", "javascript"), (".jsx", "javascript"),
    (".rs", "rust"), (".go", "go"), (".kt", "kotlin"), (".java", "java"),
)


def language_from_paths(paths: "list[str] | None") -> "str | None":
    """The language a set of file paths implies (DEV-781), or None.

    The change surface is the one deterministic signal for Objective-C++:
    a `.mm` file is Objective-C++ by definition, where the prose and the
    planner's guess are not. Used only when the plan does not say.
    """
    exts = {os.path.splitext(str(p))[1].lower() for p in (paths or [])}
    for ext, lang in _EXTENSION_LANGUAGES:
        if ext in exts:
            return lang
    return None


# The corpus is Apple API documentation, so it applies to every language that
# calls those APIs: Swift, Objective-C and the Objective-C half of
# Objective-C++ (DEV-781; probed 2026-09-21 — Objective-C phrasings land on
# the same framework chunks at 0.43–0.46 distance).
AUTONOMOUS_MEMORY_LANGUAGES = {
    normalize_language(x) for x in _parse_memory_roles(
        os.getenv("AUTONOMOUS_MEMORY_LANGUAGES",
                  "swift,objective-c,objective-c++"))
    if normalize_language(x)}


def retrieval_decision(role: str, language: "str | None") -> tuple[bool, str]:
    """Whether RAG runs for this call, and the reason in either direction.

    The reason is returned rather than logged here so the caller can put it
    on the event (DEV-657 part 2) — "retrieval did not run" and "retrieval
    ran and found nothing" look identical downstream otherwise, which is
    exactly how DEV-488 hid for weeks (DEV-501).

    An unknown language does NOT retrieve. The corpus is Apple-specific, so
    "we could not tell what this spec is" is not evidence that it is Swift,
    and the cost of a wrong guess is asymmetric: injecting irrelevant docs is
    a measured harm, while the benefit of retrieving is exactly what part 3
    has yet to establish. Every one of the 82 stored plans carries a readable
    language, so this branch is a guard, not a routine path — and it says so
    out loud rather than quietly disarming retrieval.
    """
    if role.lower() not in AUTONOMOUS_MEMORY_ROLES:
        return False, "role_not_opted_in"
    lang = normalize_language(language)
    if not lang:
        return False, "language_unknown"
    if lang not in AUTONOMOUS_MEMORY_LANGUAGES:
        return False, "language_not_covered"
    return True, "retrieved"

ARCHITECT_TIMEOUT = float(os.getenv("AUTONOMOUS_ARCHITECT_TIMEOUT", "2700"))
IMPLEMENTER_TIMEOUT = float(os.getenv("AUTONOMOUS_IMPLEMENTER_TIMEOUT", "1800"))
REVIEWER_TIMEOUT = float(os.getenv("AUTONOMOUS_REVIEWER_TIMEOUT", "2700"))

ARCHITECT_MAX_TOKENS = int(os.getenv("AUTONOMOUS_ARCHITECT_MAX_TOKENS", "8000"))
# DEV-760: a rejection retry is a harder generation than the first pass. It
# must hold the previous design and the feedback and still re-derive the whole
# document, and since DEV-556 its reasoning shares this budget with the answer.
# Run 46 (spec_c6f4902f): the first pass finished a 9,505-byte design in 4,879
# completion tokens; the retry spent 10,000 four times and its last call was
# cut off forty words into the correct design. So a retry gets twice the room.
ARCHITECT_RETRY_MAX_TOKENS = int(os.getenv(
    "AUTONOMOUS_ARCHITECT_RETRY_MAX_TOKENS", str(2 * ARCHITECT_MAX_TOKENS)))


def architect_max_tokens(is_retry: bool) -> int:
    """The architect's completion budget for a first pass or a retry."""
    return ARCHITECT_RETRY_MAX_TOKENS if is_retry else ARCHITECT_MAX_TOKENS


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

# DEV-581: diff-based edits. When ON, the single-call implementer emits anchored
# SEARCH/REPLACE edit blocks for files that ALREADY EXIST (their current content
# is shown in the prompt) instead of re-emitting the whole file, which the model
# corrupts on large files and re-corrupts on every retry. New files keep
# whole-file emission. Default ON, the value production has run since DEV-581
# (DEV-836 moved the code default to match). 0 is the rollback lever: the
# implement path is then byte-identical to the pre-DEV-581 whole-file
# behaviour, and the seam tier pins it off. See docs/EDITS.md.
DIFF_BASED_EDITS = os.getenv(
    "AUTONOMOUS_DIFF_BASED_EDITS", "1").lower() in ("1", "true", "yes")

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

# DEV-604: a large existing file cannot be faithfully re-emitted whole inside
# PER_FILE_MAX_TOKENS — runs 18 and 19 shipped fragments (33 of 117 lines;
# 43 of 5,804) exactly this way. When a manifest entry already exists, edit
# mode is unavailable, and its current content exceeds this many chars, the
# entry is refused loudly instead of generated blind. 40000 chars ≈ 10k
# tokens, comfortably inside the 16k-token per-file output budget. 0 disables.
MANIFEST_WHOLE_FILE_MAX_CHARS = int(
    os.getenv("AUTONOMOUS_MANIFEST_WHOLE_FILE_MAX_CHARS", "40000"))

# DEV-649: synthesis and its repair have no edit mode — they emit whole
# <<<FILE:>>> blocks — so re-emitting an existing file costs its whole size in
# output tokens. Run 28 asked deep_reviewer to merge a 145,825-char
# executor.py inside a 32,000-token budget: re-emitting it needs ~48,600, so
# every possible answer was a stub and the DEV-636 shrink guard refused three
# of them, 77 minutes and a 213K-token prompt later. This is the same
# arithmetic MANIFEST_WHOLE_FILE_MAX_CHARS settles for per-file mode, stated
# as a fraction of the budget rather than a second independent ceiling
# (DEV-633): the existing files synthesis must reproduce may claim at most
# this share of its output budget, leaving the rest for new files and the
# block markers. 0 disables the check.
SYNTHESIS_EMIT_HEADROOM = float(
    os.getenv("AUTONOMOUS_SYNTHESIS_EMIT_HEADROOM", "0.8"))

# Architect parse-retry: how many times to re-call the architect when its
# response can't be parsed for the <<<DESIGN>>> / <<<COMPLEXITY>>> blocks.
# Total attempts = ARCHITECT_PARSE_RETRIES + 1. Default 2 retries
# (3 attempts) is enough for stochastic markdown drift; deterministic
# failures still surface after the cap. Each failed response is
# persisted to spec_dir as architect_failed_response_attempt<N>.txt
# for post-mortem.
ARCHITECT_PARSE_RETRIES = int(os.getenv("AUTONOMOUS_ARCHITECT_PARSE_RETRIES", "2"))

# Manifest parse-retry (DEV-507): same idea, same default. A delimiter typo
# says nothing about whether the agent can do the work (DEV-431), so it buys a
# re-call of the manifest rather than spending one of the implementer's
# MAX_RETRIES attempts and rotating down a tier. Each failed response is
# persisted as manifest_failed_response_attempt<N>.txt.
MANIFEST_PARSE_RETRIES = int(os.getenv("AUTONOMOUS_MANIFEST_PARSE_RETRIES", "2"))

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


def role_to_agent(role: str) -> str:
    return ROLE_TO_AGENT.get(role, IMPLEMENTER_AGENT)


# Ceiling on existing-file context injected into the implementer prompt
# (DEV-492). The single-call implementer runs at 32k tokens total, and the
# spec, design and clarifications all have to fit alongside. ~60k chars is
# roughly 15k tokens — enough for the handful of files a change actually
# touches, small enough that it cannot crowd out the instructions telling the
# model what to do with them. Files past the ceiling are named, never silently
# dropped: "you were not shown X" is actionable, a missing section is not.
EXISTING_FILES_MAX_CHARS = int(
    os.getenv("AUTONOMOUS_EXISTING_FILES_MAX_CHARS", "60000"))

# The read-only protected context gets its OWN budget (DEV-627). It used to
# share EXISTING_FILES_MAX_CHARS, so an operator raising that override for a
# large modification target silently raised this ceiling too — run 21's
# implementer prompt picked up a 420K-char protected tree on top of a 293K
# existing file and the model server refused the body with a 413. The two
# sections serve different needs: existing files must be complete enough to
# anchor edits against; protected files only need to show what they declare,
# and the "Not shown" listing preserves the off-limits instruction for
# anything past the ceiling.
PROTECTED_FILES_MAX_CHARS = int(
    os.getenv("AUTONOMOUS_PROTECTED_FILES_MAX_CHARS", "60000"))

# The reviewer's implementation section and synthesis's attempt corpus — prior
# artifacts rather than repository state, and the lowest-priority section in
# the aggregate budget (DEV-633), because they are the one section the pipeline
# can regenerate. Generous by default: the reviewer's window is 192K tokens and
# reviewing a file it was not shown is worse than a long prompt. It is a
# preference, not an independent ceiling — the allocator gives the section
# less whenever the sum against the destination window demands it.
PRIOR_ARTIFACTS_MAX_CHARS = int(
    os.getenv("AUTONOMOUS_PRIOR_ARTIFACTS_MAX_CHARS", "300000"))
