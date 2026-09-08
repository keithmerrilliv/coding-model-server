# Whole-file artifacts keep their trailing newline (DEV-641, run 24b)

## Context

Target repo: **coding-model-server** (self). The implementer's whole-file
`<<<FILE: path>>> … <<<END_FILE>>>` blocks are parsed by a regex whose
surrounding `\s*` strips the file's final newline along with the marker
whitespace, and nothing puts it back — so every whole-file artifact the
pipeline writes ends mid-line (`\ No newline at end of file` in every diff,
ruff W292 on every delivered branch). Since DEV-642 every artifact write goes
through one door, `ArtifactLedger.write` (and `restore` for snapshot
restores and repair rollbacks) in `src/coding_model_autonomous/workspace.py`,
so the fix belongs there. Edit-mode applies preserve whatever the base file
had and are unaffected. Diagnostics written through `note` are not artifacts
and must stay verbatim.

## Authoritative design — reproduce this in your design document

1. Add a module-level helper `_with_trailing_newline(content: str) -> str`
   to `src/coding_model_autonomous/workspace.py`. It returns `content`
   unchanged when `content` is empty or already ends with `"\n"`; otherwise
   it returns `content + "\n"`.
2. `ArtifactLedger.write` applies the helper to `content` exactly once, as
   the first statement of the method, before any guard, hash, line count or
   file write. The sha256 recorded on the ledger entry, on the artifacts row
   and on the returned `WriteOutcome` is therefore the sha256 of the bytes
   on disk.
3. `ArtifactLedger.restore` applies the helper the same way, as its first
   statement.
4. `ArtifactLedger.note` is unchanged: diagnostics are written verbatim.
5. Nothing else changes: guard order, guard thresholds, the ledger entry
   fields, `read_entries`, `attempt_files_from_ledger` and `renamed_path`
   keep their current behaviour. The module's own imports
   (`from .executor import _count_declarations, artifact_path` and
   `from .models import ArtifactKind`) stay exactly as they are.
6. The test file imports the code under test by its PACKAGE name, and the
   design's Criterion Seams must quote this line verbatim:
   `from coding_model_autonomous.workspace import ArtifactLedger, read_entries`.
   In the test sandbox the repository's `src/` directory is the package root
   on `sys.path`; it is NOT a package, so `from src.coding_model_autonomous…`
   fails at collection with `ModuleNotFoundError` (run 24 lost five attempts
   to exactly that line). Never import through `src.`.

## Change surface

| Path | Action |
|---|---|
| `src/coding_model_autonomous/workspace.py` | modify — add `_with_trailing_newline`; `write` and `restore` apply it first |
| `tests/test_whole_file_trailing_newline.py` | new test file |

## Protected paths — must not be modified

- `src/coding_model_server/` (all of it)
- `src/coding_model_client/`
- All existing tests.

## Acceptance criteria (hermetic pytest, tmp workspace, no model calls, no runner, no database)

Tests begin with
`from coding_model_autonomous.workspace import ArtifactLedger, read_entries`
(package name, never `src.`) and construct the ledger directly with no
database: `ArtifactLedger(None, "spec_t", tmp_path)` — `db=None` is
supported and skips the artifacts-table row. Use `hashlib.sha256` on the
on-disk bytes.

- T1: `write("src/a.py", "x = 1", role="implementer")` leaves the file with
  bytes `b"x = 1\n"`, and the returned outcome's `sha256` equals the sha256
  of those on-disk bytes.
- T2: `write("src/b.py", "y = 2\n", role="implementer")` leaves the bytes
  unchanged (`b"y = 2\n"`, exactly one newline).
- T3: `write("empty.txt", "", role="implementer")` leaves a zero-byte file.
- T4: `restore("src/c.py", "z = 3", role="implementer")` leaves
  `b"z = 3\n"` and its outcome `sha256` matches the on-disk bytes.
- T5: `note("test_output.txt", "4 passed")` writes exactly `b"4 passed"`
  with no newline added.
- T6: after T1, `read_entries(tmp_path)[0].lines == 1` and
  `read_entries(tmp_path)[0].sha256` equals the on-disk sha256.

## Constraints

- No new dependencies. No DB schema changes. No change to guard semantics.
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
