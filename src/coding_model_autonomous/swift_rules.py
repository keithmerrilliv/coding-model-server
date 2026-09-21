"""Swift-specific guidance and repair targeting (DEV-764, DEV-767).

Runs 44-48 (2026-09-19/20) each ended a few one-token Swift slips from
delivery, and the repair round could not turn the compiler's diagnostic into
the edit it named:

* run 47: ``static member 'maxFramesInFlight' cannot be used on instance`` at
  one line; the repair left that line alone and REMOVED qualifiers from four
  others (1 -> 6). Retry 1, synthesis and repair all made the same slip.
* run 48: 14 diagnostics, every one in the test file (``try #require`` inside
  ``@Test func`` not declared ``throws``); the repair rewrote the renderer and
  never touched the test file (14 -> 14).

This module is the DEV-644 treatment for Swift. DEV-644 fixed "the feedback
named the module, so each agent fixed the module rather than the import" with
a standing paragraph rendered into every Python prompt. Three pieces here:

1. :func:`render_swift_rules` — the standing paragraph, rendered into the
   implementer, synthesis and repair prompts whenever the file set has Swift.
2. :func:`located_diagnostics` + :func:`fix_hints` — the ``path:line``
   citations a build reported, ANSI-stripped, mapped onto artifact paths, with
   a per-class hint saying what the diagnostic MEANS to change.
3. :func:`filter_repair_to_cited` — cite-or-refuse: a repair may only replace
   files the diagnostics name, and is not worth a Mac round trip unless it
   changed at least one cited line.

Everything here is pure; the daemon wires it in and the tests drive it
directly.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

# Same pattern as outcome.ANSI_SGR_RE (DEV-755); duplicated rather than
# imported because outcome -> context -> test_runner -> workspace -> executor
# -> here would be a cycle, and executor is the first importer of this module.
ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")

# ── 1. The standing paragraph ────────────────────────────────────────────────

# FROZEN as of DEV-778 (2026-09-20): a new diagnostic class goes into
# _FIX_HINTS (reactive, rendered only when the diagnostic is present) and
# swift_prechecks (detected before the build) — NOT into this paragraph,
# which every Swift prompt pays for whether or not the class is at issue.
_SWIFT_RULES = (
    "## Swift rules — MANDATORY\n\n"
    "Each of these has cost a full attempt; the compiler's message for each is "
    "quoted so you recognise it and apply the ONE edit it asks for.\n\n"
    "1. A `static` member used inside an instance method, initializer or "
    "computed property MUST be qualified: `Self.name` or `TypeName.name`. "
    "The diagnostic `static member 'X' cannot be used on instance of type 'T'` "
    "means ADD `Self.` on that line. It never means remove qualifiers "
    "elsewhere.\n"
    "2. A Swift Testing `@Test func` whose body uses `try` — including "
    "`try #require(...)` — MUST be declared `throws`: `@Test func name() "
    "throws {`. The diagnostic `errors thrown from here are not handled` "
    "means add `throws` to the enclosing function.\n"
    "3. `main actor-isolated ... in a synchronous nonisolated context` means "
    "annotate the ENCLOSING declaration `@MainActor` (the test method, the "
    "helper, the closure's owner) — not the callee. Every helper a `@MainActor` "
    "test calls needs the same annotation.\n"
    "4. `requires that 'X' conform to 'Hashable'` (or `Equatable`, `Sendable`) "
    "means add the conformance to X's DECLARATION, not rewrite the caller.\n"
    "5. `missing return in ... expected to return 'T'` means a `switch` or "
    "`if` arm lacks `return` — add it; do not restructure the function.\n"
    "6. A Metal vertex function declared with `[[stage_in]]` / "
    "`[[attribute(n)]]` needs an `MTLVertexDescriptor` set on the pipeline "
    "descriptor, or `makeRenderPipelineState` throws at runtime.\n\n"
    "A diagnostic names the line to change. Change that line. Do not touch "
    "files or lines no diagnostic names.\n\n"
)


def has_swift(paths: "list[str]") -> bool:
    return any(str(p).endswith(".swift") for p in paths or [])


def render_swift_rules(paths: "list[str]") -> str:
    """The Swift rules section, or "" when no path is Swift (so every
    non-Swift prompt stays byte-identical)."""
    return _SWIFT_RULES if has_swift(paths) else ""


# ── 2. Located diagnostics and fix hints ─────────────────────────────────────

_LOCATED_RE = re.compile(
    r"^\s*(?P<path>\S.*?\.\w+):(?P<line>\d+):(?P<col>\d+): error: (?P<msg>.+?)\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class LocatedDiagnostic:
    path: str        # as the compiler printed it (often absolute)
    line: int
    message: str
    artifact: str | None = None   # the artifact relpath it maps onto, if any

    def located(self) -> str:
        return f"{self.artifact or self.path}:{self.line}"


def map_to_artifact(diag_path: str, artifact_paths: "list[str]") -> str | None:
    """The artifact relpath *diag_path* names, by path-boundary suffix.

    The compiler prints the worktree's absolute path
    (``/Users/km4/.../worktrees/spec_x-abc/Sources/A/B.swift``); the artifact
    is ``Sources/A/B.swift``. Longest suffix match wins so ``Tests/X.swift``
    is not confused with ``Sources/Tests/X.swift``.
    """
    diag_path = diag_path.replace("\\", "/")
    best: str | None = None
    for rel in artifact_paths:
        r = rel.replace("\\", "/").lstrip("./")
        if diag_path == r or diag_path.endswith("/" + r):
            if best is None or len(r) > len(best):
                best = rel
    return best


def located_diagnostics(output: str,
                        artifact_paths: "list[str] | None" = None
                        ) -> list[LocatedDiagnostic]:
    """Every ``path:line:col: error: msg`` in *output*, ANSI-stripped, in
    order, deduplicated on (path, line, message)."""
    if not output:
        return []
    text = ANSI_SGR_RE.sub("", output)
    seen: set = set()
    out: list[LocatedDiagnostic] = []
    for m in _LOCATED_RE.finditer(text):
        key = (m.group("path"), int(m.group("line")), m.group("msg"))
        if key in seen:
            continue
        seen.add(key)
        art = map_to_artifact(m.group("path"), artifact_paths or [])
        out.append(LocatedDiagnostic(path=m.group("path"),
                                     line=int(m.group("line")),
                                     message=m.group("msg"), artifact=art))
    return out


# (regex on the message, the hint). First match wins.
_FIX_HINTS: "list[tuple[re.Pattern, str]]" = [
    (re.compile(r"static member '(\w+)' cannot be used on instance"),
     "qualify the reference on this line as `Self.{0}` — do not remove "
     "qualifiers anywhere else"),
    (re.compile(r"errors thrown from here are not handled"),
     "declare the enclosing function `throws` (for a test: "
     "`@Test func name() throws {{`)"),
    (re.compile(r"main actor-isolated .* (?:in a synchronous nonisolated "
                r"context|from a nonisolated context)"),
     "annotate the ENCLOSING declaration `@MainActor` (a test: "
     "`@MainActor func name() async throws {{`) and `await` the call — never "
     "mark the callee `nonisolated`, and every helper that test calls needs "
     "the same annotation"),
    (re.compile(r"call to main actor-isolated"),
     "annotate the ENCLOSING declaration `@MainActor` (a test: "
     "`@MainActor func name() async throws {{`) and `await` the call — never "
     "mark the callee `nonisolated`"),
    (re.compile(r"'nil' is not compatible with expected argument type '([^']+)'"),
     "the parameter is not optional — pass a `{0}` value (for an OptionSet "
     "such as `MTLResourceOptions`, `[]`)"),
    (re.compile(r"invalid redeclaration of '(\w+)'"),
     "delete this duplicate declaration of `{0}` and use the one that "
     "already exists — never rename one copy to dodge the clash"),
    (re.compile(r"requires that '(\w+)' conform to '(\w+)'"),
     "add `: {1}` to the declaration of `{0}`"),
    (re.compile(r"missing return in .* expected to return"),
     "add `return` to the arm that lacks it"),
    (re.compile(r"'(\w+)' is inaccessible due to '(\w+)' protection level"),
     "widen `{0}`'s access at its declaration (e.g. `private(set) var` or "
     "`internal`)"),
    (re.compile(r"incorrect argument label in call \(have '([^']+)', "
                r"expected '([^']+)'\)"),
     "use the labels `{1}`"),
    (re.compile(r"cannot find '(\w+)' in scope"),
     "`{0}` is not declared in any file you were given — it lives in a file "
     "outside your set (use it as declared there; do NOT invent a "
     "declaration for it) or the name is misspelled; if the build also "
     "reports a failed emit-module, this is a consequence of that, not the "
     "cause"),
    (re.compile(r"value of type '(\w+)' has no member '(\w+)'"),
     "`{0}` has no `{1}` — use the member it actually declares"),
]


def fix_hint(message: str) -> str | None:
    """The fix a diagnostic *message* asks for, or None when unknown."""
    for pattern, hint in _FIX_HINTS:
        m = pattern.search(message)
        if m:
            try:
                return hint.format(*m.groups())
            except (IndexError, KeyError):
                return hint
    return None


def render_cited_diagnostics(diags: "list[LocatedDiagnostic]",
                             *, repair: bool = True) -> str:
    """Every cited location with its hint (DEV-778: rendered ONLY for the
    diagnostics actually present), followed by the rule for this prompt.

    ``repair=True`` is the synthesis repair round (DEV-767): edits must land
    on cited files and lines or the repair is refused. ``repair=False`` is
    the implementer's build-failure retry, where whole-file re-emission is
    the contract and other files are legal, so the rule is softer: change
    the named lines, and nothing a diagnostic does not require.
    """
    if not diags:
        return ""
    lines = ["## Cited locations — your edits MUST land here\n\n"]
    for d in diags[:40]:
        hint = fix_hint(d.message)
        lines.append(f"- `{d.located()}`: {d.message}")
        if hint:
            lines.append(f"  → fix: {hint}")
        lines.append("")
    if repair:
        lines.append(
            "Emit a <<<FILE: path>>> block ONLY for files listed above, and make "
            "sure each block changes at least one of its cited lines. A block for "
            "any other file is discarded before the build; a repair that changes "
            "no cited line is not built at all.\n\n"
        )
    else:
        lines.append(
            "Each `→ fix` is the ONE edit that diagnostic asks for; apply it on "
            "the line named (or on the declaration it points at). Do not "
            "rewrite lines no diagnostic names, and do not restructure a "
            "function to avoid a one-token fix.\n\n"
        )
    return "\n".join(lines)


# ── 3. Cite-or-refuse ────────────────────────────────────────────────────────

@dataclass
class CiteFilterResult:
    kept: "list[tuple[str, str]]"
    dropped: "list[str]" = field(default_factory=list)      # uncited paths
    untouched: "list[str]" = field(default_factory=list)    # cited, no cited line changed
    touched_cited_line: bool = False
    applied: bool = False   # False when there was nothing to cite against

    def refuse(self) -> bool:
        """True when the repair is not worth building."""
        return self.applied and not self.touched_cited_line


def _changed_line_indices(before: str, after: str) -> set:
    """0-based indices of *before* lines that a diff replaces or deletes,
    plus the neighbours of pure insertions (an insertion between lines
    i-1 and i touches both)."""
    a = before.splitlines()
    b = after.splitlines()
    changed: set = set()
    for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(
            None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            changed.update({i1 - 1, i1})
        else:
            changed.update(range(i1, i2))
    return changed


def filter_repair_to_cited(repair_files: "list[tuple[str, str]]",
                           before: "dict[str, str | None]",
                           diags: "list[LocatedDiagnostic]") -> CiteFilterResult:
    """Keep only the emitted files a diagnostic cites, and say whether any
    cited line changed.

    *before* maps each emitted relpath to its pre-repair content (None when
    the file did not exist). When no diagnostic maps onto an artifact the
    filter is a no-op (``applied=False``) — there is nothing to cite against,
    e.g. a near-miss test failure rather than a build failure.
    """
    cited: "dict[str, set]" = {}
    for d in diags:
        if d.artifact:
            cited.setdefault(d.artifact, set()).add(d.line - 1)  # 0-based
    if not cited:
        return CiteFilterResult(kept=list(repair_files), applied=False,
                                touched_cited_line=True)
    res = CiteFilterResult(kept=[], applied=True)
    for rel, content in repair_files:
        if rel not in cited:
            res.dropped.append(rel)
            continue
        prev = before.get(rel)
        if prev is None:
            # A cited file the workspace does not have: nothing to compare
            # against, keep it and count it as touched.
            res.kept.append((rel, content))
            res.touched_cited_line = True
            continue
        changed = _changed_line_indices(prev, content)
        # A cited line counts as touched if it, or an immediate neighbour,
        # changed — `throws` lands on the `func` line one above a cited `try`.
        hits = {i + off for i in cited[rel] for off in (-1, 0, 1)}
        if changed & hits:
            res.kept.append((rel, content))
            res.touched_cited_line = True
        else:
            res.kept.append((rel, content))
            res.untouched.append(rel)
    return res
