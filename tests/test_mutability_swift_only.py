"""DEV-831: undeclared_mutability is a Swift check and scans only Swift files.

Run 66 (spec_ae73361b, the first LLab run) served the C++ header
`LLab Shared/Generator/LSystem.h`. Its only column-0 `struct|enum` is
`enum NodeType { isLeaf, isBranch, noModules };`, so the header's one "value
type" was a C++ enum, and DEV-828's single-type rule charged the design's new
`axiomError` member — which goes on `class LSystem` — to it. The architect lost
a revision round deciding whether a C++ enum "remains a struct".
"""

from pathlib import Path

from coding_model_autonomous import design_testability as dt

FIXTURES = Path(__file__).parent / "fixtures"
DESIGN = (FIXTURES / "dev831_run66_design.md").read_text(encoding="utf-8")
HEADER = (FIXTURES / "dev831_LSystem.h").read_text(encoding="utf-8")
HEADER_PATH = "LLab Shared/Generator/LSystem.h"


def test_run66_cpp_header_raises_no_mutability_finding():
    """The replay: run 66's attempt-1 design against the header it was served."""
    assert dt.check_declared_mutability(DESIGN, {HEADER_PATH: HEADER}) == []


def test_non_swift_sources_declare_no_value_types():
    for path in (HEADER_PATH, "LLab Shared/Store/Loader.m",
                 "LLab Shared/Renderer/Renderer.mm", "src/thing.cpp"):
        assert dt.served_value_types({path: "enum NodeType { a, b };\n"
                                            "struct Point { float x; };\n"}) == {}


def test_same_shape_in_a_swift_file_still_fires():
    """Negative control: the check is narrowed, not disarmed."""
    design = DESIGN.replace("LSystem.h", "LSystem.swift")
    source = "enum NodeType { case leaf, branch }\n\nfinal class LSystem {}\n"
    findings = dt.check_declared_mutability(
        design, {"LLab Shared/Generator/LSystem.swift": source})
    assert [f.kind for f in findings] == [dt.KIND_UNDECLARED_MUTABILITY]
