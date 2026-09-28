"""Deterministic fixes applied to generated files before they are written
(pinned dependencies, the Swift Foundation import, protected-type
collisions) and deterministic scans the reviewer is shown (TypeScript
`any`, tautological asserts)."""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

from . import swift_prechecks


logger = logging.getLogger("orchestrator.executor")


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
def _swift_code_only(src: str) -> str:
    """Blank out string literals and comments so symbol matching sees code.

    Without this, a doc comment reading "returns a UUID" would request an
    import the file does not need. One scanner for every Swift check
    (DEV-838): swift_prechecks', which also handles nested block comments.
    """
    return swift_prechecks.blank_comments_and_strings(src)


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
# declares. Column-0 declarations only, on both sides: a NESTED type of the
# same name is a different type in a different scope, and extending a
# protected type is the correct way to add to it. The reading is
# swift_prechecks' (DEV-838), which also sees attributed declarations
# (`@MainActor final class C`); this module's own regex did not, so a
# default-MainActor target's protected types were invisible to the check.
def declared_top_level_types(content: str) -> "set[str]":
    """Type names declared at file scope in Swift source."""
    return {d.name for d in swift_prechecks.top_level_declarations("", content)}


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
