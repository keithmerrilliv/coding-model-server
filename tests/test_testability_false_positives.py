"""DEV-809: four testability findings that fired on correct designs.

Run 60's design (the fixture, verbatim) drew all four and was correct from
round 0; run 57's drew prose_seam on a static lookup. Each fix is paired with a
negative control, because every one of them loosens a guard the DEV-710
incentive would otherwise teach the architect to game.
"""
from pathlib import Path

from coding_model_autonomous import design_testability as T

RUN60 = (Path(__file__).parent / "fixtures" / "dev809_run60_design.md").read_text()


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
