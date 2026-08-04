"""Synthesis that fails to BUILD gets a repair round too — DEV-469.

DEV-406 gives a near-miss synthesis one targeted repair before the spec fails,
gated on a test pass-rate. A build failure produces no runner summary, so the
rate is unmeasurable and the repair used to be skipped — inverting the intent,
since a synthesis that fails to compile is usually nearer to right than one
that compiles and fails a third of its assertions.

It matters more since DEV-433 made synthesis reachable from build-driven
exhaustion: every failure arriving that way is a build failure by construction.
On spec_cc7dd609 synthesis merged six attempts, got further than any single
attempt, and died on two one-line type errors with no repair attempted.
"""
from types import SimpleNamespace
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import SpecStatus

# Trimmed from spec_cc7dd609's actual synthesis output.
BUILD_FAILURE = """\
Building for debugging...
[9/16] Emitting module CentipedeCore
/Users/km4/.../Sources/CentipedeCore/SeededRandomGenerator.swift:7:53: error: \
cannot convert value of type 'Int' to expected argument type 'Int64'
 7 |         self.state = seed != 0 ? UInt64(bitPattern: seed) : 1
error: fatalError
"""

TEST_FAILURE_NEAR_MISS = "Executed 20 tests\n18 passed, 2 failed in 1.2s\n"
TEST_FAILURE_FAR = "Executed 20 tests\n4 passed, 16 failed in 1.2s\n"
NO_SIGNAL = "the runner produced nothing useful\n"


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def synth_spec(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    spec_dir = db.spec_dir(spec.id)
    (spec_dir / "spec.md").write_text("# spec")
    (spec_dir / "design.md").write_text("# design")
    (spec_dir / "impl.swift").write_text("struct A {}")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    impl = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="build")
    return db.get_spec(spec.id), impl, spec_dir


def _run(db, spec, impl, spec_dir, first_output, *, repair_passes=False,
         framework="swift_test"):
    """Drive _run_synthesis to its repair decision; report whether it repaired."""
    synth = SimpleNamespace(files=[("impl.swift", "struct A {}")])
    outputs = [(False, first_output)]
    if repair_passes:
        outputs.append((True, "Executed 20 tests\n20 passed in 1.0s\n"))
    else:
        outputs.append((False, first_output))
    calls = {"agent": 0}

    def _agent(*a, **k):
        calls["agent"] += 1
        return "raw"

    with mock.patch.object(d, "call_agent", side_effect=_agent), \
         mock.patch.object(d, "build_synthesis_message", return_value=[]), \
         mock.patch.object(d, "parse_implementer_response", return_value=synth), \
         mock.patch.object(d, "run_tests", side_effect=outputs), \
         mock.patch.object(d.executor, "build_synthesis_repair_message",
                           return_value=[]):
        d._run_synthesis(db, spec, impl, spec_dir, framework, {})
    # one call synthesises; a second means the repair round ran
    return calls["agent"] >= 2


def test_build_failure_now_gets_a_repair_round(db, synth_spec):
    spec, impl, spec_dir = synth_spec
    assert _run(db, spec, impl, spec_dir, BUILD_FAILURE) is True


def test_near_miss_test_failure_still_repairs(db, synth_spec):
    """The original DEV-406 behaviour must be untouched."""
    spec, impl, spec_dir = synth_spec
    assert _run(db, spec, impl, spec_dir, TEST_FAILURE_NEAR_MISS) is True


def test_far_from_passing_still_does_not_repair(db, synth_spec):
    spec, impl, spec_dir = synth_spec
    assert _run(db, spec, impl, spec_dir, TEST_FAILURE_FAR) is False


def test_no_summary_and_no_diagnostic_does_not_repair(db, synth_spec):
    """A broken runner is the hallucinated-PASS case; repairing code can't fix
    it, so the call is not spent."""
    spec, impl, spec_dir = synth_spec
    assert _run(db, spec, impl, spec_dir, NO_SIGNAL) is False


def test_repair_converting_to_a_pass_is_reported(db, synth_spec):
    spec, impl, spec_dir = synth_spec
    synth = SimpleNamespace(files=[("impl.swift", "struct A {}")])
    with mock.patch.object(d, "call_agent", return_value="raw"), \
         mock.patch.object(d, "build_synthesis_message", return_value=[]), \
         mock.patch.object(d, "parse_implementer_response", return_value=synth), \
         mock.patch.object(d, "run_tests",
                           side_effect=[(False, BUILD_FAILURE),
                                        (True, "Executed 20 tests\n20 passed in 1s\n")]), \
         mock.patch.object(d.executor, "build_synthesis_repair_message",
                           return_value=[]):
        passed, _ = d._run_synthesis(db, spec, impl, spec_dir, "swift_test", {})
    assert passed is True


def test_unknown_framework_build_failure_is_not_assumed(db, synth_spec):
    """_detect_build_failure has no signature for it, so the rate stays the
    only signal — do not repair on a guess."""
    spec, impl, spec_dir = synth_spec
    assert _run(db, spec, impl, spec_dir, BUILD_FAILURE,
                framework="cargo_test") is False


def test_pytest_collection_error_repairs(db, synth_spec):
    spec, impl, spec_dir = synth_spec
    out = "ERROR collecting tests/test_x.py\nE   ModuleNotFoundError: no module\n"
    assert _run(db, spec, impl, spec_dir, out, framework="pytest") is True
