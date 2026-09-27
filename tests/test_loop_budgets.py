"""The four loops that bound themselves outside outcome.dispose — DEV-838.

The planner's no-verdicts, crash recovery, testability revisions and free
harness retries each count their OWN records rather than task.retry_count
(DEV-558, DEV-545). They were written separately and had drifted:

* the planner's cap had no test at all;
* the testability counter read only the 20 newest AGENT_RAN rows, so a late
  architect re-run could find its earlier revisions out of the window;
* the harness counter lived in a file the retry wipe deletes, so from the
  first charged retry on every free retry reset it and the cap never bound.

These tests pin the behaviour each loop is meant to have, whatever counts it.
"""
import json
import logging

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import outcome as _outcome
from coding_model_autonomous.models import (
    EventKind, GateType, SpecStatus, TaskStatus,
)
from coding_model_autonomous.outcome import Failure, FailureClass
from coding_model_autonomous.retry_policy import _clean_spec_dir_for_retry


def _planner_failure(detail="empty completion"):
    return Failure(FailureClass.EMPTY_COMPLETION, "planner", "model_call",
                   detail, phase="planner")


def _classified(db, spec_id):
    return [json.loads(e.payload_json)
            for e in db.list_events_by_kind(spec_id=spec_id,
                                            kind=EventKind.FAILURE_CLASSIFIED,
                                            limit=-1)]


# ── the planner's no-verdict cap (DEV-629) ───────────────────────────────────

@pytest.fixture
def pending_plan(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    return db.get_spec(spec.id)


def test_planner_no_verdicts_requeue_up_to_the_cap(db, pending_plan):
    cap = _planner_failure().cap
    assert cap is not None and cap >= 1
    for n in range(1, cap + 1):
        d._planner_no_verdict(db, pending_plan, _planner_failure())
        rows = _classified(db, pending_plan.id)
        assert rows[0]["disposition"] == "requeue"
        assert rows[0]["consecutive"] == n
    assert db.get_spec(pending_plan.id).status is SpecStatus.PENDING_PLAN
    assert not db.list_gates_for_spec(pending_plan.id, GateType.CLARIFICATION)


def test_planner_no_verdict_past_the_cap_parks_behind_a_gate(db, pending_plan):
    cap = _planner_failure().cap
    for _ in range(cap + 1):
        d._planner_no_verdict(db, pending_plan, _planner_failure("model down"))
    rows = _classified(db, pending_plan.id)
    assert rows[0]["disposition"] == "park"
    assert rows[0]["consecutive"] == cap + 1
    assert db.get_spec(pending_plan.id).status is SpecStatus.NEEDS_CLARIFICATION
    gates = db.list_gates_for_spec(pending_plan.id, GateType.CLARIFICATION)
    assert len(gates) == 1
    assert "Infrastructure gate" in gates[0].prompt_md
    assert "model down" in gates[0].prompt_md


def test_an_unreadable_planner_count_is_said_out_loud(db, pending_plan,
                                                      monkeypatch, caplog):
    """DEV-630: a cap that cannot be read counts as 0, and never silently."""
    def boom(**_kw):
        raise RuntimeError("disk I/O error")
    monkeypatch.setattr(db, "list_events_by_kind", boom)
    with caplog.at_level(logging.WARNING, logger="orchestrator"):
        d._planner_no_verdict(db, pending_plan, _planner_failure())
    assert "NOT enforced" in caplog.text
    assert db.get_spec(pending_plan.id).status is SpecStatus.PENDING_PLAN


# ── testability revisions (DEV-545) ──────────────────────────────────────────

@pytest.fixture
def architect(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md",
                          status=SpecStatus.EXECUTING)
    task = db.create_task(spec_id=spec.id, agent="dense_architect",
                          role="architect", title="design")
    return db.get_spec(spec.id), db.get_task(task.id)


def test_testability_rounds_survive_a_long_run_of_later_events(db, architect):
    """Two revisions early, then a full implementer/reviewer cycle's worth of
    AGENT_RAN rows: a later architect pass must still see both rounds spent."""
    spec, task = architect
    for _ in range(2):
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"role": "testability_check", "model_call": False,
                                 "findings": 1, "revised": True})
    for i in range(40):
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"role": "implementer", "agent": "implementer",
                                 "n": i})
    assert d._testability_rounds_used(db, spec.id) == 2


# ── crash recovery (DEV-193, DEV-558) ────────────────────────────────────────

def test_crash_recoveries_survive_a_long_run_of_later_events(db, architect):
    spec, task = architect
    for n in range(3):
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"role": "crash_recovery", "model_call": False,
                                 "recovery": n + 1})
    for i in range(40):
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"role": "architect", "agent": "x", "n": i})
    assert d._crash_recoveries_used(db, spec.id, task.id) == 3


# ── free harness retries (DEV-404) ───────────────────────────────────────────

BROKEN_HARNESS_TAP = """\
  error: 'assert.ok is not a function'
# tests 3
# pass 0
# fail 3
"""


@pytest.fixture
def reviewing(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md",
                          status=SpecStatus.EXECUTING)
    impl = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="impl")
    rev = db.create_task(spec_id=spec.id, agent="reviewer",
                         role="reviewer", title="review")
    db.update_task_status(impl.id, TaskStatus.DONE)
    db.update_task_status(rev.id, TaskStatus.RUNNING)
    spec_dir = db.spec_dir(spec.id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    return db.get_spec(spec.id), db.get_task(rev.id), spec_dir


def test_the_harness_cap_holds_across_the_retry_wipe(db, reviewing):
    """After one charged retry, every implementer pass wipes spec_dir. A free
    harness retry does not advance retry_count, so the wipe runs again — and
    the cap must still bind."""
    spec, rev, spec_dir = reviewing
    for _ in range(d._HARNESS_FREE_RETRIES):
        assert d._harness_retry(db, spec, rev, spec_dir, "broken import",
                                BROKEN_HARNESS_TAP, "node_test") is True
        _clean_spec_dir_for_retry(spec_dir, 1)   # the implementer re-runs
    assert d._harness_retry(db, spec, rev, spec_dir, "broken import",
                            BROKEN_HARNESS_TAP, "node_test") is False


def test_every_loop_counts_through_the_one_counter():
    """One cap path (DEV-838): no loop reads its own events by hand."""
    import inspect
    for fn in (d._planner_no_verdict, d._crash_recoveries_used,
               d._testability_rounds_used, d._harness_retry):
        src = inspect.getsource(fn)
        assert "count_own_records" in src, fn.__name__
        assert "list_events_by_kind" not in src, fn.__name__
    assert hasattr(_outcome, "count_own_records")
