"""Mechanical testability check on the design — DEV-481 fix 1.

DEV-481 shipped the prompt half first (architect rule 10, design-review
AFFORDANCE). Run 6 complied with most of rule 10 and still stranded a criterion:
`Mushroom` was never declared Equatable, so "the same seed produces an identical
mushroom field" could not be asserted. Every source file compiled; the spec died
on two lines in the test file. Run 4 died on three of these at once.

The cases below are the real ones. `MISSING_EQUATABLE_*` reproduce run 6 and
run 4; `READONLY` reproduces run 4's `chains`; `UNRESOLVED` reproduces run 4's
invented `Field.bottomPlayerZoneStart`.
"""
import pytest

from coding_model_autonomous import design_testability as dt

# ── fixtures modelled on the real designs ────────────────────────────────────

CLEAN = """\
# Architecture: Demo

## Data Models
- `Position`: `{ col: Int, row: Int }` — Equatable, Hashable
- `Mushroom`: `{ hits: Int }` — Equatable
- `MushroomField`: `{ cells: [Position: Mushroom] }`
- `World`:
  - `chains: [Chain]` (settable for test injection)
  - `field: MushroomField`

## Implementation Notes
1. Declare `PLAYER_ZONE_START_ROW = 25` in `Constants.swift`.

## Acceptance Criteria Checklist
- [ ] Same seed produces identical mushroom field
- [ ] No mushrooms seeded in the player zone

## Criterion Seams
- Same seed → identical field | setup: `World(seed: 42)` | act: `w.field.cells` \
| assert: `w1.field.cells == w2.field.cells`
- Player zone empty | setup: `World(seed: 42)` | act: `w.field.cells` \
| assert: `w.field.cells.keys.allSatisfy { $0.row < PLAYER_ZONE_START_ROW }`
"""

# Run 6: identical to CLEAN except Mushroom never declares Equatable. The
# comparison is on `[Position: Mushroom]`, and nothing in the assert says
# "Mushroom" — the type is reachable only through the member's declared type.
RUN6 = CLEAN.replace("- `Mushroom`: `{ hits: Int }` — Equatable",
                     "- `Mushroom`: `{ hits: Int }`")

# Run 4: an enum with associated values, compared against a `.case` literal.
RUN4_ENUM = """\
# Architecture: Demo

## Data Models
- `HitOutcome`: enum — `.mushroom(damage: Int)`, `.chain(id: Int)`, `.empty`

## Acceptance Criteria Checklist
- [ ] Striking a vacant cell returns `.empty`

## Criterion Seams
- Vacant cell | setup: `World(seed: 1)` | act: `world.hit(at: p)` \
| assert: `outcome == .empty`
"""

# Run 4: `chains` read-only, and the criterion needs a chain placed low.
RUN4_READONLY = """\
# Architecture: Demo

## Data Models
- `World`:
  - `chains: [Chain]` — `public private(set)`, only `init(seed:)`

## Acceptance Criteria Checklist
- [ ] Chains descending past the bottom row are purged

## Criterion Seams
- Fall-off purge | setup: `world.chains.append(lowChain)` | act: `world.step()` \
| assert: `world.chains.isEmpty`
"""

# Run 4: the player-zone bound lived in prose, so the test invented a symbol.
RUN4_UNRESOLVED = """\
# Architecture: Demo

## Data Models
- `Field`: `{ columns: Int, rows: Int }`

## Implementation Notes
1. No mushrooms are seeded where `row >= Field.rows - 5`.

## Acceptance Criteria Checklist
- [ ] No mushrooms in the bottom five rows

## Criterion Seams
- Player zone | setup: `World(seed: 1)` | act: `w.field` \
| assert: `Field.bottomPlayerZoneStart == 25`
"""


def _kinds(design):
    return [f.kind for f in dt.check_design_testability(design)]


# ── the DEV-440 guard: silence on a sound design ─────────────────────────────

class TestCleanDesignPasses:
    def test_a_testable_design_produces_no_findings(self):
        assert dt.check_design_testability(CLEAN) == []

    def test_no_checklist_is_not_a_defect(self):
        """Nothing to strand — the check has no opinion."""
        assert dt.check_design_testability("# Design\n\n## Overview\nhi\n") == []

    def test_hashable_satisfies_the_equality_requirement(self):
        """Hashable refines Equatable; demanding both would be a false reject."""
        design = CLEAN.replace("- `Mushroom`: `{ hits: Int }` — Equatable",
                               "- `Mushroom`: `{ hits: Int }` — Hashable")
        assert dt.check_design_testability(design) == []


# ── the real failures ────────────────────────────────────────────────────────

class TestRun6Equatable:
    def test_flags_the_element_type_of_a_compared_collection(self):
        assert dt.KIND_MISSING_EQUATABLE in _kinds(RUN6)

    def test_names_the_type_the_architect_must_fix(self):
        finding = next(f for f in dt.check_design_testability(RUN6)
                       if f.kind == dt.KIND_MISSING_EQUATABLE)
        assert "Mushroom" in finding.detail

    def test_explains_the_conditional_conformance(self):
        """The reason run 6's architect missed it: nothing in the expression
        mentions Mushroom."""
        finding = next(f for f in dt.check_design_testability(RUN6)
                       if f.kind == dt.KIND_MISSING_EQUATABLE)
        assert "only Equatable if" in finding.detail


class TestRun6UntypedComparison:
    """Run 6's ACTUAL design compares `w1.field.cells == w2.field.cells` and
    declares `cells` nowhere, so the Equatable rule has nothing to resolve. The
    comparison still did not compile. This is the rule that closes that."""

    DESIGN = """\
# Architecture: Demo

## Data Models
- `Position`: `{ col: Int, row: Int }`
- `Mushroom`: `{ hits: Int }`
- `CentipedeWorld`:
  - `field: MushroomField`

## Acceptance Criteria Checklist
- [ ] Same seed produces identical mushroom field

## Criterion Seams
- Same seed | setup: `CentipedeWorld(seed: 42)` | act: `w.field.cells` \
| assert: `w1.field.cells == w2.field.cells`
"""

    def test_comparing_an_undeclared_aggregate_is_flagged(self):
        assert dt.KIND_UNTYPED_COMPARISON in _kinds(self.DESIGN)

    def test_it_asks_for_the_element_conformance(self):
        finding = next(f for f in dt.check_design_testability(self.DESIGN)
                       if f.kind == dt.KIND_UNTYPED_COMPARISON)
        assert "ELEMENT type" in finding.detail

    def test_a_declared_member_does_not_fire_it(self):
        """CLEAN declares `cells`, so this rule must stay silent there."""
        assert dt.KIND_UNTYPED_COMPARISON not in _kinds(CLEAN)

    def test_comparison_against_a_literal_is_left_alone(self):
        """`w.chains.count == 2` is not an aggregate comparison."""
        design = self.DESIGN.replace(
            "`w1.field.cells == w2.field.cells`", "`w.chains.count == 2`")
        assert dt.KIND_UNTYPED_COMPARISON not in _kinds(design)


class TestRun4:
    def test_enum_compared_to_a_case_literal_needs_equatable(self):
        assert dt.KIND_MISSING_EQUATABLE in _kinds(RUN4_ENUM)

    def test_setup_writing_readonly_state_is_stranded(self):
        assert dt.KIND_READONLY_SETUP in _kinds(RUN4_READONLY)

    def test_seam_naming_an_undeclared_member_is_flagged(self):
        assert dt.KIND_UNRESOLVED_SYMBOL in _kinds(RUN4_UNRESOLVED)

    def test_declared_members_are_not_flagged(self):
        """`Field.rows` IS declared — only the invented symbol should fire."""
        findings = [f for f in dt.check_design_testability(RUN4_UNRESOLVED)
                    if f.kind == dt.KIND_UNRESOLVED_SYMBOL]
        assert len(findings) == 1
        assert "bottomPlayerZoneStart" in findings[0].detail


# ── structure ────────────────────────────────────────────────────────────────

class TestSeamStructure:
    def test_a_design_with_criteria_and_no_seams_is_flagged_once(self):
        design = CLEAN.split("## Criterion Seams")[0]
        findings = dt.check_design_testability(design)
        assert [f.kind for f in findings] == [dt.KIND_NO_SECTION]

    def test_count_mismatch_is_reported_once_not_per_item(self):
        design = CLEAN.replace(
            "- Player zone empty | setup: `World(seed: 42)` | act: "
            "`w.field.cells` | assert: "
            "`w.field.cells.keys.allSatisfy { $0.row < PLAYER_ZONE_START_ROW }`",
            "")
        kinds = _kinds(design)
        assert kinds.count(dt.KIND_COUNT_MISMATCH) == 1

    def test_a_seam_missing_a_step_is_flagged(self):
        design = CLEAN.replace(
            "| setup: `World(seed: 42)` | act: `w.field.cells` "
            "| assert: `w1.field.cells == w2.field.cells`",
            "| act: `w.field.cells` | assert: `w1.field.cells == w2.field.cells`",
            1)
        assert dt.KIND_INCOMPLETE_SEAM in _kinds(design)

    def test_nested_bullet_form_parses_the_same_as_inline(self):
        """The architect may emit either shape; neither should read as absent."""
        design = CLEAN.split("## Criterion Seams")[0] + """## Criterion Seams
- Same seed → identical field
  - setup: `World(seed: 42)`
  - act: `w.field.cells`
  - assert: `w1.field.cells == w2.field.cells`
- Player zone empty
  - setup: `World(seed: 42)`
  - act: `w.field.cells`
  - assert: `w.field.cells.keys.count >= 0`
"""
        assert dt.parse_seams(design) and len(dt.parse_seams(design)) == 2
        assert dt.KIND_NO_SECTION not in _kinds(design)


class TestParsing:
    def test_checklist_strips_checkboxes(self):
        assert dt.parse_checklist(CLEAN) == [
            "Same seed produces identical mushroom field",
            "No mushrooms seeded in the player zone",
        ]

    def test_declared_types_come_from_data_models(self):
        assert {"Position", "Mushroom", "MushroomField", "World"} <= \
            dt.declared_types(CLEAN)

    def test_findings_are_reported_against_the_checklist_wording(self):
        """The architect should read back the criterion it wrote, not the
        abbreviation it used in the seam."""
        finding = next(f for f in dt.check_design_testability(RUN6)
                       if f.kind == dt.KIND_MISSING_EQUATABLE)
        assert finding.criterion == "Same seed produces identical mushroom field"


class TestFormatting:
    def test_empty_findings_format_to_empty(self):
        assert dt.format_findings([]) == ""

    def test_feedback_tells_the_architect_to_fix_the_design(self):
        text = dt.format_findings(dt.check_design_testability(RUN6))
        assert "Fix the DESIGN" in text
        assert "Mushroom" in text


@pytest.mark.parametrize("design", [CLEAN, RUN6, RUN4_ENUM, RUN4_READONLY,
                                    RUN4_UNRESOLVED])
def test_check_never_raises(design):
    """Fail-open: this runs inside the architect path and must not kill a spec."""
    dt.check_design_testability(design)


# DEV-564: run 13's architect numbered its seams (`1. **Name** | setup: ...`)
# and the whole section parsed as empty — no_seams_section fired three times
# on a section that existed, and every per-seam rule was silently skipped.
NUMBERED_SEAMS = """\
# Architecture: Demo

## Data Models
- `Position`: `{ col: Int, row: Int }` — Equatable, Hashable
- `Mushroom`: `{ hits: Int }` — Equatable
- `World`:
  - `chains: [Chain]` (settable for test injection)

## Acceptance Criteria Checklist
- [ ] Same seed produces identical mushroom field
- [ ] No mushrooms seeded in the player zone

## Criterion Seams

1. **Same seed identical field** | setup: `const a = createWorld({seed: 42}); const b = createWorld({seed: 42});` | act: `const s1 = snapshot(a); const s2 = snapshot(b);` | assert: `deepStrictEqual(s1, s2)`
2. **Player zone empty** | setup: `const w = createWorld({seed: 7});` | act: `const ms = snapshot(w).grid;` | assert: `Object.keys(ms).every(k => Number(k.split(',')[1]) < 25)`
"""


class TestNumberedListSeams:
    def test_numbered_entries_parse_as_seams(self):
        seams = dt.parse_seams(NUMBERED_SEAMS)
        assert len(seams) == 2
        assert all(s.setup and s.act and s.assert_ for s in seams)
        assert seams[0].criterion == "Same seed identical field"

    def test_no_seams_section_stays_silent_on_numbered_form(self):
        kinds = [f.kind for f in dt.check_design_testability(NUMBERED_SEAMS)]
        assert dt.KIND_NO_SECTION not in kinds

    def test_paren_style_ordered_markers_also_parse(self):
        design = NUMBERED_SEAMS.replace("1. **", "1) **").replace("2. **", "2) **")
        assert len(dt.parse_seams(design)) == 2

    def test_a_design_truly_lacking_the_section_still_fires(self):
        design = NUMBERED_SEAMS.split("## Criterion Seams")[0]
        kinds = [f.kind for f in dt.check_design_testability(design)]
        assert dt.KIND_NO_SECTION in kinds

    def test_dash_bullet_form_is_unchanged(self):
        assert len(dt.parse_seams(CLEAN)) == 2
