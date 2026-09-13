"""Pure application of anchored SEARCH/REPLACE edit blocks (DEV-581).

When diff-based edits are enabled, the IMPLEMENTER emits, for every file that
ALREADY EXISTS in the repository, one or more anchored SEARCH/REPLACE blocks
instead of re-emitting the whole file:

    ### path/to/File.swift
    <<<<<<< SEARCH
    <exact contiguous lines copied from the current file>
    =======
    <replacement lines>
    >>>>>>> REPLACE

Multiple blocks per file are allowed. Each SEARCH must match the current file
content EXACTLY ONCE. Matching is a ladder (DEV-638): byte-exact first, then
per-line trailing whitespace ignored, then indent-relative (the block may sit
at a different indentation than the model copied), then a unique high-
similarity window. Each tier runs only when the previous found nothing, and
every tier refuses when it finds more than one place — a model that transcribes
a 20-line anchor with one slipped space lands its edit; one that quotes
something present twice still does not. New files are unaffected: they keep
whole-file emission and never reach this module.

Why this module exists: re-emitting a whole existing file makes the model
corrupt large files and re-corrupt them on every retry (the "whole-file
re-emission" failure). Applying a small, anchored edit mechanically removes that
failure mode for the parts of the file the change does not touch.

Everything here is PURE: it reads no files, touches no globals, logs nothing,
and is fully unit-testable in isolation. The orchestrator supplies the current
file contents and decides how to route failures.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

# Conflict-style fence markers. Git/aider use exactly seven characters; we
# accept 5+ to tolerate a model that miscounts, and require the marker to own
# its whole line (leading whitespace allowed, trailing label optional). The
# SEARCH/REPLACE labels are optional so `<<<<<<<` alone still opens a block.
_SEARCH_RE = re.compile(r"^[ \t]*<{5,}[ \t]*(?:SEARCH)?[ \t]*$")
_DIVIDER_RE = re.compile(r"^[ \t]*={5,}[ \t]*$")
_REPLACE_RE = re.compile(r"^[ \t]*>{5,}[ \t]*(?:REPLACE)?[ \t]*$")
# A file header: `### path`. Only recognised OUTSIDE a SEARCH/REPLACE body, so a
# `###` heading inside replacement content is treated as content, not a header.
_HEADER_RE = re.compile(r"^[ \t]*#{2,4}[ \t]+(.+?)[ \t]*$")


@dataclass(frozen=True)
class EditBlock:
    """One anchored replacement: find `search` exactly once, swap in `replace`."""
    search: str
    replace: str


@dataclass
class FileEdits:
    """All edit blocks the model emitted for a single file path (in order)."""
    path: str
    blocks: list[EditBlock]


@dataclass
class ParsedEdits:
    """Result of parsing edit-block text.

    ``files`` holds the per-file edit blocks, in first-seen path order.
    ``malformed`` holds human-readable diagnostics for structurally broken
    blocks (a SEARCH with no target header, an unterminated block, …). The
    caller decides how to surface them; the parser never raises.
    """
    files: list[FileEdits] = field(default_factory=list)
    malformed: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.files and not self.malformed


@dataclass
class ApplyResult:
    """Outcome of applying a file's edit blocks to its current content.

    On failure the block that stopped the apply is carried whole
    (``failed_block``, 1-based ``failed_index``, machine-readable ``reason``)
    so a caller can keep the COMPLETE SEARCH body for diagnosis; ``error`` is
    the human-readable text, which previews only the first few lines
    (DEV-637: the preview alone left run 21's six anchor misses
    unclassifiable).
    """
    ok: bool
    content: str | None = None      # new full-file content when ok
    error: str | None = None        # precise diagnostic when not ok
    failed_index: int | None = None  # 1-based index of the block that failed
    failed_block: EditBlock | None = None
    reason: str | None = None       # "empty_search" | "not_found" | "ambiguous"
    # DEV-638: how each block landed, in order — which ladder tier matched it
    # and (for the fuzzy tier) how similar the matched window was.
    applied: list["AppliedBlock"] = field(default_factory=list)


@dataclass(frozen=True)
class AppliedBlock:
    """How one edit block was matched (DEV-638)."""
    index: int          # 1-based block index within the file
    tier: str           # TIER_EXACT | TIER_TRAILING_WS | TIER_INDENT | TIER_FUZZY
    ratio: float        # 1.0 for the exact tiers; difflib ratio for the fuzzy tier
    line: int | None    # 1-based line of the matched window's first line (None for exact)


@dataclass(frozen=True)
class EditApplied:
    """Per-file view of AppliedBlock for the resolve result (DEV-638)."""
    path: str
    block: int
    tier: str
    ratio: float
    line: int | None


@dataclass(frozen=True)
class EditFailure:
    """One edit block (or file) that could not be applied, kept in full.

    ``search`` is the complete SEARCH text as the model emitted it — never the
    4-line preview ``_snippet`` puts in the prose diagnostic. ``block`` is the
    1-based index within the file's blocks, or 0 for a file-level failure
    (no base content to edit against, malformed block structure).
    """
    path: str
    block: int
    reason: str      # "not_found" | "ambiguous" | "empty_search" | "no_base" | "malformed"
    search: str
    detail: str      # the same human-readable text that appears in ``errors``


@dataclass
class ResolveResult:
    """Combined new-files + applied-edit-files, plus any apply/parse errors.

    ``files`` is the write-ready ``(path, full_content)`` list. When ``errors``
    is non-empty the caller MUST NOT write anything — an unappliable edit routes
    the whole attempt back to the implementer rather than partially applying.
    ``failures`` is parallel to ``errors`` (same order, same length) and carries
    the structured record — path, block index, reason and the COMPLETE SEARCH
    body — for the retained diagnostics (DEV-637).
    """
    files: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    failures: list[EditFailure] = field(default_factory=list)
    # DEV-638: every block that landed, with its tier — exact applies included,
    # so an event can count tiers and a gate can show the non-exact ones.
    applied: list[EditApplied] = field(default_factory=list)


def _snippet(text: str, max_lines: int = 4) -> str:
    """A short, indented preview of a SEARCH body for diagnostics."""
    lines = text.splitlines()
    head = lines[:max_lines]
    preview = "\n".join("    " + ln for ln in head)
    if len(lines) > max_lines:
        preview += f"\n    … (+{len(lines) - max_lines} more line(s))"
    return preview or "    (empty)"


def parse_edit_blocks(text: str) -> ParsedEdits:
    """Extract per-file SEARCH/REPLACE blocks from model output.

    Scans line by line. A `### path` line sets the current target file. A
    `<<<<<<< SEARCH` line opens a block: lines up to `=======` are the search
    body, lines up to `>>>>>>> REPLACE` are the replacement body. Bodies are
    captured verbatim (whitespace preserved); their trailing newline is dropped
    so a block matches a mid-file line span naturally.

    Anything outside a block that is not a header is ignored, so prose the model
    interleaves does not break parsing.
    """
    lines = text.splitlines()
    parsed = ParsedEdits()
    by_path: dict[str, FileEdits] = {}
    current_path: str | None = None
    i = 0
    n = len(lines)

    def file_for(path: str) -> FileEdits:
        fe = by_path.get(path)
        if fe is None:
            fe = FileEdits(path=path, blocks=[])
            by_path[path] = fe
            parsed.files.append(fe)
        return fe

    while i < n:
        line = lines[i]
        if _SEARCH_RE.match(line):
            # Collect the search body until the divider.
            search_lines: list[str] = []
            i += 1
            while i < n and not _DIVIDER_RE.match(lines[i]):
                if _SEARCH_RE.match(lines[i]) or _REPLACE_RE.match(lines[i]):
                    break  # nested/again marker → malformed, stop the body here
                search_lines.append(lines[i])
                i += 1
            if i >= n or not _DIVIDER_RE.match(lines[i]):
                parsed.malformed.append(
                    f"SEARCH block for {current_path or '(no file header)'} "
                    "has no `=======` divider")
                continue
            # Collect the replace body until the closing marker.
            replace_lines: list[str] = []
            i += 1
            while i < n and not _REPLACE_RE.match(lines[i]):
                if _SEARCH_RE.match(lines[i]) or _DIVIDER_RE.match(lines[i]):
                    break
                replace_lines.append(lines[i])
                i += 1
            if i >= n or not _REPLACE_RE.match(lines[i]):
                parsed.malformed.append(
                    f"SEARCH block for {current_path or '(no file header)'} "
                    "has no `>>>>>>> REPLACE` terminator")
                continue
            i += 1  # consume the REPLACE marker
            if current_path is None:
                parsed.malformed.append(
                    "SEARCH/REPLACE block found with no `### path` header before "
                    "it — cannot tell which file to edit")
                continue
            file_for(current_path).blocks.append(
                EditBlock(search="\n".join(search_lines),
                          replace="\n".join(replace_lines)))
            continue

        m = _HEADER_RE.match(line)
        if m:
            current_path = m.group(1).strip().strip("`").lstrip("/").strip()
        i += 1

    # A header with no blocks is not an edit for that file; drop empties.
    parsed.files = [fe for fe in parsed.files if fe.blocks]
    return parsed


TIER_EXACT = "exact"
TIER_TRAILING_WS = "trailing_ws"
TIER_INDENT = "indent"
TIER_FUZZY = "fuzzy"
# The synthetic "tier" recorded when a NEW path's single empty-SEARCH block is
# taken as the whole file (resolve_edits, DEV-638 item 2).
TIER_WHOLE_FROM_EMPTY_SEARCH = "whole_from_empty_search"

# Fuzzy tier: a window must reach this similarity to count, and the best must
# beat any NON-overlapping runner-up by the margin. Calibration: a one-token
# slip scores 0.99+ at any anchor length; ONE DROPPED LINE scores roughly
# 1 - 1/(2L) — 0.94 for a 9-line anchor, 0.97 for 18 lines — and that is the
# slip a 6,000-line file must still land, so the floors sit just under it.
# Large files get a slightly stricter floor because they hold more
# near-duplicates; the non-overlapping-rival rule is the real guard there.
FUZZY_RATIO = 0.93
FUZZY_RATIO_LARGE = 0.95
FUZZY_LARGE_FILE_LINES = 2000
FUZZY_MARGIN = 0.02
# Below this many anchor lines the fuzzy tier is not attempted — a 2-line
# anchor at 95% similarity is a coincidence, not a transcription slip.
FUZZY_MIN_LINES = 3
# Anchors this long may also match a window one line shorter or longer (the
# model dropped or duplicated a line while copying).
FUZZY_SIZE_SLACK_MIN_LINES = 6


def _leading_ws(line: str) -> str:
    return line[:len(line) - len(line.lstrip(" \t"))]


def _common_indent(lines: list[str]) -> str:
    """The whitespace prefix shared by every non-blank line, or "" when the
    prefixes disagree (tabs vs spaces) or nothing is indented."""
    nonblank = [ln for ln in lines if ln.strip()]
    if not nonblank:
        return ""
    prefix = _leading_ws(nonblank[0])
    for ln in nonblank[1:]:
        ws = _leading_ws(ln)
        k = 0
        while k < len(prefix) and k < len(ws) and prefix[k] == ws[k]:
            k += 1
        prefix = prefix[:k]
        if not prefix:
            break
    return prefix


def _dedent(lines: list[str], prefix: str) -> list[str]:
    if not prefix:
        return list(lines)
    return [ln[len(prefix):] if ln.startswith(prefix) else ln for ln in lines]


def _reindent(lines: list[str], old: str, new: str) -> list[str]:
    """Move REPLACE lines from the SEARCH's indentation to the window's."""
    out = []
    for ln in lines:
        if not ln.strip():
            out.append(ln)
        elif old and ln.startswith(old):
            out.append(new + ln[len(old):])
        elif not old:
            out.append(new + ln)
        else:
            out.append(ln)  # shallower than the anchor: leave it alone
    return out


def _windows_equal(content_lines: list[str], target: list[str],
                   norm) -> list[int]:
    """Start indexes of every window whose normalised lines equal ``target``."""
    L = len(target)
    hits: list[int] = []
    for i in range(len(content_lines) - L + 1):
        for j in range(L):
            if norm(content_lines[i + j]) != target[j]:
                break
        else:
            hits.append(i)
    return hits


def _indent_relative_windows(content_lines: list[str],
                             search_lines: list[str]) -> list[int]:
    """Windows whose lines equal the search's after both are dedented to their
    own common indent (trailing whitespace ignored)."""
    target = [ln.rstrip() for ln in _dedent(search_lines,
                                            _common_indent(search_lines))]
    L = len(target)
    hits: list[int] = []
    for i in range(len(content_lines) - L + 1):
        window = content_lines[i:i + L]
        dedented = _dedent(window, _common_indent(window))
        if all(dedented[j].rstrip() == target[j] for j in range(L)):
            hits.append(i)
    return hits


def _fuzzy_windows(content_lines: list[str], search_lines: list[str],
                   threshold: float) -> list[tuple[float, int, int]]:
    """(ratio, start, size) for every window scoring at least ``threshold``.

    Windows are the anchor's line count, plus one line either side for
    anchors of FUZZY_SIZE_SLACK_MIN_LINES or more. Cheap upper bounds
    (real_quick_ratio / quick_ratio) gate the O(n*m) ratio so a 6,000-line
    file costs milliseconds, not seconds.
    """
    L = len(search_lines)
    sizes = {L}
    if L >= FUZZY_SIZE_SLACK_MIN_LINES:
        sizes |= {L - 1, L + 1}
    target = "\n".join(ln.rstrip() for ln in search_lines)
    sm = difflib.SequenceMatcher(autojunk=False)
    sm.set_seq2(target)
    stripped = [ln.rstrip() for ln in content_lines]
    out: list[tuple[float, int, int]] = []
    for size in sorted(sizes):
        if size < 1:
            continue
        for i in range(len(stripped) - size + 1):
            sm.set_seq1("\n".join(stripped[i:i + size]))
            if sm.real_quick_ratio() < threshold or sm.quick_ratio() < threshold:
                continue
            r = sm.ratio()
            if r >= threshold:
                out.append((r, i, size))
    return out


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[0] + b[1] and b[0] < a[0] + a[1]


def _splice(content: str, start: int, size: int,
            replacement: list[str]) -> str:
    """Replace ``size`` lines from ``start`` with ``replacement``, preserving
    the file's own line structure (a trailing newline stays a trailing
    newline; a file without one stays without one)."""
    lines = content.split("\n")
    lines[start:start + size] = replacement
    return "\n".join(lines)


def apply_search_replace(current: str, blocks: list[EditBlock]) -> ApplyResult:
    """Apply anchored edit blocks to ``current``; return new content or an error.

    Blocks apply SEQUENTIALLY against the evolving content. Each SEARCH must
    locate EXACTLY ONE place in the content at the moment it is applied, found
    by the first ladder tier that finds anything (DEV-638):
      1. exact — the SEARCH occurs byte-for-byte (substring, as before);
      2. trailing_ws — per-line equality with trailing whitespace ignored;
      3. indent — per-line equality after dedenting both the anchor and the
         candidate window to their own common indentation; the REPLACE is
         re-indented to the window's indentation;
      4. fuzzy — the unique window (same line count, ±1 for long anchors)
         whose difflib similarity reaches FUZZY_RATIO (FUZZY_RATIO_LARGE on
         files over FUZZY_LARGE_FILE_LINES lines) and beats every
         non-overlapping runner-up by FUZZY_MARGIN.
    A tier that finds MORE than one place refuses as ambiguous — it never falls
    through, because every later tier is looser. An empty SEARCH is rejected
    (it would match everywhere / nowhere). An empty REPLACE is a deletion and
    is fully supported. ``applied`` records the tier per block.
    """
    if not blocks:
        return ApplyResult(ok=True, content=current)
    content = current
    applied: list[AppliedBlock] = []
    for idx, block in enumerate(blocks, start=1):
        if block.search == "":
            return ApplyResult(
                ok=False,
                error=(f"edit block #{idx} has an empty SEARCH — a SEARCH must "
                       "quote the exact lines to replace"),
                failed_index=idx, failed_block=block, reason="empty_search",
                applied=applied)

        # Tier 1: exact substring, byte-identical to the pre-ladder behaviour.
        count = content.count(block.search)
        if count == 1:
            content = content.replace(block.search, block.replace, 1)
            applied.append(AppliedBlock(idx, TIER_EXACT, 1.0, None))
            continue
        if count > 1:
            return ApplyResult(
                ok=False,
                error=(f"edit block #{idx}: SEARCH text matches {count} places "
                       "(ambiguous) — add surrounding lines until it is unique. "
                       f"The SEARCH was:\n{_snippet(block.search)}"),
                failed_index=idx, failed_block=block, reason="ambiguous",
                applied=applied)

        content_lines = content.split("\n")
        search_lines = block.search.split("\n")
        replace_lines = block.replace.split("\n") if block.replace != "" else []

        def _ambiguous(tier: str, n: int) -> ApplyResult:
            return ApplyResult(
                ok=False,
                error=(f"edit block #{idx}: SEARCH text matches {n} places "
                       f"(ambiguous, {tier} match) — add surrounding lines "
                       "until it is unique. The SEARCH was:\n"
                       f"{_snippet(block.search)}"),
                failed_index=idx, failed_block=block, reason="ambiguous",
                applied=applied)

        # Tier 2: trailing whitespace ignored on both sides.
        target = [ln.rstrip() for ln in search_lines]
        hits = _windows_equal(content_lines, target, str.rstrip)
        if len(hits) > 1:
            return _ambiguous(TIER_TRAILING_WS, len(hits))
        if hits:
            content = _splice(content, hits[0], len(search_lines), replace_lines)
            applied.append(AppliedBlock(idx, TIER_TRAILING_WS, 1.0, hits[0] + 1))
            continue

        # Tier 3: indent-relative.
        hits = _indent_relative_windows(content_lines, search_lines)
        if len(hits) > 1:
            return _ambiguous(TIER_INDENT, len(hits))
        if hits:
            window = content_lines[hits[0]:hits[0] + len(search_lines)]
            new_lines = _reindent(replace_lines,
                                  _common_indent(search_lines),
                                  _common_indent(window))
            content = _splice(content, hits[0], len(search_lines), new_lines)
            applied.append(AppliedBlock(idx, TIER_INDENT, 1.0, hits[0] + 1))
            continue

        # Tier 4: unique high-similarity window.
        threshold = (FUZZY_RATIO_LARGE
                     if len(content_lines) > FUZZY_LARGE_FILE_LINES
                     else FUZZY_RATIO)
        closest = ""
        if len(search_lines) >= FUZZY_MIN_LINES:
            cands = _fuzzy_windows(content_lines, search_lines, threshold)
            if cands:
                cands.sort(key=lambda c: (-c[0], c[1]))
                best = cands[0]
                rivals = [c for c in cands[1:]
                          if not _overlaps((best[1], best[2]), (c[1], c[2]))]
                if rivals and best[0] - rivals[0][0] < FUZZY_MARGIN:
                    return _ambiguous(TIER_FUZZY, 1 + len(rivals))
                content = _splice(content, best[1], best[2], replace_lines)
                applied.append(AppliedBlock(idx, TIER_FUZZY, round(best[0], 4),
                                            best[1] + 1))
                continue
            # No window reached the threshold: say how close the best got so
            # the retry (and a human) can see whether it was a near miss.
            near = _fuzzy_windows(content_lines, search_lines, 0.80)
            if near:
                near.sort(key=lambda c: -c[0])
                closest = (f" Closest window: {near[0][0]:.2f} similarity at "
                           f"line {near[0][1] + 1}, below the {threshold:.2f} "
                           "threshold.")
        return ApplyResult(
            ok=False,
            error=(f"edit block #{idx}: SEARCH text not found in the current "
                   f"file.{closest} The SEARCH was:\n{_snippet(block.search)}"),
            failed_index=idx, failed_block=block, reason="not_found",
            applied=applied)
    return ApplyResult(ok=True, content=content, applied=applied)


def resolve_edits(
    whole_files: list[tuple[str, str]],
    edit_text: str,
    existing: dict[str, str],
) -> ResolveResult:
    """Combine whole-file (new) blocks with applied SEARCH/REPLACE (existing) edits.

    * ``whole_files`` — ``(path, content)`` pairs the model emitted as whole
      files (new files; parsed by the normal ``<<<FILE>>>`` path). Passed
      through unchanged.
    * ``edit_text`` — the raw model response, scanned for `### path` +
      SEARCH/REPLACE edit blocks.
    * ``existing`` — ``path -> current repo content`` for files that already
      exist (exactly the set shown to the model as "Current contents of files
      you must modify"). Membership here is the ground truth for "this file
      exists", so the applier and the prompt never disagree about which files
      are editable.

    Returns write-ready files plus a list of errors. When errors is non-empty
    the caller must not write anything and should route the attempt back to the
    implementer with the diagnostics.
    """
    resolved: dict[str, str] = {}
    order: list[str] = []

    def put(path: str, content: str) -> None:
        if path not in resolved:
            order.append(path)
        resolved[path] = content

    # New files pass through untouched.
    for path, content in whole_files:
        put(path, content)

    errors: list[str] = []
    failures: list[EditFailure] = []
    applied: list[EditApplied] = []
    parsed = parse_edit_blocks(edit_text)
    for note in parsed.malformed:
        errors.append(note)
        failures.append(EditFailure(path="", block=0, reason="malformed",
                                    search="", detail=note))

    for fe in parsed.files:
        current = existing.get(fe.path)
        if current is None:
            # DEV-638: one block whose SEARCH is empty against a path that does
            # not exist is unambiguous — the model meant "create this file with
            # the REPLACE as its content". Take it as the whole file.
            if len(fe.blocks) == 1 and fe.blocks[0].search == "":
                put(fe.path, fe.blocks[0].replace)
                applied.append(EditApplied(fe.path, 1,
                                           TIER_WHOLE_FROM_EMPTY_SEARCH, 1.0, None))
                continue
            # Otherwise the model emitted edit blocks for a file we never showed
            # it — there is no base content to apply against. Never invent one.
            detail = (
                f"`{fe.path}` is a NEW file — EMIT WHOLE. It is not among the "
                "existing files shown to you, so there is no content to edit "
                "and SEARCH/REPLACE blocks cannot apply to it. Emit it as one "
                f"complete whole-file block opening with <<<FILE: {fe.path}>>> "
                "and closed by the END_FILE marker on its own line.")
            errors.append(detail)
            # Keep the first block's SEARCH: a new-path block set with an empty
            # SEARCH is the "meant to emit whole" signature DEV-638 wants to
            # recognise, and only the retained text can show it.
            failures.append(EditFailure(
                path=fe.path, block=0, reason="no_base",
                search=fe.blocks[0].search if fe.blocks else "", detail=detail))
            continue
        outcome = apply_search_replace(current, fe.blocks)
        if not outcome.ok:
            detail = f"`{fe.path}`: {outcome.error}"
            errors.append(detail)
            failures.append(EditFailure(
                path=fe.path, block=outcome.failed_index or 0,
                reason=outcome.reason or "not_found",
                search=outcome.failed_block.search if outcome.failed_block else "",
                detail=detail))
            continue
        put(fe.path, outcome.content or "")
        applied.extend(EditApplied(fe.path, a.index, a.tier, a.ratio, a.line)
                       for a in outcome.applied)

    files = [(p, resolved[p]) for p in order]
    return ResolveResult(files=files, errors=errors, failures=failures,
                         applied=applied)
