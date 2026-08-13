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
content EXACTLY — byte-for-byte, including whitespace — and EXACTLY ONCE. New
files are unaffected: they keep whole-file emission and never reach this module.

Why this module exists: re-emitting a whole existing file makes the model
corrupt large files and re-corrupt them on every retry (the "whole-file
re-emission" failure). Applying a small, anchored edit mechanically removes that
failure mode for the parts of the file the change does not touch.

Everything here is PURE: it reads no files, touches no globals, logs nothing,
and is fully unit-testable in isolation. The orchestrator supplies the current
file contents and decides how to route failures.
"""
from __future__ import annotations

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
    """Outcome of applying a file's edit blocks to its current content."""
    ok: bool
    content: str | None = None      # new full-file content when ok
    error: str | None = None        # precise diagnostic when not ok


@dataclass
class ResolveResult:
    """Combined new-files + applied-edit-files, plus any apply/parse errors.

    ``files`` is the write-ready ``(path, full_content)`` list. When ``errors``
    is non-empty the caller MUST NOT write anything — an unappliable edit routes
    the whole attempt back to the implementer rather than partially applying.
    """
    files: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


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


def apply_search_replace(current: str, blocks: list[EditBlock]) -> ApplyResult:
    """Apply anchored edit blocks to ``current``; return new content or an error.

    Blocks apply SEQUENTIALLY against the evolving content. Each SEARCH must
    occur EXACTLY ONCE in the content at the moment it is applied:
      * 0 matches  → error (SEARCH not found)
      * >1 matches → error (ambiguous)
    An empty SEARCH is rejected (it would match everywhere / nowhere). An empty
    REPLACE is a deletion and is fully supported.
    """
    if not blocks:
        return ApplyResult(ok=True, content=current)
    content = current
    for idx, block in enumerate(blocks, start=1):
        if block.search == "":
            return ApplyResult(
                ok=False,
                error=(f"edit block #{idx} has an empty SEARCH — a SEARCH must "
                       "quote the exact lines to replace"))
        count = content.count(block.search)
        if count == 0:
            return ApplyResult(
                ok=False,
                error=(f"edit block #{idx}: SEARCH text not found in the current "
                       f"file. The SEARCH was:\n{_snippet(block.search)}"))
        if count > 1:
            return ApplyResult(
                ok=False,
                error=(f"edit block #{idx}: SEARCH text matches {count} places "
                       "(ambiguous) — add surrounding lines until it is unique. "
                       f"The SEARCH was:\n{_snippet(block.search)}"))
        content = content.replace(block.search, block.replace, 1)
    return ApplyResult(ok=True, content=content)


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
    parsed = parse_edit_blocks(edit_text)
    for note in parsed.malformed:
        errors.append(note)

    for fe in parsed.files:
        current = existing.get(fe.path)
        if current is None:
            # The model emitted edit blocks for a file we never showed it — we
            # have no base content to apply against. Never invent one.
            errors.append(
                f"`{fe.path}`: edit blocks were emitted but this file is not "
                "among the existing files shown to you, so there is no content "
                "to edit. Emit it as a whole new file, or edit a file that was "
                "shown.")
            continue
        outcome = apply_search_replace(current, fe.blocks)
        if not outcome.ok:
            errors.append(f"`{fe.path}`: {outcome.error}")
            continue
        put(fe.path, outcome.content or "")

    files = [(p, resolved[p]) for p in order]
    return ResolveResult(files=files, errors=errors)
