# The pytest test counter counts `async def test_…` (DEV-911, the v0.6.0 proof run)

## Context

Target repo: **coding-model-server** (self). The pipeline counts the test
declarations each attempt adds, and the human gate shows the result as
"Tests added: N". Since DEV-839 each language's counter lives in its
language pack. The Python pack's counter, in
`src/coding_model_autonomous/languages/python.py`, recognises only
`def test_…(`: an `async def test_…(` (pytest-asyncio, anyio) counts as
zero. The archive holds 10 such declarations in 4 files, each missing from
its attempt's count. `delivery.test_names` already accepts `async def`.

This change makes the counter accept an optional `async` before `def`.
Nothing else changes.

## Authoritative design — reproduce this in your design document

All source edits are in `src/coding_model_autonomous/languages/python.py`.
No other source file changes.

1. Replace the one existing line

```python
_PYTEST_TEST_RE = re.compile(r'^\s*def\s+test_\w+\s*\(')
```

with these three lines (a comment, then the pattern with an optional
`async` prefix):

```python
# `async def test_…` is a test too (pytest-asyncio, anyio); delivery's
# test_names already reads it (DEV-911).
_PYTEST_TEST_RE = re.compile(r'^\s*(?:async\s+)?def\s+test_\w+\s*\(')
```

That line is unique in the file, so it is the edit's anchor. No other line
of `python.py` changes: `_count_pytest_tests` and `PythonPack` stay exactly
as they are.

2. The test file imports the code under test by its PACKAGE name, and the
design's Criterion Seams must quote these lines verbatim as shared setup:

```python
from coding_model_autonomous.test_runner import count_test_declarations, declaration_delta
from coding_model_autonomous.languages import pack_for_framework
```

In the test sandbox the repository's `src/` directory is the package root
on `sys.path`. It is NOT a package, so `from src.coding_model_autonomous…`
fails at collection with `ModuleNotFoundError`. Never import through `src.`.

## Change surface

| Path | Action |
|---|---|
| `src/coding_model_autonomous/languages/python.py` | modify — the `_PYTEST_TEST_RE` line becomes a comment and the pattern with an optional `async` |
| `tests/test_pytest_async_count.py` | new test file |

## Protected paths — must not be modified

- `src/coding_model_server/` (all of it: the daemon and the server)
- `src/coding_model_client/`
- `src/coding_model_autonomous/test_runner.py`
- `src/coding_model_autonomous/workspace.py`
- `src/coding_model_autonomous/outcome.py`
- `src/coding_model_autonomous/context.py`
- `src/coding_model_autonomous/retry_policy.py`
- `src/coding_model_autonomous/executor.py`
- `src/coding_model_autonomous/db.py`
- `src/coding_model_autonomous/models.py`
- `src/coding_model_autonomous/languages/__init__.py`
- `src/coding_model_autonomous/languages/base.py`
- `src/coding_model_autonomous/languages/swift/`
- All existing tests.

## Acceptance criteria (hermetic pytest, no model calls, no runner, no network)

Each source string below is written with `\n` line breaks; build it exactly.

- **T1** — `count_test_declarations("async def test_x():\n    pass\n", "pytest") == 1`.
- **T2** — `count_test_declarations("def test_a():\n    pass\n\nasync def test_b():\n    pass\n\nclass TestC:\n    async def test_c(self):\n        pass\n", "pytest") == 3` (a top-level `def`, a top-level `async def`, and an indented `async def` method).
- **T3** — `count_test_declarations("# async def test_old():\ndef test_new():\n    pass\n", "pytest") == 1` (a commented-out declaration does not count).
- **T4** — `count_test_declarations('"""\nasync def test_in_docstring():\n"""\ndef test_real():\n    pass\n', "pytest") == 1` (a declaration inside a triple-quoted string does not count).
- **T5** — `count_test_declarations("async def helper():\n    pass\n\nasync def fixture_test_x():\n    pass\n", "pytest") == 0` (only names starting `test_` count).
- **T6** — `declaration_delta({"t.py": "def test_a():\n    pass\n"}, {"t.py": "def test_a():\n    pass\n\nasync def test_b():\n    pass\n"}, "pytest") == {"t.py": 1}`.
- **T7** — `pack_for_framework("pytest").count_tests("async def test_x():\n    pass\n") == 1` (the Python pack itself counts it, not only the framework dispatch).
- **T8** — `count_test_declarations("def test_a():\n    pass\n", "pytest") == 1` (a plain `def` still counts, unchanged).

## Constraints

- No new dependencies. No DB schema changes. No daemon changes. No change
  to `_count_pytest_tests`, `PythonPack`, `count_test_declarations` or
  `declaration_delta`.
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
  - src/coding_model_autonomous/test_runner.py
  - src/coding_model_autonomous/workspace.py
  - src/coding_model_autonomous/outcome.py
  - src/coding_model_autonomous/context.py
  - src/coding_model_autonomous/retry_policy.py
  - src/coding_model_autonomous/executor.py
  - src/coding_model_autonomous/db.py
  - src/coding_model_autonomous/models.py
  - src/coding_model_autonomous/languages/__init__.py
  - src/coding_model_autonomous/languages/base.py
  - src/coding_model_autonomous/languages/swift/
```
