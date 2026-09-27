"""DEV-828: undeclared_mutability charges added state to the type that gains it.

Run 64 (spec_28e195d8, Centipede DEV-826) added `hud` to `FrameSnapshot` and
said "FrameSnapshot remains a struct". The check still spent both revision
rounds on `GridPosition` and `CellKind`, the file's other two value types,
which the design never touched. The fixtures are run 64's final design.md and
the FrameSnapshot.swift it was served (Centipede e09322a), byte for byte.
"""
from pathlib import Path

from coding_model_autonomous import design_testability as dt

FIXTURES = Path(__file__).parent / "fixtures"
DESIGN = (FIXTURES / "dev828_run64_design.md").read_text()
PATH = "Sources/CentipedeRender/FrameSnapshot.swift"
SERVED = {PATH: (FIXTURES / "dev828_FrameSnapshot.swift").read_text()}
CONTRACT = "// FrameSnapshot remains a struct; ALL stored properties are mutable vars:"


def _flagged(design: str, served: dict = SERVED) -> list:
    """The type names each finding charges, in order."""
    out = []
    for f in dt.check_declared_mutability(design, served):
        out += [n for n in dt.served_value_types(served)[PATH] if f"`{n}`" in f.detail]
    return out


def test_the_file_really_declares_three_value_types():
    assert dt.served_value_types(SERVED) == {PATH: ["GridPosition", "CellKind", "FrameSnapshot"]}


def test_run64_final_design_raises_nothing():
    """The design stated FrameSnapshot's contract; the siblings were never touched."""
    assert dt.check_declared_mutability(DESIGN, SERVED) == []


def test_without_a_contract_only_the_type_gaining_state_is_charged():
    """Round 0's shape: the same File Structure line and a Data Models entry,
    with no contract stated anywhere. The finding is still right to fire, but
    about FrameSnapshot, not about GridPosition and CellKind. (The final design
    states the contract in three places, so it cannot be made by deleting one.)"""
    assert CONTRACT in DESIGN
    fs_line = next(l for l in dt._section(DESIGN, dt.FILE_STRUCTURE_HEADING).splitlines()
                   if "FrameSnapshot.swift" in l)
    design = (f"## File Structure\n{fs_line}\n\n"
              "## Data Models\n```swift\npublic struct FrameSnapshot {\n"
              "    public var hud: HUD\n}\n```\n")
    assert _flagged(design) == ["FrameSnapshot"]


def test_a_file_with_one_value_type_is_attributed_without_being_named():
    """Control: the DEV-722 case. spec_0aab1c17 added a cursor to the struct
    MetricsParticleBridge, and its File Structure line never names the type
    apart from through the filename."""
    served = {"ElectricSheep/Bridge.swift": "struct Bridge {\n    var x = 0\n}\n"}
    design = ("## File Structure\n"
              "ElectricSheep/Bridge.swift — add consumption cursor (_lastSeen)\n\n"
              "## Data Models\nNone.\n")
    [finding] = dt.check_declared_mutability(design, served)
    assert "`Bridge`" in finding.detail


def test_several_value_types_none_named_is_silent():
    """No way to tell which type gains the state, so no guess is made."""
    served = {"Shapes.swift": "struct Point {\n}\n\nenum Kind {\n    case a\n}\n"}
    design = ("## File Structure\nShapes.swift — add a cache for lookups\n\n"
              "## Data Models\nNone introduced.\n")
    assert dt.check_declared_mutability(design, served) == []


def test_a_type_declared_in_data_models_counts_as_touched():
    served = {"Shapes.swift": "struct Point {\n}\n\nenum Kind {\n    case a\n}\n"}
    design = ("## File Structure\nShapes.swift — add a stored label\n\n"
              "## Data Models\n```swift\nstruct Point {\n    var label: String\n}\n```\n")
    assert _flagged_generic(design, served) == ["Point"]


def _flagged_generic(design: str, served: dict) -> list:
    [path] = served
    out = []
    for f in dt.check_declared_mutability(design, served):
        out += [n for n in dt.served_value_types(served)[path] if f"`{n}`" in f.detail]
    return out
