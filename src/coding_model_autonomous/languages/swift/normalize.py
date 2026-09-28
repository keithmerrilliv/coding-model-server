"""Swift's deterministic file fixes: the missing `import Foundation`, and
the file-scope type names the protected-type collision check compares."""
from __future__ import annotations

import re

from . import prechecks


# ── Deterministic Swift `import Foundation` (DEV-540) ───────────────────────
#
# Same principle as the JavaScript pack's package.json pinning, for Swift:
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
    (DEV-838): prechecks', which also handles nested block comments.
    """
    return prechecks.blank_comments_and_strings(src)


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


def declared_top_level_types(content: str) -> "set[str]":
    """Type names declared at file scope in Swift source."""
    return {d.name for d in prechecks.top_level_declarations("", content)}


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
