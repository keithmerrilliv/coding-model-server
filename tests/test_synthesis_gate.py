"""Behavioural tests for the MAX_RETRIES synthesis escape hatch (DEV-121).

A passing synthesis used to mark the spec DONE directly — skipping both the
release_approval gate and the structural test guard, for exactly the specs
that had already exhausted every retry. These pin the corrected behavior:

  * synthesis pass          -> release_approval gate, spec stays EXECUTING
  * clean exit, no summary  -> structural guard forces FAIL (no hallucinated PASS)
  * synthesis test failure  -> spec FAILED (unchanged legacy outcome)

Everything runs against a throwaway DB under tmp_path with the LLM call and
test runner mocked; no network, no sandbox.
"""
from types import SimpleNamespace
from unittest import mock

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
def exhausted_spec(db):
    """A spec whose implementer has burned every retry, plus its reviewer task.

    The live spec_dir carries one implementation file so _read_retry_attempts
    finds a "current" attempt to synthesize from.
    """
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    spec_dir = db.spec_dir(spec.id)
    (spec_dir / "spec.md").write_text("# spec\nbuild a thing")
    (spec_dir / "design.md").write_text("# design")
    (spec_dir / "impl.py").write_text("def f():\n    return 1\n")
    db.update_spec_status(
        spec.id, SpecStatus.EXECUTING,
        normalized_yaml="test_strategy:\n  framework: pytest\n  required: true\n",
    )
    impl_task = db.create_task(
        spec_id=spec.id, agent="implementer", role="implementer", title="build demo",
    )
    reviewer_task = db.create_task(
        spec_id=spec.id, agent="reviewer", role="reviewer", title="review demo",
    )
    return db.get_spec(spec.id), impl_task, reviewer_task, spec_dir


def _run_retry(db, spec, reviewer_task, *, tests_pass, test_output):
    """Drive _legacy_attempt_retry into the synthesis branch with mocks."""
    synth_result = SimpleNamespace(
        files=[("impl.py", "def f():\n    return 2\n")],
    )
    with mock.patch.object(d, "MAX_RETRIES", 0), \
            mock.patch.object(d, "call_agent", return_value="raw"), \
            mock.patch.object(d, "build_synthesis_message", return_value=[]), \
            mock.patch.object(d, "parse_implementer_response",
                              return_value=synth_result), \
            mock.patch.object(d, "run_tests",
                              return_value=(tests_pass, test_output)):
        d._legacy_attempt_retry(db, spec, reviewer_task, "tests kept failing")


def test_synthesis_pass_creates_release_gate_not_done(db, exhausted_spec):
    spec, impl_task, reviewer_task, spec_dir = exhausted_spec
    _run_retry(db, spec, reviewer_task,
               tests_pass=True, test_output="1 passed in 0.01s")

    gates = db.list_open_gates(spec.id)
    assert len(gates) == 1
    assert gates[0].gate_type is GateType.RELEASE_APPROVAL
    assert "Synthesized after" in gates[0].prompt_md
    # The human decides; the spec must NOT be auto-completed.
    assert db.get_spec(spec.id).status is SpecStatus.EXECUTING
    assert db.get_task(reviewer_task.id).status is TaskStatus.BLOCKED_ON_REVIEW
    assert db.get_task(impl_task.id).status is TaskStatus.DONE


def test_structural_guard_blocks_hallucinated_synthesis_pass(db, exhausted_spec):
    # Runner exits 0 but emits no pytest summary line (the bwrap/AppArmor
    # short-circuit) — the guard must fail the spec, not open a release gate.
    spec, impl_task, reviewer_task, spec_dir = exhausted_spec
    _run_retry(db, spec, reviewer_task,
               tests_pass=True, test_output="(no tests ran)")

    assert db.list_open_gates(spec.id) == []
    assert db.get_spec(spec.id).status is SpecStatus.FAILED


def test_synthesis_test_failure_still_fails_spec(db, exhausted_spec):
    spec, impl_task, reviewer_task, spec_dir = exhausted_spec
    _run_retry(db, spec, reviewer_task,
               tests_pass=False, test_output="1 failed in 0.01s")

    assert db.list_open_gates(spec.id) == []
    assert db.get_spec(spec.id).status is SpecStatus.FAILED
