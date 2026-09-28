"""Message builders: what each role is sent. Also the sizing decisions that
depend on the design (implementer output budget, manifest mode) and the
retrieval queries sent with a call."""
from __future__ import annotations

import logging
import re
from typing import Optional

import yaml

from . import settings
from .design_testability import FILE_STRUCTURE_HEADING
from .design_testability import _section as _design_section
from .normalize import scan_any_violations, scan_tautological_asserts
from .parsers import ManifestEntry
from .prompts import (
    ARCHITECT_SYSTEM_PROMPT,
    ARCHITECT_TOOL_PROTOCOL,
    DESIGN_REVIEW_SYSTEM_PROMPT,
    IMPLEMENTER_EDIT_MODE_INSTRUCTIONS,
    IMPLEMENTER_SYSTEM_PROMPT,
    MANIFEST_SYSTEM_PROMPT,
    PER_FILE_EDIT_MODE_INSTRUCTIONS,
    PER_FILE_SYSTEM_PROMPT,
    REVIEWER_SYSTEM_PROMPT,
    SYNTHESIS_SYSTEM_PROMPT,
)
from . import languages
from .citations import render_cited_diagnostics


logger = logging.getLogger("orchestrator.executor")


def whole_file_emission_tokens(files: "list[tuple[str, str]]") -> int:
    """Output tokens needed to re-emit *files* whole, conservatively.

    Divides by the allocator's own chars-per-token estimator, which is below
    what real tokenizers average on source — so this OVERestimates the cost,
    the safe direction when the question is "can the answer fit at all".
    """
    from .context import CHARS_PER_TOKEN
    return sum(len(c) for _, c in files) // CHARS_PER_TOKEN


# ── Dynamic implementer output budget ────────────────────────────────────────

_DESIGN_FILE_PATH_RE = re.compile(
    r"[\w./-]+\.(?:ts|tsx|js|jsx|mjs|cjs|json|py|rs|go|java|kt|swift|"
    r"c|h|cc|cpp|hpp|css|scss|html|md|toml|yaml|yml|sh|sql|proto)\b"
)


def _file_structure_section(design_md: str) -> str | None:
    """The body of the design's File Structure section, or None when absent.

    One reader (DEV-838): design_testability's, which the guards already use.
    This module had its own, with a looser heading match and a fixed level-2
    boundary; on all 405 archived designs the two returned the same body.
    """
    return _design_section(design_md or "", FILE_STRUCTURE_HEADING) or None


def estimate_design_file_count(design_md: str) -> int:
    """Count the distinct file paths a design document enumerates.

    A cheap proxy for how much code the implementer must emit. Matches tokens
    that look like a path with a known code/config extension — in the File
    Structure tree, the outputs list, or prose — and de-dupes them. Used only
    to size the implementer's output budget (see implementer_max_tokens_for),
    so an over- or under-count just nudges the clamp, never breaks correctness.

    When a '## File Structure' section exists and contains at least one matching
    path, counts are scoped to that section only. Otherwise falls back to
    whole-document scanning as before.
    """
    if not design_md:
        return 0

    # Try to extract File Structure section
    fs_section = _file_structure_section(design_md)

    # If we have a section and it contains matches, use it; otherwise fallback
    if fs_section is not None:
        paths_from_section = set()
        for m in _DESIGN_FILE_PATH_RE.finditer(fs_section):
            p = m.group(0).strip("./")
            if p:
                paths_from_section.add(p)

        if paths_from_section:  # Only use scoped count if we found matches
            return len(paths_from_section)

    # Fallback to whole-document scan
    paths = set()
    for m in _DESIGN_FILE_PATH_RE.finditer(design_md):
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
    est = settings.IMPLEMENTER_TOKENS_BASE + n * settings.IMPLEMENTER_TOKENS_PER_FILE
    return max(settings.IMPLEMENTER_MAX_TOKENS, min(settings.IMPLEMENTER_MAX_TOKENS_CEILING, est))


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


def _render_reference_files(reference_files: list[tuple[str, str]],
                            *, omitted: list[str] | None = None) -> str:
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
    # DEV-633: the allocator trims this list and hands back what it dropped,
    # so those paths are still named as off-limits. DEV-648: when it has done
    # so (``omitted`` is a list, even an empty one) its decision is final and
    # this render does NOT clamp again — the allocator may deliberately grant
    # a section more than its knob when the window has room, and re-applying
    # the knob here would drop the file it just chose to include. The knob
    # survives only as the fallback for a caller that renders unbudgeted.
    dropped: list[str] = list(omitted or [])
    budget = float("inf") if omitted is not None else settings.PROTECTED_FILES_MAX_CHARS
    for path, content in reference_files:
        if len(content) > budget:
            dropped.append(path)
            continue
        budget -= len(content)
        out.append(f"### {path} (read-only)\n\n````\n{content}\n````\n\n")
    if dropped:
        out.append(
            "**Not shown** (over the context budget), but still off-limits and "
            "still compiled in: " + ", ".join(dropped) + "\n\n"
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


def _render_unreadable_modifications(
        unreadable: list[tuple[str, str, str]],
        *, shown: int, base_ref: str | None, tools: bool) -> str:
    """The section that says a declared modification could not be read — DEV-730.

    Run 42's architect was handed zero editable files and no sentence saying
    so. Attempt 0 designed blind against files it had never seen and was
    thrown away; attempt 1, after a rejection nudged it toward inspecting,
    asked for exactly the right three files and got them. The capability was
    there the whole time. What was missing was anyone telling it that it had
    a hole to fill — so the trigger for using the tool was FAILURE rather
    than noticing the gap, and DEV-431 sends retries to weaker models that
    may never notice at all.

    Two things must be said and kept apart. What the runner reported — git's
    own words, per path — and what it MEANS, which git cannot tell us: a
    wrong path and a file that is genuinely not there produce the same
    sentence. Presenting that as certainty is how DEV-604 made whole-file
    emission "correct" over three files that existed.
    """
    at_ref = f" at `{base_ref}`" if base_ref else ""
    total = shown + len(unreadable)
    lines = [
        "## Files the plan MODIFIES that could not be read (DEV-730)\n",
        f"**{len(unreadable)} of the {total} file(s) this spec asks you to "
        f"modify could not be read{at_ref}.** You have not seen their "
        f"contents, and nothing below is a substitute for them:\n",
    ]
    for path, _status, reason in unreadable:
        lines.append(f"- `{path}` — {reason}")
    lines.append(
        "\nThat message is what the repository said, not what it means. A "
        "path that is simply WRONG — a bare filename where the repository "
        "holds `Dir/File.swift`, or the right name under the wrong directory "
        "— answers identically to a file that is genuinely absent. These are "
        "declared as modifications, so a real file almost certainly exists "
        "for each of them under some path.")
    if tools:
        lines.append(
            "\n**Read them before you design.** One `<<<READ_FILE>>>` line "
            "per file and nothing else in that reply. If a path above comes "
            "back empty, ask again for the same basename under a directory "
            "you have seen in this prompt — the plan's other paths, the "
            "read-only references, an import line. Inferring a corrected "
            "path is expected here and is the whole point of this section; "
            "it is the one case where the guidance below about preferring "
            "paths you have already seen does not apply.")
    else:
        lines.append(
            "\nDesign conservatively: do NOT assume these files are new, "
            "empty, or small, and do not specify rewriting them whole. "
            "Describe the change each one needs in terms of behaviour, and "
            "say in the design that their current contents were unavailable.")
    return "\n".join(lines) + "\n\n---\n\n"


def build_architect_message(spec_md: str,
                            rejection_notes: str | None = None,
                            plan_yaml: str | None = None,
                            reference_files: list[tuple[str, str]] | None = None,
                            approval_conditions: str | None = None,
                            existing_files: list[tuple[str, str]] | None = None,
                            omitted_existing: list[str] | None = None,
                            omitted_reference: list[str] | None = None,
                            unresolved: list[str] | None = None,
                            unreadable: list[tuple[str, str, str]] | None = None,
                            base_ref: str | None = None,
                            tools: bool = False,
                            standing_rules: str | None = None,
                            ) -> list[dict[str, str]]:
    user_parts: list[str] = []
    # On a re-run (a human or design-review rejection), the
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
    # DEV-599: the implementer has received the real contents of the files it
    # must modify since DEV-571; the architect designed blind against the same
    # files and invented APIs (run 16) or refused to design at all and asked
    # to "examine the module" (run 20). Editable-existing is distinct from
    # read-only-protected: the architect needs both, for different reasons.
    # DEV-633: this section was the one unbudgeted file render in the pipeline
    # — a raw join of every editable file, straight into an 8000-token-budget
    # architect. It now shares the aggregate allocation with the protected
    # section and, like every other render, names what it could not show
    # rather than leaving the model to assume it saw everything.
    if existing_files or omitted_existing:
        blocks = "\n\n".join(
            f"### {path}\n\n```\n{content}\n```"
            for path, content in (existing_files or []))
        shown = ("every file you may modify is shown here in full"
                 if not omitted_existing else
                 "the files shown here are shown in full")
        # DEV-714: "do not ask to examine anything" was the right instruction
        # when asking was futile (run 20's architect refused to design and
        # asked for the module). With a tool loop it is simply false, and a
        # model that believes it will guess at the API instead of reading it.
        ask = ("and where you need a file you were not given, READ IT with the "
               "tool below rather than guessing"
               if tools else "and do not ask to examine anything")
        user_parts.append(
            "## Current contents of files the plan will MODIFY\n\n"
            "Design against THIS code — its real names, signatures, and "
            f"structure. Do not assume or invent an API, {ask}: "
            f"{shown}.\n\n" + blocks + "\n\n")
        if omitted_existing:
            user_parts.append(
                "**Not shown** (over the context budget), but the plan still "
                "modifies them: " + ", ".join(omitted_existing) + ". Design "
                "the change they need without restating their current "
                "contents — you have not read them.\n\n")
        if unresolved:
            # DEV-698: run 39's architect saw Game.tick() call spawnWaveChain()
            # and no definition anywhere, and said so in its own words — "But
            # wait ... it calls spawnWaveChain() which doesn't exist yet!" —
            # then burned five attempts on the confusion across two rounds.
            # Being TOLD what it cannot see costs a line; inferring it cost a
            # cancelled spec.
            user_parts.append(
                "**Referenced but not shown.** These symbols are called by the "
                "files above and are defined in neither the modifiable set nor "
                "the read-only references: "
                + ", ".join(f"`{name}`" for name in unresolved[:20])
                + (f" (and {len(unresolved) - 20} more)"
                   if len(unresolved) > 20 else "")
                + ". They are defined outside the set you were given — in "
                "another file of this repository, or in a framework. Treat "
                "them as present and working, design against the call as "
                "written, and do NOT invent a definition, rename them, or "
                "conclude they are missing."
                + (" If a design decision turns on one of their real "
                   "signatures, read the file that defines it with the tool "
                   "below instead of assuming." if tools else "")
                + "\n\n")
        user_parts.append("---\n\n")
    # DEV-730: OUTSIDE the section above on purpose. The run that motivated
    # this served zero editable files, so that section did not render at all
    # and the gap it should have announced went with it — the one shape where
    # saying nothing is worst is the one where there is nothing to say it in.
    if unreadable:
        user_parts.append(_render_unreadable_modifications(
            unreadable, shown=len(existing_files or []),
            base_ref=base_ref, tools=tools))
    if reference_files or omitted_reference:
        user_parts.append(
            _render_reference_files(reference_files or [],
                                    omitted=omitted_reference) + "---\n\n")
    if tools:
        user_parts.append(ARCHITECT_TOOL_PROTOCOL + "\n---\n\n")
    user_parts.append(
        "## Specification\n\n"
        f"{spec_md}\n\n---\n\n"
        "Your task: produce a complete architecture design for this project. "
        + ("If a file you need was not shown, read it first — one "
           "`<<<READ_FILE>>>` line per file and nothing else in the reply. "
           "Otherwise, output " if tools else "Output ")
        + "exactly one <<<DESIGN>>>…<<<END>>> block as instructed."
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
    if standing_rules:   # DEV-784
        user_parts.append("\n\n" + standing_rules)
    return [
        {"role": "system", "content": ARCHITECT_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


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


_SRC_PACKAGE_RE = re.compile(r"^src/([A-Za-z_]\w*)/")


def import_packages(paths: "list[str]") -> "list[str]":
    """Top-level packages a Python change touches, from `src/<pkg>/....py`.

    DEV-644. Deliberately NOT `outcome.repo_packages`: that one also collects
    `<pkg>/....py` (so `tests/test_x.py` yields `tests`) because it serves
    sandbox-provisioning classification. `tests` is not an import root.
    """
    found = set()
    for raw in paths or []:
        norm = str(raw).replace("\\", "/").lstrip("./")
        if not norm.endswith(".py"):
            continue
        m = _SRC_PACKAGE_RE.match(norm)
        if m:
            found.add(m.group(1))
    return sorted(found)


def render_import_root(paths: "list[str]") -> str:
    """The import-root section, or "" when this is not a src/-layout Python
    change (a Swift or node spec renders nothing and its prompt is
    byte-identical).

    DEV-644: on run 24 fast_implementer, implementer and deep_implementer
    each wrote `from src.coding_model_autonomous.workspace import ...`, which
    fails at collection because the sandbox puts `src/` itself on sys.path.
    The build feedback named the missing module, so each next agent "fixed"
    the module rather than the import, and one deleted a real import. Every
    self-target spec since carried a hand-written paragraph saying this; it
    is stated on every Python run now.
    """
    packages = import_packages(paths)
    if not packages:
        return ""
    example = packages[0]
    return (
        "## Import root — MANDATORY\n\n"
        "The test sandbox puts the repository's `src/` directory on "
        "`sys.path`, so `src/` is the PACKAGE ROOT and is NOT itself a "
        "package. Import the code under test by its package name:\n\n"
        f"    from {example}.<module> import <name>\n\n"
        f"Package(s) in this change: {', '.join('`' + p + '`' for p in packages)}.\n\n"
        f"NEVER write `from src.{example}...` or `import src.{example}`. There "
        "is no `src` package: that import fails at collection with "
        f"`ModuleNotFoundError: No module named 'src.{example}'`, and the "
        "module's own relative imports then resolve against the wrong root.\n\n"
    )


def _render_file_modes(existing_paths: list[str],
                       new_paths: list[str]) -> str:
    """Per-path MANDATORY output mode for edit-mode prompts (DEV-638 item 2).

    Run 21 spent five of eleven rotations on SEARCH/REPLACE blocks aimed at a
    test file that did not exist yet; run 17 had hit the same class. The
    split between existing and new paths is known before dispatch, so the
    prompt states it per path instead of leaving the model to infer it from
    which heading a file appeared under.
    """
    out = ["## File modes — MANDATORY\n\n",
           "Each planned path has exactly ONE permitted output form:\n\n"]
    for p in existing_paths:
        out.append(f"- EDIT ONLY — existing: `{p}` — SEARCH/REPLACE edit blocks "
                   "against its shown content; never a <<<FILE:>>> block.\n")
    for p in new_paths:
        # DEV-655: never render a closed marker pair around a real path in
        # instructional text. Runs 28 and 29 echoed this line back and the
        # parser took the echo for a write — a 4-byte file named `{p}`
        # holding the literal `...`. The opener is named, the closer is
        # described, and the pair never appears together on one line.
        out.append(f"- EMIT WHOLE — new file: `{p}` — one complete whole-file "
                   f"block opening with <<<FILE: {p}>>> and closed by the "
                   "END_FILE marker on its own line; never SEARCH/REPLACE "
                   "blocks.\n")
    out.append("\nAny other path you create is NEW: emit it whole. An edit "
               "block aimed at a NEW path is rejected — there is no content "
               "for it to search.\n\n")
    return "".join(out)


def _render_existing_files(existing_files: list[tuple[str, str]],
                           *, edit_mode: bool = False,
                           omitted: list[str] | None = None) -> str:
    """Current repo contents, framed as ground truth the model must preserve.

    Deliberately not wrapped in the <<<FILE:…>>> delimiters the implementer
    emits — those are parsed out of the *response*, and echoing the input
    format invites the model to treat these as already-emitted files.

    With ``edit_mode`` on (DEV-581) the framing tells the model to emit anchored
    SEARCH/REPLACE edit blocks against this exact text rather than re-emit the
    whole file. The heading is identical in both modes because the system prompt
    refers to it by name. With ``edit_mode`` off the output is byte-identical to
    the pre-DEV-581 wording.
    """
    if edit_mode:
        body = (
            "These are the ACTUAL contents in the repository right now. They are "
            "ground truth and outrank any description of these files in the spec "
            "or design. Each file here ALREADY EXISTS, so do NOT re-emit it whole "
            "— emit anchored SEARCH/REPLACE edit blocks against this exact text "
            "(see \"Editing existing files\" in your instructions). Copy every "
            "SEARCH anchor byte-for-byte from the content below, and change only "
            "the lines the design requires.\n\n"
        )
    else:
        body = (
            "These are the ACTUAL contents in the repository right now. They are "
            "ground truth and outrank any description of these files in the spec "
            "or design. Reproduce every declaration exactly as it appears here "
            "except for the specific changes the design calls for — do not "
            "rewrite, reorder, re-indent, modernise or 'improve' anything you "
            "were not asked to change. Anything you drop is deleted from the "
            "repository.\n\n"
        )
    out = [
        "## Current contents of files you must modify\n\n",
        body,
    ]
    # See _render_reference_files: the allocator does the trimming and passes
    # the paths it dropped in ``omitted``; when it has, its decision is final
    # and the knob below is not re-applied (DEV-633, DEV-648).
    dropped: list[str] = list(omitted or [])
    budget = float("inf") if omitted is not None else settings.EXISTING_FILES_MAX_CHARS
    for path, content in existing_files:
        if len(content) > budget:
            dropped.append(path)
            continue
        budget -= len(content)
        out.append(f"### {path}\n\n````\n{content}\n````\n\n")
    if dropped:
        out.append(
            "**Not shown** (over the context budget): "
            + ", ".join(dropped)
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
    edit_mode: bool = False,
    new_files: list[str] | None = None,
    omitted_existing: list[str] | None = None,
    omitted_reference: list[str] | None = None,
    unresolved: list[str] | None = None,
    standing_rules: str | None = None,
) -> list[dict[str, str]]:
    # DEV-581: edit-mode only changes anything when there ARE existing files to
    # edit. With no existing files the response is all new whole files, so the
    # message is byte-identical to whole-file mode either way.
    edit_mode = bool(edit_mode and existing_files)
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
    # DEV-633: a section the allocator emptied still renders — the whole point
    # of the "Not shown" listing is that the model is TOLD what it did not see.
    if existing_files or omitted_existing:
        user_parts.append("\n\n")
        user_parts.append(_render_existing_files(existing_files or [],
                                                 edit_mode=edit_mode,
                                                 omitted=omitted_existing))
        if edit_mode:
            # DEV-638: only in edit mode, so the flag-off prompt stays
            # byte-identical. ``new_files`` is the plan's implement outputs
            # minus the existing set; absent, only the EDIT ONLY rows render.
            existing_paths = [p for p, _ in (existing_files or [])]
            user_parts.append(_render_file_modes(
                existing_paths,
                [p for p in (new_files or []) if p not in existing_paths]))
    import_root = render_import_root(
        [p for p, _ in (existing_files or [])] + list(new_files or []))
    if import_root:
        user_parts.append("\n\n" + import_root)
    # DEV-764: the language packs' standing rules (Swift's is the counterpart
    # of the import-root paragraph). Empty for a file set whose languages
    # carry none, so every such prompt stays byte-identical.
    language_rules = languages.render_rules(
        [p for p, _ in (existing_files or [])] + list(new_files or []))
    if language_rules:
        user_parts.append("\n\n" + language_rules)
    if standing_rules:   # DEV-784
        user_parts.append("\n\n" + standing_rules)
    if reference_files or omitted_reference:
        user_parts.append("\n\n")
        user_parts.append(_render_reference_files(reference_files or [],
                                                  omitted=omitted_reference))
    if unresolved:
        # DEV-698. The architect got this list first; the implementer is the
        # role that actually needs it. spec_0aab1c17 died here: it needed an
        # audio spy, subclassed `AudioManager` without being served the file,
        # and `AudioManager` is `final`. Five attempts, a synthesis pass and a
        # repair pass, all on "what shape is this type I cannot see".
        user_parts.append(
            "\n\n---\n\n## Referenced but NOT shown\n\n"
            "These symbols are used by the files above and are defined "
            "nowhere in what you were given: "
            + ", ".join(f"`{n}`" for n in unresolved[:20])
            + (f" (and {len(unresolved) - 20} more)"
               if len(unresolved) > 20 else "")
            + ".\n\nThey are real — in another file of this repository, or in "
            "a framework. You have NOT seen their declarations, so you do not "
            "know their shape.\n\n"
            "- Call them as the existing code already calls them. Copy the "
            "call form from the files above.\n"
            "- Do NOT subclass one, conform to one, or construct one from "
            "guesswork. A type you cannot read may be `final`, may have "
            "required initialisers, or may be a protocol with members you "
            "cannot see.\n"
            "- If a test needs a double for one of these, inject a closure or "
            "a small protocol you define yourself rather than subclassing "
            "theirs.\n")
    if rejection_notes:
        if edit_mode:
            task_line = (
                "\n\n---\n\n"
                "Your task: fix the issues identified above. For each file shown "
                "under \"Current contents of files you must modify\", emit "
                "anchored SEARCH/REPLACE edit blocks (`### path` then "
                "`<<<<<<< SEARCH` / `=======` / `>>>>>>> REPLACE`) against its "
                "shown content — do NOT re-emit those files whole. Emit any NEW "
                "file as a complete <<<FILE: path>>>…<<<END_FILE>>> block. Leave "
                "every file you are not changing untouched.")
        else:
            task_line = (
                "\n\n---\n\n"
                "Your task: fix the issues identified above and re-implement. "
                "Output <<<FILE: path>>>…<<<END_FILE>>> blocks for EVERY file. "
                "You must output ALL files again (complete files, not diffs).")
        user_parts.extend([
            "\n\n## Previous Attempt — Review Feedback\n\n",
            rejection_notes,
            task_line,
        ])
    else:
        if edit_mode:
            task_line = (
                "\n\n---\n\n"
                "Your task: implement ALL components described in the design. For "
                "each file shown under \"Current contents of files you must "
                "modify\", emit anchored SEARCH/REPLACE edit blocks against its "
                "shown content — do NOT re-emit those files whole. Emit every NEW "
                "file as a complete <<<FILE: path>>>…<<<END_FILE>>> block. Paths "
                "are relative to the project workspace.")
        else:
            task_line = (
                "\n\n---\n\n"
                "Your task: implement ALL components described in the design. "
                "Output <<<FILE: path>>>…<<<END_FILE>>> blocks for every file. "
                "Paths are relative to the project workspace.")
        user_parts.extend([task_line])
    system_prompt = (
        IMPLEMENTER_SYSTEM_PROMPT + IMPLEMENTER_EDIT_MODE_INSTRUCTIONS
        if edit_mode else IMPLEMENTER_SYSTEM_PROMPT
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "".join(user_parts)},
    ]
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
    # DEV-643: the structural signals read the File Structure section when
    # it names anything, for the same reason estimate_design_file_count does
    # — Criterion Seams quote fixture paths that are not build units. The
    # prose count ("ten modules") keeps the whole document.
    scope = text
    section = _file_structure_section(text)
    if section and (_DESIGN_UNIT_PATH_RE.search(section)
                    or _BARE_FILENAME_RE.search(section)
                    or _TREE_NODE_RE.search(section)):
        scope = section
    named = {m.group(0).strip("./").lower() for m in _DESIGN_UNIT_PATH_RE.finditer(scope)}
    named |= {m.group(1).lower() for m in _BARE_FILENAME_RE.finditer(scope)}
    tree_nodes = len(_TREE_NODE_RE.findall(scope))
    structural = max(len(named), tree_nodes)
    return max(structural, _prose_unit_count(text))


def use_manifest_mode(design_md: str) -> bool:
    """Whether to generate this design file-by-file rather than in one call."""
    if settings.IMPLEMENTER_MODE == "manifest":
        return True
    if settings.IMPLEMENTER_MODE == "single":
        return False
    # OR the budget file-count with the broader unit estimate: either crossing
    # the threshold means "too big for one capped call". The unit estimate is a
    # superset for path-based designs, so this never lowers the count the tests
    # pin for .ts-file designs — it only adds coverage for the shapes the file
    # regex misses.
    return (
        estimate_design_file_count(design_md) >= settings.MANIFEST_FILE_THRESHOLD
        or estimate_design_unit_count(design_md) >= settings.MANIFEST_FILE_THRESHOLD
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
    edit_mode: bool = False,
    edit_errors: str | None = None,
    omitted_reference: list[str] | None = None,
) -> list[dict[str, str]]:
    # DEV-604: edit mode only means anything when the target already exists.
    # With no existing content the file is new, whole-file emission is correct,
    # and the message is byte-identical to the pre-DEV-604 prompt either way.
    edit_mode = bool(edit_mode and existing_content is not None)
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
    if reference_files or omitted_reference:
        parts.append("\n" + _render_reference_files(
            reference_files or [], omitted=omitted_reference))
    # Manifest mode writes one file per call, so unlike the single-call path it
    # needs only the target's own current content — which is exactly the file at
    # risk of being reconstructed (DEV-492).
    if existing_content is not None:
        if edit_mode:
            preamble = (
                "You are EDITING this file, not writing it from scratch. What "
                "follows is its actual content in the repository. Do NOT "
                "re-emit the file — emit anchored SEARCH/REPLACE edit blocks "
                "against this exact text (see the edit-block rules in your "
                "instructions), changing only what the design requires.\n\n"
            )
        else:
            preamble = (
                "You are EDITING this file, not writing it from scratch. What "
                "follows is its actual content in the repository. Reproduce "
                "every declaration exactly except for the specific changes "
                "asked of you above; anything you omit is deleted from the "
                "repository.\n\n"
            )
        parts.extend([
            f"\n## Current content of {target.path} — this file ALREADY EXISTS\n\n",
            preamble,
            f"````\n{existing_content}\n````\n",
        ])
    if rejection_notes:
        parts.extend([
            "\n## Reviewer feedback to address in this file\n\n",
            rejection_notes, "\n",
        ])
    if edit_mode and edit_errors:
        parts.extend([
            "\n## Your previous edit blocks failed to apply\n\n",
            edit_errors,
            "\n\nRe-emit ALL edit blocks for this file, copying each SEARCH "
            "anchor byte-for-byte from the current content shown above.\n",
        ])
    if edit_mode:
        parts.append(
            f"\nOutput one or more SEARCH/REPLACE edit blocks for "
            f"{target.path}, introduced by a `### {target.path}` line. Do NOT "
            f"emit a <<<FILE: ...>>> whole-file block."
        )
        system_prompt = PER_FILE_SYSTEM_PROMPT + PER_FILE_EDIT_MODE_INSTRUCTIONS
    else:
        parts.append(
            f"\nOutput exactly one <<<FILE: {target.path}>>> … <<<END_FILE>>> block."
        )
        system_prompt = PER_FILE_SYSTEM_PROMPT
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "".join(parts)},
    ]


def build_reviewer_message(
    spec_md: str,
    design_md: str,
    code_files: list[tuple[str, str]],
    test_framework: str = "pytest",
    rejection_notes: Optional[str] = None,
    omitted_code: Optional[list[str]] = None,
) -> list[dict[str, str]]:
    file_sections = []
    for path, content in code_files:
        file_sections.append(f"### {path}\n```\n{content}\n```\n")
    # DEV-633: the reviewer's section was the only one with no budget at all —
    # the attempts it reviews grow with every retry, and nothing summed them
    # against the reviewer window. What the allocator could not fit is named,
    # because a review that silently never saw a file still returns a verdict
    # on it.
    if omitted_code:
        file_sections.append(
            "### Files NOT shown (over the context budget)\n\n"
            + ", ".join(omitted_code)
            + "\n\nYou have NOT read these. Do not judge them: say so in the "
              "review rather than passing or failing them unseen.\n"
        )

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
            + render_import_root([p for p, _ in code_files])
            + tautology_section
            + retry_block
            + f"\n\n---\n\n"
            f"Test framework: {test_framework}\n\n"
            "Your task: write test files and review the implementation. "
            "Output <<<FILE: test_*.py>>>…<<<END_FILE>>> blocks for test files, "
            "then a <<<REVIEW>>>…<<<END_REVIEW>>> block with your verdict."
        )},
    ]


def build_synthesis_message(
    spec_md: str,
    design_md: str,
    attempts: list[dict],
    review_notes: list[str] | None = None,
    reference_files: list[tuple[str, str]] | None = None,
    current_design_digest: str | None = None,
    omitted_reference: list[str] | None = None,
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
    if reference_files or omitted_reference:
        parts.append("\n\n" + _render_reference_files(
            reference_files or [], omitted=omitted_reference))
    if review_notes:
        parts.append("\n\n## Reviewer feedback on the attempts below\n\n")
        parts.append(
            "These are the human reviews that rejected the attempts, oldest "
            "first. Where a review says a part is correct and should be kept, "
            "keep it verbatim; where it names a defect, fix it.\n\n"
        )
        for i, note in enumerate(review_notes, 1):
            parts.append(f"### Review {i}\n\n{note}\n\n")
    # DEV-553: the corpus is not homogeneous once the architect has revised.
    # Run 10 merged six attempts spanning four designs and resurrected a
    # signature a reviewer had explicitly struck two revisions earlier — the
    # stale variant was simply more numerous than the current one.
    stale = [a for a in attempts
             if current_design_digest and a.get("design_digest")
             and a["design_digest"] != current_design_digest]
    parts.append("\n\n## Prior Attempts\n\n")
    if stale:
        parts.append(
            f"**{len(stale)} of these {len(attempts)} attempts were written "
            f"against an EARLIER version of the design and are marked "
            f"SUPERSEDED below.** The architecture was revised after they ran, "
            f"so where a superseded attempt and the design above disagree — a "
            f"type, a signature, a return type — **the design above is "
            f"correct and the attempt is wrong**. Their behaviour is still "
            f"worth harvesting; their API is not. Do not reintroduce a "
            f"signature that appears only in a superseded attempt.\n\n"
        )
    for att in attempts:
        mark = ""
        if current_design_digest and att.get("design_digest"):
            mark = (" — SUPERSEDED design"
                    if att["design_digest"] != current_design_digest
                    else " — current design")
        parts.append(
            f"### Attempt retry={att['retry']} (agent={att['agent']}){mark}\n\n"
        )
        if att.get("test_summary"):
            parts.append("#### Test result\n\n```\n")
            parts.append(att["test_summary"])
            parts.append("\n```\n\n")
        for relpath, content in att.get("files", {}).items():
            parts.append(f"#### {relpath}\n\n```\n{content}\n```\n\n")
    # DEV-764: synthesis reproduced retry 1's unqualified static member
    # verbatim on run 47; it merges text and needs the same rules.
    language_rules = languages.render_rules(
        [p for att in attempts for p in att.get("files", {})])
    if language_rules:
        parts.append(language_rules)
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
    omitted_reference: list[str] | None = None,
    cited_diagnostics: "list | None" = None,
) -> list[dict[str, str]]:
    """One targeted repair round on a synthesized artifact.

    ``cited_diagnostics`` (DEV-767) is the located ``path:line`` list the
    build reported, already mapped onto artifact paths. When present, the
    build-failure prompt lists each with the fix its class asks for and
    tells the repair its edits must land on those lines — the daemon then
    discards blocks for uncited files and refuses to build a repair that
    changed no cited line. Runs 47 and 48 both produced repairs that never
    touched the cited line.

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
    if reference_files or omitted_reference:
        parts.append("\n\n" + _render_reference_files(
            reference_files or [], omitted=omitted_reference))
    parts.append(state)
    for relpath, content in files:
        parts.append(f"### {relpath}\n\n```\n{content}\n```\n\n")
    if warning_only:
        assert warning_diagnostic is not None  # implied by warning_only
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
        # DEV-764 / DEV-767: rules first, then the cited locations with their
        # hints, then the raw diagnostics the two are drawn from.
        parts.append(languages.render_rules([p for p, _ in files]))
        parts.append(render_cited_diagnostics(cited_diagnostics or []))
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
