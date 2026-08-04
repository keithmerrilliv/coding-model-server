"""Pre-gate build check — DEV-429.

A code_review gate that opens on code which cannot compile spends the
expensive resource (a human reviewer) on a decision the free one (the
compiler) already made. On DEV-102 slice 1 that happened four times in a row
on one spec, burning four of five retries on access-control and declaration
errors.

These pin two things: the build-vs-test-failure discrimination, which is what
makes the short-circuit safe, and the _run_implementer transition itself.
"""
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.executor import ImplementerResult
from coding_model_autonomous.models import (
    GateStatus, GateType, SpecStatus, TaskStatus,
)

# Real diagnostics from the DEV-102 run.
SWIFT_BUILD_FAILURE = """\
Building for debugging...
[6/13] Compiling CentipedeCore Simulation.swift
/Users/km4/.../Sources/CentipedeCore/MushroomGrid.swift:17:19: error: \
'DeterministicRNG' is inaccessible due to 'private' protection level
15 |
17 |         var rng = DeterministicRNG(seed: seed)
   |                   `- error: 'DeterministicRNG' is inaccessible
error: fatalError
"""

SWIFT_TEST_FAILURE = """\
Building for debugging...
[13/13] Compiling CentipedeCore Simulation.swift
Test Suite 'All tests' started
✘ Test chainDescendsOnEdgeCollision() recorded an issue: Expectation failed
Test Suite 'All tests' failed at 2026-08-03.
     Executed 17 tests, with 1 failure (0 unexpected)
"""


# ── the discriminator ────────────────────────────────────────────────────────

def test_swift_compiler_diagnostic_is_a_build_failure():
    reason = d._detect_build_failure(SWIFT_BUILD_FAILURE, "swift_test", passed=False)
    assert reason is not None
    assert "DeterministicRNG" in reason


def test_swift_assertion_failure_is_not_a_build_failure():
    """The build succeeded and a test failed — that needs a human, not a retry."""
    assert d._detect_build_failure(SWIFT_TEST_FAILURE, "swift_test", passed=False) is None


def test_passing_run_is_never_a_build_failure():
    """Some suites legitimately print 'error:' in their own output. A green
    suite proves the build was fine, so the text never gets consulted."""
    noisy = SWIFT_TEST_FAILURE + "\n/tmp/x.swift:1:1: error: printed by a test\n"
    assert d._detect_build_failure(noisy, "swift_test", passed=True) is None


def test_pytest_collection_error_is_a_build_failure():
    out = "ERROR collecting tests/test_x.py\nE   ModuleNotFoundError: No module named 'app'\n"
    assert d._detect_build_failure(out, "pytest", passed=False) is not None


def test_pytest_assertion_failure_is_not_a_build_failure():
    out = "E   AssertionError: assert 1 == 2\n1 failed in 0.02s\n"
    assert d._detect_build_failure(out, "pytest", passed=False) is None


def test_unknown_framework_never_short_circuits():
    """No signature for the framework means we cannot tell — keep the gate."""
    assert d._detect_build_failure(SWIFT_BUILD_FAILURE, "cargo_test", passed=False) is None


def test_empty_output_is_not_a_build_failure():
    assert d._detect_build_failure("", "swift_test", passed=False) is None


# ── the transition ───────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def impl_spec(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    task = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="build")
    spec_dir = db.spec_dir(spec.id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text("# demo\n")
    (spec_dir / "design.md").write_text("# design\n")
    return db.get_spec(spec.id), db.get_task(task.id), spec_dir


def _run(db, spec, task, spec_dir, *, build_passed, build_output):
    result = ImplementerResult(files=[("Sources/A.swift", "struct A {}")], raw="")
    with mock.patch.object(d, "_generate_implementation", return_value=result), \
         mock.patch.object(d, "_load_plan",
                           return_value={"test_strategy": {"framework": "swift_test",
                                                           "repo": "centipede"}}), \
         mock.patch.object(d, "_run_tests_with_guard",
                           return_value=(build_passed, build_output)) as run_tests:
        d._run_implementer(db, spec, task, spec_dir)
    return run_tests


def test_build_failure_retries_without_opening_a_human_gate(db, impl_spec):
    spec, task, spec_dir = impl_spec
    _run(db, spec, task, spec_dir, build_passed=False,
         build_output=SWIFT_BUILD_FAILURE)

    gates = db.list_gates_for_spec(spec.id)
    assert len(gates) == 1
    # The only gate is the synthetic one that carries the diagnostics to the
    # next attempt — already answered, so no human is ever asked.
    assert gates[0].status is GateStatus.REJECTED
    assert "does not compile" in gates[0].reviewer_notes
    assert "DeterministicRNG" in gates[0].reviewer_notes
    assert not [g for g in gates if g.status is GateStatus.PENDING]

    assert db.get_task(task.id).status is TaskStatus.PENDING
    assert db.get_task(task.id).retry_count == task.retry_count + 1
    assert db.get_spec(spec.id).status is SpecStatus.EXECUTING
    # The compiler's own words are kept for the record.
    assert (spec_dir / "build_failure.txt").exists()


def test_compiling_code_still_opens_the_review_gate(db, impl_spec):
    spec, task, spec_dir = impl_spec
    _run(db, spec, task, spec_dir, build_passed=True, build_output="Executed 17 tests")

    pending = [g for g in db.list_gates_for_spec(spec.id)
               if g.status is GateStatus.PENDING]
    assert len(pending) == 1
    assert pending[0].gate_type is GateType.CODE_REVIEW
    assert "compiled and the suite passed" in pending[0].prompt_md
    assert db.get_task(task.id).status is TaskStatus.BLOCKED_ON_REVIEW
    assert db.get_task(task.id).retry_count == task.retry_count


def test_test_failure_still_opens_the_review_gate(db, impl_spec):
    """Behaviour defects are exactly what the human gate is for."""
    spec, task, spec_dir = impl_spec
    _run(db, spec, task, spec_dir, build_passed=False,
         build_output=SWIFT_TEST_FAILURE)

    pending = [g for g in db.list_gates_for_spec(spec.id)
               if g.status is GateStatus.PENDING]
    assert len(pending) == 1
    assert "compiled" in pending[0].prompt_md
    assert db.get_task(task.id).status is TaskStatus.BLOCKED_ON_REVIEW


def test_dispatch_error_does_not_burn_a_retry(db, impl_spec):
    """Mac asleep, key missing, network down — never cost the spec an attempt."""
    spec, task, spec_dir = impl_spec
    result = ImplementerResult(files=[("Sources/A.swift", "struct A {}")], raw="")
    with mock.patch.object(d, "_generate_implementation", return_value=result), \
         mock.patch.object(d, "_load_plan",
                           return_value={"test_strategy": {"framework": "swift_test",
                                                           "repo": "centipede"}}), \
         mock.patch.object(d, "_run_tests_with_guard",
                           side_effect=OSError("connection refused")):
        d._run_implementer(db, spec, task, spec_dir)

    pending = [g for g in db.list_gates_for_spec(spec.id)
               if g.status is GateStatus.PENDING]
    assert len(pending) == 1
    assert db.get_task(task.id).retry_count == task.retry_count
    assert db.get_spec(spec.id).status is SpecStatus.EXECUTING


def test_build_failure_respects_the_retry_budget(db, impl_spec):
    """Without this the short-circuit loops past MAX_RETRIES forever, each pass
    costing a generation plus a runner dispatch, and the spec can never reach
    the synthesis escape hatch. Observed live on spec_ead8f7fc at "attempt 6/5"."""
    spec, task, spec_dir = impl_spec
    db.create_task(spec_id=spec.id, agent="reviewer", role="reviewer",
                   title="review")
    for _ in range(d.MAX_RETRIES):
        db.increment_task_retry(task.id)
    task = db.get_task(task.id)
    assert task.retry_count == d.MAX_RETRIES

    with mock.patch.object(d, "_legacy_attempt_retry") as exhausted:
        _run(db, spec, task, spec_dir, build_passed=False,
             build_output=SWIFT_BUILD_FAILURE)

    # Handed to synthesis, not looped round again.
    exhausted.assert_called_once()
    assert db.get_task(task.id).retry_count == d.MAX_RETRIES  # not incremented
    assert not [g for g in db.list_gates_for_spec(spec.id)
                if g.status is GateStatus.PENDING]


def test_build_failure_at_budget_with_no_reviewer_fails_the_spec(db, impl_spec):
    spec, task, spec_dir = impl_spec
    for _ in range(d.MAX_RETRIES):
        db.increment_task_retry(task.id)
    _run(db, spec, db.get_task(task.id), spec_dir, build_passed=False,
         build_output=SWIFT_BUILD_FAILURE)

    assert db.get_task(task.id).status is TaskStatus.FAILED
    assert db.get_spec(spec.id).status is SpecStatus.FAILED


def test_no_test_strategy_skips_the_check_entirely(db, impl_spec):
    spec, task, spec_dir = impl_spec
    result = ImplementerResult(files=[("a.py", "x = 1")], raw="")
    with mock.patch.object(d, "_generate_implementation", return_value=result), \
         mock.patch.object(d, "_load_plan", return_value={}), \
         mock.patch.object(d, "_run_tests_with_guard") as run_tests:
        d._run_implementer(db, spec, task, spec_dir)

    run_tests.assert_not_called()
    pending = [g for g in db.list_gates_for_spec(spec.id)
               if g.status is GateStatus.PENDING]
    assert len(pending) == 1
    assert "not run" in pending[0].prompt_md
