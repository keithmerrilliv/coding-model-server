# A VM that never ran the code requeues the build check; it never opens code_review (DEV-921)

## Context

Target repo: **coding-model-server** (self), `main` = `8e0ec8b`. Every fact below
was read from that commit.

On 2026-10-03 spec_39c092a6 (DEV-860) sent its pre-gate build check to the Mac
runner. The tart guest booted but never answered ssh. The runner answered HTTP
200, and its whole output was one line:

```
[vm] guest not reachable over ssh within 300s (CODING_MODEL_RUNNER_VM_BOOT_TIMEOUT); last ip=192.168.64.2
```

The daemon recorded `TEST_RAN {phase: pre_gate_build_check, passed: false,
build_failed: false}` and opened a `code_review` gate on code that was never
compiled. An auto-approver passed that gate six seconds later.

Why it happened, in `src/coding_model_server/orchestrator_daemon.py`:

- `_pre_gate_build_check` has a DEV-477/DEV-538 "inconclusive" branch: `passed`
  is False, there is no `build_reason`, and no test run was observed. That branch
  requeues only when `test_runner.is_runner_unreachable(build_output)` is true.
  That function matches only the literal `"mac-runner unreachable"`, so the VM
  line fell through to `return BuildCheck(...)` and the gate opened.
- DEV-705's VM classifier (`outcome.classify_test_run` →
  `_vm_infrastructure_refusal`) already reads that line as
  `FailureClass.SANDBOX_PROVISIONING`, a no-verdict. Today it is consulted only
  on the **build-failure** path (`if build_reason is not None:` in the
  implementer pass, the DEV-629 block) and on the reviewer's path (DEV-816). It
  is never consulted on the inconclusive path, which is the one a VM failure
  actually takes.

The fix applies the existing classifier on the inconclusive path too, and
disposes a SANDBOX_PROVISIONING failure the way the build-failure path already
does: `_dispose(...)`, which requeues uncharged and parks behind dispose's
infrastructure gate once `NO_VERDICT_CAP` is spent. No classifier, cap or
outcome change is needed.

### Reference run (made before this spec was written)

In a throwaway worktree of `8e0ec8b`, with exactly the edit and test file below:

| Tree | `tests/seams/test_vm_boot_build_check.py` | Whole `tests/` suite |
| --- | --- | --- |
| `main` + new test file only | 2 failed, 1 passed (the classifier test passes: it pins existing behaviour) | not run |
| `main` + the edit + new test file | 3 passed | 3145 passed, 20 skipped |

On `main` the routing test fails at `assert len(model.calls_for("implementer")) == 2`
(`1 == 2`): the code_review gate opened after the first, never-built attempt.
Do not re-derive any of this.

### How to emit the two files (run 1 failed on exactly this)

The first run of this spec (spec_fa57f216) failed twice in a row on emission,
not on code: the implementer sent the NEW test file as a SEARCH/REPLACE block.
A file that does not exist yet has nothing to search, so the edit could not
apply, and with the daemon file at ~349k characters synthesis could not
recover. So:

- **`src/coding_model_server/orchestrator_daemon.py` exists and is about 6,900
  lines. Emit its one change as an anchored SEARCH/REPLACE block, never as a
  whole file.** The SEARCH block is the three anchor lines quoted in Edit 1; the
  REPLACE block is those same three lines followed by the inserted lines.
- **`tests/seams/test_vm_boot_build_check.py` does NOT exist. Emit it as ONE
  complete whole-file block** that opens with
  `<<<FILE: tests/seams/test_vm_boot_build_check.py>>>` and is closed by the
  END_FILE marker on its own line. Never as SEARCH/REPLACE.

## Authoritative design — reproduce this in your design document

### Edit 1 — `src/coding_model_server/orchestrator_daemon.py` (the only source edit)

In `_pre_gate_build_check`, the inconclusive branch ends with these three lines.
They are unique in the file, so they are the anchor:

```python
            if test_runner.is_runner_unreachable(build_output):
                if _requeue_for_unreachable_runner(db, spec, task):
                    return BuildCheck(requeued=True)
```

Immediately after them, **at the same 12-space indentation, still inside the
`elif` branch**, and before the function's final
`return BuildCheck(build_reason, build_output, build_passed, build_framework,`,
insert exactly:

```python
            # DEV-921: the runner answered, but its VM layer never ran the
            # code — a guest that did not come up, a failed sync. DEV-705's
            # classifier already says so on the build-failure path below and
            # on the reviewer's (DEV-816); without it here that output fell
            # through to a code_review gate on code nothing had compiled.
            vm_failure = classify_test_run(
                build_output, role="implementer", passed=False,
                build_reason=None, unreachable=False)
            if (vm_failure is not None
                    and vm_failure.cls is FailureClass.SANDBOX_PROVISIONING):
                _dispose(db, spec, task, vm_failure)
                return BuildCheck(requeued=True)
```

Notes for the design:

- `classify_test_run`, `FailureClass`, `_dispose` and `BuildCheck` are already
  imported or defined in `orchestrator_daemon.py`. **Add no imports.**
- `BuildCheck(requeued=True)` is the existing way to tell the caller "the task
  was put down, return now". The caller (`if check.requeued: return`) already
  handles it. Use it whether `_dispose` requeued or parked.
- Do not touch the DEV-548 crash branch above, or the
  `is_runner_unreachable` requeue: an unreachable runner still goes the
  RUNNER_OUTAGE way, with its own cap of 3.
- `_vm_infrastructure_refusal` returns None when the output carries a real build
  or suite result, so a test that merely prints `[vm]` stays a verdict. That
  guarantee already lives in `outcome.py` and is already tested
  (`tests/test_vm_leak_guard.py`). Do not re-implement it.

### Edit 2 — new test file `tests/seams/test_vm_boot_build_check.py`

It goes in `tests/seams/` because it uses that directory's `db`, `model` and
`runner` fixtures (`tests/seams/conftest.py`) and the `seam_fakes` /
`seam_harness` helpers, which are importable by bare module name only from
there. Create it with exactly this content:

```python
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
```

`TestOutcome` is imported under the alias `_TestOutcome` on purpose: imported
under its own name, pytest tries to collect it as a test class and warns.

## Change surface

| Path | Action |
|---|---|
| `src/coding_model_server/orchestrator_daemon.py` | modify — the one insertion in Edit 1, nothing else — emitted as ONE anchored SEARCH/REPLACE block |
| `tests/seams/test_vm_boot_build_check.py` | new test file, verbatim from Edit 2 — emitted WHOLE (`<<<FILE: …>>>`), never SEARCH/REPLACE |

## Protected paths — must not be modified

- `src/coding_model_autonomous/` (all of it: `outcome.py` already has the classifier, and `test_runner.py` keeps `is_runner_unreachable` as it is)
- `src/coding_model_client/`
- `src/coding_model_server/` other than `orchestrator_daemon.py`
- `mac_runner/` (the runner's message wording is not changed by this spec)
- `scripts/` (the auto-approver hazard named in DEV-921 is a separate change)
- `tests/seams/conftest.py`, `tests/seams/seam_fakes.py`, `tests/seams/seam_harness.py`
- All existing tests.

## Acceptance criteria (hermetic pytest: no model calls, no runner, no network)

- **T1** `test_the_dev_921_output_is_a_vm_no_verdict`: the verbatim DEV-921
  line classifies as `SANDBOX_PROVISIONING` with outcome `NO_VERDICT`.
- **T2** `test_vm_boot_timeout_requeues_instead_of_opening_code_review`: one VM
  boot timeout, then a passing check. The implementer is called twice with
  `retry_count == 0`. The only FAILURE_CLASSIFIED event is
  `("sandbox_provisioning", "no_verdict", "requeue")`. The build-check
  `passed` values are `[False, True]`. Exactly one code_review gate exists, the
  one after the pass.
- **T3** `test_a_vm_that_never_comes_up_never_reaches_code_review`: VM boot
  timeouts on every dispatch. No code_review gate is ever created. `retry_count`
  stays 0. The last sandbox_provisioning event is a `park` and every earlier one
  is a `requeue`, so the run waits on dispose's infrastructure gate (CLARIFICATION).
- **T4** The whole existing suite still passes, in particular
  `tests/seams/test_fault_matrix.py` (`TestRunnerFaults`),
  `tests/test_unreachable_runner_requeue.py`, `tests/test_vm_leak_guard.py`,
  `tests/test_build_check_reporting.py` and
  `tests/test_build_failure_vs_crashed_run.py`.

## Constraints

- No new dependencies. No DB schema change. No new event kind, failure class or cap.
- No change to `is_runner_unreachable`, `RUNNER_UNREACHABLE`, `classify_test_run`,
  `_vm_infrastructure_refusal`, `_CAPS` or `NO_VERDICT_CAP`.
- The plan must carry the `repo` and `protected_paths` keys exactly as written
  below, so the existing daemon file is fetched at `base_ref`.

## test_strategy

```yaml
repo: coding-model-server
framework: pytest
required: true
protected_paths:
  - src/coding_model_autonomous/
  - src/coding_model_client/
  - mac_runner/
  - scripts/
  - tests/seams/conftest.py
  - tests/seams/seam_fakes.py
  - tests/seams/seam_harness.py
```
