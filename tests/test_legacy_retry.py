"""State-transition tests for the legacy (non-supervisor) retry path (DEV-129).

_legacy_attempt_retry and _rotation_pick run exactly when a spec is already
going wrong, and had zero coverage. These pin the normal (non-exhausted)
retry transitions and the rotation chain; the MAX_RETRIES synthesis escape
hatch is pinned separately in test_synthesis_gate.py.
"""
import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import retry_policy
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import GateStatus, GateType, SpecStatus, TaskStatus


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def spec_with_tasks(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    impl_task = db.create_task(
        spec_id=spec.id, agent="implementer", role="implementer", title="build",
    )
    reviewer_task = db.create_task(
        spec_id=spec.id, agent="reviewer", role="reviewer", title="review",
    )
    return db.get_spec(spec.id), impl_task, reviewer_task


def test_retry_creates_rejected_gate_and_resets_tasks(db, spec_with_tasks):
    spec, impl_task, reviewer_task = spec_with_tasks
    d._legacy_attempt_retry(db, spec, reviewer_task, "AssertionError: boom")

    # The failure detail travels as a synthetic rejected code_review gate,
    # which _run_implementer reads back as rejection_notes on its next run.
    gates = db.list_gates_for_spec(spec.id)
    assert len(gates) == 1
    assert gates[0].gate_type is GateType.CODE_REVIEW
    assert gates[0].status is GateStatus.REJECTED
    assert gates[0].reviewer_notes == "AssertionError: boom"
    # Implementer retries; reviewer re-runs against the new attempt.
    assert db.get_task(impl_task.id).retry_count == impl_task.retry_count + 1
    assert db.get_task(impl_task.id).status is TaskStatus.PENDING
    assert db.get_task(reviewer_task.id).status is TaskStatus.PENDING
    assert db.get_spec(spec.id).status is SpecStatus.EXECUTING


def test_no_implementer_task_fails_spec(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    reviewer_task = db.create_task(
        spec_id=spec.id, agent="reviewer", role="reviewer", title="review",
    )
    d._legacy_attempt_retry(db, db.get_spec(spec.id), reviewer_task, "boom")

    assert db.get_task(reviewer_task.id).status is TaskStatus.FAILED
    assert db.get_spec(spec.id).status is SpecStatus.FAILED


class TestRotationPick:
    def test_retry_zero_keeps_architect_recommendation(self):
        assert retry_policy._rotation_pick("moe_implementer", 0) == "moe_implementer"

    def test_none_initial_agent_stays_none(self):
        assert retry_policy._rotation_pick(None, 3) is None

    def test_consecutive_retries_never_repeat(self):
        picks = [retry_policy._rotation_pick("implementer", n) for n in range(8)]
        for a, b in zip(picks, picks[1:]):
            assert a != b

    def test_chain_starts_with_initial_agent(self):
        # Retry index N maps to the Nth agent of a chain headed by the
        # initial agent, so the first retry moves OFF the failing model.
        first_retry = retry_policy._rotation_pick("deep_implementer", 1)
        assert first_retry != "deep_implementer"
        assert first_retry in retry_policy._IMPLEMENTER_ROTATION

    def test_unknown_initial_agent_falls_back_to_rotation(self):
        pick = retry_policy._rotation_pick("dense_architect", 2)
        assert pick == retry_policy._IMPLEMENTER_ROTATION[2 % len(retry_policy._IMPLEMENTER_ROTATION)]

    def test_wraps_modulo_chain_length(self):
        chain_len = len(retry_policy._IMPLEMENTER_ROTATION)
        assert (retry_policy._rotation_pick("implementer", chain_len)
                == retry_policy._rotation_pick("implementer", 2 * chain_len))
