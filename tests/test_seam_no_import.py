"""Tests for DEV-661 seam import check."""


from coding_model_autonomous.design_testability import (
    KIND_SEAM_NO_IMPORT,
    check_design_testability,
    is_python_design,
)

# Fixtures - exactly as specified in the design document
PY_DESIGN = '''# Design: placeholder rules

## Overview
Two rules are added to `is_placeholder_path`.

## File Structure
```text
src/
└── coding_model_autonomous/
    └── workspace.py          # Modified: is_placeholder_path updated
tests/
└── test_placeholder_vocabulary.py  # New
```

## Data Models
None.

## Acceptance Criteria Checklist
- [ ] T1 — `is_placeholder_path("{p}")` is True
- [ ] T2 — `is_placeholder_path("test_*.py")` is True

## Criterion Seams
Shared setup for all seams: ``from coding_model_autonomous.workspace import is_placeholder_path``

- T1 | setup: ``input_val = "{p}"`` | act: ``result = is_placeholder_path(input_val)`` | assert: ``assert result is True``
- T2 | setup: ``input_val = "test_*.py"`` | act: ``result = is_placeholder_path(input_val)`` | assert: ``assert result is True``
'''

PY_DESIGN_NO_IMPORT = PY_DESIGN.replace(
    'Shared setup for all seams: ``from coding_model_autonomous.workspace import is_placeholder_path``\n', '')

PY_DESIGN_SRC_IMPORT = PY_DESIGN.replace(
    'from coding_model_autonomous.workspace import', 'from src.coding_model_autonomous.workspace import')

PY_DESIGN_IMPORT_IN_SEAM = PY_DESIGN_NO_IMPORT.replace(
    'setup: ``input_val = "{p}"``', 'setup: ``from coding_model_autonomous.workspace import is_placeholder_path; input_val = "{p}"``')

SWIFT_DESIGN = '''# Design: restoration

## File Structure
```text
Sources/CentipedeCore/World.swift   # Modified
Tests/CentipedeCoreTests/GameTests.swift
```

## Data Models
None.

## Acceptance Criteria Checklist
- [ ] T1 — restored mushrooms score 5 each

## Criterion Seams
- T1 | setup: ``var g = Game(world: World(mushrooms: field, chains: []), gameState: GameState(), playerPosition: Position(column: 10, row: 29))`` | act: ``g.tick()`` | assert: ``XCTAssertEqual(g.state.score, 10)``
'''


def test_T1_kind_constant():
    """T1 — KIND_SEAM_NO_IMPORT == "seam_no_import"."""
    assert KIND_SEAM_NO_IMPORT == "seam_no_import"


def test_T2_python_design_with_shared_setup_has_no_findings():
    """T2 — check_design_testability(PY_DESIGN) returns []."""
    findings = check_design_testability(PY_DESIGN)
    assert findings == []


def test_T3_missing_import_finds_seam_no_import():
    """T3 — kinds(PY_DESIGN_NO_IMPORT) == ['seam_no_import'] with required detail phrases."""
    findings = check_design_testability(PY_DESIGN_NO_IMPORT)
    assert [f.kind for f in findings] == ["seam_no_import"]
    detail = findings[0].detail
    assert "never import it" in detail
    assert "from <pkg>.<module> import <name>" in detail
    assert 'never import through `src.`' in detail
    assert findings[0].criterion == ""


def test_T4_src_import_finds_seam_no_import():
    """T4 — kinds(PY_DESIGN_SRC_IMPORT) == ['seam_no_import'] with no src package message."""
    findings = check_design_testability(PY_DESIGN_SRC_IMPORT)
    assert [f.kind for f in findings] == ["seam_no_import"]
    detail = findings[0].detail
    assert "There is no `src` package" in detail


def test_T5_import_in_seam_counts_as_shared_setup():
    """T5 — check_design_testability(PY_DESIGN_IMPORT_IN_SEAM) returns []."""
    findings = check_design_testability(PY_DESIGN_IMPORT_IN_SEAM)
    assert findings == []


def test_T6_swift_design_returns_empty_and_is_not_python():
    """T6 — Swift design has no findings and is not a Python design."""
    findings = check_design_testability(SWIFT_DESIGN)
    py_check = is_python_design(SWIFT_DESIGN)
    assert findings == []
    assert py_check is False


def test_T7_python_design_detected_correctly():
    """T7 — is_python_design(PY_DESIGN) is True."""
    result = is_python_design(PY_DESIGN)
    assert result is True


def test_T8_missing_sections_yields_early_return():
    """T8 — Python design missing checklist/seams sections yields [] from early return."""
    truncated = '\n'.join(PY_DESIGN.split('\n')[:PY_DESIGN.index('## Acceptance Criteria Checklist')])
    findings = check_design_testability(truncated)
    assert findings == []
