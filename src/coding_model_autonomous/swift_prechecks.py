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


# ── DEV-764: unqualified static members inside instance context ──────────────
#
# Run 47 (spec_ffe89fdd): `frameIndex = (frameIndex + 1) % maxFramesInFlight`
# inside an instance method, with `static let maxFramesInFlight` on the same
# class. swiftc: `static member 'maxFramesInFlight' cannot be used on instance
# of type 'HalluRenderer'`. Retry 1, synthesis AND the repair round all made
# it, and run 46's retry 1 had made it on the identical line — three Mac
# round trips for a rule decidable from the text.
#
# Conservative by design (a false positive rejects code swiftc would accept):
#   * only `static let` / `static var` declared DIRECTLY in a type body count;
#   * only bare uses inside a `func`/`init` that is not itself `static`/`class`
#     in that same type body;
#   * a use is skipped when the name is preceded by `.`, `\.` or `#`, followed
#     by `:` (an argument label), or is any kind of declaration or binding;
#   * the whole function is skipped for that name when any token in its
#     signature or body could be a binding of the name (`let`/`var`/`for`/
#     `case`/`catch` + name, a `name in` closure parameter, or the name
#     appearing anywhere between `func` and its `{`).
_STATIC_MEMBER_DECL = {"let", "var"}
_BINDING_KEYWORDS = {"let", "var", "for", "case", "catch", "func", "class",
                     "struct", "enum", "actor", "protocol", "typealias",
                     "associatedtype", "import"}
_FUNC_KEYWORDS = {"func", "init"}


def unqualified_static_member_references(
    files: list[tuple[str, str]],
) -> list[Violation]:
    """Bare `NAME` inside an instance method where `NAME` is a `static`
    stored property of the enclosing type — one Violation per use."""
    violations: list[Violation] = []
    for path, content in files:
        if not path.endswith(".swift"):
            continue
        if _CONDITIONAL_COMPILATION_RE.search(content):
            continue  # same exemption as the other detectors
        blanked = blank_comments_and_strings(content)
        toks = list(_TOKEN_RE.finditer(blanked))
        words = [t.group(0) for t in toks]

        # Pass 1: scope tree. Each `{` gets a scope id; record its kind and
        # parent, and collect `static let/var NAME` directly inside type
        # scopes.
        kind_of: dict[int, str] = {}     # scope id (token index of '{') -> kind
        parent_of: dict[int, int | None] = {}
        statics: dict[int, set] = {}     # type scope id -> static names
        stack: list[int] = []
        pending: str | None = None
        for i, w in enumerate(words):
            if w == "{":
                kind_of[i] = pending or "other"
                parent_of[i] = stack[-1] if stack else None
                stack.append(i)
                pending = None
            elif w == "}":
                if stack:
                    stack.pop()
                pending = None
            elif w in _TYPE_SCOPE_KEYWORDS:
                pending = "type"
            elif w in _FUNC_KEYWORDS:
                # `static func` / `class func` -> a static context.
                prev = words[i - 1] if i > 0 else ""
                pending = "static_func" if prev in ("static", "class") else "func"
            elif (w == "static" and i + 2 < len(words)
                  and words[i + 1] in _STATIC_MEMBER_DECL
                  and stack and kind_of.get(stack[-1]) == "type"):
                name = words[i + 2]
                if name not in ("{", "}"):
                    statics.setdefault(stack[-1], set()).add(name)
        if not statics:
            continue

        # Pass 2: walk again, tracking the enclosing type scope and the
        # enclosing func scope, and flag bare uses.
        stack = []
        pending = None
        for i, w in enumerate(words):
            if w == "{":
                stack.append(i)
                pending = None
                continue
            if w == "}":
                if stack:
                    stack.pop()
                pending = None
                continue
            # Find the innermost enclosing func scope and its type parent.
            func_scope = next((sid for sid in reversed(stack)
                               if kind_of.get(sid) in ("func", "static_func")), None)
            if func_scope is None or kind_of[func_scope] != "func":
                continue
            type_scope = parent_of.get(func_scope)
            # A func directly inside a type body; nested closures/blocks are
            # deeper scopes whose ancestor chain still reaches this func.
            while type_scope is not None and kind_of.get(type_scope) != "type":
                type_scope = parent_of.get(type_scope)
            names = statics.get(type_scope, set()) if type_scope is not None else set()
            if w not in names:
                continue
            tok = toks[i]
            before = blanked[max(0, tok.start() - 2):tok.start()]
            after = blanked[tok.end():tok.end() + 1]
            if before.endswith(".") or before.endswith("#") or before.endswith("\\"):
                continue
            if after == ":":
                continue  # argument label or dictionary key
            if i > 0 and words[i - 1] in _BINDING_KEYWORDS:
                continue  # a declaration of the name, not a use
            if i + 1 < len(words) and words[i + 1] == "in":
                continue  # a closure parameter `{ name in ... }`
            # Shadowing: scan the enclosing function's signature + body.
            fs = func_scope
            sig_start = None
            for k in range(fs - 1, -1, -1):
                if words[k] in _FUNC_KEYWORDS:
                    sig_start = k
                    break
                if words[k] in ("{", "}"):
                    break
            depth = 0
            body_end = len(words)
            for k in range(fs, len(words)):
                if words[k] == "{":
                    depth += 1
                elif words[k] == "}":
                    depth -= 1
                    if depth == 0:
                        body_end = k
                        break
            shadowed = False
            lo = sig_start if sig_start is not None else fs
            for k in range(lo, body_end):
                if k == i:
                    continue
                if words[k] != w:
                    continue
                if k < fs:                      # anywhere in the signature
                    shadowed = True
                    break
                if k > 0 and words[k - 1] in _BINDING_KEYWORDS:
                    shadowed = True
                    break
                if k + 1 < len(words) and words[k + 1] == "in":
                    shadowed = True
                    break
            if shadowed:
                continue
            violations.append(Violation(
                kind="unqualified_static_member",
                message=(f"static member '{w}' cannot be used on instance "
                         f"(write Self.{w})"),
                path=path, line=_line_of(blanked, tok.start()), notes=()))
    return violations



# ── DEV-777 (v0.4.0 phase A1): the runs-44–49 classes ────────────────────────
#
# Each detector below is named after the swiftc diagnostic it pre-empts and
# the run that paid a Mac round trip for it. Same stance as the originals: a
# false negative costs nothing new, a false positive rejects code swiftc would
# accept, so every rule is written to stay silent when unsure. All were
# measured against the spec archive before shipping (scripts/
# sweep_swift_prechecks.py); the numbers are on DEV-777.

_TEST_FUNC_NAME_RE = re.compile(r"^test")
_TEST_ATTR_RE = re.compile(r"@Test\b")
_MAIN_ACTOR_ATTR_RE = re.compile(r"@MainActor\b")


def _attribute_slice(blanked: str, tok_start: int) -> str:
    """The raw text between the previous `{`/`}`/`;` (or the start of the
    previous declaration) and *tok_start* — where attributes and modifiers
    for the declaration at *tok_start* live."""
    lo = max(blanked.rfind("{", 0, tok_start), blanked.rfind("}", 0, tok_start),
             blanked.rfind(";", 0, tok_start))
    # Attributes precede modifiers; a previous `func`'s body brace bounds them.
    return blanked[lo + 1:tok_start]


def _func_scopes(blanked: str, words: list[str], toks: list) -> list[tuple[int, int, int]]:
    """(func_token_index, body_open_index, body_close_index) for every
    `func` whose body brace is found; closes at the matching `}` or the
    end of the token stream."""
    out: list[tuple[int, int, int]] = []
    for i, w in enumerate(words):
        if w != "func":
            continue
        # The body `{` is the first brace after the signature's parens —
        # any `{` before a `(`‑balanced `)` would be in a default value
        # closure, which we treat as "unsure": skip such a func.
        depth = 0
        body_open = None
        # walk raw text from the func token to find the first `{` at paren depth 0
        k = toks[i].end()
        n = len(blanked)
        while k < n:
            c = blanked[k]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            elif c == "{" and depth == 0:
                break
            elif c == "}" and depth == 0:
                k = None
                break
            k += 1
        if k is None or k >= n:
            continue
        # translate raw offset to the token index of that `{`
        for t_idx in range(i + 1, len(words)):
            if toks[t_idx].start() == k:
                body_open = t_idx
                break
        if body_open is None:
            continue
        d = 0
        body_close = len(words)
        for t_idx in range(body_open, len(words)):
            if words[t_idx] == "{":
                d += 1
            elif words[t_idx] == "}":
                d -= 1
                if d == 0:
                    body_close = t_idx
                    break
        out.append((i, body_open, body_close))
    return out



def _is_test_func(blanked: str, words: list[str], toks: list, fi: int) -> bool:
    name = words[fi + 1] if fi + 1 < len(words) else ""
    if _TEST_FUNC_NAME_RE.match(name):
        return True
    return bool(_TEST_ATTR_RE.search(_attribute_slice(blanked, toks[fi].start())))


def missing_throws_on_test_functions(
    files: list[tuple[str, str]],
) -> list[Violation]:
    """A `@Test` / `func test…` whose body uses `try` at body level with no
    `throws` on the signature and no `do` anywhere in the body.

    Run 48 (spec_a8b7c3e5): six `@Test func` bodies each `try #require(...)`
    with no `throws` — twelve `errors thrown from here are not handled`
    diagnostics, every one mechanical. One Violation per function, at the
    first offending `try`. A `try` inside a nested `{ … }` (a closure that
    may itself throw) does not count; `try?` / `try!` never count.
    """
    violations: list[Violation] = []
    for path, content in files:
        if not path.endswith(".swift"):
            continue
        blanked = blank_comments_and_strings(content)
        toks = list(_TOKEN_RE.finditer(blanked))
        words = [t.group(0) for t in toks]
        for fi, bo, bc in _func_scopes(blanked, words, toks):
            if not _is_test_func(blanked, words, toks, fi):
                continue
            sig = words[fi:bo]
            if "throws" in sig or "rethrows" in sig:
                continue
            body = words[bo + 1:bc]
            if "do" in body:
                continue
            depth = 0
            for k in range(bo + 1, bc):
                w = words[k]
                if w == "{":
                    depth += 1
                elif w == "}":
                    depth -= 1
                elif w == "try" and depth == 0:
                    after = blanked[toks[k].end():toks[k].end() + 1]
                    if after in ("?", "!"):
                        continue
                    violations.append(Violation(
                        kind="missing_throws_on_test",
                        message=(f"errors thrown from here are not handled "
                                 f"(add 'throws' to func {words[fi + 1]})"),
                        path=path, line=_line_of(blanked, toks[k].start()),
                        notes=()))
                    break
    return violations


_REQUIRE_RE = re.compile(r"#require\s*\(")


def require_without_try(files: list[tuple[str, str]]) -> list[Violation]:
    """A `#require(...)` not preceded by `try` / `try?` / `try!` (DEV-791).

    `#require` is a throwing macro; without `try` Swift 6 reports
    `call can throw but is not marked with 'try'` at a `macro expansion`
    pseudo-location that nothing downstream could locate. Run 52's retry 3
    lost the attempt to it. One Violation per offending `#require`, at the
    compiler's message, so the retry sees the same words the Mac would print.
    Comments and strings are blanked first, so a `#require` in prose does not
    count.
    """
    violations: list[Violation] = []
    for path, content in files:
        if not path.endswith(".swift"):
            continue
        blanked = blank_comments_and_strings(content)
        for m in _REQUIRE_RE.finditer(blanked):
            before = blanked[:m.start()].rstrip()
            if re.search(r"\btry[?!]?$", before):
                continue
            violations.append(Violation(
                kind="require_without_try",
                message=("call can throw but is not marked with 'try' "
                         "(write `try #require(...)`)"),
                path=path, line=_line_of(blanked, m.start()), notes=()))
    return violations


# DEV-784: closures the code hands to these APIs run as nonisolated
# synchronous contexts. Under SWIFT_DEFAULT_ACTOR_ISOLATION=MainActor every
# unannotated type is isolated, so a bare instance-method call inside one is
# `call to main actor-isolated instance method … in a synchronous nonisolated
# context` — with no `@MainActor` anywhere in the text to warn the model.
_NONISOLATED_CLOSURE_RE = re.compile(
    r"(?:\.sink\s*\{"
    r"|addObserver\s*\((?:[^{}]|\n)*?\)\s*\{"
    r"|DispatchQueue[^{\n]*?\.async(?:After)?\s*(?:\([^{}\n]*\))?\s*\{"
    r"|Timer\.scheduledTimer\s*\((?:[^{}]|\n)*?\)\s*\{)")
_HOP_RE = re.compile(r"\bTask\s*(?:\.detached\s*)?(?:\([^{}]*\))?\s*\{|MainActor\.(?:assumeIsolated|run)\b|\bawait\b")
_INSTANCE_FUNC_RE = re.compile(
    r"^[ \t]+(?:@\w+(?:\([^)]*\))?[ \t]+)*(?!static\b|class\b|nonisolated\b)"
    r"(?:(?:public|internal|fileprivate|private|open|final|override|mutating)[ \t]+)*"
    r"func[ \t]+([A-Za-z_]\w*)\s*\(", re.MULTILINE)


def nonisolated_closure_calls_isolated_method(
    files: list[tuple[str, str]], default_isolation: "str | None",
) -> list[Violation]:
    """Under a default-MainActor target, an instance method of the same file
    called synchronously inside a NotificationCenter / Combine sink /
    DispatchQueue / Timer closure with no `Task { @MainActor in … }` or
    `MainActor.assumeIsolated` hop (DEV-784, run 50's repair round).

    Conservative on purpose: app-target files only (no `Tests/` component),
    the closure must be introduced by one of the four APIs above, the callee
    must be an instance `func` declared in the same file, and any hop anywhere
    in the closure body clears it. One Violation per offending closure.
    """
    if not default_isolation or default_isolation.strip().lower() != "mainactor":
        return []
    violations: list[Violation] = []
    for path, content in files:
        if not path.endswith(".swift") or "Tests/" in path.replace("\\", "/"):
            continue
        blanked = blank_comments_and_strings(content)
        methods = set(_INSTANCE_FUNC_RE.findall(blanked))
        if not methods:
            continue
        call_re = re.compile(r"(?:self\??\.)?\b(" + "|".join(map(re.escape, sorted(methods))) + r")\s*\(")
        for m in _NONISOLATED_CLOSURE_RE.finditer(blanked):
            start = m.end() - 1          # the `{`
            depth = 0; end = None
            for i in range(start, len(blanked)):
                ch = blanked[i]
                if ch == "{": depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i; break
            if end is None:
                continue
            body = blanked[start:end + 1]
            if _HOP_RE.search(body):
                continue
            hit = None
            for cm in call_re.finditer(body):
                before = body[max(0, cm.start() - 5):cm.start()]
                if before.endswith("func "):
                    continue
                hit = cm; break
            if hit is None:
                continue
            name = hit.group(1)
            violations.append(Violation(
                kind="nonisolated_closure_isolated_call",
                message=(f"call to main actor-isolated instance method '{name}()' in a "
                         f"synchronous nonisolated context (the type is isolated by "
                         f"SWIFT_DEFAULT_ACTOR_ISOLATION=MainActor; wrap the closure body "
                         f"in Task {{ @MainActor in ... }})"),
                path=path, line=_line_of(blanked, start + hit.start()), notes=()))
    return violations


# Column-0 struct/class declaration with its inheritance clause up to `{`.
_TYPE_WITH_CLAUSE_RE = re.compile(
    r"^(?:@[A-Za-z_]\w*(?:\s*\([^)]*\))?[ \t]+)*"
    r"(?:(?:public|internal|fileprivate|private|final|open)[ \t]+)*"
    r"(struct|class)[ \t]+([A-Za-z_]\w*)([^{]*)\{", re.MULTILINE)
_EXTENSION_CLAUSE_RE = re.compile(
    r"^(?:(?:public|internal|fileprivate|private)[ \t]+)?"
    r"extension[ \t]+([A-Za-z_]\w*)([^{]*)\{", re.MULTILINE)


def missing_hashable_conformance(
    files: list[tuple[str, str]],
) -> list[Violation]:
    """`Set<T>` or a `[T: …]` dictionary type where `T` is a struct/class
    declared in the emitted set with no `Hashable` in its inheritance clause
    or in any emitted `extension T: …`.

    Run 48 (spec_a8b7c3e5): `public struct RGBA: Equatable, Sendable` and a
    `Set<RGBA>` in the tests — `generic struct 'Set' requires that 'RGBA'
    conform to 'Hashable'`, twice. Enums are skipped (an enum without
    associated values is Hashable for free and telling the cases apart is
    not worth a false positive). One Violation per (type, file).
    """
    decls: dict[str, tuple[str, int, str]] = {}   # name -> (path, line, kind)
    hashable: set[str] = set()
    blanked_by_path: dict[str, str] = {}
    for path, content in files:
        if not path.endswith(".swift"):
            continue
        b = blank_comments_and_strings(content)
        blanked_by_path[path] = b
        for m in _TYPE_WITH_CLAUSE_RE.finditer(b):
            kind, name, clause = m.group(1), m.group(2), m.group(3)
            decls.setdefault(name, (path, _line_of(b, m.start()), kind))
            if "Hashable" in clause:
                hashable.add(name)
        for m in _EXTENSION_CLAUSE_RE.finditer(b):
            if "Hashable" in m.group(2):
                hashable.add(m.group(1))
    candidates = {n for n in decls if n not in hashable}
    if not candidates:
        return []
    # `Set(Palette.mushroom)` — run 48's actual shape: the element type is
    # only visible through the declaration `static let mushroom: [RGBA]`.
    array_members: dict[str, str] = {}
    for b in blanked_by_path.values():
        for m in re.finditer(r"\b(?:let|var)[ \t]+([A-Za-z_]\w*)[ \t]*:[ \t]*\[([A-Za-z_]\w*)\]", b):
            array_members.setdefault(m.group(1), m.group(2))
    use_re = re.compile(
        r"\bSet<([A-Za-z_]\w*)>"
        r"|\[([A-Za-z_]\w*)[ \t]*:[ \t]*[A-Za-z_\[]"
        r"|\bSet\(\s*(?:[A-Za-z_]\w*\.)?([A-Za-z_]\w*)\s*\)")
    violations: list[Violation] = []
    for path, b in blanked_by_path.items():
        seen: set[str] = set()
        for m in use_re.finditer(b):
            if m.group(3):
                name = array_members.get(m.group(3), "")
            else:
                name = m.group(1) or m.group(2)
            if name not in candidates or name in seen:
                continue
            seen.add(name)
            dpath, dline, kind = decls[name]
            container = "Dictionary" if m.group(2) else "Set"
            violations.append(Violation(
                kind="missing_hashable_conformance",
                message=(f"generic struct '{container}' requires that '{name}' "
                         f"conform to 'Hashable' (add Hashable to the {kind} "
                         f"declaration)"),
                path=path, line=_line_of(b, m.start()),
                notes=((dpath, dline),)))
    return violations


# (callee, argument label) -> the non-optional SDK type swiftc will name.
# Run 49 retry 1 (spec_4baf2650): `device.makeBuffer(bytes:length:options: nil)`
# — `'nil' is not compatible with expected argument type 'MTLResourceOptions'`.
# Deliberately tiny: an entry is added only after a run has paid for it.
_KNOWN_NON_OPTIONAL_ARGS: dict[tuple[str, str], str] = {
    ("makeBuffer", "options"): "MTLResourceOptions",
    ("makeTexture", "descriptor"): "MTLTextureDescriptor",
}
_NIL_ARG_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)")
_LABEL_NIL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*:\s*nil\b")


def _emitted_signatures(files: list[tuple[str, str]]) -> dict[str, list[dict[str, str]]]:
    """name -> list of {label: type} for every `func name(` / `init(` in the
    emitted set (the list holds one dict per overload)."""
    sigs: dict[str, list[dict[str, str]]] = {}
    sig_re = re.compile(r"\b(?:func[ \t]+([A-Za-z_]\w*)|(init))\s*(?:<[^>]*>)?\s*\(")
    for path, content in files:
        if not path.endswith(".swift"):
            continue
        b = blank_comments_and_strings(content)
        for m in sig_re.finditer(b):
            name = m.group(1) or m.group(2)
            k = m.end()
            depth = 1
            while k < len(b) and depth:
                depth += b[k] == "("
                depth -= b[k] == ")"
                k += 1
            params = b[m.end():k - 1]
            table: dict[str, str] = {}
            for piece in _split_top_level(params):
                pm = re.match(r"\s*(?:([A-Za-z_]\w*)\s+)?([A-Za-z_]\w*)\s*:\s*([^=]+?)\s*(=.*)?$",
                              piece, re.S)
                if not pm:
                    continue
                label = pm.group(1) or pm.group(2)
                if label == "_":
                    continue
                table[label] = pm.group(3).strip() + ("=" if pm.group(4) else "")
            sigs.setdefault(name, []).append(table)
    return sigs


def _split_top_level(text: str) -> list[str]:
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for c in text:
        if c in "([<":
            depth += 1
        elif c in ")]>":
            depth -= 1
        if c == "," and depth == 0:
            out.append("".join(cur)); cur = []
        else:
            cur.append(c)
    if cur:
        out.append("".join(cur))
    return out


def nil_for_non_optional_argument(
    files: list[tuple[str, str]],
) -> list[Violation]:
    """`label: nil` passed to a parameter that cannot take it: a known SDK
    entry in `_KNOWN_NON_OPTIONAL_ARGS`, or an emitted signature with exactly
    one overload whose `label:` type carries no `?`/`!`, no `Optional<`,
    and no default value."""
    sigs = _emitted_signatures(files)
    violations: list[Violation] = []
    for path, content in files:
        if not path.endswith(".swift"):
            continue
        b = blank_comments_and_strings(content)
        for m in _NIL_ARG_RE.finditer(b):
            callee, args = m.group(1), m.group(2)
            if "nil" not in args:
                continue
            for lm in _LABEL_NIL_RE.finditer(args):
                label = lm.group(1)
                expected = _KNOWN_NON_OPTIONAL_ARGS.get((callee, label))
                if expected is None:
                    overloads = sigs.get(callee, [])
                    if len(overloads) != 1 or label not in overloads[0]:
                        continue
                    t = overloads[0][label]
                    if t.endswith(("?", "!", "=")) or t.startswith("Optional<") or t == "Any":
                        continue
                    expected = t
                violations.append(Violation(
                    kind="nil_for_non_optional_argument",
                    message=(f"'nil' is not compatible with expected argument "
                             f"type '{expected}' (parameter '{label}' of "
                             f"{callee} is not optional)"),
                    path=path, line=_line_of(b, m.start() + lm.start()),
                    notes=()))
    return violations


def main_actor_types_called_from_nonisolated_tests(
    files: list[tuple[str, str]],
) -> list[Violation]:
    """A type declared `@MainActor` in the emitted set, constructed inside a
    test function that is neither `@MainActor` (itself or via its enclosing
    type) nor `async`.

    Runs 44 and 47 (DEV-753): `@MainActor final class AudioLifecycle` and
    `final class AudioLifecycleTests: XCTestCase { func testX() { let lc =
    AudioLifecycle() … } }` — `call to main actor-isolated initializer
    'init()' in a synchronous nonisolated context`, on every test. One
    Violation per test function, at the first construction.
    """
    isolated: dict[str, tuple[str, int]] = {}
    blanked_by_path: dict[str, str] = {}
    for path, content in files:
        if not path.endswith(".swift"):
            continue
        b = blank_comments_and_strings(content)
        blanked_by_path[path] = b
        for m in _TYPE_WITH_CLAUSE_RE.finditer(b):
            attrs = _attribute_slice(b, m.start()) + m.group(0)[:m.group(0).find(m.group(1))]
            if _MAIN_ACTOR_ATTR_RE.search(attrs):
                isolated.setdefault(m.group(2), (path, _line_of(b, m.start())))
    if not isolated:
        return []
    ctor_re = re.compile(r"\b(" + "|".join(map(re.escape, isolated)) + r")\s*\(")
    violations: list[Violation] = []
    for path, b in blanked_by_path.items():
        toks = list(_TOKEN_RE.finditer(b))
        words = [t.group(0) for t in toks]
        # Enclosing type annotated @MainActor => every method is isolated.
        type_scopes_isolated: dict[int, bool] = {}
        stack: list[int] = []
        pending_isolated: bool | None = None
        for i, w in enumerate(words):
            if w == "{":
                stack.append(i)
                type_scopes_isolated[i] = bool(pending_isolated)
                pending_isolated = None
            elif w == "}":
                if stack:
                    stack.pop()
                pending_isolated = None
            elif w in _TYPE_SCOPE_KEYWORDS:
                pending_isolated = bool(_MAIN_ACTOR_ATTR_RE.search(
                    _attribute_slice(b, toks[i].start())))
        for fi, bo, bc in _func_scopes(b, words, toks):
            if not _is_test_func(b, words, toks, fi):
                continue
            sig = words[fi:bo]
            if "async" in sig:
                continue
            if _MAIN_ACTOR_ATTR_RE.search(_attribute_slice(b, toks[fi].start())):
                continue
            # any enclosing scope annotated @MainActor?
            enclosing = [sid for sid in type_scopes_isolated
                         if toks[sid].start() < toks[fi].start()
                         and _scope_contains(words, sid, fi)]
            if any(type_scopes_isolated[sid] for sid in enclosing):
                continue
            body_text = b[toks[bo].start():toks[bc].start() if bc < len(toks) else len(b)]
            cm = ctor_re.search(body_text)
            if not cm:
                continue
            name = cm.group(1)
            dpath, dline = isolated[name]
            violations.append(Violation(
                kind="main_actor_call_from_nonisolated_test",
                message=(f"call to main actor-isolated initializer 'init' of "
                         f"'{name}' in a synchronous nonisolated context "
                         f"(mark func {words[fi + 1]} @MainActor and async, "
                         f"or the enclosing test type @MainActor)"),
                path=path,
                line=_line_of(b, toks[bo].start() + cm.start()),
                notes=((dpath, dline),)))
    return violations


def _scope_contains(words: list[str], open_idx: int, tok_idx: int) -> bool:
    depth = 0
    for k in range(open_idx, len(words)):
        if words[k] == "{":
            depth += 1
        elif words[k] == "}":
            depth -= 1
            if depth == 0:
                return open_idx < tok_idx < k
    return open_idx < tok_idx


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
    default_isolation: "str | None" = None,
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
    violations += unqualified_static_member_references(generated_files)
    # DEV-777: the runs-44–49 classes.
    violations += missing_throws_on_test_functions(generated_files)
    violations += require_without_try(generated_files)   # DEV-791
    violations += missing_hashable_conformance(generated_files)
    violations += nil_for_non_optional_argument(generated_files)
    violations += main_actor_types_called_from_nonisolated_tests(generated_files)
    # DEV-784: only when the spec's test_strategy declares the target's default.
    violations += nonisolated_closure_calls_isolated_method(generated_files, default_isolation)
    return SwiftPrecheckResult(violations=violations)
