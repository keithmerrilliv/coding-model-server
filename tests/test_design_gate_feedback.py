"""DEV-124 — design-gate rejection notes must actually reach the architect.

In default mode (AUTONOMOUS_SUPERVISOR=0) the legacy branch stored the
human's rejection notes in a synthetic CLARIFICATION gate — a channel only
the planner reads — and never incremented the architect task's retry count.
_run_architect only looks for feedback when retry_count > 0 (via
_latest_architect_feedback, which reads design_review_feedback.md), so the
architect re-ran blind, regenerated ~the same design, and created another
gate: an unbounded reject→regenerate cycle where the README-promised
"rejection notes feed back into the agent" never happened.
"""
import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import GateType, SpecStatus, TaskStatus


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def spec_arch(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md",
                          status=SpecStatus.EXECUTING)
    task = db.create_task(spec_id=spec.id, agent="architect",
                          role="architect", title="design demo")
    db.update_task_status(task.id, TaskStatus.BLOCKED_ON_REVIEW)
    return db.get_spec(spec.id), db.get_task(task.id)


def _reject_design(db, spec, task, notes):
    gate = db.create_gate(spec_id=spec.id, task_id=task.id,
                          gate_type=GateType.DESIGN_APPROVAL,
                          prompt_md="## Design ready for review")
    db.respond_to_gate(gate.id, "rejected", notes=notes)
    return gate


def test_rejection_notes_land_in_the_architect_feedback_channel(
        db, spec_arch, monkeypatch):
    spec, task = spec_arch
    monkeypatch.setattr(d, "SUPERVISOR_ENABLED", False)
    _reject_design(db, spec, task, "add error handling to the API layer")

    d._check_execution_gate(db, spec, db.get_task(task.id))

    fresh = db.get_task(task.id)
    assert fresh.status is TaskStatus.PENDING
    assert fresh.retry_count == 1, (
        "_run_architect only surfaces feedback when retry_count > 0"
    )
    fb = db.spec_dir(spec.id) / "design_review_feedback.md"
    assert fb.is_file()
    assert "add error handling" in fb.read_text()


def test_feedback_flows_through_latest_architect_feedback(
        db, spec_arch, monkeypatch):
    # The exact reader _run_architect uses on its re-run must return the
    # notes (and consume the file so they don't bleed into later cycles).
    spec, task = spec_arch
    monkeypatch.setattr(d, "SUPERVISOR_ENABLED", False)
    _reject_design(db, spec, task, "the cache invariant is wrong")

    d._check_execution_gate(db, spec, db.get_task(task.id))

    spec_dir = db.spec_dir(spec.id)
    feedback = d._latest_architect_feedback(db, spec, spec_dir)
    assert feedback is not None and "cache invariant" in feedback
    assert not (spec_dir / "design_review_feedback.md").exists(), (
        "feedback is consume-once"
    )


def test_each_rejection_cycle_increments_the_retry_count(
        db, spec_arch, monkeypatch):
    # The regenerate loop is no longer invisible: every human rejection now
    # advances retry_count instead of leaving it pinned at 0.
    spec, task = spec_arch
    monkeypatch.setattr(d, "SUPERVISOR_ENABLED", False)

    for round_no, notes in enumerate(["first pass", "second pass"], start=1):
        db.update_task_status(task.id, TaskStatus.BLOCKED_ON_REVIEW)
        _reject_design(db, spec, task, notes)
        d._check_execution_gate(db, spec, db.get_task(task.id))
        assert db.get_task(task.id).retry_count == round_no
