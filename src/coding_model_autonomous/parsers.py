"""Parsers for the agents' structured responses: the <<<MARKER>>> blocks
of the architect, implementer, reviewer, design reviewer and manifest.

Every parser returns a result dataclass or a ParseError; none raises on
model output."""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from .retry_policy import ALLOWED_IMPLEMENTER_AGENTS, TIER_TO_IMPLEMENTER
from .thinking import strip_thinking as _server_strip_thinking

logger = logging.getLogger("orchestrator.executor")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _strip_thinking(text: str) -> str:
    """Defensive strip before parsing structured output (DEV-153).

    Delegates to thinking.strip_thinking — the one implementation that
    handles all three observed real patterns (full block, orphan close,
    unclosed open) plus <REACT>. The local regex copy only handled full
    blocks, so as a defensive layer it didn't defend against the patterns
    actually seen; if the server-side strip ever regresses, un-stripped
    reasoning would have flowed straight into the YAML/FILE-marker parsers.
    """
    return _server_strip_thinking(text).strip()


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
# DEV-711: a citation of the form `path::testName` in the Verdict Evidence
# block. Run 41's reviewer wrote a Python placeholder containing six print
# statements and then cited it ten times, once per acceptance criterion, for
# tests that live in GameTests.swift. The verdict was right and the evidence
# trail was fabricated — and a wrong verdict gets caught by a red suite, while
# a wrong citation gets caught by nobody.
_CITATION_RE = re.compile(r"([\w./-]+\.(?:py|swift|js|ts|mjs))::(\w+)")
# How a test declares itself, across the frameworks this pipeline dispatches.
_TEST_DECL_RES = (
    re.compile(r"\bdef\s+(\w+)\s*\("),            # pytest
    re.compile(r"\bfunc\s+(\w+)\s*\("),           # XCTest / swift-testing
    re.compile(r"\btest\s*\(\s*[\"\']([^\"\']+)"),  # node:test / vitest
    re.compile(r"\bit\s*\(\s*[\"\']([^\"\']+)"),
)


def _unresolvable_citations(
        evidence_body: str,
        test_files: "list[tuple[str, str]]") -> list[str]:
    """Citations naming a test the cited file does not define.

    Only files THIS reviewer wrote are judged. A citation pointing at the
    implementer's file or the repository's own suite cannot be resolved from
    here, and DEV-630's rule applies: unknown is not wrong, so it is left
    alone rather than failed on a guess.
    """
    written = {path: content for path, content in test_files}
    if not written:
        return []
    bad: list[str] = []
    for path, name in _CITATION_RE.findall(evidence_body or ""):
        content = written.get(path)
        if content is None:
            continue                      # not ours to judge
        declared: set[str] = set()
        for decl in _TEST_DECL_RES:
            declared.update(decl.findall(content))
        if name not in declared:
            bad.append(f"{path}::{name}")
    return bad


_VERDICT_EVIDENCE_RE = re.compile(
    r"###\s*Verdict\s+Evidence\s*\n+(.*?)(?=\n###\s|\Z)",
    re.DOTALL | re.IGNORECASE,
)


def _missing_verdict_reason(review_md: str) -> str:
    """Why no verdict was found, naming the near miss when there is one.

    `### Verdict Evidence` sits directly below `### Verdict` in the template
    and satisfies neither regex for the other, so a reviewer that writes only
    the longer heading looks — to the old code — exactly like one that
    deliberately failed the work. Say which of the two it actually wrote
    (DEV-807).
    """
    base = ("No '### Verdict' heading followed by PASS or FAIL in the "
            "<<<REVIEW>>> block")
    if _VERDICT_EVIDENCE_RE.search(review_md):
        return (f"{base}. The block does have '### Verdict Evidence', which is "
                "a separate, additional section: it records WHY and does not "
                "state the verdict. Write both headings — '### Verdict' with "
                "PASS or FAIL on the next line, then '### Verdict Evidence'.")
    return f"{base}. State it on a line of its own under '### Verdict'."


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
    # DEV-581: diagnostics for edit blocks that could not be applied (anchor not
    # found / ambiguous / no base file). Non-empty means the attempt must NOT be
    # written — the orchestrator routes it back to the implementer. Always empty
    # when diff-based edits are off (the whole-file path never populates it).
    apply_errors: list[str] = field(default_factory=list)
    # DEV-655: <<<FILE:>>> blocks whose body was only an ellipsis — an echoed
    # instruction, not a file. Dropped by the parser and listed here so the
    # daemon can record the anomaly instead of writing a 4-byte junk file.
    echoed_placeholders: list[str] = field(default_factory=list)
    # DEV-637: the structured twin of apply_errors, parallel and same order —
    # one dict per failure with ``path``, ``block`` (1-based, 0 = file-level),
    # ``reason`` and the COMPLETE ``search`` text (apply_errors previews only
    # its first lines). Plain dicts so this module stays free of apply_edits.
    apply_failures: list[dict] = field(default_factory=list)
    # DEV-638: how every edit block landed — ``path``, ``block``, ``tier``
    # (exact / trailing_ws / indent / fuzzy / whole_from_empty_search),
    # ``ratio``, ``line``. Empty on the whole-file path.
    edit_applies: list[dict] = field(default_factory=list)


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


# DEV-782: the whole-file template's own placeholder line, as the prompt
# prints it (`<complete file content>` and the longer "— NOT a diff" form).
# Run 50 retry 1 copied it verbatim above an otherwise-correct file and lost
# the attempt to "expressions are not allowed at the top level".
_TEMPLATE_PLACEHOLDER_LINE_RE = re.compile(
    r"^[ \t]*<complete file content[^\n>]*>[ \t]*$", re.IGNORECASE)


def _strip_template_placeholder(content: str) -> str:
    """Drop a leading or trailing template placeholder line (DEV-782).

    Only the exact template line, only at the edges: a file that legitimately
    begins with `<` (XML, HTML) is untouched, and a placeholder in the middle
    of a file is the model's own content and stays visible to the build.
    """
    lines = content.split("\n")
    while lines and _TEMPLATE_PLACEHOLDER_LINE_RE.match(lines[0]):
        lines.pop(0)
    while lines and _TEMPLATE_PLACEHOLDER_LINE_RE.match(lines[-1]):
        lines.pop()
    return "\n".join(lines)


def parse_implementer_response(text: str) -> ImplementerResult | ParseError:
    cleaned = _strip_thinking(text)
    matches = _FILE_RE.findall(cleaned)
    if not matches:
        return ParseError("No <<<FILE: path>>>…<<<END_FILE>>> blocks found", text)
    raw_files = [(path.strip(),
                  _strip_template_placeholder(_strip_markdown_fence(content)))
                 for path, content in matches]
    # DEV-655, defence in depth behind the prompt fix above: a block whose
    # whole body is `...`/`…`/whitespace is an echoed template, not a file the
    # model composed. It is dropped and NAMED, never silently — an empty
    # emission the model meant is a different fault and must stay visible.
    echoed = [p for p, c in raw_files if not c.strip().strip(".…").strip()]
    if echoed:
        raw_files = [(p, c) for p, c in raw_files if p not in echoed]
    files, duplicates = _dedupe_files_last_wins(raw_files)
    if duplicates:
        logger.warning(
            "implementer response contained duplicate file paths "
            "(last-write-wins applied): %s",
            duplicates,
        )
    return ImplementerResult(files=files, raw=text, duplicate_paths=duplicates,
                             echoed_placeholders=echoed)


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

    # Extract verdict. DEV-807: a MISSING heading is NOT a FAIL. "The reviewer
    # rejected this" and "the reviewer never said" are different facts, and
    # DEV-629 exists to stop them being collapsed. Returning a ParseError sends
    # it through the classifier, which re-runs the reviewer on its own small
    # budget instead of charging the implementer for a heading nobody typed.
    # Run 56's reviewer wrote "No issues found", mapped all six criteria in a
    # full evidence block, and was recorded as a rejection over a 71-passed,
    # 0-failed suite; only DEV-560's adjudication gate stopped that becoming a
    # retry. Inferring PASS from the absence is the other way to get this
    # wrong, and DEV-405 is why we do not: silence is not approval either.
    verdict_match = _VERDICT_RE.search(review_md)
    if not verdict_match:
        return ParseError(_missing_verdict_reason(review_md), text)
    verdict = verdict_match.group(1).upper()

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

    # DEV-711: the citation format being satisfied is not the same as the
    # citation being true. A test named in the evidence must exist in the file
    # the evidence points at.
    if verdict == "PASS" and (bad := _unresolvable_citations(
            evidence_body, test_files)):
        verdict = "FAIL"
        listed = ", ".join(f"`{c}`" for c in bad[:8])
        more = f" (and {len(bad) - 8} more)" if len(bad) > 8 else ""
        review_md += (
            "\n\n---\n\n"
            "**[orchestrator guard]** Verdict downgraded to FAIL: the Verdict "
            f"Evidence cites {listed}{more}, but the file you wrote does not "
            "define that test. Cite the file that actually contains the test "
            "you are pointing at. If the tests live in a file you did not "
            "write — an existing suite the spec directed you to extend — name "
            "that file, not a placeholder beside it. An evidence trail that "
            "does not resolve is not evidence (DEV-711)."
        )

    return ReviewerResult(
        test_files=test_files,
        review_md=review_md,
        verdict=verdict,
        raw=text,
        duplicate_paths=dup_paths,
    )


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
# DEV-507: the same one-token delimiter corruption DEV-498 found in the
# architect also lands on the manifest, where it costs more — the architect
# gets ARCHITECT_PARSE_RETRIES before anything else is spent, while a manifest
# parse failure used to propagate straight to the caller's rotation retry and
# burn one of MAX_RETRIES. Accept an opening delimiter that starts with MAN and
# is followed by the usual structure. END_MANIFEST starts with END, so the
# closing delimiter can never be mistaken for an opener and there is no
# ambiguity about where the block ends.
_MANIFEST_FUZZY_RE = re.compile(
    r"<{1,3}MAN[A-Z_]*>{1,3}\s*(.*?)\s*<{1,3}END_MANIFEST>{1,3}",
    re.DOTALL | re.IGNORECASE,
)
# Leading list markers ("1.", "- ", "* ") the model sometimes prefixes.
_LIST_MARKER_RE = re.compile(r"^\s*(?:\d+[.)]|[-*])\s*")


def parse_manifest_response(text: str) -> ManifestResult | ParseError:
    cleaned = _strip_thinking(text)
    m = _MANIFEST_RE.search(cleaned)
    if not m:
        # DEV-507: fall back to a near-miss opening delimiter before giving up,
        # exactly as the architect path does for <<<DESIGN>>>.
        m = _MANIFEST_FUZZY_RE.search(cleaned)
        if m:
            logger.warning(
                "manifest opening delimiter was %r, not <<<MANIFEST>>> — "
                "recovered the block anyway (DEV-507)",
                m.group(0)[:m.group(0).find(">") + 3],
            )
    if not m:
        # Name what delimiters were actually present: "no block found" reads as
        # "the model produced nothing", which is rarely what happened.
        seen_delims = _ANY_DELIMITER_RE.findall(cleaned)
        detail = (f" — delimiters present: {', '.join(sorted(set(seen_delims))[:6])}"
                  if seen_delims else "")
        return ParseError(
            f"No <<<MANIFEST>>>…<<<END_MANIFEST>>> block found{detail}", text)
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
