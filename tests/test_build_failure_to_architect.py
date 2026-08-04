"""A recurring build failure goes upstream to the architect — DEV-468.

Retrying the implementer is the wrong response when the implementer is
faithfully writing what the design specified. The design is re-read unchanged
on every attempt, so those diagnostics cannot self-correct; they just consume
the budget.

On spec_cc7dd609 three of five attempts reproduced three Swift errors that were
in the approved design — `mutating` on a `final class`, a `static` split with no
receiver, and a reference to an undeclared nested type.
"""
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import (
    GateStatus, GateType, SpecStatus, TaskStatus,
)

DIAG = ("World.swift:57:5: error: 'mutating' is not valid on instance methods "
        "in classes")


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def spec_with_roles(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    arch = db.create_task(spec_id=spec.id, agent="architect", role="architect",
                          title="design")
    impl = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="build")
    spec_dir = db.spec_dir(spec.id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    return db.get_spec(spec.id), arch, impl, spec_dir


def _prior_build_failure(db, spec_id, notes):
    gate = db.create_gate(spec_id=spec_id, task_id=None,
                          gate_type=GateType.CODE_REVIEW,
                          prompt_md="## Automated build-failure retry (DEV-429)")
    db.respond_to_gate(gate.id, "rejected", notes=notes)


def test_first_build_failure_stays_with_the_implementer(db, spec_with_roles):
    """One failure is not evidence of an upstream fault."""
    spec, arch, impl, spec_dir = spec_with_roles
    routed = d._route_build_failure_to_architect(
        db, spec, impl, spec_dir, f"## does not compile\n{DIAG}", DIAG)
    assert routed is False
    assert db.get_task(arch.id).retry_count == 0


def test_repeated_identical_failure_goes_to_the_architect(db, spec_with_roles):
    spec, arch, impl, spec_dir = spec_with_roles
    notes = f"## does not compile\n{DIAG}"
    _prior_build_failure(db, spec.id, notes)

    routed = d._route_build_failure_to_architect(db, spec, impl, spec_dir,
                                                 notes, DIAG)
    assert routed is True
    # architect re-engaged with the diagnostics as design feedback
    assert db.get_task(arch.id).status is TaskStatus.PENDING
    assert db.get_task(arch.id).retry_count == 1
    fb = (spec_dir / "design_review_feedback.md").read_text()
    assert "cannot be built as written" in fb
    assert "mutating" in fb
    assert "behavioural invariants" in fb  # told not to rewrite what was approved


def test_the_implementers_budget_is_not_charged(db, spec_with_roles):
    """The attempt was not the implementer's fault."""
    spec, arch, impl, spec_dir = spec_with_roles
    notes = f"## does not compile\n{DIAG}"
    _prior_build_failure(db, spec.id, notes)
    before = db.get_task(impl.id).retry_count

    d._route_build_failure_to_architect(db, spec, impl, spec_dir, notes, DIAG)

    assert db.get_task(impl.id).retry_count == before
    assert db.get_task(impl.id).status is TaskStatus.PENDING


def test_a_different_diagnostic_is_progress_and_stays_local(db, spec_with_roles):
    spec, arch, impl, spec_dir = spec_with_roles
    _prior_build_failure(db, spec.id, "## does not compile\nA.swift:1:1: error: alpha")
    routed = d._route_build_failure_to_architect(
        db, spec, impl, spec_dir, "## does not compile\nB.swift:2:2: error: beta",
        "B.swift:2:2: error: beta")
    assert routed is False


def test_an_exhausted_architect_does_not_loop(db, spec_with_roles):
    """Otherwise design and implementation ping-pong forever."""
    spec, arch, impl, spec_dir = spec_with_roles
    for _ in range(d.MAX_RETRIES):
        db.increment_task_retry(arch.id)
    notes = f"## does not compile\n{DIAG}"
    _prior_build_failure(db, spec.id, notes)

    routed = d._route_build_failure_to_architect(db, spec, impl, spec_dir,
                                                 notes, DIAG)
    assert routed is False
    assert db.get_task(arch.id).retry_count == d.MAX_RETRIES


def test_no_architect_task_is_handled(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    impl = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="build")
    spec_dir = db.spec_dir(spec.id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    notes = f"## does not compile\n{DIAG}"
    _prior_build_failure(db, spec.id, notes)
    assert d._route_build_failure_to_architect(
        db, spec, db.get_task(impl.id), spec_dir, notes, DIAG) is False


# ── end to end through _run_implementer ──────────────────────────────────────

def test_run_implementer_routes_upstream_instead_of_rotating(db, spec_with_roles):
    from coding_model_autonomous.executor import ImplementerResult
    spec, arch, impl, spec_dir = spec_with_roles
    (spec_dir / "spec.md").write_text("# demo\n")
    (spec_dir / "design.md").write_text("# design\n")
    notes_seed = ("## The code does not compile\n\nNo review was performed — "
                  f"the build failed, so there is nothing to review yet. First "
                  f"compiler diagnostic:\n\n    {DIAG}\n\n")
    _prior_build_failure(db, spec.id, notes_seed)

    result = ImplementerResult(files=[("Sources/A.swift", "struct A {}")], raw="")
    build_out = f"Building for debugging...\n{DIAG}\n"
    with mock.patch.object(d, "_generate_implementation", return_value=result), \
         mock.patch.object(d, "_load_plan",
                           return_value={"test_strategy": {"framework": "swift_test",
                                                           "repo": "centipede"}}), \
         mock.patch.object(d, "_run_tests_with_guard",
                           return_value=(False, build_out)):
        d._run_implementer(db, spec, db.get_task(impl.id), spec_dir)

    assert db.get_task(arch.id).status is TaskStatus.PENDING
    assert db.get_task(arch.id).retry_count == 1
    # no human gate, and no implementer retry charged
    assert not [g for g in db.list_gates_for_spec(spec.id)
                if g.status is GateStatus.PENDING]
    assert db.get_task(impl.id).retry_count == 0
