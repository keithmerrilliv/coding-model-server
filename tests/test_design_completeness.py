"""The design's type set and its file set must agree — DEV-509.

Manifest mode generates exactly the files the design's File Structure names, so
the two sets are really one. Each direction has cost a run:

  * run 6 (spec_1ba2db3d) declared `SeededRNG` and allocated no file. The
    implementer improvised one, the next regeneration deleted it, the diagnostic
    alternated between "the RNG is wrong" and "there is no RNG", and because
    `_persistent_diagnostics` intersects consecutive failures it found nothing
    stable — so DEV-468's architect routing never fired and both budgets went.

  * run 7 (spec_9e190582) did the reverse: `MushroomField` had a file, typed
    `GameWorld.field`, was a parameter of `init(field:chains:)`, and was never
    declared. The design then asserted every seam symbol "is declared above".
"""
import pytest

from coding_model_autonomous import design_testability as dt

# Run 6's shape: SeededRNG named in Data Models, absent from File Structure.
RUN6_TYPE_WITHOUT_FILE = """\
# Architecture: Demo

## File Structure
```
Sources/CentipedeCore/
├── Position.swift
├── MushroomField.swift
└── CentipedeWorld.swift
```

## Data Models
- `Position`: `{ col: Int, row: Int }`
- `MushroomField`: `{ cells: [Position: Mushroom] }`
- `SeededRNG`: `{ init(seed: Int); mutating func next() -> UInt64 }`
- `CentipedeWorld`:
  - `rng: SeededRNG` (internal)
"""

# Run 7's shape, in the code-block spelling that design actually used.
RUN7_FILE_WITHOUT_TYPE = """\
# Architecture: Demo

## File Structure
```
Sources/CentipedeCore/
├── Position.swift
├── MushroomField.swift
└── GameWorld.swift
```

## Data Models
```swift
struct Position { let col: Int; let row: Int }

struct GameWorld: Equatable {
    var field: MushroomField
    init(field: MushroomField, chains: [CentipedeChain])
}
```
"""

SOUND = """\
# Architecture: Demo

## File Structure
```
Sources/Core/
├── Position.swift
└── World.swift
```

## Data Models
```swift
struct Position: Equatable { let col: Int; let row: Int }
struct World { var origin: Position }
```
"""


def _kinds(design):
    return [f.kind for f in dt.check_design_completeness(design)]


class TestTypeWithoutFile:
    def test_run6_seeded_rng_is_flagged(self):
        assert dt.KIND_TYPE_WITHOUT_FILE in _kinds(RUN6_TYPE_WITHOUT_FILE)

    def test_it_names_the_type(self):
        f = next(x for x in dt.check_design_completeness(RUN6_TYPE_WITHOUT_FILE)
                 if x.kind == dt.KIND_TYPE_WITHOUT_FILE)
        assert "SeededRNG" in f.detail

    def test_it_explains_why_retrying_cannot_fix_it(self):
        """The expensive part was not the missing file, it was five retries
        against a defect no retry could reach."""
        f = next(x for x in dt.check_design_completeness(RUN6_TYPE_WITHOUT_FILE)
                 if x.kind == dt.KIND_TYPE_WITHOUT_FILE)
        assert "regeneration deletes it" in f.detail

    def test_types_that_have_files_are_not_flagged(self):
        flagged = [f.detail for f in
                   dt.check_design_completeness(RUN6_TYPE_WITHOUT_FILE)]
        assert not any("`Position`" in d for d in flagged)
        assert not any("`MushroomField`" in d for d in flagged)


class TestFileWithoutType:
    def test_run7_mushroomfield_is_flagged(self):
        assert dt.KIND_FILE_WITHOUT_TYPE in _kinds(RUN7_FILE_WITHOUT_TYPE)

    def test_it_names_the_type(self):
        f = next(x for x in dt.check_design_completeness(RUN7_FILE_WITHOUT_TYPE)
                 if x.kind == dt.KIND_FILE_WITHOUT_TYPE)
        assert "MushroomField" in f.detail

    def test_a_file_never_used_as_a_type_is_ignored(self):
        """`Constants.swift` holding loose constants declares no type. Flagging
        it would be a false rejection."""
        design = RUN7_FILE_WITHOUT_TYPE.replace(
            "├── Position.swift", "├── Position.swift\n├── Constants.swift")
        flagged = [f.detail for f in dt.check_design_completeness(design)]
        assert not any("`Constants`" in d for d in flagged)

    def test_test_files_are_not_treated_as_undeclared_types(self):
        design = RUN7_FILE_WITHOUT_TYPE.replace(
            "└── GameWorld.swift",
            "└── GameWorld.swift\n\nTests/CoreTests/\n└── WorldTests.swift")
        flagged = [f.detail for f in dt.check_design_completeness(design)]
        assert not any("`WorldTests`" in d for d in flagged)


class TestNoFalseRejections:
    """DEV-440: every rule here can cost an architect revision."""

    def test_a_consistent_design_is_silent(self):
        assert dt.check_design_completeness(SOUND) == []

    def test_stdlib_types_in_signatures_are_never_flagged(self):
        design = SOUND.replace(
            "struct World { var origin: Position }",
            "struct World { var origin: Position; var tags: [String];"
            " var id: UUID; func at(_ i: Int) -> Double }")
        assert dt.check_design_completeness(design) == []

    def test_a_design_with_neither_section_is_silent(self):
        assert dt.check_design_completeness("# Design\n\n## Overview\nhi\n") == []

    def test_code_block_declarations_count_as_declared(self):
        """Reading only bullet-style Data Models reported four of run 7's
        declared types as missing. Both spellings must parse."""
        assert {"Position", "World"} <= dt.declared_types(SOUND)

    def test_bullet_declarations_still_count(self):
        assert {"Position", "MushroomField", "SeededRNG"} <= \
            dt.declared_types(RUN6_TYPE_WITHOUT_FILE)


class TestAgainstTheRealRun7Design:
    """The design I rejected by hand at gate_8e14f676.

    Frozen into tests/fixtures rather than read from the live workspace. The
    first version of these tests read `var/tasks_db/specs/.../design.md`, which
    the architect overwrote with its next revision minutes later — a test that
    silently changes meaning as a run progresses is worse than no test.
    """

    @pytest.fixture
    def design(self):
        from pathlib import Path
        return (Path(__file__).parent / "fixtures"
                / "run7_design_v3.md").read_text()

    def test_it_catches_the_defect_i_caught_by_hand(self, design):
        f = [x for x in dt.check_design_completeness(design)
             if x.kind == dt.KIND_FILE_WITHOUT_TYPE]
        assert any("MushroomField" in x.detail for x in f)

    def test_it_also_catches_one_i_missed(self, design):
        """WorldSnapshot is declared and has no file — run 6's defect, present
        in run 7's design, and I did not spot it reviewing by hand."""
        f = [x for x in dt.check_design_completeness(design)
             if x.kind == dt.KIND_TYPE_WITHOUT_FILE]
        assert any("WorldSnapshot" in x.detail for x in f)

    def test_it_does_not_flag_the_types_that_are_fine(self, design):
        """Position, HitResult, CentipedeChain, Direction, MushroomCell and
        GameWorld are all declared AND allocated."""
        flagged = " ".join(f.detail
                           for f in dt.check_design_completeness(design))
        for ok in ("`Position`", "`HitResult`", "`CentipedeChain`",
                   "`Direction`", "`MushroomCell`", "`GameWorld`"):
            assert ok not in flagged


def test_check_never_raises():
    """Fail-open: this runs in the architect path."""
    for design in (RUN6_TYPE_WITHOUT_FILE, RUN7_FILE_WITHOUT_TYPE, SOUND, ""):
        dt.check_design_completeness(design)


def test_absent_file_structure_is_not_treated_as_zero_files():
    """Cannot-assess must not read as everything-missing. A design with no
    File Structure section would otherwise have every declared type flagged,
    rejecting the whole design over a parsing gap (DEV-440)."""
    design = """\
# Architecture: Demo

## Data Models
```swift
struct Position { let col: Int }
struct World { var origin: Position }
```
"""
    assert dt.check_design_completeness(design) == []


class TestNestedTypesNeedNoFile:
    """Run 7's design v4 nested `MushroomEntry` inside `WorldSnapshot` — at my
    own suggestion, and correctly. A rule blind to nesting flagged it on the
    first live design it ever saw, which is the DEV-440 false rejection."""

    NESTED = """\
# Architecture: Demo

## File Structure
```
Sources/Core/
├── Position.swift
└── WorldSnapshot.swift
```

## Data Models
```swift
struct Position: Equatable { let col: Int }

struct WorldSnapshot: Equatable {
    struct MushroomEntry: Equatable {
        let position: Position
        let damage: Int
    }
    let mushrooms: [MushroomEntry]
}
```
"""

    def test_a_nested_type_is_not_reported_as_unallocated(self):
        flagged = " ".join(f.detail for f in
                           dt.check_design_completeness(self.NESTED))
        assert "MushroomEntry" not in flagged

    def test_the_enclosing_type_still_needs_its_file(self):
        design = self.NESTED.replace("└── WorldSnapshot.swift", "")
        flagged = " ".join(f.detail for f in
                           dt.check_design_completeness(design))
        assert "WorldSnapshot" in flagged

    def test_nested_types_still_resolve_as_declared(self):
        """Only the file rule ignores them; they are real declarations."""
        assert "MushroomEntry" in dt.declared_types(self.NESTED)
        assert "MushroomEntry" not in dt.top_level_types(self.NESTED)
