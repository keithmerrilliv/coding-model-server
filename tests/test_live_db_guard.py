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
