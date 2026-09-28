"""DEV-809: four testability findings that fired on correct designs.

Run 60's design (the fixture, verbatim) drew all four and was correct from
round 0; run 57's drew prose_seam on a static lookup. Each fix is paired with a
negative control, because every one of them loosens a guard the DEV-710
incentive would otherwise teach the architect to game.
"""
from pathlib import Path

from fixture_files import load_fixture
from coding_model_autonomous import design_testability as T

RUN60 = load_fixture("dev809_run60_design.md")


def _kinds(md):
    return [f.kind for f in T.check_design_testability(md) + T.check_design_completeness(md)]


def test_run60_design_draws_no_findings():
    assert _kinds(RUN60) == []


# ── prose_seam: an explicitly empty setup ────────────────────────────────────

def _design(seam_line, checklist="- [ ] C1: the palette maps shot to its colour"):
    return (f"# Architecture\n\n## Data Models\n\n- `Palette`: enum namespace\n\n"
            f"## Acceptance Criteria Checklist\n{checklist}\n\n"
            f"## Criterion Seams\n- {seam_line}\n")


def test_a_none_setup_with_real_calls_passes():
    md = _design("C1 | setup: (none — static palette lookup) | "
                 "act: `let c = Palette.color(for: .shot)` | "
                 "assert: `c == Palette.shot`")
    assert T.KIND_PROSE_SEAM not in _kinds(md)


def test_a_none_setup_does_not_excuse_a_prose_act():
    md = _design("C1 | setup: (none) | act: look the colour up | "
                 "assert: `c == Palette.shot`")
    assert T.KIND_PROSE_SEAM in _kinds(md)


def test_a_none_setup_does_not_excuse_a_vacuous_assert():
    md = _design("C1 | setup: (none) | act: `let c = Palette.color(for: .shot)` | "
                 "assert: `true`")
    kinds = _kinds(md)
    assert T.KIND_PROSE_SEAM in kinds or T.KIND_VACUOUS_SEAM in kinds


def test_a_prose_setup_that_is_not_none_still_fails():
    md = _design("C1 | setup: build a palette first | "
                 "act: `let c = Palette.color(for: .shot)` | assert: `c == Palette.shot`")
    assert T.KIND_PROSE_SEAM in _kinds(md)


# ── seam_count_mismatch: lettered sub-seams are one criterion ────────────────

_TWO = "- [ ] C1: first\n- [ ] C2: second"
_S = "| setup: `let p = Palette.self` | act: `let c = Palette.color(for: .shot)` | assert: `c == Palette.shot`"


def test_sub_seams_count_as_one_criterion():
    md = _design(f"C1 {_S}\n- C2a {_S}\n- C2b {_S}", checklist=_TWO)
    assert T.KIND_COUNT_MISMATCH not in _kinds(md)


def test_a_duplicated_plain_label_still_counts_twice():
    md = _design(f"C1 {_S}\n- C1 {_S}\n- C2 {_S}", checklist=_TWO)
    assert T.KIND_COUNT_MISMATCH in _kinds(md)


# ── type_without_file: a type the File Structure places in a file ───────────

def _typed(file_line, extra_types="- `Helper`: a test-local stub\n"):
    return ("# Architecture\n\n## File Structure\n```\n"
            f"{file_line}\n```\n\n## Data Models\n\n- `Suite`: the tests\n{extra_types}")


def test_a_helper_named_as_housed_on_a_file_line_has_a_file():
    md = _typed("Tests/SuiteTests.swift — new: XCTest suite with Helper helpers")
    assert T.KIND_TYPE_WITHOUT_FILE not in [
        f.kind for f in T.check_design_completeness(md) if "Helper" in f.detail]


def test_a_type_merely_used_on_a_file_line_still_has_no_file():
    """`World.swift — add removeMushroom(at: Helper)` uses Helper; it does not
    house it. That is the finding DEV-509 exists for."""
    md = _typed("Sources/World.swift — add removeMushroom(at: Helper)")
    assert any(f.kind == T.KIND_TYPE_WITHOUT_FILE and "`Helper`" in f.detail
               for f in T.check_design_completeness(md))


def test_a_list_of_types_on_a_file_line_houses_each():
    md = _typed("Sources/World.swift — NEW: Helper, Direction, World",
                extra_types="- `Helper`: x\n- `Direction`: y\n")
    names = [f.detail for f in T.check_design_completeness(md)
             if f.kind == T.KIND_TYPE_WITHOUT_FILE]
    assert not any("`Helper`" in d or "`Direction`" in d for d in names)


# ── file_without_type: a type declared by its Data Models heading ────────────

_REFERENCED = ("# Architecture\n\n## File Structure\n```\nSources/Engine.swift — modified\n"
               "Sources/Forcer.swift — modified\n```\n\n## Data Models\n\n"
               "{heading}\n| member | type |\n|---|---|\n\n"
               "- `Engine`: owns `currentForcer: Forcer?`\n")


def test_a_type_heading_with_a_parenthetical_declares_the_type():
    md = _REFERENCED.format(heading="### Forcer (existing type, reference declaration)")
    assert not any(f.kind == T.KIND_FILE_WITHOUT_TYPE and "`Forcer`" in f.detail
                   for f in T.check_design_completeness(md))


def test_a_prose_heading_declares_nothing():
    md = _REFERENCED.format(heading="### How the forcer is wired")
    assert any(f.kind == T.KIND_FILE_WITHOUT_TYPE and "`Forcer`" in f.detail
               for f in T.check_design_completeness(md))


def test_the_architect_prompt_names_the_empty_setup_spelling():
    """DEV-715: a hatch the architect cannot discover is not a hatch."""
    from coding_model_autonomous import executor
    src = Path(executor.__file__).read_text()
    assert "setup: (none)" in src and "sub-seams" in src


# ── type_without_file: a type the design uses but does not create (DEV-855) ──

RUN73 = load_fixture("dev855_run73_design.md")


def _without_file(md, served=None):
    return [f.detail for f in T.check_design_completeness(md, served)
            if f.kind == T.KIND_TYPE_WITHOUT_FILE]


def test_run73_design_draws_no_type_without_file():
    """Run 73 said "No new types declared" over bullets for existing types and
    was told to allocate a file for HallucinationSimulator."""
    assert _without_file(RUN73) == []


def _uses(statement, bullets="- `Helper` — the existing renderer\n"):
    return ("# Architecture\n\n## File Structure\n```\n"
            "Tests/SuiteTests.swift — new: the suite\n```\n\n"
            f"## Data Models\n{statement}\n{bullets}")


def test_a_no_new_types_statement_clears_bulleted_types():
    assert _without_file(_uses("No new types declared. Uses only these:")) == []


def test_no_new_data_structures_counts_too():
    assert _without_file(_uses("No new data structures; the test uses:")) == []


def test_a_carve_out_keeps_the_named_type():
    """ "No new types except `Helper`" creates Helper: it still needs a file."""
    md = _uses("No new types except `Helper`, which the suite owns.")
    assert any("`Helper`" in d for d in _without_file(md))


def test_a_fenced_declaration_is_still_a_claim_to_create():
    md = _uses("No new types beyond the helper below.",
               bullets="- `Helper` — test double\n```swift\nstruct Helper {}\n```\n")
    assert any("`Helper`" in d for d in _without_file(md))


def test_a_bullet_marked_existing_is_used():
    assert _without_file(_uses("", bullets="- `Helper` (existing): the renderer\n")) == []


def test_existing_elsewhere_in_the_bullet_does_not_count():
    """"replaces the existing X" describes something else; Helper is new."""
    md = _uses("", bullets="- `Helper`: replaces the existing stub\n")
    assert any("`Helper`" in d for d in _without_file(md))


def test_a_type_a_served_file_declares_is_used():
    served = {"Sources/Helper.swift": "final class Helper {\n}\n"}
    assert _without_file(_uses("", bullets="- `Helper`: the renderer\n"), served) == []


def test_an_extension_in_served_code_does_not_prove_existence():
    served = {"Sources/Other.swift": "extension Helper {}\n"}
    assert any("`Helper`" in d for d in
               _without_file(_uses("", bullets="- `Helper`: the renderer\n"), served))


def test_run6_shape_still_fires():
    """Known-true case: a declared type with no file, no statement, nothing
    served — run 6's SeededRNG. That is the finding DEV-509 exists for."""
    md = _uses("", bullets="- `SeededRNG`: a deterministic generator\n")
    assert any("`SeededRNG`" in d for d in _without_file(md))
