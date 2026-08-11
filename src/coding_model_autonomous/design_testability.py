"""Mechanical testability check on the architect's design — DEV-481 fix 1.

A design can pass review, be implemented faithfully, compile completely, and
still fail because the API it specifies makes its own acceptance checklist
impossible to test. Run 4 of DEV-102 died on three of those at once; run 6 died
on one, `Mushroom` needing `Equatable` for the criterion "same seed produces an
identical mushroom field", with every source file compiled.

DEV-481 shipped the prompt half of the fix first — architect rule 10 and the
design-review AFFORDANCE check. Run 6's design complied with most of rule 10
(a settable `chains`, a test-visible initialiser, a named `PLAYER_ZONE_START_ROW`)
and still stranded a criterion, because rule 10's conformance bullet is written
about *enums* and run 6's gap was a struct reached through `Dictionary`'s
conditional `Equatable`. A rule the model must notice is not the same as a check
that runs.

So this module is the mechanical half. It reads the design's own
`## Criterion Seams` section — the architect naming, per checklist item, the API
a test would use to set up, act and assert — and verifies the seams actually
resolve against the API the same document specifies.

DESIGN PRINCIPLE: **fail open.** Every rule here returns nothing when it cannot
resolve something confidently. A false rejection costs an architect revision out
of a budget of one (DEV-440), which is worse than a missed defect — the human
gate and the design review both still sit downstream. Each rule below is narrow
and cites the run that motivates it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Rules are keyed so callers (and tests) can assert on kind rather than prose.
KIND_NO_SECTION = "no_seams_section"
KIND_COUNT_MISMATCH = "seam_count_mismatch"
KIND_INCOMPLETE_SEAM = "incomplete_seam"
KIND_UNRESOLVED_SYMBOL = "unresolved_symbol"
KIND_MISSING_EQUATABLE = "missing_equatable"
KIND_UNTYPED_COMPARISON = "untyped_comparison"
KIND_READONLY_SETUP = "readonly_setup"
# DEV-509: the design's named types and its allocated files must agree.
KIND_TYPE_WITHOUT_FILE = "type_without_file"
KIND_FILE_WITHOUT_TYPE = "file_without_type"
KIND_DUPLICATE_TYPE = "duplicate_type_declaration"
# DEV-523: a seam step that names no call is not a seam. Every other rule in
# this module reads backticked spans, so a step written as English prose is
# not merely unchecked — it makes all of them unreachable.
KIND_PROSE_SEAM = "prose_seam"
KIND_ELIDED_STEP = "elided_step"
# DEV-525: a collection of tuples on a type declaring Equatable. Unlike the
# rules above this needs no seam — it is wrong in the declaration itself.
KIND_TUPLE_CONFORMANCE = "tuple_conformance"

FILE_STRUCTURE_HEADING = "File Structure"

SEAMS_HEADING = "Criterion Seams"
CHECKLIST_HEADING = "Acceptance Criteria Checklist"
DATA_MODELS_HEADING = "Data Models"

_LABELS = ("setup", "act", "assert")


@dataclass(frozen=True)
class Finding:
    """One stranded criterion. `detail` is written to be pasted to the architect."""
    kind: str
    criterion: str
    detail: str


@dataclass(frozen=True)
class Seam:
    criterion: str
    setup: str
    act: str
    assert_: str

    def missing(self) -> list[str]:
        return [name for name, val in
                (("setup", self.setup), ("act", self.act),
                 ("assert", self.assert_)) if not val.strip()]


# ── parsing ──────────────────────────────────────────────────────────────────

def _section(design_md: str, heading: str) -> str:
    """Body of a `## heading` section, to the next heading of the same or
    higher level. Returns "" when absent — every caller treats that as
    'cannot check', not 'defect'."""
    pattern = re.compile(
        r"^(#{1,6})\s*" + re.escape(heading) + r"\s*$", re.MULTILINE | re.IGNORECASE)
    m = pattern.search(design_md)
    if not m:
        return ""
    level = len(m.group(1))
    rest = design_md[m.end():]
    nxt = re.search(r"^#{1," + str(level) + r"}\s+\S", rest, re.MULTILINE)
    return rest[:nxt.start()] if nxt else rest


def _bullets(body: str) -> list[str]:
    """Top-level `- `/`* ` bullets, each joined with its indented continuations.

    The architect may emit a seam as one line or as a bullet with a nested
    list; both collapse to the same string here so the label scan below does
    not care which it got.
    """
    out: list[str] = []
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        # Indent decides parent vs child. An indented `- ` is a NESTED bullet
        # and belongs to the entry above it, not beside it — otherwise a seam
        # written as a bullet with three sub-bullets parses as four seams and
        # the count check fires on a design that did nothing wrong.
        if indent <= 1 and re.match(r"^[-*]\s+", line.lstrip()):
            out.append(line.strip())
        elif out:
            out[-1] += " " + line.strip().lstrip("-*").strip()
    return out


def parse_checklist(design_md: str) -> list[str]:
    """Checklist items, checkbox markers and emphasis stripped."""
    items = []
    for b in _bullets(_section(design_md, CHECKLIST_HEADING)):
        text = re.sub(r"^[-*]\s*", "", b)
        text = re.sub(r"^\[[ xX]?\]\s*", "", text)
        if text.strip():
            items.append(text.strip())
    return items


def parse_seams(design_md: str) -> list[Seam]:
    """Seam entries, matched to criteria BY POSITION.

    Position, not text similarity, is deliberate: fuzzy matching a seam to a
    criterion invents disagreements, and a disagreement here costs a revision
    (DEV-440). A count mismatch is reported once and plainly instead.
    """
    body = _section(design_md, SEAMS_HEADING)
    if not body.strip():
        return []
    seams: list[Seam] = []
    for entry in _bullets(body):
        entry = re.sub(r"^[-*]\s*", "", entry)
        # Split on the labels wherever they appear, in any order.
        parts = re.split(r"(?i)\b(" + "|".join(_LABELS) + r")\s*[:=]", entry)
        head = parts[0].strip(" |—-\t")
        found = {}
        for i in range(1, len(parts) - 1, 2):
            found[parts[i].lower()] = parts[i + 1].strip(" |—-\t")
        if not found:
            continue
        seams.append(Seam(criterion=_clean_criterion(head),
                          setup=found.get("setup", ""),
                          act=found.get("act", ""),
                          assert_=found.get("assert", "")))
    return seams


def _clean_criterion(text: str) -> str:
    text = re.sub(r"^\[[ xX]?\]\s*", "", text)
    return text.strip(" *_`").strip()


def _code_spans(text: str) -> list[str]:
    """Backticked spans — the only part of a seam treated as code."""
    return re.findall(r"`([^`]+)`", text)


# A span that stands in for a call rather than naming one: a bare ellipsis, or
# an assignment whose right-hand side is elided (`let snapshot = ...`). Run 7's
# criterion 15 was the latter — it carries a span, so requiring a span alone
# does not reach it. `world.step(...)` is unaffected: no `=`, not bare.
_PLACEHOLDER_SPAN_RE = re.compile(r"^(?:\.{3}|…)$|=\s*(?:\.{3}|…)\s*$")


def _is_placeholder_span(span: str) -> bool:
    return bool(_PLACEHOLDER_SPAN_RE.search(span.strip()))


# ── the design's own vocabulary ──────────────────────────────────────────────

def declared_types(design_md: str) -> set[str]:
    """Type names the design declares in Data Models.

    Two spellings, because architects use both and reading only one produces
    false findings: a bullet list (``- `Position`: {col, row}``) and a fenced
    code block (``struct Position { ... }``). Run 7's design used the code
    block exclusively, and reading bullets alone reported four types as
    undeclared that were declared perfectly well — precisely the false
    rejection DEV-440 warns about.
    """
    body = _section(design_md, DATA_MODELS_HEADING)
    types = set()
    for b in _bullets(body):
        m = re.match(r"^[-*]\s*`?([A-Z]\w*)`?\s*[:—-]", b)
        if m:
            types.add(m.group(1))
    for m in re.finditer(
            r"\b(?:struct|class|enum|protocol|actor|typealias|interface)\s+"
            r"([A-Z]\w*)", body):
        types.add(m.group(1))
    return types


def top_level_types(design_md: str) -> set[str]:
    """Declared types that need a file of their own.

    A NESTED declaration lives in its enclosing type's file and must never be
    reported as unallocated. Run 7's design v4 nested `MushroomEntry` inside
    `WorldSnapshot` — correctly, and at my own suggestion — and a rule that
    ignored nesting flagged it immediately. That is the DEV-440 false rejection
    arriving on the first live design it ever saw.

    Indentation is the signal: a declaration indented inside a fenced block is
    inside something else.
    """
    body = _section(design_md, DATA_MODELS_HEADING)
    decl = re.compile(
        r"^(\s*)(?:struct|class|enum|protocol|actor|typealias|interface)\s+"
        r"([A-Z]\w*)")
    top = set()
    for line in body.splitlines():
        m = decl.match(line)
        if m and not m.group(1):
            top.add(m.group(2))
    for b in _bullets(body):
        m = re.match(r"^[-*]\s*`?([A-Z]\w*)`?\s*[:—-]", b)
        if m:
            top.add(m.group(1))
    return top


def top_level_type_counts(design_md: str) -> dict[str, int]:
    """How many times Data Models declares each top-level type — DEV-554.

    `top_level_types` returns a set, so a type declared three times is
    indistinguishable from one declared once. Run 11's design 4 declared
    `HitResult` three times in a row with the architect's own working notes
    between the versions ("Actually simpler… No, criterion says… Let's just
    track damage"), and its Criterion Seams then referenced cases from two of
    the three mutually exclusive definitions.

    Only fenced declarations are counted. The bullet spelling is prose and an
    architect legitimately mentions a type in several bullets; counting those
    would fire on ordinary writing.
    """
    body = _section(design_md, DATA_MODELS_HEADING)
    decl = re.compile(
        r"^(\s*)(?:struct|class|enum|protocol|actor|typealias|interface)\s+"
        r"([A-Z]\w*)")
    counts: dict[str, int] = {}
    for line in body.splitlines():
        m = decl.match(line)
        if m and not m.group(1):
            counts[m.group(2)] = counts.get(m.group(2), 0) + 1
    return counts


def declared_members(design_md: str) -> dict[str, str]:
    """member name → the declared type text on its right-hand side.

    Built from every `name: Type` pair in Data Models, which is how this
    codebase's designs consistently spell members. Used only to answer "what
    type does `.cells` hold" for the Equatable rule.
    """
    members: dict[str, str] = {}
    body = _section(design_md, DATA_MODELS_HEADING)
    for name, typ in re.findall(r"\b([a-z]\w*)\s*:\s*([^,;)}`\n]+)", body):
        members.setdefault(name, typ.strip())
    return members


def _declares_equatable(design_md: str, type_name: str) -> bool:
    """True when any line names both the type and Equatable/Hashable.

    Hashable counts: it refines Equatable, so a design asking for Hashable has
    supplied `==` too. Deliberately loose — this gates whether we STAY SILENT,
    so loose means fewer false rejections.
    """
    for line in design_md.splitlines():
        if re.search(r"\b" + re.escape(type_name) + r"\b", line) and \
                re.search(r"\b(Equatable|Hashable)\b", line):
            return True
    return False


# DEV-525: a collection whose element is a tuple. Swift tuples conform to no
# protocol, so such a property can never be Equatable and the enclosing type
# can never synthesise `==`. `->` excluded so an array of closures — a
# different (and rarer) problem — does not land in this message.
_TUPLE_COLLECTION_RE = re.compile(r"\[[^\]]*\([^)]*,[^)]*\)[^\]]*\]")


def _tuple_collection_members(design_md: str) -> list[tuple[str, str, str]]:
    """(enclosing type, member, declared type) for the one shape always wrong.

    Scoped to members of a type that declares Equatable/Hashable, because that
    is where the conformance cannot be synthesised and therefore where the
    design is definitely broken. A tuple collection on a type nobody compares
    is legal Swift and must stay silent (DEV-440).

    Indentation gives the nesting: run 7 v4 nested `MushroomEntry` inside
    `WorldSnapshot`, and both declared Equatable, so the enclosing declaration
    is whichever one is still open at the member's indent.
    """
    body = _section(design_md, DATA_MODELS_HEADING)
    decl_re = re.compile(
        r"^(\s*)(?:struct|class|enum|actor)\s+([A-Z]\w*)([^{]*)")
    member_re = re.compile(r"^(\s*)(?:let|var)\s+([a-z]\w*)\s*:\s*(.+?)\s*$")

    out: list[tuple[str, str, str]] = []
    stack: list[tuple[int, str, bool]] = []  # (indent, name, declares_eq)
    for line in body.splitlines():
        if m := decl_re.match(line):
            indent = len(m.group(1))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, m.group(2),
                          bool(re.search(r"\b(Equatable|Hashable)\b", m.group(3)))))
            continue
        if m := member_re.match(line):
            indent = len(m.group(1))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            if not stack or not stack[-1][2]:
                continue  # no enclosing type, or it claims no conformance
            typ = m.group(3).rstrip(",")
            hit = _TUPLE_COLLECTION_RE.search(typ)
            if hit and "->" not in hit.group(0):
                out.append((stack[-1][1], m.group(2), typ))
    return out


def _readonly_members(design_md: str) -> set[str]:
    """Members the design declares as not directly writable."""
    out = set()
    body = _section(design_md, DATA_MODELS_HEADING)
    for b in _bullets(body):
        m = re.search(r"`?\b([a-z]\w*)\b`?\s*:", b)
        if m and re.search(r"private\(set\)|read-only|readonly|\blet\b", b):
            out.add(m.group(1))
    return out


# ── the rules ────────────────────────────────────────────────────────────────

def _check_symbols(seam: Seam, types: set[str], design_md: str) -> list[Finding]:
    """A seam naming `Type.member` the design never declares (run 4).

    Run 4's player-zone criterion lived in prose with no symbol, so the test
    invented `Field.bottomPlayerZoneStart` and did not compile. Only dotted
    references whose BASE is a design-declared type are considered: a bare
    `world.chains` says nothing, because `world` is a local the design need
    not name.
    """
    findings = []
    for segment in (seam.setup, seam.act, seam.assert_):
        for span in _code_spans(segment):
            for base, member in re.findall(r"\b([A-Z]\w*)\.(\w+)", span):
                if base not in types:
                    continue
                # Does the member appear anywhere else in the document?
                elsewhere = re.findall(r"\b" + re.escape(member) + r"\b", design_md)
                if len(elsewhere) <= 1:
                    findings.append(Finding(
                        kind=KIND_UNRESOLVED_SYMBOL,
                        criterion=seam.criterion,
                        detail=(
                            f"the seam names `{base}.{member}`, but `{member}` is "
                            f"declared nowhere in this design. Either declare it on "
                            f"`{base}` or change the seam to a symbol that exists — "
                            f"a test cannot assert against a value that is only "
                            f"described in prose."),
                    ))
    return findings


def _compared_types(assert_text: str, types: set[str],
                    members: dict[str, str]) -> set[str]:
    """Types an assert compares for equality.

    Three narrow resolutions, each from a real failure:
      * the type is named outright in the compared span;
      * the span reads a member whose declared type mentions it — run 6's
        `w1.field.cells == w2.field.cells`, where `cells` is
        `[Position: Mushroom]` and `Dictionary` is Equatable only if its Value
        is (nothing in that expression says "Mushroom");
      * the span compares against a leading-dot enum literal `== .empty`, so
        the type is whichever declared type lists that case — run 4's
        `HitOutcome`.
    """
    spans = [s for s in _code_spans(assert_text) if re.search(r"[=!]=", s)]
    if not spans:
        # Unbackticked assert: fall back to the raw text, still requiring `==`.
        spans = [assert_text] if re.search(r"[=!]=", assert_text) else []
    hits: set[str] = set()
    for span in spans:
        for t in types:
            if re.search(r"\b" + re.escape(t) + r"\b", span):
                hits.add(t)
        # Only the LAST member of a dotted path is the compared value.
        # `w1.field.cells == w2.field.cells` compares `cells`, not `field` —
        # resolving every step flags `MushroomField`, which is merely traversed.
        for path in re.findall(r"\b\w+(?:\.\w+)+", span):
            member = path.rsplit(".", 1)[-1]
            declared = members.get(member)
            if not declared:
                continue
            for t in types:
                if re.search(r"\b" + re.escape(t) + r"\b", declared):
                    hits.add(t)
    return hits


def _enum_literal_types(assert_text: str, types: set[str],
                        design_md: str) -> set[str]:
    """Declared types that list a `.case` the assert compares against."""
    hits: set[str] = set()
    for span in _code_spans(assert_text) or [assert_text]:
        if not re.search(r"[=!]=", span):
            continue
        for case in re.findall(r"[=!]=\s*\.(\w+)", span):
            for b in _bullets(_section(design_md, DATA_MODELS_HEADING)):
                m = re.match(r"^[-*]\s*`?([A-Z]\w*)`?\s*[:—-]", b)
                if m and m.group(1) in types and \
                        re.search(r"\." + re.escape(case) + r"\b", b):
                    hits.add(m.group(1))
    return hits


def _check_equatable(seam: Seam, types: set[str], members: dict[str, str],
                     design_md: str) -> list[Finding]:
    """A criterion comparing a design-declared type that is not Equatable.

    Run 4 (`HitOutcome`) and run 6 (`Mushroom`) both died here, one word short
    each time.
    """
    if not re.search(r"[=!]=", seam.assert_):
        return []
    candidates = (_compared_types(seam.assert_, types, members)
                  | _enum_literal_types(seam.assert_, types, design_md))
    findings = []
    for t in sorted(candidates):
        if _declares_equatable(design_md, t):
            continue
        findings.append(Finding(
            kind=KIND_MISSING_EQUATABLE,
            criterion=seam.criterion,
            detail=(
                f"the assert compares values of type `{t}`, but this design "
                f"never declares `{t}` Equatable, so the comparison does not "
                f"compile. Declare the conformance on the type. Note this "
                f"applies through the standard library too: a collection of "
                f"`{t}` is only Equatable if `{t}` is."),
        ))
    return findings


def _check_untyped_comparison(seam: Seam, members: dict[str, str]) -> list[Finding]:
    """An assert comparing two whole aggregates whose type the design never states.

    This is run 6's REAL design, and it is why the Equatable rule alone was not
    enough. That design compares `w1.field.cells == w2.field.cells` but declares
    `cells` nowhere in Data Models — so nothing can resolve what is being
    compared, and nobody, human or machine, can tell whether the comparison
    compiles. It did not: `cells` is `[Position: Mushroom]` and `Mushroom` was
    not Equatable.

    Narrow on purpose. It fires only when BOTH sides are dotted paths ending in
    the SAME member, which is the shape of "these two aggregates are identical".
    `w.chains.count == 2` compares against a literal and is left alone.
    """
    m = re.search(r"([\w.]+)\s*[=!]=\s*([\w.]+)", seam.assert_)
    if not m:
        return []
    left, right = m.group(1), m.group(2)
    if "." not in left or "." not in right:
        return []
    member = left.rsplit(".", 1)[-1]
    if member != right.rsplit(".", 1)[-1] or member in members:
        return []
    return [Finding(
        kind=KIND_UNTYPED_COMPARISON,
        criterion=seam.criterion,
        detail=(
            f"the assert compares `{left}` with `{right}` in full, but this "
            f"design never declares the type of `{member}`. State it in Data "
            f"Models, and declare the conformance that comparison needs — if "
            f"`{member}` is a collection, its ELEMENT type is what must be "
            f"Equatable."),
    )]


def _check_readonly(seam: Seam, readonly: set[str]) -> list[Finding]:
    """A setup writing state the design declares read-only (run 4's `chains`).

    Run 4's criterion "chains descending past the bottom row are purged" had no
    way to place a chain near the bottom row: `chains` was `public private(set)`
    with `init(seed:)` as its only constructor.
    """
    findings = []
    for member in sorted(readonly):
        for span in _code_spans(seam.setup) or [seam.setup]:
            if re.search(r"\.\s*" + re.escape(member) +
                         r"\s*(=[^=]|\.append|\.insert|\+=)", span):
                findings.append(Finding(
                    kind=KIND_READONLY_SETUP,
                    criterion=seam.criterion,
                    detail=(
                        f"the setup writes `{member}`, which this design declares "
                        f"read-only. A criterion whose starting state cannot be "
                        f"constructed is stranded — give `{member}` a test-visible "
                        f"initialiser or an entry point that places the state."),
                ))
                break
    return findings


def _check_names_a_call(seam: Seam) -> list[Finding]:
    """DEV-523: every seam step must name a call, not describe one.

    Run 7 emitted 16 seams for 16 criteria and produced zero findings, while
    four of those criteria had no reachable setup at all. The reason is
    structural rather than a missing rule: `_check_symbols` resolves
    `Type.member` inside backticked spans, `_check_readonly` looks for an
    assignment, `_check_equatable` needs a `==`. A step written as English
    prose — "place single-segment chain at rightmost column" — offers none of
    those, so every rule fails open and the seam is recorded as complete.

    So this is not one more rule alongside the others. It is the precondition
    that makes them reachable at all.

    Deliberately narrow, because a false finding here costs an architect
    revision out of a budget shared with human rejections and DEV-468's
    routing (DEV-440). "This step contains no backticked span" is not a
    judgement call, which is what makes it safe to enforce.
    """
    prose: list[str] = []
    elided: list[str] = []
    for label, step in (("setup", seam.setup), ("act", seam.act),
                        ("assert", seam.assert_)):
        if not step.strip():
            continue  # empty steps are Seam.missing()'s business, not ours
        spans = _code_spans(step)
        if not spans:
            prose.append(label)
        elif all(_is_placeholder_span(s) for s in spans):
            elided.append(label)

    findings: list[Finding] = []
    if prose:
        findings.append(Finding(
            kind=KIND_PROSE_SEAM,
            criterion=seam.criterion,
            detail=(
                f"the {' and '.join(prose)} step describes what a test would do "
                f"but names no API to do it with. Write each step as the actual "
                f"call in backticks — `world.place(chain, at: Position(col: 29, "
                f"row: 0))` rather than \"place a chain at the rightmost "
                f"column\". If no call exists to write, that is the defect: the "
                f"criterion has no reachable setup and the API needs a seam."),
        ))
    if elided:
        findings.append(Finding(
            kind=KIND_ELIDED_STEP,
            criterion=seam.criterion,
            detail=(
                f"the {' and '.join(elided)} step is a placeholder — its only "
                f"code span elides the call with `...`. Name the real "
                f"expression. An elided step is how a criterion with no "
                f"reachable API reads as though it had one."),
        ))
    return findings


def check_design_testability(design_md: str) -> list[Finding]:
    """Findings for a design whose checklist its own API cannot carry out.

    Empty list means "nothing mechanically detectable", NOT "the design is
    testable" — the design review and the human gate remain the real checks.
    """
    # DEV-525 runs FIRST and unconditionally: this defect is in the type
    # declaration, so it holds whether or not the design has a checklist or a
    # seam section to strand. Every other rule below reasons about criteria;
    # this one reasons about Swift.
    tuple_findings = [
        Finding(
            kind=KIND_TUPLE_CONFORMANCE,
            criterion=f"{owner}.{member}",
            detail=(
                f"`{owner}` declares Equatable but `{member}: {typ}` is a "
                f"collection of TUPLES. Swift tuples conform to no protocol, "
                f"so this element can never be Equatable and `{owner}` cannot "
                f"synthesise `==` — this does not compile however it is "
                f"written. Replace the tuple with a named struct that declares "
                f"Equatable, and check every sibling property for the same "
                f"shape before re-emitting."),
        )
        for owner, member, typ in _tuple_collection_members(design_md)
    ]

    criteria = parse_checklist(design_md)
    if not criteria:
        # No checklist to strand. Nothing criterion-shaped can be assessed.
        return tuple_findings

    seams = parse_seams(design_md)
    if not seams:
        return tuple_findings + [Finding(
            kind=KIND_NO_SECTION,
            criterion="",
            detail=(
                f"this design has {len(criteria)} acceptance criteria and no "
                f"`## {SEAMS_HEADING}` section. For each criterion, name the API "
                f"a test would use to set up the state, invoke the behaviour, and "
                f"assert the outcome, in checklist order. A criterion you cannot "
                f"name a seam for is a design defect, not a test problem."),
        )]

    findings: list[Finding] = list(tuple_findings)
    if len(seams) != len(criteria):
        findings.append(Finding(
            kind=KIND_COUNT_MISMATCH,
            criterion="",
            detail=(
                f"{len(criteria)} acceptance criteria but {len(seams)} seams. "
                f"Emit exactly one seam per criterion, in checklist order."),
        ))

    types = declared_types(design_md)
    members = declared_members(design_md)
    readonly = _readonly_members(design_md)


    for i, seam in enumerate(seams):
        # Prefer the checklist's own wording when the counts line up, so the
        # architect reads back the criterion it wrote.
        labelled = seam
        if len(seams) == len(criteria):
            labelled = Seam(criterion=criteria[i], setup=seam.setup,
                            act=seam.act, assert_=seam.assert_)
        if missing := labelled.missing():
            findings.append(Finding(
                kind=KIND_INCOMPLETE_SEAM,
                criterion=labelled.criterion,
                detail=(
                    f"the seam names no {' and no '.join(missing)} step. A test "
                    f"needs all three — construct the state, invoke the "
                    f"behaviour, observe the outcome."),
            ))
        findings.extend(_check_names_a_call(labelled))
        findings.extend(_check_symbols(labelled, types, design_md))
        findings.extend(_check_equatable(labelled, types, members, design_md))
        findings.extend(_check_untyped_comparison(labelled, members))
        findings.extend(_check_readonly(labelled, readonly))
    return findings


# ── DEV-509: the type set and the file set must agree ───────────────────────

def allocated_files(design_md: str) -> set[str]:
    """Basenames the File Structure allocates, without extension.

    The manifest is generated from this section at implementer time, and
    manifest mode creates exactly the files enumerated there — so a type with
    no file here can never come into existence, whatever the design says.
    """
    body = _section(design_md, FILE_STRUCTURE_HEADING)
    return {m.group(1)
            for m in re.finditer(r"\b(\w+)\.(?:swift|py|ts|tsx|js|kt|java|go|rs)\b",
                                 body)}


def _type_references(design_md: str) -> set[str]:
    """Capitalised names used AS TYPES in the design's declarations.

    Only positions where something must already be a type: after `:` in a
    property or parameter, and after `->`. Bare prose mentions do not count —
    a design may discuss a concept it does not declare.
    """
    body = _section(design_md, DATA_MODELS_HEADING)
    refs: set[str] = set()
    for m in re.finditer(r"(?::|->)\s*\[?\[?([A-Z]\w*)", body):
        refs.add(m.group(1))
    return refs


# Types the standard library supplies. Only needs to cover what shows up in a
# design's signatures — anything missed here is silent, never a false finding,
# because a name must ALSO have a file allocated before it is ever reported.
_STDLIB = frozenset({
    "Int", "Int8", "Int16", "Int32", "Int64", "UInt", "UInt8", "UInt16",
    "UInt32", "UInt64", "Double", "Float", "Bool", "String", "Character",
    "Array", "Dictionary", "Set", "Optional", "Result", "Data", "Date",
    "UUID", "Void", "Any", "AnyObject", "Self", "Error", "Range",
    "ClosedRange", "Sequence", "Collection", "Equatable", "Hashable",
    "Comparable", "Codable", "Encodable", "Decodable", "List", "Tuple",
    "None", "True", "False",
})


def check_design_completeness(design_md: str) -> list[Finding]:
    """Findings where the design's types and its files disagree — DEV-509.

    Two directions of one invariant, each from a run that died on it:

      * run 6 (spec_1ba2db3d) declared `SeededRNG` in Data Models and allocated
        no file for it. Manifest mode generates exactly the enumerated files, so
        the implementer improvised one, the next regeneration dropped it, and the
        diagnostic alternated between "the RNG is wrong" and "there is no RNG" —
        so `_persistent_diagnostics` found an empty intersection and DEV-468's
        architect routing never fired. Both budgets went.

      * run 7 (spec_9e190582) did the reverse: `MushroomField` had a file, was
        the type of `GameWorld.field` and a parameter of `init(field:chains:)`,
        and was never declared. The design then stated outright that every seam
        symbol "is declared above". Nothing checked the claim.

    Fail-open: a name is only reported when the design commits to it in BOTH a
    file and a signature, or declares it outright. Ambiguity stays silent.
    """
    declared = declared_types(design_md)
    files = allocated_files(design_md)
    findings: list[Finding] = []

    # No File Structure section, or one that parsed to nothing, means we cannot
    # tell "allocated nowhere" from "we failed to read it". Judging every
    # declared type unallocated on that basis would reject a whole design over
    # a parsing gap, so this direction stays silent instead.
    for name in sorted(top_level_types(design_md) - files) if files else ():
        findings.append(Finding(
            kind=KIND_TYPE_WITHOUT_FILE,
            criterion="",
            detail=(
                f"`{name}` is declared in Data Models but no file in the File "
                f"Structure holds it. The manifest is built from that section "
                f"and generates exactly the files it names, so `{name}` can "
                f"never be created — the implementer improvises one, the next "
                f"regeneration deletes it, and the retry loop cannot converge. "
                f"Allocate a file for `{name}`."),
        ))

    # The reverse: a file was allocated and something is typed by that name,
    # but nothing declares it. Capitalised only — `main.py` implies no type.
    referenced = _type_references(design_md)
    for name in sorted(files & referenced):
        if name in declared or name in _STDLIB:
            continue
        findings.append(Finding(
            kind=KIND_FILE_WITHOUT_TYPE,
            criterion="",
            detail=(
                f"`{name}` has a file in the File Structure and is used as a "
                f"type in a declaration, but Data Models never declares it. The "
                f"implementer is told to fill `{name}`'s file with no statement "
                f"of what goes in it, so every attempt invents a different "
                f"shape. Declare `{name}` with its storage and its entry "
                f"points."),
        ))

    # DEV-554: the same type declared twice. `extension` is absent from the
    # regex, so extending a type is never reported — that is the correct way to
    # add to one. Nested declarations are excluded by the indentation guard, as
    # in top_level_types.
    for name, n in sorted(top_level_type_counts(design_md).items()):
        if n < 2:
            continue
        findings.append(Finding(
            kind=KIND_DUPLICATE_TYPE,
            criterion="",
            detail=(
                f"`{name}` is declared {n} times in Data Models. Only one can "
                f"exist — the rest are `invalid redeclaration of '{name}'`. "
                f"This happens when a design is revised in place and the "
                f"earlier version is left in the document; the reader cannot "
                f"tell which one is meant, and a design over the manifest "
                f"threshold is generated file-by-file, so each file's "
                f"generation may resolve it differently. Worse, seams written "
                f"against the abandoned version keep referencing members the "
                f"surviving one does not have. Keep exactly one declaration of "
                f"`{name}`, delete the others and the prose between them, and "
                f"re-check every seam that mentions it."),
        ))
    return findings


def format_findings(findings: list[Finding]) -> str:
    """Architect-facing feedback. Same shape as design_review_feedback.md."""
    if not findings:
        return ""
    lines = [
        "## Testability check failed (DEV-481)",
        "",
        "These are mechanical findings against the design's own "
        "`## Criterion Seams`: each names a criterion your API cannot carry "
        "out as specified. Fix the DESIGN — the criteria are not the problem.",
        "",
    ]
    for i, f in enumerate(findings, 1):
        where = f' — criterion "{f.criterion}"' if f.criterion else ""
        lines.append(f"{i}. [{f.kind}]{where}: {f.detail}")
    return "\n".join(lines) + "\n"
