# The design file count reads the File Structure, not the whole document (DEV-643, run 25)

## Context

Target repo: **coding-model-server** (self). `estimate_design_file_count` in
`src/coding_model_autonomous/executor.py` scans the ENTIRE design document
with `_DESIGN_FILE_PATH_RE` and de-duplicates every token that looks like a
path with a code extension. Since DEV-481 every design carries a
`## Criterion Seams` section whose test fixtures name paths (`src/a.py`,
`empty.txt`, `test_output.txt`, …), so run 24's two-file design counted as
"~9 files", crossed `MANIFEST_FILE_THRESHOLD` (8) and was dispatched in
manifest mode — the fragile path — for a single-call change. The count also
sizes the implementer's output budget through `implementer_max_tokens_for`.

## Authoritative design — reproduce this in your design document

1. Add a module-level helper `_file_structure_section(design_md: str) -> str | None`
   to `src/coding_model_autonomous/executor.py`. It returns the text of the
   design's `## File Structure` section — from a line that starts with
   `## File Structure` (case-insensitive, any number of `#` from 2 to 4,
   optional trailing text) up to the next line that starts with `## ` (a
   heading of level 2 or shallower) or the end of the document — or `None`
   when the design has no such heading.
2. `estimate_design_file_count` counts paths with the existing
   `_DESIGN_FILE_PATH_RE` over the File Structure section ONLY when the
   helper returns a section that contains at least one match; otherwise it
   counts over the whole document exactly as today. The de-duplication and
   the `strip("./")` normalisation are unchanged.
3. `estimate_design_unit_count`, `use_manifest_mode`,
   `implementer_max_tokens_for` and `_DESIGN_FILE_PATH_RE` are not modified;
   they pick up the narrower count through the existing call.
4. Nothing else in the module changes. Its imports stay exactly as they are.
5. The test file imports the code under test by its PACKAGE name, and the
   design's Criterion Seams must quote this line verbatim:
   `from coding_model_autonomous.executor import estimate_design_file_count, use_manifest_mode`.
   In the test sandbox the repository's `src/` directory is the package root
   on `sys.path`; it is NOT a package, so `from src.coding_model_autonomous…`
   fails at collection with `ModuleNotFoundError` (run 24 lost four attempts
   to exactly that line). Never import through `src.`.

## Change surface

| Path | Action |
|---|---|
| `src/coding_model_autonomous/executor.py` | modify — add `_file_structure_section`; `estimate_design_file_count` scopes its scan to it when present |
| `tests/test_design_file_count_scope.py` | new test file |

## Protected paths — must not be modified

- `src/coding_model_server/` (all of it)
- `src/coding_model_client/`
- All existing tests.

## Acceptance criteria (hermetic pytest, no model calls, no runner, no database)

Tests begin with
`from coding_model_autonomous.executor import estimate_design_file_count, use_manifest_mode`
(package name, never `src.`). Where a test asserts on `use_manifest_mode`
it first pins the mode knobs with `monkeypatch.setattr` on the
`coding_model_autonomous.executor` module: `IMPLEMENTER_MODE` to `"auto"`
and `MANIFEST_FILE_THRESHOLD` to `8`.

Let SCOPED be a design string with a `## File Structure` section whose
fenced tree names exactly `src/pkg/alpha.py` and `tests/test_alpha.py`, and
a later `## Criterion Seams` section that mentions `src/a.py`, `src/b.py`,
`src/c.py`, `empty.txt`, `test_output.txt` and `other/d.py`. Let FLAT be the
same document with the `## File Structure` heading and its tree removed.

- C1: `estimate_design_file_count(SCOPED) == 2`.
- C2: `estimate_design_file_count(FLAT) == 6` — no File Structure section
  means the whole-document scan, as today.
- C3: with the knobs pinned, `use_manifest_mode(SCOPED)` is `False`.
- C4: a design whose `## File Structure` section is present but names no
  path with a known extension (for example a tree of directories only)
  falls back to the whole-document count: for such a document whose prose
  names three distinct `.py` paths the result is `3`.
- C5: the section boundary is the next level-2 heading: a `### Notes`
  sub-heading inside the File Structure section does NOT end it, so a path
  named under that sub-heading is counted; a path named after the next
  `## ` heading is not.
- C6: `estimate_design_file_count("")` and
  `estimate_design_file_count(None)` both return `0` (unchanged).

## Constraints

- No new dependencies. No DB schema changes. No change to the regex, to
  `estimate_design_unit_count` or to the manifest threshold.
- The spec needs a `test_strategy.repo` of `coding-model-server` so the
  existing file is fetched; the plan must carry the `repo` and
  `protected_paths` keys exactly as written below.

## test_strategy

```yaml
repo: coding-model-server
framework: pytest
required: true
protected_paths:
  - src/coding_model_server/
  - src/coding_model_client/
```
