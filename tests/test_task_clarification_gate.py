"""DEV-122 — a task parked behind a CLARIFICATION gate must resume, not wedge.

outcome.park leaves a task BLOCKED_ON_REVIEW behind a CLARIFICATION gate
bound to it when an attempt's failures give no verdict too many times in a
row (an infrastructure fault). Before DEV-122, _check_execution_gate only
read the role's review gate, so the human's answer on the clarification gate
was read by nothing and the spec sat in EXECUTING forever.
"""
import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.models import (
    GateStatus,
    GateType,
    SpecStatus,
    TaskStatus,
)


@pytest.fixture
def spec_tasks(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md",
                          status=SpecStatus.EXECUTING)
    impl = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="impl")
    rev = db.create_task(spec_id=spec.id, agent="reviewer",
                         role="reviewer", title="review")
    db.update_task_status(rev.id, TaskStatus.BLOCKED_ON_REVIEW)
    return db.get_spec(spec.id), db.get_task(impl.id), db.get_task(rev.id)


def _parked(db, spec, task, *, decision=None, notes=None):
    """The task-bound gate outcome.park opens."""
    gate = db.create_gate(spec_id=spec.id, task_id=task.id,
                          gate_type=GateType.CLARIFICATION,
                          prompt_md="## Parked: the runner is unreachable")
    if decision:
        db.respond_to_gate(gate.id, decision, notes=notes)
    return db.get_gate(gate.id)


def test_pending_clarification_keeps_the_task_parked(db, spec_tasks):
    spec, _impl, rev = spec_tasks
    _parked(db, spec, rev)  # PENDING — the human hasn't answered

    d._check_execution_gate(db, spec, db.get_task(rev.id))

    assert db.get_task(rev.id).status is TaskStatus.BLOCKED_ON_REVIEW
    clars = db.list_gates_for_spec(spec.id, GateType.CLARIFICATION)
    assert len(clars) == 1, "no new gates while waiting"


def test_approved_clarification_reruns_the_task_without_charging_it(db, spec_tasks):
    spec, _impl, rev = spec_tasks
    before = db.get_task(rev.id).retry_count
    clar = _parked(db, spec, rev, decision="approved", notes="runner is back")

    d._check_execution_gate(db, spec, db.get_task(rev.id))

    fresh = db.get_task(rev.id)
    assert fresh.status is TaskStatus.PENDING
    assert fresh.retry_count == before, "a park is a no-verdict: never charged"
    assert db.get_gate(clar.id).status is GateStatus.CANCELLED, (
        "the answered gate must be consumed so it can never be re-processed"
    )


def test_approved_clarification_is_processed_exactly_once(db, spec_tasks):
    spec, _impl, rev = spec_tasks
    clar = _parked(db, spec, rev, decision="approved", notes="fixed")

    d._check_execution_gate(db, spec, db.get_task(rev.id))
    # The task parks again before the next tick; the consumed gate must not
    # release it a second time.
    db.update_task_status(rev.id, TaskStatus.BLOCKED_ON_REVIEW)
    d._check_execution_gate(db, spec, db.get_task(rev.id))

    assert db.get_gate(clar.id).status is GateStatus.CANCELLED
    assert db.get_task(rev.id).status is TaskStatus.BLOCKED_ON_REVIEW


def test_rejected_clarification_aborts_the_spec(db, spec_tasks):
    spec, _impl, rev = spec_tasks
    clar = _parked(db, spec, rev, decision="rejected", notes="give up")

    d._check_execution_gate(db, spec, db.get_task(rev.id))

    assert db.get_task(rev.id).status is TaskStatus.FAILED
    assert db.get_spec(spec.id).status is SpecStatus.FAILED
    assert db.get_gate(clar.id).status is GateStatus.CANCELLED


def test_retry_style_rejection_keeps_its_gate_for_rejection_notes(db, spec_tasks):
    # A rejection whose handling moves the task on must NOT be cancelled —
    # _run_implementer reads REJECTED CODE_REVIEW gates for notes.
    spec, impl, _rev = spec_tasks
    db.update_task_status(impl.id, TaskStatus.BLOCKED_ON_REVIEW)
    code = db.create_gate(spec_id=spec.id, task_id=impl.id,
                          gate_type=GateType.CODE_REVIEW, prompt_md="code")
    db.respond_to_gate(code.id, "rejected", notes="fix the loop bounds")

    d._check_execution_gate(db, spec, db.get_task(impl.id))

    assert db.get_task(impl.id).status is TaskStatus.PENDING
    assert db.get_gate(code.id).status is GateStatus.REJECTED, (
        "rejection notes must stay readable for the implementer's next run"
    )
