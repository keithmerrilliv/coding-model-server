"""A collection of tuples on an Equatable type cannot compile (DEV-525).

Swift tuples conform to no protocol. So `[(position: Position, damage: Int)]`
can never be Equatable, and a type holding one cannot synthesise `==` however
it is declared. This killed run 6 and hit run 7's design twice — the second
time one revision after the first was rejected with the fix spelled out, in a
document that had just added a named struct for the sibling property and left
this one alone.

The reason it needs a mechanical rule rather than a better prompt: DEV-481's
rule 10 was widened for exactly this class, and occurrence 3 happened *after*
that shipped, in a design that had demonstrably read it — the design's own
Equatable checklist existed because of the rule, and asserted completeness
while the failing property sat two lines above. The prompt sentence went in
too (DEV-525 fix 3), but on the evidence it cannot be the only defence.

DEV-481's `_check_equatable` cannot reach this: it asks whether a compared
type *declares* Equatable, and `WorldSnapshot` does. The question here is
whether the declaration can be *synthesised*.
"""
from pathlib import Path

import pytest

from coding_model_autonomous import design_testability as dt

FIXTURE = Path(__file__).parent / "fixtures" / "run7_design_v3.md"


def _design(body: str) -> str:
    return "## Data Models\n\n```swift\n" + body + "\n```\n"


class TestTheShapeThatIsAlwaysWrong:
    def test_run7_v3_flags_mushrooms(self):
        """The ticket's acceptance, against the real artefact."""
        findings = dt.check_design_testability(FIXTURE.read_text())
        tuples = [f for f in findings if f.kind == dt.KIND_TUPLE_CONFORMANCE]
        assert [f.criterion for f in tuples] == ["WorldSnapshot.mushrooms"]

    def test_the_sibling_shape_from_v4_is_caught_too(self):
        """v4 fixed `mushrooms` and left `chains` — the recurrence that made
        this a ticket rather than a one-off correction."""
        md = _design(
            "struct WorldSnapshot: Equatable {\n"
            "    struct MushroomEntry: Equatable { let position: Position }\n"
            "    let chains: [(segments: [[Position]], direction: Direction)]\n"
            "    let mushrooms: [MushroomEntry]\n"
            "}"
        )
        found = dt._tuple_collection_members(md)
        assert [(o, m) for o, m, _ in found] == [("WorldSnapshot", "chains")]

    def test_dictionary_of_tuples_is_caught(self):
        md = _design("struct S: Equatable {\n"
                     "    let byKey: [String: (a: Int, b: Int)]\n}")
        assert dt._tuple_collection_members(md)

    def test_reported_even_without_a_checklist(self):
        """The defect is in the declaration, so it does not depend on there
        being criteria to strand — the early return for 'no checklist' must
        not swallow it."""
        md = _design("struct S: Equatable {\n    let xs: [(a: Int, b: Int)]\n}")
        assert dt.parse_checklist(md) == []
        assert [f.kind for f in dt.check_design_testability(md)] == \
            [dt.KIND_TUPLE_CONFORMANCE]


class TestSilenceWhereItShouldBeSilent:
    """Every false finding costs an architect revision out of a budget shared
    with human rejections and DEV-468's routing (DEV-440)."""

    def test_tuple_on_a_type_claiming_no_conformance_is_legal(self):
        md = _design("struct S {\n    let xs: [(a: Int, b: Int)]\n}")
        assert dt._tuple_collection_members(md) == []

    def test_collection_of_named_type_is_fine(self):
        md = _design("struct S: Equatable {\n    let xs: [MushroomEntry]\n}")
        assert dt._tuple_collection_members(md) == []

    def test_a_bare_tuple_property_is_not_this_rule(self):
        """Not a collection — the synthesised-conformance question is
        different, and this rule deliberately does not opine on it."""
        md = _design("struct S: Equatable {\n    let pair: (a: Int, b: Int)\n}")
        assert dt._tuple_collection_members(md) == []

    def test_array_of_closures_is_not_reported_as_a_tuple(self):
        """`[(Int, String) -> Void]` has a comma inside parens but is a
        different problem; this message would be actively misleading."""
        md = _design("struct S: Equatable {\n"
                     "    let handlers: [(Int, String) -> Void]\n}")
        assert dt._tuple_collection_members(md) == []

    @pytest.mark.parametrize("path", sorted(
        Path("var/tasks_db/specs").glob("*/design.md")))
    def test_no_shipped_design_regresses(self, path):
        """Every design this pipeline has actually produced. A rule that fires
        on the corpus would be rejecting sound work."""
        found = dt._tuple_collection_members(path.read_text())
        assert not found, f"{path}: {found}"
