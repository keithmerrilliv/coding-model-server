"""Concurrent-writer behavior for the autonomous task DB (DEV-26).

The daemon and the FastAPI server each hold their own connection to this SQLite
file. A read-then-write transaction under plain BEGIN (DEFERRED) could hit
SQLITE_BUSY_SNAPSHOT — not retried by busy_timeout — and surface as
"database is locked". BEGIN IMMEDIATE + busy_timeout makes contention a
retryable wait instead.
"""
import threading

import pytest

from coding_model_autonomous.db import Database
from coding_model_autonomous.models import EventKind


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "t.sqlite"


def test_transaction_uses_immediate(db_path, tmp_path):
    """The transaction issues BEGIN IMMEDIATE, not a bare (DEFERRED) BEGIN.

    Asserted by behavior: after `BEGIN IMMEDIATE` a second connection's write
    lock acquisition blocks, so a zero-timeout writer on another connection gets
    OperationalError. A DEFERRED begin would NOT hold the write lock yet, so that
    second writer would succeed — which is exactly the difference we rely on.
    """
    import sqlite3

    db = Database(db_path=db_path, workspace_root=tmp_path / "ws")
    db.create_spec(title="t", source_md_path="s.md")  # ensure the file exists

    other = sqlite3.connect(db_path, isolation_level=None, timeout=0)
    with db.transaction() as _c:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("BEGIN IMMEDIATE")  # write lock already held by us
    other.close()
    db.close_all()


def test_busy_timeout_is_set(db_path, tmp_path):
    db = Database(db_path=db_path, workspace_root=tmp_path / "ws")
    (val,) = db._conn().execute("PRAGMA busy_timeout").fetchone()
    assert val >= 5000
    db.close_all()


def test_concurrent_writers_do_not_raise_busy_snapshot(db_path, tmp_path):
    """Several threads, each with its own connection (Database keeps thread-local
    connections), all writing. Under the old DEFERRED path this could raise
    'database is locked'; with IMMEDIATE + busy_timeout it must serialize
    cleanly."""
    db = Database(db_path=db_path, workspace_root=tmp_path / "ws")
    spec = db.create_spec(title="t", source_md_path="s.md")
    errors = []

    def worker(n):
        try:
            w = Database(db_path=db_path, workspace_root=tmp_path / "ws")
            for i in range(25):
                w.record_event(EventKind.AGENT_RAN, spec_id=spec.id,
                               payload={"t": n, "i": i})
            w.close_all()
        except Exception as e:  # noqa: BLE001 - collect for assertion
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent writers raised: {errors}"
    assert db.count_events(spec_id=spec.id, kind=EventKind.AGENT_RAN) == 100
    db.close_all()
