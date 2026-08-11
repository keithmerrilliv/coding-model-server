"""The testability check always looks, even when it may not revise — DEV-545.

`TESTABILITY_CHECK_MAX_ROUNDS` used to gate whether the check RAN, not just
whether it could force a revision, and it was keyed off `task.retry_count` —
a counter shared with human rejections, DEV-468's upstream routing and crash
recovery.

Run 9: both rounds went on drafts no human ever saw, and the two designs that
did reach a human arrived unchecked. One of them carried the tuple-comparison
defect the check exists to catch. Run 11 repeated it — the unchecked design
carried 11 prose seams, the same class the check had found 11 of one revision
earlier.

So: the check runs every time. The budget now bounds revisions only, is counted
from the check's own firings, and when it is spent the findings go onto the
human's gate instead of into a log line nobody opens.
"""
import json
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import design_testability, executor
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import (
    EventKind, GateStatus, GateType, SpecStatus, TaskStatus,
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
    spec_dir = db.spec_dir(spec.id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text("# demo\n")
    return db.get_spec(spec.id), db.get_task(task.id), spec_dir


def _finding(kind="prose_seam", text="Criterion 4's seam is prose"):
    return design_testability.Finding(kind=kind, criterion="Criterion 4",
                                      detail=text)


def _record_round(db, spec, task, n, revised=True):
    """Simulate n prior firings of the check."""
    for _ in range(n):
        db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"role": "testability_check",
                                 "model_call": False, "findings": 1,
                                 "kinds": ["prose_seam"], "revised": revised})


# ── the counter ──────────────────────────────────────────────────────────────

def test_rounds_are_counted_from_the_checks_own_firings(db, spec_task):
    spec, task, _ = spec_task
    assert d._testability_rounds_used(db, spec.id) == 0
    _record_round(db, spec, task, 2)
    assert d._testability_rounds_used(db, spec.id) == 2


def test_a_firing_that_did_not_revise_does_not_spend_a_round(db, spec_task):
    """Carrying findings to the gate must not consume budget — otherwise the
    check would retire itself by observing."""
    spec, task, _ = spec_task
    _record_round(db, spec, task, 3, revised=False)
    assert d._testability_rounds_used(db, spec.id) == 0


def test_other_agent_events_are_not_counted(db, spec_task):
    """retry_count is shared; this counter must not be."""
    spec, task, _ = spec_task
    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": "architect", "model_call": True})
    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": "manifest", "model_call": False})
    assert d._testability_rounds_used(db, spec.id) == 0


# ── the behaviour ────────────────────────────────────────────────────────────

def _run_architect(db, spec, task, spec_dir, findings):
    """Drive the real _run_architect with the model call stubbed out."""
    result = mock.Mock(design_md="# design\n", complexity=None)
    with mock.patch.object(d, "call_agent", return_value="raw"), \
         mock.patch.object(d, "parse_architect_response", return_value=result), \
         mock.patch.object(design_testability, "check_design_testability",
                           return_value=findings), \
         mock.patch.object(design_testability, "check_design_completeness",
                           return_value=[]), \
         mock.patch.object(executor, "DESIGN_REVIEW_ENABLED", False), \
         mock.patch.object(d, "_fetch_protected_files_for_spec", return_value=[]), \
         mock.patch.object(d, "_approved_gate_conditions", return_value=None):
        d._run_architect(db, spec, task, spec_dir)


def test_findings_with_budget_left_still_revise(db, spec_task):
    """The original behaviour is untouched while rounds remain."""
    spec, task, spec_dir = spec_task
    _run_architect(db, spec, task, spec_dir, [_finding()])

    assert db.get_task(task.id).status is TaskStatus.PENDING
    assert db.get_task(task.id).retry_count == task.retry_count + 1
    assert not db.list_gates_for_spec(spec.id), "no gate — it went back to revise"


def test_exhausted_budget_carries_the_findings_onto_the_gate(db, spec_task):
    """The acceptance criterion: an unchecked design is no longer possible."""
    spec, task, spec_dir = spec_task
    _record_round(db, spec, task, executor.TESTABILITY_CHECK_MAX_ROUNDS)

    _run_architect(db, spec, task, spec_dir,
                   [_finding(text="Criterion 4's seam is prose"),
                    _finding("missing_equatable", "World is never Equatable")])

    gates = db.list_gates_for_spec(spec.id)
    assert len(gates) == 1
    gate = gates[0]
    assert gate.gate_type is GateType.DESIGN_APPROVAL
    assert gate.status is GateStatus.PENDING
    # the reviewer can see what the check found, on the gate itself
    assert "2 unresolved finding(s)" in gate.prompt_md
    assert "Criterion 4's seam is prose" in gate.prompt_md
    assert "World is never Equatable" in gate.prompt_md
    assert "revision budget" in gate.prompt_md
    assert db.get_task(task.id).status is TaskStatus.BLOCKED_ON_REVIEW


def test_carrying_to_the_gate_does_not_burn_an_architect_retry(db, spec_task):
    spec, task, spec_dir = spec_task
    _record_round(db, spec, task, executor.TESTABILITY_CHECK_MAX_ROUNDS)
    _run_architect(db, spec, task, spec_dir, [_finding()])
    assert db.get_task(task.id).retry_count == task.retry_count


def test_a_clean_design_gets_an_unannotated_gate(db, spec_task):
    """No findings must mean no change to what the reviewer sees."""
    spec, task, spec_dir = spec_task
    _record_round(db, spec, task, executor.TESTABILITY_CHECK_MAX_ROUNDS)
    _run_architect(db, spec, task, spec_dir, [])

    gate = db.list_gates_for_spec(spec.id)[0]
    assert "testability check" not in gate.prompt_md.lower()
    assert "unresolved finding" not in gate.prompt_md


def test_the_check_still_runs_after_a_human_rejection(db, spec_task):
    """The run-9 case exactly: retry_count is high because a HUMAN rejected the
    design, and the check must still look at what came back."""
    spec, task, spec_dir = spec_task
    for _ in range(4):                      # human rejections, not check rounds
        db.increment_task_retry(task.id)
    task = db.get_task(task.id)
    assert task.retry_count > executor.TESTABILITY_CHECK_MAX_ROUNDS

    _run_architect(db, spec, task, spec_dir, [_finding()])

    # budget is its own counter, so it still has rounds and revises
    assert db.get_task(task.id).status is TaskStatus.PENDING
    payloads = [json.loads(e.payload_json or "{}")
                for e in db.list_events_by_kind(spec_id=spec.id,
                                                kind=EventKind.AGENT_RAN)]
    checks = [p for p in payloads if p.get("role") == "testability_check"]
    assert checks, "the check ran despite a high retry_count"
    assert checks[-1]["revised"] is True
