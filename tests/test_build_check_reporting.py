"""The code_review gate must not claim a build it cannot show — DEV-477/478.

Run 4 of the Centipede spec (spec_8dac1142, 2026-08-04) opened a human gate
saying `Build check: **compiled**, but the suite has failing tests`. The code
did not compile; re-running the identical check produced five Swift errors.
The reviewer was told not to look for the very thing that was wrong, and the
output that would have contradicted it had been discarded.

Two separate defects, tested separately:
  DEV-477 — `build_passed is False` was read as proof of a successful compile.
            It means only "did not pass, and no diagnostic was recognised",
            which is equally true of a dispatch error or a timeout.
  DEV-478 — the runner's output was persisted only on the diagnostic path, so
            the gate named no failing test and left no artifact behind.
"""
import pathlib
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import GateStatus, SpecStatus, TaskStatus

# A real swift-testing summary and a real XCTest summary.
SWIFT_TESTING_FAIL = (
    "Test run started.\n"
    "✘ Test \"mushroom absorbs four hits\" recorded an issue at "
    "MushroomFieldTests.swift:41:9\n"
    "✘ Test run with 17 tests failed after 0.412 seconds with 2 issues.\n"
)
XCTEST_FAIL = (
    "Test Suite 'All tests' failed at 2026-08-04 12:00:00.000\n"
    "Executed 17 tests, with 2 failures (0 unexpected) in 0.412 seconds\n"
)
# What a runner-level failure looks like: no summary, no diagnostic.
DISPATCH_FAILURE = (
    "mac runner: worktree materialisation failed for base_ref 'main'\n"
    "connection reset by peer\n"
)
# The real thing, trimmed from run 4's re-dispatched check.
REAL_BUILD_FAILURE = (
    "Building for debugging...\n"
    "/Users/km4/Library/Caches/coding-model-runner/worktrees/"
    "spec_8dac1142-cfb386ed/Sources/CentipedeCore/WorldState.swift:92:33: "
    "error: cannot use mutating member on immutable value: 'field' is a "
    "'let' constant\n"
    "error: fatalError\n"
)


# ── DEV-477: the wording must match the evidence ─────────────────────────────

def test_a_dispatch_failure_is_not_reported_as_compiled():
    """The regression. Nothing was built, so nothing may claim it built."""
    line = d._build_check_line(False, DISPATCH_FAILURE, "swift_test")
    assert "compiled" not in line.lower()
    assert "inconclusive" in line.lower()
    assert "unverified" in line.lower()


def test_empty_output_is_not_reported_as_compiled():
    assert "inconclusive" in d._build_check_line(False, "", "swift_test").lower()


@pytest.mark.parametrize("summary", [SWIFT_TESTING_FAIL, XCTEST_FAIL])
def test_a_real_swift_run_still_reads_as_behaviour_failures(summary):
    """The useful message must survive the fix, or reviewers lose a real signal."""
    line = d._build_check_line(False, summary, "swift_test")
    assert "**compiled**" in line
    assert "behaviour" in line


def test_a_passing_build_is_unchanged():
    assert "compiled and the suite passed" in d._build_check_line(
        True, SWIFT_TESTING_FAIL, "swift_test")


def test_no_framework_is_unchanged():
    assert "not run" in d._build_check_line(None, "", "")


def test_pytest_summary_is_still_recognised():
    line = d._build_check_line(False, "2 failed, 3 passed in 0.5s\n", "pytest")
    assert "**compiled**" in line


def test_pytest_without_a_summary_is_inconclusive():
    line = d._build_check_line(False, "bwrap: sandbox setup failed\n", "pytest")
    assert "inconclusive" in line.lower()


def test_observation_never_guesses_on_swift():
    """Swift patterns are unvalidated against a real green run, so absence of
    a match must mean 'no evidence', never 'assume it ran'."""
    assert d._observed_a_test_run(SWIFT_TESTING_FAIL, "swift_test") is True
    assert d._observed_a_test_run(XCTEST_FAIL, "swift_test") is True
    assert d._observed_a_test_run(REAL_BUILD_FAILURE, "swift_test") is False
    assert d._observed_a_test_run(DISPATCH_FAILURE, "swift_test") is False


def test_the_guard_itself_is_untouched():
    """_observed_a_test_run must not have tightened the anti-hallucination
    guard — that one gates control flow, and a false negative there fails a
    genuinely passing suite."""
    ok, _ = d._validate_test_output_structure(REAL_BUILD_FAILURE, "swift_test")
    assert ok is True


# ── DEV-478: the output must survive ─────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def impl_spec(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    db.create_task(spec_id=spec.id, agent="architect", role="architect", title="d")
    impl = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="build")
    spec_dir = db.spec_dir(spec.id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text("# demo\n")
    (spec_dir / "design.md").write_text("# design\n")
    return db.get_spec(spec.id), impl, spec_dir


def _run(db, spec, impl, spec_dir, output):
    from coding_model_autonomous.executor import ImplementerResult
    result = ImplementerResult(files=[("Sources/A.swift", "struct A {}")], raw="")
    with mock.patch.object(d, "_generate_implementation", return_value=result), \
         mock.patch.object(d, "_load_plan",
                           return_value={"test_strategy": {"framework": "swift_test",
                                                           "repo": "centipede"}}), \
         mock.patch.object(d, "_run_tests_with_guard", return_value=(False, output)):
        d._run_implementer(db, spec, db.get_task(impl.id), spec_dir)


def test_output_is_kept_even_when_no_diagnostic_matched(db, impl_spec):
    """The regression: this is exactly the path that used to keep nothing."""
    spec, impl, spec_dir = impl_spec
    _run(db, spec, impl, spec_dir, SWIFT_TESTING_FAIL)
    kept = spec_dir / "build_check_output.txt"
    assert kept.is_file()
    assert "17 tests failed" in kept.read_text()


def test_the_gate_shows_the_reviewer_the_failures(db, impl_spec):
    spec, impl, spec_dir = impl_spec
    _run(db, spec, impl, spec_dir, SWIFT_TESTING_FAIL)
    gate = [g for g in db.list_gates_for_spec(spec.id)
            if g.status is GateStatus.PENDING][0]
    assert "mushroom absorbs four hits" in gate.prompt_md, (
        "a gate that says 'the suite has failing tests' must name them")


def test_a_dispatch_failure_gate_says_so(db, impl_spec):
    """End to end: the run-4 shape, with the claim corrected."""
    spec, impl, spec_dir = impl_spec
    _run(db, spec, impl, spec_dir, DISPATCH_FAILURE)
    gate = [g for g in db.list_gates_for_spec(spec.id)
            if g.status is GateStatus.PENDING][0]
    assert "inconclusive" in gate.prompt_md.lower()
    assert "**compiled**" not in gate.prompt_md
    assert (spec_dir / "build_check_output.txt").is_file()


def test_a_passing_build_still_keeps_its_output(db, impl_spec):
    spec, impl, spec_dir = impl_spec
    from coding_model_autonomous.executor import ImplementerResult
    result = ImplementerResult(files=[("Sources/A.swift", "struct A {}")], raw="")
    passing = "✔ Test run with 17 tests passed after 0.4 seconds.\n"
    with mock.patch.object(d, "_generate_implementation", return_value=result), \
         mock.patch.object(d, "_load_plan",
                           return_value={"test_strategy": {"framework": "swift_test",
                                                           "repo": "centipede"}}), \
         mock.patch.object(d, "_run_tests_with_guard", return_value=(True, passing)):
        d._run_implementer(db, spec, db.get_task(impl.id), spec_dir)
    assert (spec_dir / "build_check_output.txt").is_file()


def test_the_diagnostic_path_still_writes_build_failure_txt(db, impl_spec):
    """DEV-468's lookback and the retry feedback both read this file; the new
    artifact must be additional, not a replacement."""
    spec, impl, spec_dir = impl_spec
    _run(db, spec, impl, spec_dir, REAL_BUILD_FAILURE)
    assert (spec_dir / "build_failure.txt").is_file()
    assert (spec_dir / "build_check_output.txt").is_file()


# ── replay of the real run-4 artifacts ───────────────────────────────────────

RUN4 = pathlib.Path("var/tasks_db/specs/spec_8dac1142")


@pytest.mark.skipif(not (RUN4 / "retry_history").is_dir(),
                    reason="run 4 artifacts not present on this machine")
def test_every_recorded_run4_build_failure_is_correctly_classified():
    """Each retry that recorded a diagnostic must read as a build failure, and
    none of them may be described as having compiled."""
    seen = 0
    for r in sorted((RUN4 / "retry_history").glob("retry_*")):
        f = r / "build_failure.txt"
        if not f.is_file():
            continue
        seen += 1
        text = f.read_text()
        assert d._detect_build_failure(text, "swift_test", False) is not None
        assert "**compiled**" not in d._build_check_line(False, text, "swift_test")
    assert seen >= 2, f"expected several recorded failures, saw {seen}"
