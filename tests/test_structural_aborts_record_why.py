"""DEV-652: a spec that ends leaves one row saying why.

Twelve sites failed a spec with a bare ``update_spec_status(..., FAILED)`` —
state-machine aborts (missing source markdown, an unparseable plan, no
phases, a supervisor decision that cannot be applied). They are genuinely not
agent failures and their routing is untouched. But a spec that died that way
left no ``failure_classified`` row at all, so "why did this spec fail" had two
different answers depending on which branch ended it, and DEV-532's invariant
(no task row left claiming to be in flight) was not upheld on those paths.

All twelve now go through ``outcome.terminate`` with ``FailureClass.ABORTED``.
"""
import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import SpecStatus, TaskStatus


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite",
                        workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


def _terminal_rows(db, spec_id):
    return [e.payload for e in db.list_events_by_kind(
                spec_id=spec_id, kind=d.EventKind.FAILURE_CLASSIFIED, limit=20)]


def _assert_one_row_saying_why(db, spec_id, *, phase, role, detail_has):
    spec = db.get_spec(spec_id)
    assert spec.status is SpecStatus.FAILED
    rows = _terminal_rows(db, spec_id)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["cls"] == "aborted"
    assert row["outcome"] == "terminal"
    assert row["disposition"] == "terminal"
    assert row["role"] == role
    assert row["source"] == "daemon"
    assert row["phase"] == phase
    assert detail_has in row["detail"]
    return row


def test_missing_source_markdown_records_why(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    db.spec_dir(spec.id).mkdir(parents=True, exist_ok=True)
    # spec.md deliberately never written

    d._process_pending_plan(db, db.get_spec(spec.id))

    _assert_one_row_saying_why(db, spec.id, phase="source_md", role="planner",
                               detail_has="source markdown missing")


def test_executing_without_a_plan_records_why(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    assert db.get_spec(spec.id).normalized_yaml is None

    d._bootstrap_tasks(db, db.get_spec(spec.id))

    _assert_one_row_saying_why(db, spec.id, phase="bootstrap", role="daemon",
                               detail_has="no normalized_yaml")


def test_a_structural_abort_closes_in_flight_tasks(db):
    """DEV-532's invariant now holds on these paths too: terminate() closes
    every task the bare status update used to leave RUNNING."""
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    db.spec_dir(spec.id).mkdir(parents=True, exist_ok=True)
    t = db.create_task(spec_id=spec.id, agent="planner", role="planner",
                       title="plan")
    db.update_task_status(t.id, TaskStatus.RUNNING)

    d._process_pending_plan(db, db.get_spec(spec.id))

    assert db.get_spec(spec.id).status is SpecStatus.FAILED
    assert db.get_task(t.id).status is TaskStatus.FAILED
    assert "closed 1 task(s)" in _terminal_rows(db, spec.id)[0]["disposition_detail"]
