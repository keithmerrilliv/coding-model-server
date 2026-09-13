# The testability check requires a Python design's seams to import the code under test (DEV-661, run 34)

## Context

Target repo: **coding-model-server** (self). `design_testability.check_design_testability`
judges a design's Criterion Seams on shape — a prose seam, a count that does
not match the checklist, an unresolved symbol, an elided step. It never asks
where the code under test comes from. A Python seam like
`act: result = is_placeholder_path(input_val)` with no
`from coding_model_autonomous.workspace import is_placeholder_path` anywhere in
the seams passes, and the implementer is left to guess the import — which on
run 24 three different models guessed as `from src.…` and burned the rotation
on `ModuleNotFoundError` at collection. Every self-target spec since has
carried a hand-written rule demanding the line. This makes the check ask.

## Authoritative design — reproduce this in your design document

All edits are in `src/coding_model_autonomous/design_testability.py`. No other
source file changes.

1. Add, directly after the existing `KIND_TUPLE_CONFORMANCE = "tuple_conformance"`
   line, a new kind: `KIND_SEAM_NO_IMPORT = "seam_no_import"`, preceded by a
   two-line comment naming DEV-661.

2. Add three module-level patterns and one set, placed immediately after the
   existing `_LABELS = ("setup", "act", "assert")` line:
   - `_IMPORT_RE = re.compile(r"\b(?:from\s+[\w.]+\s+import\s+\w|import\s+[\w.]+)")`
   - `_SRC_IMPORT_RE = re.compile(r"\b(?:from|import)\s+src\.")`
   - `_BARE_CALL_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*\(")`
   - `_PY_BUILTINS = frozenset("len isinstance list dict set str int float bool tuple range sorted any all print repr type getattr hasattr open enumerate zip map filter min max sum abs round".split())`

3. Add a module-level function `is_python_design(design_md: str) -> bool`,
   placed immediately after `parse_seams`. It returns True when the
   `## File Structure` section (via the existing `_section(design_md, FILE_STRUCTURE_HEADING)`)
   contains the text `.py`, else False.

4. Add a module-level function `_check_seam_imports(design_md: str, seams: list[Seam]) -> list[Finding]`,
   placed immediately after `_check_names_a_call`. Behaviour, in this order:
   - If `not is_python_design(design_md)`, return `[]` (Swift designs are out
     of scope: module-level visibility, no import in a seam).
   - Let `body = _section(design_md, SEAMS_HEADING)`.
   - If `_SRC_IMPORT_RE.search(body)`, return one `Finding(kind=KIND_SEAM_NO_IMPORT, criterion="", detail=...)`
     whose detail is exactly: `the Criterion Seams import through `src.`. There is no `src` package: in the test sandbox the repository's `src/` directory is the package root on `sys.path`. Import the code under test by its package name — `from <pkg>.<module> import <name>` — and never through `src.`.`
   - Else if `_IMPORT_RE.search(body)`, return `[]`.
   - Else if any seam has a match of `_BARE_CALL_RE` in `seam.act` or
     `seam.assert_` whose group 1 is not in `_PY_BUILTINS`, return one
     `Finding(kind=KIND_SEAM_NO_IMPORT, criterion="", detail=...)` whose detail
     is exactly: `the Criterion Seams call the code under test but never import it. Quote the package-name import line as shared setup above the seams — `from <pkg>.<module> import <name>`. In the test sandbox the repository's `src/` directory is the package root on `sys.path`; it is not a package, so never import through `src.`.`
   - Else return `[]`.

5. In `check_design_testability`, immediately before its final `return findings`
   (the one after the `for i, seam in enumerate(seams):` loop), insert
   `findings.extend(_check_seam_imports(design_md, seams))`. The early returns
   above that loop (no checklist, no seams section) are unchanged, so a design
   with no criteria or no seams never reaches this rule.

6. The test file imports the code under test by its PACKAGE name, and the
   design's Criterion Seams must quote this line verbatim as shared setup:
   `from coding_model_autonomous.design_testability import KIND_SEAM_NO_IMPORT, check_design_testability, is_python_design`.
   In the test sandbox the repository's `src/` directory is the package root
   on `sys.path`; it is NOT a package, so `from src.coding_model_autonomous…`
   fails at collection with `ModuleNotFoundError`. Never import through `src.`.

## Change surface

| Path | Action |
|---|---|
| `src/coding_model_autonomous/design_testability.py` | modify — add `KIND_SEAM_NO_IMPORT`, the patterns, `is_python_design`, `_check_seam_imports`; one line in `check_design_testability` |
| `tests/test_seam_no_import.py` | new test file |

## Protected paths — must not be modified

- `src/coding_model_server/` (all of it)
- `src/coding_model_client/`
- `src/coding_model_autonomous/executor.py`
- `src/coding_model_autonomous/outcome.py`
- All existing tests.

## Fixtures — the test file defines these five module-level strings verbatim

```python
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
```

(Inside `PY_DESIGN` the inner ```` ```text ```` fence is literal text of the
design; the outer triple single quotes delimit the Python string. The five
strings are exactly as above.)

## Acceptance criteria (hermetic pytest, no model calls, no runner, no network)

Let `kinds(d) = [f.kind for f in check_design_testability(d)]`.

- **T1** — `KIND_SEAM_NO_IMPORT == "seam_no_import"`.
- **T2** — `check_design_testability(PY_DESIGN) == []` (a design that imports the code under test as shared setup has no findings at all).
- **T3** — `kinds(PY_DESIGN_NO_IMPORT) == ["seam_no_import"]`, and that finding's `detail` contains `"never import it"`, `"from <pkg>.<module> import <name>"` and `"never import through `src.`"`, and its `criterion == ""`.
- **T4** — `kinds(PY_DESIGN_SRC_IMPORT) == ["seam_no_import"]`, and that finding's `detail` contains `"There is no `src` package"`.
- **T5** — `check_design_testability(PY_DESIGN_IMPORT_IN_SEAM) == []` (an import inside a seam's own setup counts).
- **T6** — `check_design_testability(SWIFT_DESIGN) == []` and `is_python_design(SWIFT_DESIGN) is False`.
- **T7** — `is_python_design(PY_DESIGN) is True`.
- **T8** — a Python design with no `## Acceptance Criteria Checklist` section and no `## Criterion Seams` section (take `PY_DESIGN` and cut it at the line `## Acceptance Criteria Checklist`, keeping everything above) yields `check_design_testability(...) == []`.

## Constraints

- No new dependencies. No changes to `Finding`, `Seam`, `parse_seams`,
  `parse_checklist`, `_section`, or any existing kind constant or rule.
- The plan must carry the `repo` and `protected_paths` keys exactly as written
  below, so the existing file is fetched at `base_ref`.

## test_strategy

```yaml
repo: coding-model-server
framework: pytest
required: true
protected_paths:
  - src/coding_model_server/
  - src/coding_model_client/
  - src/coding_model_autonomous/executor.py
  - src/coding_model_autonomous/outcome.py
```
