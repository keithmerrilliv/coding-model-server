"""Built-then-died is not never-built — DEV-548.

`_BUILD_FAILURE_RES["swift_test"]` accepts a bare `^error: ` line, which is how
DEV-435's unattributed compile-stage failures (`emit-module command failed`,
`fatalError`) get caught. It also matched anything the *test harness* printed,
including long after the build finished.

Run 9 of DEV-102 ended with `Build complete! (3.00s)`, 19 tests launched, and
`error: Process '…swiftpm-testing-helper…' exited with unexpected signal code
5`. That was reported to the model as "the code does not compile" — false, and
it buried the one located clue in the output (the warning DEV-547 parses).

These pin the ordering discriminator, the DEV-435 cases it must not disturb,
and the third outcome: compiled, ran, crashed.
"""
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.executor import ImplementerResult
from coding_model_autonomous.models import GateStatus, SpecStatus, TaskStatus

# Run 9's real terminal shape (spec_9ff962b9).
COMPILED_THEN_CRASHED = """\
Building for debugging...
[7/10] Compiling CentipedeCore World.swift
/Users/km4/Library/Caches/coding-model-runner/worktrees/spec_9ff962b9-09f0ad65/\
Sources/CentipedeCore/World.swift:238:20: warning: value 'updateIdx' was \
defined but never used; consider replacing with boolean test [#no-usage]
[14/15] Linking CentipedePackageTests
Build complete! (3.00s)
◇ Test run started.
◇ Test "C4: Chain advances with segments following" started.
error: Process '/Applications/Xcode.app/Contents/Developer/Toolchains/\
XcodeDefault.xctoolchain/usr/libexec/swift/pm/swiftpm-testing-helper \
--parallel' exited with unexpected signal code 5
"""

# DEV-435's case: the module never emitted, no attribution anywhere.
EMIT_MODULE_FAILURE = """\
Building for debugging...
[9/12] Compiling CentipedeCore World.swift
[10/12] Emitting module CentipedeCore
error: emit-module command failed with exit code 1 (use -v to see invocation)
"""

ATTRIBUTED_FAILURE = """\
Building for debugging...
/wt/worktrees/s-1/Sources/CentipedeCore/World.swift:17:19: error: \
'DeterministicRNG' is inaccessible due to 'private' protection level
error: fatalError
"""


# ── the discriminator ────────────────────────────────────────────────────────

def test_compiled_then_crashed_is_not_a_build_failure():
    """The acceptance criterion: run 9's output stops claiming a build error."""
    assert d._detect_build_failure(
        COMPILED_THEN_CRASHED, "swift_test", passed=False) is None


def test_compiled_then_crashed_is_recognised_as_its_own_outcome():
    reason = d._detect_test_process_crash(COMPILED_THEN_CRASHED)
    assert reason is not None
    assert "signal 5" in reason


def test_emit_module_failure_is_still_a_build_failure():
    """DEV-435 regression guard. This is why the bare `error:` alternative
    exists and it must keep working — there is no completion marker here."""
    reason = d._detect_build_failure(EMIT_MODULE_FAILURE, "swift_test",
                                     passed=False)
    assert reason is not None
    assert "emit-module" in reason


def test_attributed_diagnostic_is_unchanged():
    reason = d._detect_build_failure(ATTRIBUTED_FAILURE, "swift_test",
                                     passed=False)
    assert reason is not None
    assert "DeterministicRNG" in reason


def test_bare_error_before_a_completed_build_still_counts():
    """Without a completion marker the ordering rule cannot fire, so the old
    behaviour is preserved exactly."""
    out = "Building for debugging...\nerror: fatalError\n"
    assert d._detect_build_failure(out, "swift_test", passed=False) is not None


def test_fatal_error_after_a_completed_build_still_counts():
    """`error: fatalError` is the driver reporting a crashed *build* sub-job.
    Demoting it on ordering alone would change behaviour on real failures, so
    it is listed as a compile-stage error and wins wherever it appears."""
    out = "Build complete! (1.00s)\nerror: fatalError\n"
    assert d._detect_build_failure(out, "swift_test", passed=False) is not None


def test_attributed_error_after_a_completed_build_still_counts():
    """Ordering only excuses UNATTRIBUTED errors. A compiler diagnostic naming
    a file is the compiler talking, whenever it appears."""
    out = ("Build complete! (1.00s)\n"
           "/wt/worktrees/s-1/Sources/A.swift:3:4: error: cannot find 'x'\n")
    reason = d._detect_build_failure(out, "swift_test", passed=False)
    assert reason is not None
    assert "cannot find 'x'" in reason


def test_an_earlier_real_error_is_not_masked_by_a_later_crash():
    """The scan keeps looking past a post-build process error."""
    combined = ATTRIBUTED_FAILURE + COMPILED_THEN_CRASHED
    reason = d._detect_build_failure(combined, "swift_test", passed=False)
    assert reason is not None
    assert "DeterministicRNG" in reason


def test_xcodebuild_build_commands_failed_still_counts():
    out = "** BUILD SUCCEEDED **\nThe following build commands failed:\n"
    assert d._detect_build_failure(
        out, "xcodebuild_test", passed=False) is not None


def test_pytest_is_untouched():
    out = "ERROR collecting tests/test_x.py\nE   ModuleNotFoundError: no app\n"
    assert d._detect_build_failure(out, "pytest", passed=False) is not None


def test_passing_run_is_still_never_a_build_failure():
    assert d._detect_build_failure(
        COMPILED_THEN_CRASHED, "swift_test", passed=True) is None


def test_normal_output_is_not_a_crash():
    assert d._detect_test_process_crash("Executed 17 tests, 0 failures") is None
    assert d._detect_test_process_crash("") is None


# ── what the reviewer is told ────────────────────────────────────────────────

def test_gate_says_compiled_then_crashed_not_inconclusive():
    """"No summary" is not "we learned nothing" — this output says the code
    compiles and traps, and a reviewer sent hunting a build problem is being
    sent to the wrong place."""
    line = d._build_check_line(False, COMPILED_THEN_CRASHED, "swift_test")
    assert "compiled, then crashed" in line
    assert "inconclusive" not in line
    assert "signal 5" in line


def test_gate_still_says_inconclusive_when_nothing_is_known():
    line = d._build_check_line(False, "the runner said nothing useful\n",
                               "swift_test")
    assert "inconclusive" in line


# ── end to end ───────────────────────────────────────────────────────────────

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
                           return_value=(build_passed, build_output)):
        d._run_implementer(db, spec, task, spec_dir)


def test_run9_now_reaches_the_dev547_warning_path(db, impl_spec):
    """DEV-547's acceptance criterion, which it could not meet on its own:
    run 9's real output routes back naming World.swift:238, not a false
    claim that the code does not compile."""
    spec, task, spec_dir = impl_spec
    _run(db, spec, task, spec_dir, build_passed=False,
         build_output=COMPILED_THEN_CRASHED)

    gates = db.list_gates_for_spec(spec.id)
    assert len(gates) == 1
    assert gates[0].status is GateStatus.REJECTED
    notes = gates[0].reviewer_notes
    assert "World.swift:238:20" in notes
    assert "does not compile" not in notes
    assert db.get_task(task.id).status is TaskStatus.PENDING
    assert db.get_task(task.id).retry_count == task.retry_count + 1


def test_a_crash_with_no_warning_reaches_a_human_honestly(db, impl_spec):
    """Nothing located to act on, so it needs a human — but the gate must not
    call it a build failure or an inconclusive run."""
    spec, task, spec_dir = impl_spec
    crash_only = ("Building for debugging...\nBuild complete! (1.0s)\n"
                  "◇ Test run started.\n"
                  "error: Process 'helper' exited with unexpected signal code 6\n")
    _run(db, spec, task, spec_dir, build_passed=False, build_output=crash_only)

    pending = [g for g in db.list_gates_for_spec(spec.id)
               if g.status is GateStatus.PENDING]
    assert len(pending) == 1
    assert "compiled, then crashed" in pending[0].prompt_md
    assert db.get_task(task.id).status is TaskStatus.BLOCKED_ON_REVIEW
    # And it is not mistaken for an unreachable runner, so no requeue.
    assert db.get_task(task.id).retry_count == task.retry_count


def test_emit_module_failure_still_rotates_as_a_build_failure(db, impl_spec):
    spec, task, spec_dir = impl_spec
    _run(db, spec, task, spec_dir, build_passed=False,
         build_output=EMIT_MODULE_FAILURE)

    gates = db.list_gates_for_spec(spec.id)
    assert gates[0].status is GateStatus.REJECTED
    assert "does not compile" in gates[0].reviewer_notes
    assert (spec_dir / "build_failure.txt").exists()
