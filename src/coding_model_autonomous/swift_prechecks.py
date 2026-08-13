"""Local static pre-dispatch checks for generated Swift (DEV-512).

Two of the largest single error signatures in this pipeline are decidable from
the emitted text alone, with no Swift toolchain and no Mac round-trip, yet today
every one of them costs a full manifest build plus a runner dispatch (up to
~300s) to discover:

  * ``invalid redeclaration of 'X'`` — the same top-level type declared in two
    places within one module. Concrete: a generated ``Player.swift`` declaring
    ``enum Direction { … }`` while ``CentipedeChain.swift`` already declares one.
  * ``'mutating' is not valid on instance methods in classes`` — a
    ``mutating func`` inside a ``class`` body. ``mutating`` is only valid on the
    value types (``struct`` / ``enum``); on a reference type it never compiles.

This module holds the *pure* detectors. The orchestrator runs them in front of
the Mac dispatch and routes any violation back to the implementer through the
same channel a real build failure uses (a ``build_reason`` + a
``path:line:col: error:`` report), so the round-trip is skipped and retry
behaviour is unchanged.

Design stance, straight from DEV-512: this is a *fast-feedback* measure, not a
Swift parser. Anything ambiguous is left to the real compiler, which already
runs. A false NEGATIVE just falls through to the existing build check and costs
the dispatch we would have paid anyway; a false POSITIVE rejects code the
compiler would accept, so the detectors are deliberately conservative and the
bar for firing is high (column-0 top-level declarations only, ``extension``
excluded, files carrying ``#if`` conditional compilation skipped).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# ── Comment / string blanking (line- and length-preserving) ──────────────────
#
# The scanners below must not see a keyword, brace, or type name that appears
# only inside a comment or a string literal. We replace comment and string
# CONTENT (and their delimiters) with spaces while preserving every newline and
# the overall length, so byte offsets — and therefore line numbers — still map
# straight back to the original source. Swift specifics handled: nested
# ``/* … */`` block comments, ``//`` line comments, ``"…"`` strings with ``\"``
# escapes, and ``"""…"""`` multiline strings.
#
# Known limitation (shared with executor._swift_code_only): a ``"`` nested
# inside a string interpolation — ``"\(d["k"])"`` — ends the string early. It is
# rare in the code these checks target and only ever costs a missed detection,
# never a false positive, so it is accepted rather than parsed around.

def blank_comments_and_strings(src: str) -> str:
    """Return *src* with comment/string content replaced by spaces.

    Newlines and total length are preserved, so line numbers computed on the
    result are valid for the original source.
    """
    out: list[str] = []
    i, n = 0, len(src)
    block_depth = 0            # inside N nested /* */
    state: str | None = None   # None | 'line' | 'str' | 'multi'
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if block_depth > 0:
            if c == "/" and nxt == "*":
                block_depth += 1; out.append("  "); i += 2; continue
            if c == "*" and nxt == "/":
                block_depth -= 1; out.append("  "); i += 2; continue
            out.append("\n" if c == "\n" else " "); i += 1; continue
        if state is None:
            if c == "/" and nxt == "/":
                state = "line"; out.append("  "); i += 2; continue
            if c == "/" and nxt == "*":
                block_depth = 1; out.append("  "); i += 2; continue
            if src[i:i + 3] == '"""':
                state = "multi"; out.append("   "); i += 3; continue
            if c == '"':
                state = "str"; out.append(" "); i += 1; continue
            out.append(c); i += 1; continue
        if state == "line":
            if c == "\n":
                state = None; out.append("\n")
            else:
                out.append(" ")
            i += 1; continue
        if state == "str":
            if c == "\\":
                out.append(" "); out.append("\n" if nxt == "\n" else " ")
                i += 2; continue
            if c == '"':
                state = None; out.append(" "); i += 1; continue
            out.append("\n" if c == "\n" else " "); i += 1; continue
        # state == "multi"
        if src[i:i + 3] == '"""':
            state = None; out.append("   "); i += 3; continue
        out.append("\n" if c == "\n" else " "); i += 1; continue
    return "".join(out)


def _line_of(text: str, offset: int) -> int:
    """1-based line number of *offset* within *text*."""
    return text.count("\n", 0, offset) + 1


# Files carrying conditional compilation are exempt from the duplicate check: a
# type legitimately declared once per ``#if os(…)`` branch is not a
# redeclaration, and telling them apart needs a preprocessor. Skipping the whole
# file keeps the check free of that false-positive class at the cost of a rare
# missed detection, which the compiler still catches.
_CONDITIONAL_COMPILATION_RE = re.compile(r"^\s*#(?:if|elseif|else|endif)\b",
                                         re.MULTILINE)

# A top-level (file-scope) type declaration. Column-0 anchored on purpose: a
# nested type is indented and is a *different* type in a different scope, so
# matching indented declarations would flag a legal name reuse. Leading
# attributes (``@MainActor``, ``@objc(x)``) and access/inheritance modifiers are
# consumed so ``@MainActor public final class C`` still matches. ``extension`` is
# absent from the keyword set deliberately — extending a type is not
# redeclaring it.
_TOPLEVEL_DECL_RE = re.compile(
    r"^(?:@[A-Za-z_]\w*(?:\s*\([^)]*\))?[ \t]+)*"
    r"(?:(?:public|internal|fileprivate|private|final|open|indirect)[ \t]+)*"
    r"(struct|class|enum|protocol|actor|typealias)[ \t]+([A-Za-z_]\w*)",
    re.MULTILINE)


@dataclass(frozen=True)
class Declaration:
    """One top-level type declaration: where it is and what it names."""
    path: str
    line: int
    kind: str   # struct | class | enum | protocol | actor | typealias
    name: str


def top_level_declarations(path: str, content: str) -> list[Declaration]:
    """Top-level type declarations in one Swift file, in source order.

    Line numbers are 1-based and index the original source. Column-0 anchoring
    is the file-scope heuristic; ``extension`` is intentionally not matched.
    """
    blanked = blank_comments_and_strings(content)
    out: list[Declaration] = []
    for m in _TOPLEVEL_DECL_RE.finditer(blanked):
        out.append(Declaration(path=path, line=_line_of(blanked, m.start()),
                               kind=m.group(1), name=m.group(2)))
    return out


@dataclass(frozen=True)
class Violation:
    """A statically-detected Swift error, shaped like a compiler diagnostic.

    ``kind`` is a stable machine tag for the event taxonomy;
    ``diagnostic_lines`` render as ``path:line:col: error:``/``note:`` so the
    orchestrator's existing failure-routing and persistence detection read them
    exactly as they read swiftc output.
    """
    kind: str                 # 'duplicate_declaration' | 'mutating_in_class'
    message: str              # the `error:` text, without location
    path: str                 # the offending (generated) file
    line: int
    notes: tuple[tuple[str, int], ...] = ()  # (path, line) of related sites

    def error_line(self) -> str:
        return f"{self.path}:{self.line}:1: error: {self.message}"

    def diagnostic_lines(self) -> list[str]:
        lines = [self.error_line()]
        for npath, nline in self.notes:
            lines.append(f"{npath}:{nline}:1: note: previously declared here")
        return lines


def duplicate_type_declarations(
    generated: list[tuple[str, str]],
    context: tuple | list = (),
) -> list[Violation]:
    """Top-level types declared in more than one place across the module.

    *generated* are the files this pass produced; *context* are read-only
    existing in-scope repo files (e.g. the protected scaffold) whose types the
    generated set must not collide with. A violation is raised only when the
    offending declaration lives in a GENERATED file — a collision purely between
    two pre-existing files is not this pass's doing and not its to fix. Context
    files sharing a path with a generated file are ignored: the generated
    version supersedes the old one, so that is not a collision.

    One :class:`Violation` per offending generated declaration, each naming the
    other site(s) — so both paths always appear in the diagnostic.
    """
    gen_paths = {p for p, _ in generated}

    def _eligible(path: str, content: str) -> bool:
        return (path.endswith(".swift")
                and not _CONDITIONAL_COMPILATION_RE.search(
                    blank_comments_and_strings(content)))

    # name -> declarations, generated first so the "previously declared here"
    # note prefers an existing/context site as the anchor.
    by_name: dict[str, list[Declaration]] = {}
    generated_decls: list[Declaration] = []
    for path, content in generated:
        if not _eligible(path, content):
            continue
        for decl in top_level_declarations(path, content):
            by_name.setdefault(decl.name, []).append(decl)
            generated_decls.append(decl)
    for path, content in context:
        if path in gen_paths or not _eligible(path, content):
            continue
        for decl in top_level_declarations(path, content):
            by_name.setdefault(decl.name, []).append(decl)

    violations: list[Violation] = []
    for decl in generated_decls:
        others = [d for d in by_name[decl.name]
                  if (d.path, d.line) != (decl.path, decl.line)]
        if not others:
            continue
        # Report each colliding name once, on its first generated site, so a
        # symmetric two-file clash yields one clear diagnostic rather than two.
        earlier_generated = [
            d for d in generated_decls
            if d.name == decl.name and (d.path, d.line) < (decl.path, decl.line)
        ]
        if earlier_generated:
            continue
        notes = tuple(sorted((d.path, d.line) for d in others))
        violations.append(Violation(
            kind="duplicate_declaration",
            message=f"invalid redeclaration of '{decl.name}'",
            path=decl.path, line=decl.line, notes=notes))
    return violations


# ── mutating func inside a class ─────────────────────────────────────────────
#
# Brace-matched scope tracking over the blanked source. Only the six
# type/extension keywords open a scope we care about; every other ``{`` (a
# function body, a computed property, a closure, an ``if``) is an opaque scope
# that we still push/pop so nesting stays balanced but that is never a valid
# home for a `mutating func`. When a ``mutating func`` is seen, the nearest
# ENCLOSING type scope decides: only ``class`` is an error. ``struct`` / ``enum``
# are valid, ``protocol`` declares a requirement (valid), and ``extension`` is
# left to the compiler (it could extend a value type), so none of those fire.

_TYPE_SCOPE_KEYWORDS = {"class", "struct", "enum", "actor", "protocol",
                        "extension"}
# Tokens: a brace, or a bare word (keyword / identifier). Everything else is
# skipped, which is why string/comment blanking must happen first.
_TOKEN_RE = re.compile(r"\{|\}|[A-Za-z_]\w*")
_MUTATING_FUNC_RE = re.compile(r"\bmutating\b[ \t]+func\b[ \t]+([A-Za-z_]\w*)?")


def mutating_methods_in_classes(
    files: list[tuple[str, str]],
) -> list[Violation]:
    """`mutating func` declared directly inside a `class` body, per file.

    One :class:`Violation` per offending method, located at the ``mutating``
    keyword. A ``mutating func`` inside a ``struct``/``enum`` nested within a
    class does NOT fire — the nearest enclosing type is the value type.
    """
    violations: list[Violation] = []
    for path, content in files:
        if not path.endswith(".swift"):
            continue
        blanked = blank_comments_and_strings(content)
        scope_stack: list[str] = []
        pending: str | None = None  # scope kind the next '{' opens
        for tok in _TOKEN_RE.finditer(blanked):
            t = tok.group(0)
            if t == "{":
                scope_stack.append(pending or "other")
                pending = None
            elif t == "}":
                if scope_stack:
                    scope_stack.pop()
                pending = None
            elif t in _TYPE_SCOPE_KEYWORDS:
                pending = t
            elif t == "mutating":
                # Is this the modifier on a method, not an identifier?
                m = _MUTATING_FUNC_RE.match(blanked, tok.start())
                if not m:
                    continue
                enclosing = next((s for s in reversed(scope_stack)
                                  if s in _TYPE_SCOPE_KEYWORDS), None)
                if enclosing == "class":
                    violations.append(Violation(
                        kind="mutating_in_class",
                        message=("'mutating' is not valid on instance methods "
                                 "in classes"),
                        path=path, line=_line_of(blanked, tok.start()),
                        notes=()))
    return violations


@dataclass
class SwiftPrecheckResult:
    """Outcome of the local Swift pre-checks over a generated file set."""
    violations: list[Violation] = field(default_factory=list)

    def failed(self) -> bool:
        return bool(self.violations)

    def summary(self) -> str:
        """One-line reason for the first violation — used as ``build_reason``."""
        if not self.violations:
            return ""
        return self.violations[0].message[:200]

    def report(self) -> str:
        """Full diagnostic block, in swiftc's ``path:line:col:`` shape.

        Consumed by the orchestrator exactly as a real build log is: the
        ``error:`` lines drive failure detection, the routing to the architect
        on repeats, and the retry feedback the implementer sees.
        """
        lines: list[str] = []
        for v in self.violations:
            lines.extend(v.diagnostic_lines())
        return "\n".join(lines) + ("\n" if lines else "")

    def event_payload(self) -> list[dict]:
        """Machine-readable violation list for the event timeline (DEV-529)."""
        return [
            {"kind": v.kind, "path": v.path, "line": v.line,
             "message": v.message,
             "related": [f"{p}:{ln}" for p, ln in v.notes]}
            for v in self.violations
        ]


def run_swift_prechecks(
    generated_files: list[tuple[str, str]],
    context_files: tuple | list = (),
) -> SwiftPrecheckResult:
    """Run every local Swift pre-check over a generated file set.

    *generated_files* is ``[(path, content), …]`` as produced by the
    implementer; *context_files* are read-only existing in-scope repo files
    (the protected scaffold) the generated types must not collide with. Only
    ``.swift`` files are inspected; a set with none yields no violations.
    """
    violations: list[Violation] = []
    violations += duplicate_type_declarations(generated_files, context_files)
    violations += mutating_methods_in_classes(generated_files)
    return SwiftPrecheckResult(violations=violations)
