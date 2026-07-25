"""DEV-122 — the supervisor clarification path must resume, not wedge or burn.

Two failure modes before the fix:
  (a) request_clarification parked the task BLOCKED_ON_REVIEW with a
      CLARIFICATION gate, but _check_execution_gate only ever read the
      role's review gate — the human's answer was read by nothing and the
      spec sat in EXECUTING forever;
  (b) when the parked task's role gate was REJECTED, every tick re-entered
      _handle_gate_rejection on that same gate, re-calling the supervisor
      and creating a duplicate CLARIFICATION gate (each mirrored to Jira)
      until the 8-transition budget aborted the spec.
"""
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import (
    GateStatus,
    GateType,
    SpecStatus,
    TaskStatus,
)
from coding_model_autonomous.supervisor import SupervisorDecision


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


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


def _clarification(db, spec, task, *, decision=None, notes=None):
    gate = db.create_gate(spec_id=spec.id, task_id=task.id,
                          gate_type=GateType.CLARIFICATION,
                          prompt_md="## Supervisor needs clarification")
    if decision:
        db.respond_to_gate(gate.id, decision, notes=notes)
    return db.get_gate(gate.id)


def _decision(action, target_role=None, feedback=None):
    return SupervisorDecision(action=action, target_role=target_role,
                              feedback_to_inject=feedback, reason="test",
                              raw_tool_call={})


def test_pending_clarification_parks_without_reinvoking_the_supervisor(
        db, spec_tasks, monkeypatch):
    spec, _impl, rev = spec_tasks
    monkeypatch.setattr(d, "SUPERVISOR_ENABLED", True)
    # The rejected release gate that originally triggered the supervisor.
    rel = db.create_gate(spec_id=spec.id, task_id=rev.id,
                         gate_type=GateType.RELEASE_APPROVAL, prompt_md="release")
    db.respond_to_gate(rel.id, "rejected", notes="not good enough")
    _clarification(db, spec, rev)  # PENDING — human hasn't answered

    with mock.patch.object(d._supervisor, "decide") as decide:
        d._check_execution_gate(db, spec, db.get_task(rev.id))

    decide.assert_not_called()
    assert db.get_task(rev.id).status is TaskStatus.BLOCKED_ON_REVIEW
    clars = db.list_gates_for_spec(spec.id, GateType.CLARIFICATION)
    assert len(clars) == 1, "no duplicate clarification gates while waiting"


def test_answered_clarification_resumes_the_supervisor_with_the_answer(
        db, spec_tasks, monkeypatch):
    spec, impl, rev = spec_tasks
    monkeypatch.setattr(d, "SUPERVISOR_ENABLED", True)
    clar = _clarification(db, spec, rev, decision="approved", notes="use API v2")

    with mock.patch.object(
            d._supervisor, "decide",
            return_value=_decision("retry", "implementer", "use API v2")) as decide:
        d._check_execution_gate(db, spec, db.get_task(rev.id))

    ctx = decide.call_args.args[0]
    assert ctx["outcome"] == "clarification_answered"
    assert ctx["reviewer_notes"] == "use API v2"
    assert db.get_gate(clar.id).status is GateStatus.CANCELLED, (
        "the answered gate must be consumed so it can never be re-processed"
    )
    fresh_impl = db.get_task(impl.id)
    assert fresh_impl.status is TaskStatus.PENDING
    assert fresh_impl.retry_count == 1


def test_answered_clarification_is_processed_exactly_once(
        db, spec_tasks, monkeypatch):
    spec, _impl, rev = spec_tasks
    monkeypatch.setattr(d, "SUPERVISOR_ENABLED", True)
    _clarification(db, spec, rev, decision="approved", notes="answer")

    with mock.patch.object(
            d._supervisor, "decide",
            return_value=_decision("retry", "implementer", "f")) as decide:
        d._check_execution_gate(db, spec, db.get_task(rev.id))
        d._check_execution_gate(db, spec, db.get_task(rev.id))

    assert decide.call_count == 1


def test_rejected_clarification_aborts_the_spec(db, spec_tasks, monkeypatch):
    spec, _impl, rev = spec_tasks
    monkeypatch.setattr(d, "SUPERVISOR_ENABLED", True)
    clar = _clarification(db, spec, rev, decision="rejected", notes="nope")

    with mock.patch.object(d._supervisor, "decide") as decide:
        d._check_execution_gate(db, spec, db.get_task(rev.id))

    decide.assert_not_called()
    assert db.get_task(rev.id).status is TaskStatus.FAILED
    assert db.get_spec(spec.id).status is SpecStatus.FAILED
    assert db.get_gate(clar.id).status is GateStatus.CANCELLED


def test_rejected_role_gate_is_not_reprocessed_every_tick(
        db, spec_tasks, monkeypatch):
    # The budget burner: a REJECTED release gate + request_clarification
    # used to re-call the supervisor every tick, each round minting another
    # CLARIFICATION gate until the transition budget aborted the spec.
    spec, _impl, rev = spec_tasks
    monkeypatch.setattr(d, "SUPERVISOR_ENABLED", True)
    rel = db.create_gate(spec_id=spec.id, task_id=rev.id,
                         gate_type=GateType.RELEASE_APPROVAL, prompt_md="release")
    db.respond_to_gate(rel.id, "rejected", notes="unclear requirement")

    with mock.patch.object(
            d._supervisor, "decide",
            return_value=_decision("request_clarification",
                                   feedback="which API version?")) as decide:
        for _ in range(3):  # three daemon ticks
            d._check_execution_gate(db, spec, db.get_task(rev.id))

    assert decide.call_count == 1, "the rejection must be processed once, not per tick"
    clars = db.list_gates_for_spec(spec.id, GateType.CLARIFICATION)
    assert len(clars) == 1, "exactly one clarification gate, not one per tick"
    assert db.get_gate(rel.id).status is GateStatus.CANCELLED, (
        "the handled rejection is retired once the task stays parked"
    )


def test_retry_style_rejection_keeps_its_gate_for_rejection_notes(
        db, spec_tasks, monkeypatch):
    # Counter-case: a rejection whose handling moves the task on must NOT be
    # cancelled — _run_implementer reads REJECTED CODE_REVIEW gates for notes.
    spec, impl, _rev = spec_tasks
    monkeypatch.setattr(d, "SUPERVISOR_ENABLED", False)
    db.update_task_status(impl.id, TaskStatus.BLOCKED_ON_REVIEW)
    code = db.create_gate(spec_id=spec.id, task_id=impl.id,
                          gate_type=GateType.CODE_REVIEW, prompt_md="code")
    db.respond_to_gate(code.id, "rejected", notes="fix the loop bounds")

    d._check_execution_gate(db, spec, db.get_task(impl.id))

    assert db.get_task(impl.id).status is TaskStatus.PENDING
    assert db.get_gate(code.id).status is GateStatus.REJECTED, (
        "rejection notes must stay readable for the implementer's next run"
    )


def test_supervisor_disabled_resume_reruns_the_phase(db, spec_tasks, monkeypatch):
    spec, _impl, rev = spec_tasks
    monkeypatch.setattr(d, "SUPERVISOR_ENABLED", False)
    _clarification(db, spec, rev, decision="approved", notes="answer")

    d._check_execution_gate(db, spec, db.get_task(rev.id))

    assert db.get_task(rev.id).status is TaskStatus.PENDING
