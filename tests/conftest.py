"""Pytest bootstrap.

The project is normally installed editable (``pip install -e .``), so the
``coding_model_server`` / ``coding_model_client`` / ``coding_model_autonomous`` packages import without
help. As a fallback for a bare checkout, put ``src/`` on the path too so the
suite runs regardless of install state.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# mac_runner lives at the repo root, outside src/, and is deliberately not in
# the installed package set (it deploys by git checkout on the Mac, not by
# pip). CI installs the package and runs pytest from the repo root, where
# nothing puts the root on sys.path — so without this, every mac_runner test
# module dies at collection with ModuleNotFoundError.
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# The suite drives the app through starlette's TestClient — a loopback client —
# with no ADMIN_API_KEY configured. Since DEV-127 that combination is an
# explicit opt-in enforced at lifespan startup, so opt the whole suite in here.
# Tests that assert the enforcement itself remove this var via monkeypatch.
os.environ.setdefault("CODING_MODEL_ALLOW_UNAUTH", "1")


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

# On zooshly 127.0.0.1:5050 is the reverse tunnel to the live Mac runner, and
# test_runner defaults MAC_RUNNER_URL to it. A test that drives the daemon
# without stubbing the runner therefore reads real repositories on the Mac —
# with no key it is refused (two tests did this on every merge-gate run until
# 2026-09-26), and with the key sourced it would be served. The fetch fails
# soft by design, so a raised connect error would vanish inside it; record the
# attempt instead and fail the test at teardown.
#
# The inference server on :5000 is guarded the same way. It serves the live
# pipeline, so a test whose stub misses call_agent sends a real completion to
# whatever model a run has loaded; the server answers a busy model with 503 and
# a retry schedule, so without the guard such a test hangs for minutes instead
# of failing. The suite's own app runs through TestClient, which never opens a
# socket, so no legitimate test connects to either port.
class LiveServiceBlocked(RuntimeError):
    """Not an OSError on purpose: post_chat_completion retries connection
    errors on a 10/30/60 s backoff, so a refused connect would make a leaking
    test crawl for minutes instead of failing at once."""


_RUNNER_PORT = 5050
_LIVE_PORTS = {
    _RUNNER_PORT: ("the live Mac runner",
                   "stub test_runner.fetch_repo_files or patch MAC_RUNNER_URL"),
    int(os.getenv("CODING_MODEL_SERVER_PORT", "5000")): (
        "the live inference server",
        "stub the agent call where it is made: _http.post_chat_completion, "
        "or the calling module's own name for it"),
}
_live_connects: list = []
_real_connect = socket.socket.connect


def _guarded_connect(self, address):
    if isinstance(address, tuple) and len(address) >= 2 \
            and address[1] in _LIVE_PORTS:
        _live_connects.append(address)
        what = _LIVE_PORTS[address[1]][0]
        # The runner fetch fails soft by design, and tests exercise that
        # path, so the runner port keeps a plain refused connect.
        if address[1] == _RUNNER_PORT:
            raise ConnectionRefusedError(f"tests must not reach {what} at {address}")
        raise LiveServiceBlocked(f"tests must not reach {what} at {address}")
    return _real_connect(self, address)


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


# ── shared harnesses ─────────────────────────────────────────────────────────
# Imports of the packages under test stay inside the fixtures: tests/seams
# depends on Config being imported before the daemon, and a module-level
# import here would run first for every test directory. Helpers a test module
# imports by name live in fixture_files.py and chat_harness.py, not here:
# ``from conftest import ...`` resolves to whichever conftest.py pytest loaded
# last, which is tests/seams/conftest.py once that directory has collected.


@pytest.fixture
def db(tmp_path):
    """A fresh task database under tmp_path, closed at teardown.

    A module or class that needs a different database defines its own ``db``,
    which overrides this one."""
    from coding_model_autonomous.db import Database
    database = Database(db_path=tmp_path / "t.sqlite",
                        workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def runner_client(tmp_path, monkeypatch):
    """Factory for a TestClient on the Mac runner app, keyed ``test-key``.

    ``runner_client(repo)`` registers ``repo`` as project ``proj`` in
    tmp_path/repos.yml; with no repo, REPOS_FILE points there but nothing is
    written. Keyword arguments patch further Config attributes, e.g.
    ``SANDBOX=False`` — each test module states exactly what it needs."""
    from fastapi.testclient import TestClient
    from mac_runner import server
    from mac_runner.config import Config

    def make(repo=None, **config):
        repos_file = tmp_path / "repos.yml"
        if repo is not None:
            repos_file.write_text(f"repos:\n  proj:\n    path: {repo}\n")
        monkeypatch.setattr(Config, "REPOS_FILE", repos_file)
        monkeypatch.setattr(Config, "API_KEY", "test-key")
        for name, value in config.items():
            monkeypatch.setattr(Config, name, value)
        return TestClient(server.app)

    return make
