"""Gate-rejection exhaustion must reach synthesis too — DEV-433.

Exhausting MAX_RETRIES via a failing test run went through the synthesis
escape hatch; exhausting it via human code_review rejections failed the spec
outright and discarded every attempt. Whether the accumulated work survived
depended on who noticed the defect, not on what the defect was — and the gate
path carries strictly more information, since it comes with written notes.

Observed on DEV-102 slice 1 (spec_4f1a4ec1): five attempts, none compiling but
no two failing the same way, whose union was very close to green, thrown away
at 17:19 on 2026-08-03 without a synthesis attempt.
"""
from types import SimpleNamespace
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.executor import build_synthesis_message
from coding_model_autonomous.models import (
    GateStatus, GateType, SpecStatus, TaskStatus,
)


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def exhausted_spec(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    spec_dir = db.spec_dir(spec.id)
    (spec_dir / "spec.md").write_text("# spec\nbuild a thing")
    (spec_dir / "design.md").write_text("# design")
    (spec_dir / "impl.py").write_text("def f():\n    return 1\n")
    db.update_spec_status(
        spec.id, SpecStatus.EXECUTING,
        normalized_yaml="test_strategy:\n  framework: pytest\n  required: true\n",
    )
    impl_task = db.create_task(spec_id=spec.id, agent="implementer",
                               role="implementer", title="build demo")
    reviewer_task = db.create_task(spec_id=spec.id, agent="reviewer",
                                   role="reviewer", title="review demo")
    return db.get_spec(spec.id), impl_task, reviewer_task, spec_dir


def _reject_at_gate(db, spec, impl_task, *, tests_pass, notes="not good enough"):
    """Drive a human code_review rejection at MAX_RETRIES exhaustion."""
    gate = db.create_gate(spec_id=spec.id, task_id=impl_task.id,
                          gate_type=GateType.CODE_REVIEW,
                          prompt_md="## Code review: demo")
    db.respond_to_gate(gate.id, "rejected", notes=notes)
    gate = db.get_gate(gate.id)

    synth_result = SimpleNamespace(files=[("impl.py", "def f():\n    return 2\n")])
    with mock.patch.object(d, "MAX_RETRIES", 0), \
            mock.patch.object(d, "call_agent", return_value="raw"), \
            mock.patch.object(d, "parse_implementer_response",
                              return_value=synth_result), \
            mock.patch.object(d, "run_tests",
                              return_value=(tests_pass,
                                            "1 passed in 0.01s" if tests_pass
                                            else "1 failed in 0.01s")):
        d._legacy_handle_gate_rejection(db, spec, db.get_task(impl_task.id), gate)


def test_gate_exhaustion_reaches_synthesis_and_opens_release_gate(db, exhausted_spec):
    spec, impl_task, reviewer_task, _ = exhausted_spec
    _reject_at_gate(db, spec, impl_task, tests_pass=True)

    open_gates = db.list_open_gates(spec.id)
    assert len(open_gates) == 1
    assert open_gates[0].gate_type is GateType.RELEASE_APPROVAL
    # Not failed — the human decides on the synthesized result.
    assert db.get_spec(spec.id).status is SpecStatus.EXECUTING
    assert db.get_task(reviewer_task.id).status is TaskStatus.BLOCKED_ON_REVIEW


def test_gate_exhaustion_with_failing_synthesis_still_fails(db, exhausted_spec):
    """Synthesis is a chance, not a guarantee — a failing one fails the spec."""
    spec, impl_task, reviewer_task, _ = exhausted_spec
    _reject_at_gate(db, spec, impl_task, tests_pass=False)

    assert db.list_open_gates(spec.id) == []
    assert db.get_spec(spec.id).status is SpecStatus.FAILED


def test_no_reviewer_task_falls_back_to_failing(db):
    """Nothing to synthesize into — preserve the old outcome rather than crash."""
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    impl_task = db.create_task(spec_id=spec.id, agent="implementer",
                               role="implementer", title="build")
    gate = db.create_gate(spec_id=spec.id, task_id=impl_task.id,
                          gate_type=GateType.CODE_REVIEW, prompt_md="## Code review")
    db.respond_to_gate(gate.id, "rejected", notes="no")
    with mock.patch.object(d, "MAX_RETRIES", 0):
        d._legacy_handle_gate_rejection(db, db.get_spec(spec.id),
                                        db.get_task(impl_task.id),
                                        db.get_gate(gate.id))
    assert db.get_spec(spec.id).status is SpecStatus.FAILED
    assert db.get_task(impl_task.id).status is TaskStatus.FAILED


def test_below_max_retries_is_unchanged(db, exhausted_spec):
    """The ordinary rejection path must keep retrying, not synthesize."""
    spec, impl_task, reviewer_task, _ = exhausted_spec
    gate = db.create_gate(spec_id=spec.id, task_id=impl_task.id,
                          gate_type=GateType.CODE_REVIEW, prompt_md="## Code review")
    db.respond_to_gate(gate.id, "rejected", notes="fix it")
    with mock.patch.object(d, "MAX_RETRIES", 5):
        d._legacy_handle_gate_rejection(db, spec, db.get_task(impl_task.id),
                                        db.get_gate(gate.id))
    assert db.get_task(impl_task.id).status is TaskStatus.PENDING
    assert db.get_task(impl_task.id).retry_count == 1
    assert db.get_spec(spec.id).status is SpecStatus.EXECUTING


# ── the notes that travel with it ────────────────────────────────────────────

def test_collect_rejection_notes_keeps_human_reviews_oldest_first(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    for notes in ("first review", "second review"):
        g = db.create_gate(spec_id=spec.id, task_id=None,
                           gate_type=GateType.CODE_REVIEW,
                           prompt_md="## Code review: demo")
        db.respond_to_gate(g.id, "rejected", notes=notes)

    assert d._collect_rejection_notes(db, spec.id) == ["first review", "second review"]


def test_collect_rejection_notes_skips_automated_gates(db):
    """Parse- and build-failure synthetics carry compiler output, which already
    travels with each attempt's test summary."""
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    auto = db.create_gate(spec_id=spec.id, task_id=None,
                          gate_type=GateType.CODE_REVIEW,
                          prompt_md="## Automated build-failure retry (DEV-429)")
    db.respond_to_gate(auto.id, "rejected", notes="error: cannot find type")
    human = db.create_gate(spec_id=spec.id, task_id=None,
                           gate_type=GateType.CODE_REVIEW,
                           prompt_md="## Code review: demo")
    db.respond_to_gate(human.id, "rejected", notes="keep the locomotion")

    assert d._collect_rejection_notes(db, spec.id) == ["keep the locomotion"]


def test_collect_rejection_notes_ignores_approved_gates(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    g = db.create_gate(spec_id=spec.id, task_id=None,
                       gate_type=GateType.CODE_REVIEW, prompt_md="## Code review")
    db.respond_to_gate(g.id, "approved", notes="looks good")
    assert d._collect_rejection_notes(db, spec.id) == []


def test_synthesis_prompt_carries_the_reviewer_notes():
    msgs = build_synthesis_message(
        "# spec", "# design",
        [{"retry": 0, "agent": "a", "files": {"x.py": "1"}}],
        review_notes=["retry 4 has the locomotion right — keep it verbatim"],
    )
    user = msgs[-1]["content"]
    assert "Reviewer feedback" in user
    assert "keep it verbatim" in user
    # And still contains the attempts themselves.
    assert "Prior Attempts" in user


def test_synthesis_prompt_without_notes_is_unchanged_in_shape():
    msgs = build_synthesis_message(
        "# spec", "# design", [{"retry": 0, "agent": "a", "files": {"x.py": "1"}}])
    user = msgs[-1]["content"]
    assert "Reviewer feedback" not in user
    assert "Prior Attempts" in user
