"""Crash recovery must count crashes, not reviews — DEV-558.

`_process_executing`'s RUNNING-recovery cap read `task.retry_count`, which human gate
rejections also increment. So the counter meant "sent back for any reason at
all" while the cap meant "this task keeps crashing the daemon".

Run 12 died on the difference. Five design-gate rejections — every one a
substantive review that improved the design — left retry_count at 5. Then a
manual `systemctl restart` landed mid-generation, the orchestrator could not
drain a 9-minute architect call inside TimeoutStopSec=30 and was SIGKILLed,
and recovery reported:

    task task_94a85f70 crash-recovered 6 times (MAX_RETRIES=5) — failing the
    spec instead of looping

It had crash-recovered once.

The two counts are not merely mixed, they run opposite to the cap's purpose: a
spec accumulating retries is one under heavy human review, which is the LEAST
likely to be a crash loop and the MOST expensive to throw away. Past five
rejections, any restart at all was fatal.

DEV-193's loop protection still has to work, so these tests pin both halves.
"""
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.executor import MAX_RETRIES
from coding_model_autonomous.models import (
    EventKind, SpecStatus, TaskStatus,
)


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def spec_task(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    task = db.create_task(spec_id=spec.id, agent="q36_architect",
                          role="architect", title="design")
    db.update_task_status(task.id, TaskStatus.RUNNING)
    return db.get_spec(spec.id), db.get_task(task.id)


def _reject(db, task_id, times=1):
    """What a human design-gate rejection does to the counter."""
    for _ in range(times):
        db.increment_task_retry(task_id)


def _crash(db, spec, task, times=1):
    """Drive real crash recovery, leaving the task RUNNING for the next round."""
    for _ in range(times):
        db.update_task_status(task.id, TaskStatus.RUNNING)
        d._process_executing(db, db.get_spec(spec.id))
        if db.get_spec(spec.id).status is SpecStatus.FAILED:
            return


# ── the counter ──────────────────────────────────────────────────────────────

def test_a_fresh_task_has_used_no_recoveries(db, spec_task):
    spec, task = spec_task
    assert d._crash_recoveries_used(db, spec.id, task.id) == 0


def test_rejections_do_not_count_as_recoveries(db, spec_task):
    """The whole defect, in one assertion."""
    spec, task = spec_task
    _reject(db, task.id, times=5)
    assert db.get_task(task.id).retry_count == 5
    assert d._crash_recoveries_used(db, spec.id, task.id) == 0


def test_each_recovery_counts_once(db, spec_task):
    spec, task = spec_task
    _crash(db, spec, task, times=3)
    assert d._crash_recoveries_used(db, spec.id, task.id) == 3


def test_another_tasks_recoveries_are_not_charged_to_this_one(db, spec_task):
    """The counter is per-task; a spec's implementer crashing must not spend
    the architect's budget."""
    spec, task = spec_task
    other = db.create_task(spec_id=spec.id, agent="deep_implementer",
                           role="implementer", title="build")
    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=other.id,
                    payload={"role": "crash_recovery", "model_call": False})
    assert d._crash_recoveries_used(db, spec.id, task.id) == 0
    assert d._crash_recoveries_used(db, spec.id, other.id) == 1


def test_real_model_calls_are_not_counted(db, spec_task):
    spec, task = spec_task
    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": "architect", "model_call": True,
                             "duration_ms": 486462})
    assert d._crash_recoveries_used(db, spec.id, task.id) == 0


# ── the behaviour ────────────────────────────────────────────────────────────

def test_run_12s_shape_survives(db, spec_task):
    """Five human rejections plus ONE crash must not fail the spec."""
    spec, task = spec_task
    _reject(db, task.id, times=MAX_RETRIES)
    _crash(db, spec, task, times=1)

    assert db.get_spec(spec.id).status is SpecStatus.EXECUTING
    assert db.get_task(task.id).status is TaskStatus.PENDING, \
        "the interrupted pass is requeued, not discarded"


def test_a_heavily_reviewed_spec_survives_many_restarts(db, spec_task):
    """Rejections must not erode the crash budget at all, however many."""
    spec, task = spec_task
    _reject(db, task.id, times=20)
    _crash(db, spec, task, times=MAX_RETRIES - 1)

    assert db.get_spec(spec.id).status is SpecStatus.EXECUTING
    assert d._crash_recoveries_used(db, spec.id, task.id) == MAX_RETRIES - 1


def test_a_genuine_crash_loop_still_fails_the_spec(db, spec_task):
    """DEV-193's protection is the reason the cap exists — it must survive."""
    spec, task = spec_task
    _crash(db, spec, task, times=MAX_RETRIES + 1)

    assert db.get_spec(spec.id).status is SpecStatus.FAILED
    assert db.get_task(task.id).status is TaskStatus.FAILED


def test_the_loop_fails_on_the_same_round_as_before(db, spec_task):
    """Not one round later: recovery N is allowed, the cap fires when a task
    that has already been recovered MAX_RETRIES times is found RUNNING again."""
    spec, task = spec_task
    _crash(db, spec, task, times=MAX_RETRIES)
    assert db.get_spec(spec.id).status is SpecStatus.EXECUTING

    _crash(db, spec, task, times=1)
    assert db.get_spec(spec.id).status is SpecStatus.FAILED


def test_recovery_still_advances_retry_count(db, spec_task):
    """Rotation and the parse-failure path read it; only the CAP moved off."""
    spec, task = spec_task
    before = db.get_task(task.id).retry_count
    _crash(db, spec, task, times=1)
    assert db.get_task(task.id).retry_count == before + 1


def test_the_recovery_event_records_what_it_spent(db, spec_task):
    """So an operator reading an incident sees crashes and reviews separately —
    the old log line said 'crash-recovered 6 times' about one crash."""
    import json
    spec, task = spec_task
    _reject(db, task.id, times=4)
    _crash(db, spec, task, times=1)

    events = db.list_events_by_kind(spec_id=spec.id, kind=EventKind.AGENT_RAN,
                                    limit=50)
    payloads = [json.loads(e.payload_json or "{}") for e in events]
    rec = [p for p in payloads if p.get("role") == "crash_recovery"]
    assert len(rec) == 1
    assert rec[0]["recovery"] == 1, "one crash"
    assert rec[0]["retry_count"] == 5, "five sends-back, four of them reviews"
    assert rec[0]["model_call"] is False


# ── tasks that predate this change ───────────────────────────────────────────

def test_an_unmarked_history_reads_as_zero_recoveries(db, spec_task):
    """A task recovered before this shipped carries no marker. Under-counting
    errs toward tolerance, which is the safe direction — the alternative is
    discarding work on a count we cannot substantiate."""
    spec, task = spec_task
    _reject(db, task.id, times=MAX_RETRIES + 3)
    assert d._crash_recoveries_used(db, spec.id, task.id) == 0
    _crash(db, spec, task, times=1)
    assert db.get_spec(spec.id).status is SpecStatus.EXECUTING


def test_a_broken_payload_does_not_crash_the_counter(db, spec_task):
    spec, task = spec_task
    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": "crash_recovery", "model_call": False})
    with mock.patch.object(db, "list_events_by_kind", side_effect=RuntimeError):
        assert d._crash_recoveries_used(db, spec.id, task.id) == 0
