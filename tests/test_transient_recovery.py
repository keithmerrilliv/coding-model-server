"""DEV-120: a transport error mid-inference must not permanently FAIL a spec.

_http retries transient ConnectionError/Timeout on a backoff, but if the server
stays down past the backoff window the exception still escapes. The daemon
callers used to `except Exception: update_spec_status(FAILED)`, discarding hours
of approved work over a redeploy blip. They now single out transport errors and
leave the work re-runnable (task→PENDING / spec stays PENDING_PLAN), mirroring
the existing RUNNING crash-recovery path. A genuine error still FAILs.
"""
from unittest import mock

import pytest
import requests

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import SpecStatus, TaskStatus


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


# ── _start_task ──────────────────────────────────────────────────────────────

def _executing_spec_with_task(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md",
                          status=SpecStatus.EXECUTING)
    task = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="impl demo")
    return db.get_spec(spec.id), db.get_task(task.id)


def test_start_task_transport_error_resets_task_and_keeps_spec_executing(db):
    spec, task = _executing_spec_with_task(db)
    with mock.patch.object(d, "_run_implementer",
                           side_effect=requests.ConnectionError("server restarting")):
        d._start_task(db, spec, task)
    # Task reset to PENDING for the next tick; spec NOT failed.
    assert db.get_task(task.id).status is TaskStatus.PENDING
    assert db.get_spec(spec.id).status is SpecStatus.EXECUTING


def test_start_task_read_timeout_is_also_recoverable(db):
    spec, task = _executing_spec_with_task(db)
    with mock.patch.object(d, "_run_implementer",
                           side_effect=requests.Timeout("read timed out")):
        d._start_task(db, spec, task)
    assert db.get_task(task.id).status is TaskStatus.PENDING
    assert db.get_spec(spec.id).status is SpecStatus.EXECUTING


def test_start_task_genuine_error_is_no_verdict(db):
    """DEV-629: an exception the daemon itself raises is a daemon fault, not a
    judgement on the code — the task is requeued with its budget untouched
    (a run of them parks it behind a gate; see the seam tier)."""
    spec, task = _executing_spec_with_task(db)
    with mock.patch.object(d, "_run_implementer",
                           side_effect=ValueError("unparseable model output")):
        d._start_task(db, spec, task)
    after = db.get_task(task.id)
    assert after.status is TaskStatus.PENDING and after.retry_count == 0
    assert db.get_spec(spec.id).status is SpecStatus.EXECUTING
    ev = db.list_events_by_kind(spec_id=spec.id, kind=d.EventKind.FAILURE_CLASSIFIED)
    assert ev[0].payload["cls"] == "unknown_exception"


def test_start_task_http_error_is_no_verdict(db):
    """A 4xx/5xx the server returned is transport shaping — nothing was
    judged (DEV-624 → DEV-629): requeued, never terminal."""
    spec, task = _executing_spec_with_task(db)
    with mock.patch.object(d, "_run_implementer",
                           side_effect=requests.HTTPError("500 persisted")):
        d._start_task(db, spec, task)
    after = db.get_task(task.id)
    assert after.status is TaskStatus.PENDING and after.retry_count == 0
    assert db.get_spec(spec.id).status is SpecStatus.EXECUTING
    ev = db.list_events_by_kind(spec_id=spec.id, kind=d.EventKind.FAILURE_CLASSIFIED)
    assert ev[0].payload["cls"] == "http_refusal"


# ── _process_pending_plan ────────────────────────────────────────────────────

def _pending_plan_spec(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md",
                          status=SpecStatus.PENDING_PLAN)
    spec_dir = db.spec_dir(spec.id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text("# demo spec\nbuild a thing\n")
    return db.get_spec(spec.id)


def test_pending_plan_transport_error_stays_pending_plan(db):
    spec = _pending_plan_spec(db)
    with mock.patch.object(d, "call_planner",
                           side_effect=requests.ConnectionError("server down")):
        d._process_pending_plan(db, spec)
    # Left re-runnable, not FAILED.
    assert db.get_spec(spec.id).status is SpecStatus.PENDING_PLAN


def test_pending_plan_genuine_error_is_no_verdict(db):
    """DEV-629: a planner call that raised produced no plan to judge — the
    spec stays PENDING_PLAN (the next tick re-runs the planner) and the
    fault is classified."""
    spec = _pending_plan_spec(db)
    with mock.patch.object(d, "call_planner",
                           side_effect=RuntimeError("planner blew up")):
        d._process_pending_plan(db, spec)
    assert db.get_spec(spec.id).status is SpecStatus.PENDING_PLAN
    ev = db.list_events_by_kind(spec_id=spec.id, kind=d.EventKind.FAILURE_CLASSIFIED)
    assert ev[0].payload["cls"] == "unknown_exception"
    assert ev[0].payload["phase"] == "planner"


def test_pending_plan_no_verdicts_park_behind_a_gate_at_the_cap(db):
    spec = _pending_plan_spec(db)
    with mock.patch.object(d, "call_planner",
                           side_effect=RuntimeError("planner blew up")):
        for _ in range(6):
            d._process_pending_plan(db, db.get_spec(spec.id))
            if db.get_spec(spec.id).status is not SpecStatus.PENDING_PLAN:
                break
    assert db.get_spec(spec.id).status is SpecStatus.NEEDS_CLARIFICATION
    gates = db.list_gates_for_spec(spec.id)
    assert len(gates) == 1 and "Infrastructure gate" in gates[0].prompt_md
