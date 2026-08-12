"""DEV-193 — crash-recovery resets must count against the retry budget.

A task found RUNNING at tick time means the daemon crashed mid-agent-call.
The old recovery reset it straight to PENDING without touching retry_count,
so a task whose agent call deterministically crashes the daemon looped
RUNNING → crash → systemd restart → PENDING forever; restarts more than 60s
apart never trip StartLimitBurst, so nothing bounded the loop.

Recovery still burns a retry, but the MAX_RETRIES cap is applied to
recovery's own count, not retry_count (DEV-558) — retry_count is also
incremented by human gate rejections, and charging reviews against the
crash budget killed run 12. Genuine crash loops past the budget still
fail the spec; that is what this file pins.
"""
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import SpecStatus, TaskStatus


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


def _running_spec_task(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md",
                          status=SpecStatus.EXECUTING)
    task = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="impl demo")
    db.update_task_status(task.id, TaskStatus.RUNNING)
    return db.get_spec(spec.id), db.get_task(task.id)


def test_crash_recovery_burns_a_retry(db):
    spec, task = _running_spec_task(db)

    d._process_executing(db, spec)

    fresh = db.get_task(task.id)
    assert fresh.status is TaskStatus.PENDING, "task is re-runnable next tick"
    assert fresh.retry_count == 1, "the recovery must count against the budget"
    assert db.get_spec(spec.id).status is SpecStatus.EXECUTING


def test_crash_recovery_past_budget_fails_the_spec(db):
    spec, task = _running_spec_task(db)
    # Burn the whole budget with GENUINE recoveries — inflating retry_count
    # no longer counts, that is DEV-558's point.
    for _ in range(d.MAX_RETRIES):
        d._process_executing(db, db.get_spec(spec.id))
        db.update_task_status(task.id, TaskStatus.RUNNING)

    d._process_executing(db, db.get_spec(spec.id))

    assert db.get_task(task.id).status is TaskStatus.FAILED
    assert db.get_spec(spec.id).status is SpecStatus.FAILED, (
        "a deterministic daemon-crasher must not loop forever"
    )


def test_recovered_task_is_not_restarted_in_the_same_tick(db):
    # The reset tick must only reset; the task starts on the NEXT tick with
    # its incremented retry_count (rotation picks a different model).
    spec, _task = _running_spec_task(db)
    with mock.patch.object(d, "_start_task") as start:
        d._process_executing(db, spec)
    start.assert_not_called()
