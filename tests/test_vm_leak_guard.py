"""DEV-705: a leaked tart VM must not cost the next spec its attempt.

Two xcodebuild_test runs completed normally and left their VMs registered.
The third died in 3 seconds on tart's own system limit, naming two VM ids and
no remedy — at a DIFFERENT spec's dispatch, after that attempt had already
spent its context assembly and its file write.

Three things were wrong and all three are covered here:

  1. teardown failure was swallowed with a log line, on a host nobody watches;
  2. nothing swept the leak, so it blocked every later dispatch;
  3. the refusal was classified from the test result, so infrastructure was
     charged to the implementer as a verdict.
"""

import subprocess
from unittest import mock

import pytest

from coding_model_autonomous.outcome import (
    FailureClass,
    NO_VERDICT_CLASSES,
    VERDICT_CLASSES,
    classify_test_run,
)

vm = pytest.importorskip("mac_runner.vm")


# ── 3. the classifier: infrastructure is never a verdict ────────────────────

TART_LIMIT = (
    "[vm] tart run exited 1 before the guest came up\n"
    "[tart run output]\n"
    "The number of VMs exceeds the system limit "
    "(other running VMs: cmr-898f99419541, cmr-77dff4c25155)\n"
)


@pytest.mark.parametrize("output", [
    pytest.param(TART_LIMIT, id="tart-system-limit"),
    pytest.param("[vm] tart clone 'ghcr.io/x' failed: no space left",
                 id="clone-failed"),
    pytest.param("[vm] worktree sync failed: connection closed",
                 id="sync-failed"),
    pytest.param("[vm] another VM dispatch has held the single VM slot for "
                 "more than 30s — refusing to start a second one.",
                 id="slot-refused"),
])
def test_vm_infrastructure_is_a_no_verdict(output):
    failure = classify_test_run(output, role="implementer", passed=False,
                                build_reason=None, unreachable=False)
    assert failure is not None
    assert failure.cls is FailureClass.SANDBOX_PROVISIONING
    assert failure.cls in NO_VERDICT_CLASSES
    assert failure.cls not in VERDICT_CLASSES


def test_vm_refusal_outranks_a_build_reason():
    """A guest that never came up produces both; only one is the cause."""
    failure = classify_test_run(TART_LIMIT, role="implementer", passed=False,
                                build_reason="xcodebuild: command not found",
                                unreachable=False)
    assert failure.cls is FailureClass.SANDBOX_PROVISIONING


def test_a_real_test_failure_is_still_a_verdict():
    """Negative control. The whole point is not to swallow real failures."""
    failure = classify_test_run(
        "Test Case '-[GameTests testSpiderScore]' failed (0.01 seconds).\n"
        "** TEST FAILED **", role="implementer", passed=False,
        build_reason=None, unreachable=False)
    assert failure.cls is FailureClass.TESTS_FAILED
    assert failure.cls in VERDICT_CLASSES


def test_a_test_that_merely_prints_the_vm_token_is_still_a_verdict():
    """Narrowness control: the marker alone must not reclassify a failure."""
    failure = classify_test_run(
        "[vm] starting fixture\nTest Case 'testX' failed.\n** TEST FAILED **",
        role="implementer", passed=False, build_reason=None, unreachable=False)
    assert failure.cls is FailureClass.TESTS_FAILED


def test_a_pass_is_still_a_pass():
    assert classify_test_run("** TEST SUCCEEDED **", role="implementer",
                             passed=True, build_reason=None,
                             unreachable=False) is None


# ── 1 & 2. the runner: sweep the leak, report the teardown ──────────────────

TART_LIST = (
    "Source Name              Disk Size State\n"
    "local  cmr-898f99419541  50   12   stopped\n"
    "local  cmr-77dff4c25155  50   12   running\n"
    "local  sonoma-base       50   30   stopped\n"
)


def _fake_tart(calls, *, delete_ok=True):
    def run(argv, *a, **kw):
        calls.append(list(argv))
        if argv[:2] == ["tart", "list"]:
            return subprocess.CompletedProcess(argv, 0, TART_LIST, "")
        if argv[:2] == ["tart", "delete"] and not delete_ok:
            return subprocess.CompletedProcess(argv, 1, "", "delete failed")
        return subprocess.CompletedProcess(argv, 0, "", "")
    return run


def test_list_runner_vms_finds_only_the_runner_prefix():
    calls = []
    with mock.patch.object(subprocess, "run", _fake_tart(calls)):
        assert vm.list_runner_vms() == ["cmr-898f99419541", "cmr-77dff4c25155"]


def test_sweep_reclaims_leaked_vms_and_says_so():
    calls, warnings = [], []
    with mock.patch.object(subprocess, "run", _fake_tart(calls)):
        reclaimed = vm.sweep_leaked_vms(warnings)
    assert reclaimed == ["cmr-898f99419541", "cmr-77dff4c25155"]
    assert any(["tart", "delete", "cmr-898f99419541"] == c for c in calls)
    assert warnings and "reclaimed 2 leaked VM(s)" in warnings[0]


def test_sweep_never_touches_a_vm_a_live_dispatch_owns():
    """A concurrent run must not be swept out from under itself."""
    calls, warnings = [], []
    with mock.patch.object(vm, "_ACTIVE_VMS", {"cmr-77dff4c25155"}):
        with mock.patch.object(subprocess, "run", _fake_tart(calls)):
            reclaimed = vm.sweep_leaked_vms(warnings)
    assert reclaimed == ["cmr-898f99419541"]
    assert not any("cmr-77dff4c25155" in " ".join(c) and "delete" in c
                   for c in calls)


def test_sweep_is_silent_when_there_is_nothing_to_reclaim():
    """Negative control: a clean host adds no warning."""
    calls, warnings = [], []
    clean = "Source Name         Disk Size State\nlocal  sonoma-base 50 30 stopped\n"

    def run(argv, *a, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, clean, "")

    with mock.patch.object(subprocess, "run", run):
        assert vm.sweep_leaked_vms(warnings) == []
    assert warnings == []


def test_failed_teardown_reports_the_leak_on_the_response():
    """The evidence used to live only in the Mac's log."""
    calls, warnings = [], []
    with mock.patch.object(subprocess, "run",
                           _fake_tart(calls, delete_ok=False)):
        vm._destroy("cmr-deadbeef0001", None, warnings)
    assert warnings, "a failed teardown must say so"
    assert "cmr-deadbeef0001" in warnings[0]
    assert "leaked" in warnings[0].lower()
    assert "infrastructure fault" in warnings[0].lower()


def test_successful_teardown_is_silent():
    """Negative control for the rule above."""
    calls, warnings = [], []
    with mock.patch.object(subprocess, "run", _fake_tart(calls)):
        vm._destroy("cmr-deadbeef0002", None, warnings)
    assert warnings == []
