"""DEV-822: the testability check on run 61's design (spec_360d8d96, DEV-815).

Two findings, two different verdicts:

* `undeclared_mutability` was a misdiagnosis. It read the enums nested inside
  `final class HallucinationEngine` as the file's value types and demanded a
  struct-or-class decision about a class. Fixed by matching column-0
  declarations only.
* `prose_seam` on "continues from C5a" setups was RIGHT — the implementer wrote
  C5b as its own test, dropped C5a's state, and the test could not pass. Only
  its message was wrong: it never said the setup was a continuation, so both
  revision rounds left them in place. It still fires; it now says why.

The fixture is run 61's final design.md, byte for byte.
"""
from pathlib import Path

from coding_model_autonomous import design_testability as dt

FIXTURE = Path(__file__).parent / "fixtures" / "dev822_run61_design.md"
ENGINE_PATH = "ElectricSheep/HallucinationEngine.swift"

# The shape of ES main 57eee4a's HallucinationEngine.swift: a class, with both
# of its enums nested inside it.
ENGINE_SRC = """import Foundation

/// Manages MLX model loading and text generation with forced hallucinations.
@MainActor
@Observable
final class HallucinationEngine {

    enum ModelState: Sendable, Equatable {
        case idle
        case ready
    }

    enum AvailableModel: String, CaseIterable, Identifiable, Sendable {
        case smolLM = "mlx-community/SmolLM-135M-Instruct-4bit"
        var id: String { rawValue }
    }

    private(set) var state: ModelState = .idle
}
"""


def _design() -> str:
    return FIXTURE.read_text()


# ── undeclared_mutability: a misdiagnosis, fixed ─────────────────────────────

def test_nested_enums_are_not_the_files_value_types():
    assert dt.served_value_types({ENGINE_PATH: ENGINE_SRC}) == {}


def test_run61_design_raises_no_mutability_finding():
    assert dt.check_declared_mutability(_design(), {ENGINE_PATH: ENGINE_SRC}) == []


def test_a_top_level_value_type_still_counts():
    """Negative control: the same file with the enum at top level is exactly
    the DEV-722 case, and must still fire on the same design."""
    top_level = ENGINE_SRC.replace(
        "    enum ModelState: Sendable, Equatable {\n"
        "        case idle\n        case ready\n    }\n", "")
    top_level = "enum ModelState: Sendable, Equatable {\n    case idle\n}\n\n" + top_level
    assert dt.served_value_types({ENGINE_PATH: top_level}) == {ENGINE_PATH: ["ModelState"]}
    findings = dt.check_declared_mutability(_design(), {ENGINE_PATH: top_level})
    assert [f.kind for f in findings] == [dt.KIND_UNDECLARED_MUTABILITY]


def test_modified_top_level_struct_with_nested_enum_reports_only_the_struct():
    src = "struct World {\n    enum Phase { case a }\n    var x = 0\n}\n"
    assert dt.served_value_types({"World.swift": src}) == {"World.swift": ["World"]}


# ── prose_seam on continuations: correct, now says why ───────────────────────

def _prose(findings):
    return {f.criterion: f.detail for f in findings if f.kind == dt.KIND_PROSE_SEAM}


def test_continuation_setups_still_fire():
    """The finding was right: C5b's implementer lost C5a's state."""
    assert set(_prose(dt.check_design_testability(_design()))) == {
        "C5b", "C5c", "C7b", "C9"}


def test_the_message_names_the_seam_being_continued():
    prose = _prose(dt.check_design_testability(_design()))
    for criterion, continued in {"C5b": "C5a", "C5c": "C5b",
                                 "C7b": "C7a", "C9": "C8"}.items():
        detail = prose[criterion]
        assert f"continues from {continued}" in detail, criterion
        assert f"Restate {continued}'s setup and act calls in full" in detail
        assert "names no API" not in detail


def test_a_plain_prose_setup_keeps_the_generic_message():
    """Control: a setup that is prose but not a continuation is unchanged."""
    seam = dt.Seam(criterion="C1", setup="place a chain at the rightmost column",
                   act="`world.tick()`", assert_="`#expect(world.done)`")
    [finding] = dt._check_names_a_call(seam)
    assert "names no API" in finding.detail
    assert "continues from" not in finding.detail


def test_a_continuation_with_a_prose_act_reports_both():
    seam = dt.Seam(criterion="C2b", setup="(continues from C2a)",
                   act="tick the world once", assert_="`#expect(world.done)`")
    details = [f.detail for f in dt._check_names_a_call(seam)]
    assert len(details) == 2
    assert any("continues from C2a" in d for d in details)
    assert any("the act step describes" in d for d in details)


def test_inlining_the_continued_calls_clears_the_finding():
    """What the message asks for is what the check accepts."""
    fixed = _design().replace(
        "setup: (continues from C5a state)",
        "setup: `let f = HallucinationForcer(strategy: PanelStubStrategy()); "
        "_ = f.process(logits: MLXArray([Float](arrayLiteral: 1, 2, 3, 4)))`")
    assert "C5b" not in _prose(dt.check_design_testability(fixed))
