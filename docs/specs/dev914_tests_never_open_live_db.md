# The test suite never opens the live task database (DEV-914)

## Context

Target repo: **coding-model-server** (self), `main` = `554fefc`. Every fact
below was read from that commit.

`src/coding_model_autonomous/db.py` resolves `Database()`'s default paths at
import from `CODING_MODEL_TASKS_DB` / `CODING_MODEL_TASKS_WORKSPACE`, falling
back to `var/tasks_db/tasks.sqlite` and `var/tasks_db/specs` under the
checkout. That is the database the live orchestrator daemon writes. Nothing in
the test suite changes those defaults, so when pytest runs from the main
checkout, any test that reaches a default `Database()` opens the production
database. On 2026-09-28 a merge-gate pytest run hung at 96% for over ten
minutes with one xdist worker holding `var/tasks_db/tasks.sqlite` (plus its
`-wal`/`-shm`) open.

The vector is confirmed. A probe that recorded every `sqlite3.connect` under
`var/tasks_db` across the whole suite caught exactly one test,
`tests/test_server_routes.py::test_health_ok`. `GET /health`
(`routes/meta.py`) calls `runtime.get_autonomous_db()`, which lazily builds
`Database()` at its default path and runs the schema bootstrap on it. The
route wraps that in `except Exception`, so the test passes either way.

`tests/conftest.py` already blocks tests from reaching the live services on
:5000 and :5050 (`LiveServiceBlocked`, `_no_live_services`). This change adds
the same protection for the live database, in two layers:

1. **Pin:** before any package under test is imported, conftest points both
   environment variables at a per-process scratch directory, removed at exit.
   It overrides any inherited value. A default `Database()` can then never
   resolve under `var/tasks_db`.
2. **Guard:** `sqlite3.connect` is wrapped so that a connect to any file under
   the checkout's `var/tasks_db` (plain path or `file:` URI) is recorded and
   refused. The autouse `_no_live_services` fixture fails the test at
   teardown. It records as well as raises because `/health` swallows the
   exception. A new `live_db_opens` fixture exposes the record so the guard's
   own tests can assert on it and clear it.

A reference implementation of exactly the edits and test file below ran on
Zooshly before this spec was written:

- the new test file: 6 passed with the edits; on unmodified `main`, T1 and T2
  fail and T3–T6 error (the `live_db_opens` fixture does not exist);
- the whole suite with the edits: 3412 passed, 20 skipped; ruff clean; the
  project mypy run (`python -m mypy`, configured for `src/`) clean;
- after the full suite, `var/tasks_db` holds no database file (T3 leaves
  only the empty directory that `Database.__init__` creates before it
  connects).

Do not re-derive any of this.

## Authoritative design — reproduce this in your design document

The only source edits are in `tests/conftest.py`. Emit them as the two anchored
SEARCH/REPLACE edits below, **never** the whole file. The new test file is NEW:
emit it as one whole-file block (`<<<FILE: tests/test_live_db_guard.py>>>`
… `END_FILE`), never as SEARCH/REPLACE.

1. **Edit A.** Replace the existing three lines

````python
import socket  # noqa: E402

import pytest  # noqa: E402
````

   with exactly:

````python
import atexit  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import sqlite3  # noqa: E402
import tempfile  # noqa: E402

import pytest  # noqa: E402

# db.py resolves Database()'s default paths from these two variables at import,
# falling back to var/tasks_db under the checkout — the database the live
# pipeline runs on. Any test that reaches a default Database() (/health does,
# through runtime.get_autonomous_db) would open it whenever the suite runs from
# the main checkout, holding its locks while the daemon writes. Pin both to a
# scratch directory per process (each xdist worker imports this file), and
# override rather than setdefault: an inherited live path is the case to defeat.
_TASKS_SCRATCH = tempfile.mkdtemp(prefix="cms-tests-tasks-")
atexit.register(shutil.rmtree, _TASKS_SCRATCH, ignore_errors=True)
os.environ["CODING_MODEL_TASKS_DB"] = os.path.join(_TASKS_SCRATCH, "tasks.sqlite")
os.environ["CODING_MODEL_TASKS_WORKSPACE"] = os.path.join(_TASKS_SCRATCH, "specs")
````

2. **Edit B.** Replace everything from the existing line
   `socket.socket.connect = _guarded_connect` through the end of the existing
   `_no_live_services` fixture (its last line is
   `            f"{fix}", pytrace=False)`) with exactly:

````python
socket.socket.connect = _guarded_connect

# The pin above covers every default path; this covers a test that names the
# live database outright. Like the socket guard it records the attempt and
# fails the test at teardown, because the store's one known caller in a test
# (/health) swallows every exception it raises.
_LIVE_DB_DIR = os.path.realpath(os.path.join(_ROOT, "var", "tasks_db"))
_live_db_opens: list = []
_real_sqlite_connect = sqlite3.connect


def _sqlite_target(database) -> str:
    """The resolved file *database* names, or "" when it names none."""
    try:
        name = os.fspath(database)
    except TypeError:
        return ""
    if isinstance(name, bytes):
        name = os.fsdecode(name)
    if name.startswith("file:"):
        name = name[len("file:"):].split("?", 1)[0]
    if not name or name == ":memory:":
        return ""
    return os.path.realpath(name)


def _guarded_sqlite_connect(database, *args, **kwargs):
    target = _sqlite_target(database)
    if target == _LIVE_DB_DIR or target.startswith(_LIVE_DB_DIR + os.sep):
        _live_db_opens.append(target)
        raise LiveServiceBlocked(
            f"tests must not open the live task database at {target}")
    return _real_sqlite_connect(database, *args, **kwargs)


sqlite3.connect = _guarded_sqlite_connect


@pytest.fixture(autouse=True)
def _no_live_services():
    _live_connects.clear()
    _live_db_opens.clear()
    yield
    if _live_connects:
        what, fix = _LIVE_PORTS[_live_connects[0][1]]
        pytest.fail(
            f"this test opened a connection to {what} ({_live_connects[0]}); "
            f"{fix}", pytrace=False)
    if _live_db_opens:
        pytest.fail(
            f"this test opened the live task database ({_live_db_opens[0]}); "
            f"use the db fixture, or pass Database a path under tmp_path",
            pytrace=False)


@pytest.fixture
def live_db_opens():
    """The live-database opens recorded so far in this test. A test of the
    guard itself asserts on it, then clears it so teardown stays quiet."""
    return _live_db_opens
````

   Everything after that point (the `# ── shared harnesses` comment, the `db`
   and `runner_client` fixtures) stays exactly as it is.

No other line of `tests/conftest.py` changes. `LiveServiceBlocked`,
`_LIVE_PORTS` and `_guarded_connect` are untouched. No file under `src/`
changes.

3. The test file imports the code under test by its PACKAGE name. In the test
   sandbox the repository's `src/` directory is the package root on
   `sys.path`; it is NOT a package, so never import through `src.`.

## The new test file, verbatim

````python
"""DEV-914: the suite never opens the live task database.

tests/conftest.py pins Database()'s default paths to a scratch directory and
fails any test that connects to a file under var/tasks_db. These prove both
halves, and that the db fixture and in-memory databases stay untouched.
"""
import os
import sqlite3
from pathlib import Path

import pytest

LIVE = Path(__file__).resolve().parents[1] / "var" / "tasks_db"


def _under_live(path) -> bool:
    p = os.path.realpath(path)
    live = os.path.realpath(LIVE)
    return p == live or p.startswith(live + os.sep)


def test_default_paths_are_not_the_live_ones():
    from coding_model_autonomous import db
    assert not _under_live(db.DEFAULT_DB_PATH)
    assert not _under_live(db.DEFAULT_WORKSPACE_ROOT)


def test_the_lazy_server_store_opens_the_scratch_database(monkeypatch):
    from coding_model_server import runtime
    monkeypatch.setattr(runtime, "_autonomous_db", None)
    store = runtime.get_autonomous_db()
    try:
        assert not _under_live(store.db_path)
        assert store.list_open_gates() == []
    finally:
        store.close_all()


def test_naming_the_live_database_is_blocked(tmp_path, live_db_opens):
    from coding_model_autonomous.db import Database
    probe = LIVE / "dev914-guard-probe.sqlite"
    with pytest.raises(RuntimeError, match="live task database"):
        Database(db_path=probe, workspace_root=tmp_path / "ws")
    assert live_db_opens == [os.path.realpath(probe)]
    assert not probe.exists()
    live_db_opens.clear()


def test_a_uri_naming_the_live_database_is_blocked(live_db_opens):
    probe = LIVE / "dev914-guard-probe.sqlite"
    with pytest.raises(RuntimeError, match="live task database"):
        sqlite3.connect(f"file:{probe}?mode=ro", uri=True)
    assert live_db_opens == [os.path.realpath(probe)]
    live_db_opens.clear()


def test_the_db_fixture_is_not_blocked(db, live_db_opens):
    assert db.list_open_gates() == []
    assert live_db_opens == []


def test_an_in_memory_database_is_not_blocked(live_db_opens):
    conn = sqlite3.connect(":memory:")
    try:
        assert conn.execute("select 1").fetchone() == (1,)
    finally:
        conn.close()
    assert live_db_opens == []
````

## Change surface

| Path | Action |
|---|---|
| `tests/conftest.py` | modify — pin the task-DB env vars to a scratch dir; guard `sqlite3.connect` against `var/tasks_db`; add the `live_db_opens` fixture |
| `tests/test_live_db_guard.py` | new test file |

## Protected paths — must not be modified

- `src/` (all of it)
- `mac_runner/`
- `tests/seams/`
- Every existing test file except `tests/conftest.py`.

## Acceptance criteria (hermetic pytest, no model calls, no runner, no network)

T1–T6 are the six tests in the file above, unchanged:

- **T1** — `DEFAULT_DB_PATH` and `DEFAULT_WORKSPACE_ROOT` resolve outside `var/tasks_db`.
- **T2** — the server's lazy store (`runtime.get_autonomous_db`) opens the scratch database.
- **T3** — `Database(db_path=<var/tasks_db/...>)` is refused and recorded, and no file is created.
- **T4** — a `file:` URI naming a file under `var/tasks_db` is refused and recorded.
- **T5** — the `db` fixture is not blocked.
- **T6** — an in-memory database is not blocked.

`tests/test_server_routes.py::test_health_ok` passes unchanged, and its
teardown does not fail. The whole existing suite stays green with no `--deselect`.

## Constraints

- No new dependencies. No DB schema changes. No change under `src/`.
- The plan must carry the `repo` and `protected_paths` keys exactly as
  written below, so the existing file is fetched at `base_ref`.

## test_strategy

```yaml
repo: coding-model-server
framework: pytest
required: true
protected_paths:
  - src/
  - mac_runner/
  - tests/seams/
```
