"""DEV-722: a design adding state to a Swift value type must say what the type IS.

DEV-511's value-semantics preamble is live and CORRECT. Its rules are jointly
unsatisfiable until someone decides whether the type stays a value type: mark
the members `mutating` and every `let` call site breaks, leave them and the
assignment breaks. Electric Sheep run 2 spent three implementers and 6 -> 15 ->
25 diagnostics discovering that, because its design never said.

The fix is a required DECISION, enforced here. More prompt text cannot answer a
question nobody asked.
"""

import pytest

from coding_model_autonomous.design_testability import (
    KIND_UNDECLARED_MUTABILITY,
    check_declared_mutability,
    served_value_types,
)

BRIDGE_STRUCT = """import Foundation
@MainActor
struct MetricsParticleBridge {
    func update(from engine: Engine) { }
}
"""
BRIDGE_CLASS = """import Foundation
@MainActor
final class MetricsParticleBridge {
    func update(from engine: Engine) { }
}
"""
APP_STRUCT = """import SwiftUI
struct ElectricSheepApp: App {
    @State private var engine = Engine()
    var body: some Scene { WindowGroup { } }
}
"""
SERVED = {"ElectricSheep/MetricsParticleBridge.swift": BRIDGE_STRUCT}


def _design(fs_line, body=""):
    return ("## File Structure\n\n```\nElectricSheep/\n"
            f"└── {fs_line}\n```\n\n## Data Models\n\n" + body + "\n")


def _kinds(md, served=SERVED):
    return {f.kind for f in check_declared_mutability(md, served)}


# ── the defect ─────────────────────────────────────────────────────────────

def test_adding_state_to_a_served_struct_without_a_contract_fires():
    md = _design("MetricsParticleBridge.swift  # Add consumption cursor")
    assert KIND_UNDECLARED_MUTABILITY in _kinds(md)


def test_the_finding_names_the_decision_to_make():
    md = _design("MetricsParticleBridge.swift  # Add consumption cursor")
    detail = check_declared_mutability(md, SERVED)[0].detail
    assert "REMAINS a struct" in detail and "final class" in detail
    assert "mutating" in detail and "var" in detail


# ── the decision, stated either way, clears it ─────────────────────────────

@pytest.mark.parametrize("contract", [
    "MetricsParticleBridge remains a struct; `update` becomes `mutating`.",
    "MetricsParticleBridge becomes a final class with reference semantics.",
    "`MetricsParticleBridge` is a value type; call sites bind `var`.",
])
def test_stating_the_contract_clears_the_finding(contract):
    md = _design("MetricsParticleBridge.swift  # Add consumption cursor",
                 contract)
    assert KIND_UNDECLARED_MUTABILITY not in _kinds(md)


# ── the narrowness controls ────────────────────────────────────────────────

def test_a_contract_about_a_DIFFERENT_type_does_not_clear_it():
    """Run 2's design said 'Lightweight value type' about an unrelated helper.

    A document-wide match let that suppress the check entirely, so it silently
    never fired on the design it was written for.
    """
    md = _design("MetricsParticleBridge.swift  # Add consumption cursor",
                 "`ForcerTestState`: Lightweight value type holding counters.")
    assert KIND_UNDECLARED_MUTABILITY in _kinds(md)


def test_a_reference_type_is_not_flagged():
    served = {"ElectricSheep/MetricsParticleBridge.swift": BRIDGE_CLASS}
    md = _design("MetricsParticleBridge.swift  # Add consumption cursor")
    assert KIND_UNDECLARED_MUTABILITY not in _kinds(md, served)


def test_a_swiftui_view_type_is_not_flagged():
    """@State is how a SwiftUI App mutates. Flagging it would push the
    architect toward a contract that is wrong for the framework."""
    served = {"ElectricSheep/ElectricSheepApp.swift": APP_STRUCT}
    md = _design("ElectricSheepApp.swift  # Add task lifecycle state")
    assert KIND_UNDECLARED_MUTABILITY not in _kinds(md, served)
    assert served_value_types(served) == {}


def test_merely_listing_a_file_is_not_adding_state():
    md = _design("MetricsParticleBridge.swift  # read-only reference")
    assert KIND_UNDECLARED_MUTABILITY not in _kinds(md)


def test_an_unserved_file_is_not_flagged():
    """DEV-630: with nothing served we cannot read the declaration."""
    md = _design("MetricsParticleBridge.swift  # Add consumption cursor")
    assert check_declared_mutability(md, {}) == []


def test_no_file_structure_section_is_silent():
    assert check_declared_mutability("## Data Models\n\nnothing\n", SERVED) == []


# ── the real designs ───────────────────────────────────────────────────────

def test_the_tree_format_with_bare_filenames_is_matched():
    """Both ES designs write a tree — `└── MetricsParticleBridge.swift` — not
    full paths. Requiring the full path is why the first version missed."""
    md = ("## File Structure\n\n```\nElectricSheep/\n"
          "├── HallucinationForcer.swift   # Add totalMetricsProduced counter\n"
          "└── MetricsParticleBridge.swift # Cursor tracking, clamp logic\n"
          "```\n")
    assert KIND_UNDECLARED_MUTABILITY in _kinds(md)
