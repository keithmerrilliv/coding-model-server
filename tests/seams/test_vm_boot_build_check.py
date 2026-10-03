"""A Mac-runner VM that never ran the code requeues; it never opens code_review (DEV-921).

spec_39c092a6 (DEV-860), 2026-10-03: the tart guest booted but never answered
ssh, the runner returned HTTP 200 with one `[vm]` line, and the pre-gate build
check read it as merely inconclusive. Only the literal "mac-runner unreachable"
requeued, so a code_review gate opened on code nothing had compiled — and an
auto-approver passed it six seconds later.
"""
from __future__ import annotations

from coding_model_autonomous import EventKind, GateType
from coding_model_autonomous import outcome as _outcome

from seam_fakes import PytestPass, Reply
from seam_fakes import TestOutcome as _TestOutcome  # not collected as a test class
from seam_harness import (
    approve_design, drive, events, implementer_reply, make_executing_spec,
    wait_at,
)

# Verbatim from spec_39c092a6's build_check_output.txt.
VM_BOOT_TIMEOUT = (
    "[vm] guest not reachable over ssh within 300s "
    "(CODING_MODEL_RUNNER_VM_BOOT_TIMEOUT); last ip=192.168.64.2")


def VMBootTimeout() -> _TestOutcome:
    return _TestOutcome(False, VM_BOOT_TIMEOUT, "vm_boot_timeout")


def _impl_ready(db):
    spec = make_executing_spec(db)
    approve_design(db, spec)
    return spec


def test_the_dev_921_output_is_a_vm_no_verdict():
    failure = _outcome.classify_test_run(
        VM_BOOT_TIMEOUT, role="implementer", passed=False,
        build_reason=None, unreachable=False)
    assert failure is not None
    assert failure.cls is _outcome.FailureClass.SANDBOX_PROVISIONING
    assert failure.outcome is _outcome.Outcome.NO_VERDICT


def test_vm_boot_timeout_requeues_instead_of_opening_code_review(db, model, runner):
    spec = _impl_ready(db)
    model.always("implementer", Reply(implementer_reply()))
    runner.then(VMBootTimeout(), PytestPass())

    out = drive(db, spec.id, model, wait_at(GateType.CODE_REVIEW), runner=runner)

    assert out.reason == "waiting"
    # Uncharged: the requeue spends no retry and rotates no agent.
    assert out.task("implementer").retry_count == 0
    assert len(model.calls_for("implementer")) == 2
    ev = events(db, spec.id, EventKind.FAILURE_CLASSIFIED)
    assert [(e["cls"], e["outcome"], e["disposition"]) for e in ev] == [
        ("sandbox_provisioning", "no_verdict", "requeue")]
    checks = events(db, spec.id, EventKind.TEST_RAN, phase="pre_gate_build_check")
    assert [c["passed"] for c in checks] == [False, True]
    # Exactly one code_review gate, and it is the one after the passing check.
    reviews = db.list_gates_for_spec(spec.id, GateType.CODE_REVIEW)
    assert len(reviews) == 1
    assert "passed" in out.waiting_on[0].prompt_md


def test_a_vm_that_never_comes_up_never_reaches_code_review(db, model, runner):
    spec = _impl_ready(db)
    model.always("implementer", Reply(implementer_reply()))
    runner.then(*[VMBootTimeout() for _ in range(12)])

    out = drive(db, spec.id, model, wait_at(GateType.CLARIFICATION), runner=runner)

    assert out.reason == "waiting"
    assert db.list_gates_for_spec(spec.id, GateType.CODE_REVIEW) == []
    assert out.task("implementer").retry_count == 0
    ev = events(db, spec.id, EventKind.FAILURE_CLASSIFIED,
                cls="sandbox_provisioning")
    assert ev and ev[-1]["disposition"] == "park"
    assert all(e["disposition"] == "requeue" for e in ev[:-1])
