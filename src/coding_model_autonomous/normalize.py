"""Deterministic fixes applied to generated files before they are written
(each language pack's boilerplate fixes, protected-type collisions) and
deterministic scans the reviewer is shown (TypeScript `any`, tautological
asserts)."""
from __future__ import annotations

import logging
import re
from typing import Optional

from . import languages


logger = logging.getLogger("orchestrator.executor")


# DEV-552: a generated file may not redeclare a type a protected file already
# declares. Column-0 declarations only, on both sides: a NESTED type of the
# same name is a different type in a different scope, and extending a
# protected type is the correct way to add to it. Which names a file declares
# is its language pack's reading; Swift's also sees attributed declarations
# (`@MainActor final class C`), so a default-MainActor target's protected
# types are visible to the check.
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
        declared |= languages.declared_types(path, content)
    if not declared:
        return []

    out: list[tuple[str, list[str], bool]] = []
    for path, content in files:
        if path in protected_paths:
            continue
        mine = languages.declared_types(path, content)
        hits = sorted(mine & declared)
        if hits:
            out.append((path, hits, mine == set(hits)))
    return out


def normalize_boilerplate(
    files: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[str]]:
    """Deterministically fix boilerplate the reviewer checks. Returns the
    (possibly rewritten) file list and a list of human-readable change notes.

    The fixes are the language packs' (``LanguagePack.normalize``): today,
    exact-pinned package.json ranges and a missing Swift `import Foundation`
    (DEV-540).
    """
    out: list[tuple[str, str]] = []
    notes: list[str] = []
    for path, content in files:
        content, file_notes = languages.normalize_file(path, content)
        notes += file_notes
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
