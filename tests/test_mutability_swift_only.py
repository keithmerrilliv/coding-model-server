"""DEV-831: undeclared_mutability is a Swift check and scans only Swift files.

Run 66 (spec_ae73361b, the first LLab run) served the C++ header
`LLab Shared/Generator/LSystem.h`. Its only column-0 `struct|enum` is
`enum NodeType { isLeaf, isBranch, noModules };`, so the header's one "value
type" was a C++ enum, and DEV-828's single-type rule charged the design's new
`axiomError` member — which goes on `class LSystem` — to it. The architect lost
a revision round deciding whether a C++ enum "remains a struct".
"""

from fixture_files import load_fixture
from coding_model_autonomous import design_testability as dt
from coding_model_autonomous import languages

DESIGN = load_fixture("dev831_run66_design.md")
HEADER = load_fixture("dev831_LSystem.h")
HEADER_PATH = "LLab Shared/Generator/LSystem.h"


def test_run66_cpp_header_raises_no_mutability_finding():
    """The replay: run 66's attempt-1 design against the header it was served."""
    assert dt.check_declared_mutability(DESIGN, {HEADER_PATH: HEADER}) == []


def test_non_swift_sources_declare_no_value_types():
    for path in (HEADER_PATH, "LLab Shared/Store/Loader.m",
                 "LLab Shared/Renderer/Renderer.mm", "src/thing.cpp"):
        assert dt.served_value_types({path: "enum NodeType { a, b };\n"
                                            "struct Point { float x; };\n"}) == {}


ROUND2 = load_fixture("dev831_run66_round2_design.md")


def _packs(design_md):
    """The language packs a design's File Structure allocates files in."""
    return [p.name for p in languages.packs_in_text(
        dt._section(design_md, dt.FILE_STRUCTURE_HEADING))]


def test_run66_round2_c_family_design_raises_no_completeness_finding():
    """Round 2 listed NodeType, ProductionMap and LSystem in Data Models (to
    answer round 1's false finding); all live in existing headers, and
    type_without_file asked for a file for each."""
    assert "c_family" in _packs(ROUND2)
    assert not dt.rule_applies(ROUND2, languages.RULE_COMPLETENESS)
    assert dt.check_design_completeness(ROUND2) == []


def test_completeness_still_fires_on_the_same_design_as_swift():
    """Negative control: the same design with Swift files is still checked."""
    swift = (ROUND2.replace("LSystem.h", "LSystem.swift")
                   .replace("LSystem.cpp", "LSystemImpl.swift"))
    assert dt.rule_applies(swift, languages.RULE_COMPLETENESS)
    kinds = {f.kind for f in dt.check_design_completeness(swift)}
    assert dt.KIND_TYPE_WITHOUT_FILE in kinds


def test_c_family_pattern_ignores_markdown_and_metal():
    for fs in ("README.md - notes", "Shaders.metal - kernel", "design.md"):
        assert _packs(f"## File Structure\n{fs}\n") == []
    for fs in ("ShaderTypes.h", "Renderer.mm", "Loader.m", "Turtle.cpp"):
        assert _packs(f"## File Structure\n{fs} - modified\n") == ["c_family"]


def test_same_shape_in_a_swift_file_still_fires():
    """Negative control: the check is narrowed, not disarmed."""
    design = DESIGN.replace("LSystem.h", "LSystem.swift")
    source = "enum NodeType { case leaf, branch }\n\nfinal class LSystem {}\n"
    findings = dt.check_declared_mutability(
        design, {"LLab Shared/Generator/LSystem.swift": source})
    assert [f.kind for f in findings] == [dt.KIND_UNDECLARED_MUTABILITY]
