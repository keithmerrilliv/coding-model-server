# Build-failure feedback names the import root when a test imports through `src.` (DEV-660, run 32)

## Context

Target repo: **coding-model-server** (self). When a sandbox pytest run fails at
collection with `ModuleNotFoundError: No module named 'src.coding_model_autonomous.workspace'`,
`outcome.classify_test_run` correctly returns a `BUILD_FAILURE` verdict: the
top-level segment `src` is not one of the repository's packages, so the model
made a mistake. But the feedback the charged retry receives is the diagnostic
verbatim — *a module named `src.coding_model_autonomous.workspace` is missing*.
On run 24 three consecutive implementers read that literally and "fixed" the
module (one deleted a real import from `workspace.py`) when the only defect was
the test's import line. The sandbox puts the repository's `src/` directory on
`sys.path`, so `src/` is the package root and is not a package.

This change makes the feedback name the cause. Nothing else changes: the class
stays `BUILD_FAILURE`, the charge stays, the diagnostic stays beneath the hint.

## Authoritative design — reproduce this in your design document

All edits are in `src/coding_model_autonomous/outcome.py`. No other source
file changes.

1. Add, directly after the existing module-level
   `_MISSING_MODULE_RE = re.compile(r"No module named '([\w.]+)'")`, a second
   pattern:
   `_SRC_IMPORT_RE = re.compile(r"No module named 'src\.([A-Za-z_]\w*)")`.

2. Add a module-level function `import_root_hint(text: str) -> str`, placed
   immediately after `classify_test_run`. It searches `text or ""` with
   `_SRC_IMPORT_RE`. If there is no match it returns `""`. If there is a match,
   `pkg` is group 1 and it returns exactly this paragraph (one string, no
   leading or trailing whitespace, `{pkg}` substituted):

   ```
   The import root is wrong, not the module. The test sandbox puts the repository's `src/` directory on `sys.path`, so `src/` is the package root and is not a package: `src.{pkg}` does not exist. Import the code under test by its package name — `from {pkg}.<module> import <name>` — and fix the test's import line. Do not change the module's own imports; nothing is missing from `{pkg}`.
   ```

3. Add a module-level function `with_import_root_hint(failure: Failure) -> Failure`,
   placed immediately after `import_root_hint`. Behaviour:
   - If `failure.cls is not FailureClass.BUILD_FAILURE`, return `failure` unchanged.
   - Compute `hint = import_root_hint(failure.feedback or failure.detail or "")`.
     If `hint` is empty, return `failure` unchanged.
   - If `(failure.feedback or "").startswith(hint)`, return `failure` unchanged
     (idempotent: applying it twice adds one hint).
   - Otherwise set `failure.feedback = hint + "\n\n" + (failure.feedback or failure.detail or "")`
     and return the same `failure` object (mutate in place; `Failure` is a
     mutable dataclass).

4. In `dispose`, in the section that begins with the comment
   `# implementer pays (its own failure, or one found at the reviewer stage)`,
   insert the single line `failure = with_import_root_hint(failure)` immediately
   after the `reviewer_task` resolution (the `if reviewer_task is None:` block
   that lists the reviewer tasks) and immediately before the comment block that
   begins `# DEV-631: the rotation's one lever is the model.` No other line of
   `dispose` changes. The retry's rejected `CODE_REVIEW` gate is created later
   in that same section with `notes=failure.feedback or failure.detail or heading`,
   so the hint reaches the retry through the existing path.

5. The test file imports the code under test by its PACKAGE name, and the
   design's Criterion Seams must quote these lines verbatim as shared setup:
   `from coding_model_autonomous.outcome import Failure, FailureClass, Hooks, classify_test_run, dispose, import_root_hint, with_import_root_hint`
   and, for T8 only, `from coding_model_autonomous.db import Database`,
   `from coding_model_autonomous import GateType, SpecStatus`.
   In the test sandbox the repository's `src/` directory is the package root
   on `sys.path`; it is NOT a package, so `from src.coding_model_autonomous…`
   fails at collection with `ModuleNotFoundError`. Never import through `src.`.

## Change surface

| Path | Action |
|---|---|
| `src/coding_model_autonomous/outcome.py` | modify — add `_SRC_IMPORT_RE`, `import_root_hint`, `with_import_root_hint`; one line in `dispose` |
| `tests/test_import_root_feedback.py` | new test file |

## Protected paths — must not be modified

- `src/coding_model_server/` (all of it)
- `src/coding_model_client/`
- `src/coding_model_autonomous/executor.py`
- `src/coding_model_autonomous/db.py`
- `src/coding_model_autonomous/models.py`
- All existing tests.

## Acceptance criteria (hermetic pytest, no model calls, no runner, no network)

Let `SRC_OUT = "E   ModuleNotFoundError: No module named 'src.coding_model_autonomous.workspace'"`
and `NUMPY_OUT = "E   ModuleNotFoundError: No module named 'numpy'"`.

- **T1** — `h = import_root_hint(SRC_OUT)` is non-empty and contains each of these substrings: `"import root is wrong"`, `"`coding_model_autonomous`"`, `"from coding_model_autonomous.<module> import <name>"`, `"is not a package"`, `"Do not change the module's own imports"`.
- **T2** — `import_root_hint(NUMPY_OUT) == ""`.
- **T3** — `import_root_hint("") == ""` and `import_root_hint("No module named 'src'") == ""` (a bare `src` names no package; only `src.<pkg>` does).
- **T4** — `classify_test_run(SRC_OUT, role="implementer", passed=False, build_reason=SRC_OUT, unreachable=False, packages=("coding_model_autonomous",))` returns a `Failure` whose `cls is FailureClass.BUILD_FAILURE`; the same call with `NUMPY_OUT` in both positions also returns `cls is FailureClass.BUILD_FAILURE`.
- **T5** — `f = Failure(FailureClass.BUILD_FAILURE, "implementer", "build_check", SRC_OUT, feedback="## The code does not compile\n\n" + SRC_OUT)`; after `g = with_import_root_hint(f)`: `g is f`, `f.feedback.startswith(import_root_hint(SRC_OUT))`, `f.feedback.endswith(SRC_OUT)`, and `"## The code does not compile" in f.feedback`. Applying `with_import_root_hint(f)` a second time leaves `f.feedback` unchanged.
- **T6** — a `Failure(FailureClass.TESTS_FAILED, "implementer", "tests", SRC_OUT, feedback=SRC_OUT)` passed through `with_import_root_hint` has `feedback == SRC_OUT` (unchanged: only build failures carry the hint).
- **T7** — a `Failure(FailureClass.BUILD_FAILURE, "implementer", "build_check", NUMPY_OUT, feedback=NUMPY_OUT)` passed through `with_import_root_hint` has `feedback == NUMPY_OUT`.
- **T8** — end to end through `dispose`, using a temporary database exactly as `tests/test_outcome_classifier.py` does: `db = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")`; `spec = db.create_spec(title="t", source_md_path="spec.md", status=SpecStatus.EXECUTING)`; `impl = db.create_task(spec_id=spec.id, agent="implementer", role="implementer", title="implement")`; `hooks = Hooks(max_retries=lambda: 5, synthesize=None, supervisor=None, reviewer_parse_retries=lambda: 1)`; `f = Failure(FailureClass.BUILD_FAILURE, "implementer", "build_check", SRC_OUT, feedback=SRC_OUT)`; `d = dispose(db, spec, db.get_task(impl.id), f, hooks)`. Then `d.action == "charge"`, and `gates = db.list_gates_for_spec(spec.id, GateType.CODE_REVIEW)` has exactly one gate whose `reviewer_notes.startswith(import_root_hint(SRC_OUT))` and whose `reviewer_notes.endswith(SRC_OUT)`. Call `db.close_all()` at the end.

## Constraints

- No new dependencies. No DB schema changes. No daemon changes. No changes to
  `classify_test_run`, `Failure`, `_record`, `terminate`, or any event payload.
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
  - src/coding_model_autonomous/db.py
  - src/coding_model_autonomous/models.py
```
