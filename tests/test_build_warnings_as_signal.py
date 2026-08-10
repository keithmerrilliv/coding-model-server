"""Compiler warnings as signal — DEV-547.

Run 9 of DEV-102 compiled, launched all 19 tests, and died on a runtime trap
with zero tests completed. One inverted conditional emptied `chains` on every
`step()`, so every `w.chains[0]` after a step trapped. The compiler had named
that line as a warning, in output the pipeline already captured and parsed, and
nothing read it.

These pin the parse, the deliberately-narrow blocking set (a false positive
costs a full generation), and the rotation itself — including that the
implementer is never told the code failed to compile when it did not.
"""
import json
from types import SimpleNamespace
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import executor
from coding_model_autonomous.db import Database
from coding_model_autonomous.executor import ImplementerResult
from coding_model_autonomous.models import (
    EventKind, GateStatus, GateType, SpecStatus, TaskStatus,
)

# Verbatim from run 9's final build (spec_9ff962b9), including the caret echo
# line, which repeats the message and must not be counted twice.
RUN9_WARNING_BUILD = """\
Building for debugging...
[6/10] Compiling CentipedeCore GameState.swift
[7/10] Compiling CentipedeCore World.swift
/Users/km4/Library/Caches/coding-model-runner/worktrees/spec_9ff962b9-09f0ad65/\
Sources/CentipedeCore/World.swift:238:20: warning: value 'updateIdx' was \
defined but never used; consider replacing with boolean test [#no-usage]
236 |         var newChains: [CentipedeChain] = []
237 |         for idx in 0..<chains.count {
238 |             if let updateIdx = removalIndices.firstIndex(of: idx) == nil ? idx : nil {
    |                    `- warning: value 'updateIdx' was defined but never \
used; consider replacing with boolean test [#no-usage]
239 |                 continue
[14/15] Linking CentipedePackageTests
Build complete! (3.00s)
Executed 19 tests, with 0 failures
"""

SWIFT_BUILD_FAILURE = """\
Building for debugging...
/Users/km4/.../worktrees/spec_x-1/Sources/CentipedeCore/World.swift:17:19: \
error: 'DeterministicRNG' is inaccessible due to 'private' protection level
error: fatalError
"""

# Run 9's actual terminal shape: it compiled, all 19 tests were launched, and
# the process trapped before any could report. Note the trailing driver line —
# _detect_build_failure matches it, so this output is classified a build
# failure even though it compiled. See the test that pins that below.
RUN9_TRAP_BUILD = """\
◇ Test run started.
◇ Test "C4: Chain advances with segments following" started.
Building for debugging...
[7/10] Compiling CentipedeCore World.swift
/Users/km4/Library/Caches/coding-model-runner/worktrees/spec_9ff962b9-09f0ad65/\
Sources/CentipedeCore/World.swift:238:20: warning: value 'updateIdx' was \
defined but never used; consider replacing with boolean test [#no-usage]
Build complete! (3.00s)
error: Process 'swiftpm-testing-helper --parallel' exited with unexpected signal code 5
"""

# The shape this ticket's synthesis wiring targets: compiled, the runner said
# nothing parseable, and no `error:` line anywhere — so today there is no
# diagnostic, no pass rate, and the repair round is skipped as unexplained.
COMPILED_NO_SUMMARY = """\
Building for debugging...
[7/10] Compiling CentipedeCore World.swift
/Users/km4/Library/Caches/coding-model-runner/worktrees/spec_x-1/\
Sources/CentipedeCore/World.swift:238:20: warning: value 'updateIdx' was \
defined but never used; consider replacing with boolean test [#no-usage]
Build complete! (3.00s)
◇ Test run started.
"""

NO_SIGNAL = "the runner produced nothing useful\n"


# ── the parse ────────────────────────────────────────────────────────────────

def test_run9_warning_is_parsed_with_location_and_id():
    """The exact diagnostic that killed run 9."""
    warnings = d._parse_build_warnings(RUN9_WARNING_BUILD)
    assert len(warnings) == 1
    w = warnings[0]
    assert w.path == "Sources/CentipedeCore/World.swift"
    assert w.line == 238
    assert w.column == 20
    assert w.diag_id == "no-usage"
    assert "was defined but never used" in w.message
    # The id is lifted out of the message, not left dangling on the end.
    assert "[#no-usage]" not in w.message
    assert w.blocking is True


def test_caret_echo_line_is_not_counted_twice():
    """The compiler repeats the message under a caret with no path:line:col."""
    assert len(d._parse_build_warnings(RUN9_WARNING_BUILD)) == 1


def test_worktree_prefix_is_stripped():
    """The dispatch dir changes every run, so it is noise in an artifact and
    breaks any comparison against protected_paths."""
    w = d._parse_build_warnings(RUN9_WARNING_BUILD)[0]
    assert "worktrees" not in w.path
    assert not w.path.startswith("/")


def test_empty_output_parses_to_nothing():
    assert d._parse_build_warnings("") == []
    assert d._parse_build_warnings("Build complete! (3.00s)\n") == []


def test_warning_without_a_diagnostic_id_still_parses():
    """Older toolchains and most non-Swift compilers emit no [#id]."""
    out = "/tmp/wt/worktrees/s-1/Sources/A.swift:9:5: warning: code after 'return' will never be executed\n"
    w = d._parse_build_warnings(out)[0]
    assert w.diag_id == ""
    assert w.blocking is True


# ── the blocking set is deliberately narrow ──────────────────────────────────

@pytest.mark.parametrize("message", [
    "value 'x' was defined but never used; consider replacing with boolean test",
    "code after 'return' will never be executed",
    "comparison of 'Int' with 'Int' is always true",
    "condition is always false",
])
def test_high_signal_classes_block(message):
    out = f"/wt/worktrees/s-1/Sources/A.swift:3:4: warning: {message}\n"
    assert d._parse_build_warnings(out)[0].blocking is True


@pytest.mark.parametrize("message", [
    # Style. A human would rightly ignore these, and a false positive costs a
    # full implementer generation plus a runner dispatch.
    "variable 'total' was never mutated; consider changing to 'let' constant",
    "immutable value 'i' was never used; consider replacing with '_' or removing it",
    "initialization of immutable value 'x' was never used",
    "no calls to throwing functions occur within 'try' expression",
])
def test_style_warnings_are_recorded_but_never_block(message):
    out = f"/wt/worktrees/s-1/Sources/A.swift:3:4: warning: {message}\n"
    parsed = d._parse_build_warnings(out)
    assert len(parsed) == 1, "still recorded, so DEV-529 can widen this later"
    assert parsed[0].blocking is False


def test_warning_on_a_protected_path_never_blocks():
    """The pipeline cannot edit protected files — DEV-427 drops them before
    dispatch — so rejecting an attempt over one would loop until exhaustion."""
    out = ("/wt/worktrees/s-1/Sources/CentipedeCore/GameState.swift:12:9: "
           "warning: value 'q' was defined but never used\n")
    parsed = d._parse_build_warnings(
        out, ["Sources/CentipedeCore/GameState.swift"])
    assert parsed[0].blocking is False
    assert d._blocking_build_warnings(
        out, ["Sources/CentipedeCore/GameState.swift"]) == []
    # …and the identical warning on a generated file does block.
    assert len(d._blocking_build_warnings(out, ["Package.swift"])) == 1


# ── the rotation ─────────────────────────────────────────────────────────────

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


def _run(db, spec, task, spec_dir, *, build_passed, build_output,
         protected=None):
    result = ImplementerResult(files=[("Sources/A.swift", "struct A {}")], raw="")
    strategy = {"framework": "swift_test", "repo": "centipede"}
    if protected is not None:
        strategy["protected_paths"] = protected
    with mock.patch.object(d, "_generate_implementation", return_value=result), \
         mock.patch.object(d, "_load_plan", return_value={"test_strategy": strategy}), \
         mock.patch.object(d, "_run_tests_with_guard",
                           return_value=(build_passed, build_output)):
        d._run_implementer(db, spec, task, spec_dir)


def test_run9_build_is_rejected_before_a_human_sees_it(db, impl_spec):
    """The acceptance criterion: replaying run 9's final build rejects it."""
    spec, task, spec_dir = impl_spec
    _run(db, spec, task, spec_dir, build_passed=True,
         build_output=RUN9_WARNING_BUILD)

    gates = db.list_gates_for_spec(spec.id)
    assert len(gates) == 1
    assert gates[0].status is GateStatus.REJECTED
    assert not [g for g in gates if g.status is GateStatus.PENDING]
    assert "World.swift:238:20" in gates[0].reviewer_notes
    assert "was defined but never used" in gates[0].reviewer_notes

    assert db.get_task(task.id).status is TaskStatus.PENDING
    assert db.get_task(task.id).retry_count == task.retry_count + 1
    assert db.get_spec(spec.id).status is SpecStatus.EXECUTING
    assert (spec_dir / "build_warnings.txt").exists()


def test_the_implementer_is_not_told_the_build_failed(db, impl_spec):
    """DEV-477 in the other direction: this build DID compile, and saying
    otherwise sends the implementer hunting a syntax error that is not there."""
    spec, task, spec_dir = impl_spec
    _run(db, spec, task, spec_dir, build_passed=True,
         build_output=RUN9_WARNING_BUILD)

    notes = db.list_gates_for_spec(spec.id)[0].reviewer_notes
    assert "does not compile" not in notes
    assert "The build succeeded" in notes
    # The build-failure artifact is for build failures only.
    assert not (spec_dir / "build_failure.txt").exists()


def test_a_real_compiler_error_still_wins(db, impl_spec):
    """A diagnostic is strictly better feedback than a warning; stacking the
    two would bury it."""
    spec, task, spec_dir = impl_spec
    _run(db, spec, task, spec_dir, build_passed=False,
         build_output=SWIFT_BUILD_FAILURE + RUN9_WARNING_BUILD)

    notes = db.list_gates_for_spec(spec.id)[0].reviewer_notes
    assert "does not compile" in notes
    assert "DeterministicRNG" in notes
    assert (spec_dir / "build_failure.txt").exists()
    assert not (spec_dir / "build_warnings.txt").exists()


def test_clean_compiling_build_still_reaches_the_human_gate(db, impl_spec):
    """No blocking warning must mean no behaviour change at all."""
    spec, task, spec_dir = impl_spec
    _run(db, spec, task, spec_dir, build_passed=True,
         build_output="Build complete! (3.00s)\nExecuted 17 tests\n")

    pending = [g for g in db.list_gates_for_spec(spec.id)
               if g.status is GateStatus.PENDING]
    assert len(pending) == 1
    assert pending[0].gate_type is GateType.CODE_REVIEW
    assert db.get_task(task.id).status is TaskStatus.BLOCKED_ON_REVIEW
    assert db.get_task(task.id).retry_count == task.retry_count


def test_style_only_warnings_do_not_rotate(db, impl_spec):
    spec, task, spec_dir = impl_spec
    out = ("Build complete! (3.00s)\n/wt/worktrees/s-1/Sources/A.swift:4:9: "
           "warning: variable 'n' was never mutated; consider changing to 'let'\n")
    _run(db, spec, task, spec_dir, build_passed=True, build_output=out)

    assert db.get_task(task.id).status is TaskStatus.BLOCKED_ON_REVIEW
    assert db.get_task(task.id).retry_count == task.retry_count


def test_protected_path_warning_does_not_rotate(db, impl_spec):
    spec, task, spec_dir = impl_spec
    out = ("Build complete!\n/wt/worktrees/s-1/Sources/CentipedeCore/"
           "GameState.swift:12:9: warning: value 'q' was defined but never used\n")
    _run(db, spec, task, spec_dir, build_passed=True, build_output=out,
         protected=["Sources/CentipedeCore/GameState.swift"])

    assert db.get_task(task.id).status is TaskStatus.BLOCKED_ON_REVIEW
    assert db.get_task(task.id).retry_count == task.retry_count


def test_kill_switch_restores_the_old_behaviour(db, impl_spec, monkeypatch):
    """First check that can reject an attempt whose build succeeded, so it
    needs a way off without a deploy."""
    monkeypatch.setattr(d, "BLOCK_ON_BUILD_WARNINGS", False)
    spec, task, spec_dir = impl_spec
    _run(db, spec, task, spec_dir, build_passed=True,
         build_output=RUN9_WARNING_BUILD)

    assert db.get_task(task.id).status is TaskStatus.BLOCKED_ON_REVIEW
    assert db.get_task(task.id).retry_count == task.retry_count


def test_warnings_are_recorded_on_the_event_even_when_not_blocking(db, impl_spec):
    """DEV-529 wants warnings queryable, not living only in the raw log."""
    spec, task, spec_dir = impl_spec
    out = ("Build complete!\n/wt/worktrees/s-1/Sources/A.swift:4:9: warning: "
           "variable 'n' was never mutated; consider changing to 'let'\n")
    _run(db, spec, task, spec_dir, build_passed=True, build_output=out)

    payloads = [json.loads(e.payload_json or "{}")
                for e in db.list_events_by_kind(spec_id=spec.id,
                                                kind=EventKind.TEST_RAN)]
    checks = [p for p in payloads if p.get("phase") == "pre_gate_build_check"]
    assert checks, "the pre-gate check must record an event"
    assert checks[-1]["warnings"] == 1
    assert checks[-1]["blocking_warnings"] == []


# ── the synthesis repair trigger ─────────────────────────────────────────────

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


def _synth(db, spec, impl, spec_dir, first_output, *, opts=None):
    """Drive _run_synthesis to its repair decision.

    Returns (repaired, repair_kwargs) — repair_kwargs is None when no repair
    round ran.
    """
    synth = SimpleNamespace(files=[("impl.swift", "struct A {}")])
    calls = {"agent": 0}

    def _agent(*a, **k):
        calls["agent"] += 1
        return "raw"

    repair_builder = mock.MagicMock(return_value=[])
    with mock.patch.object(d, "call_agent", side_effect=_agent), \
         mock.patch.object(d, "build_synthesis_message", return_value=[]), \
         mock.patch.object(d, "parse_implementer_response", return_value=synth), \
         mock.patch.object(d, "run_tests",
                           side_effect=[(False, first_output),
                                        (False, first_output)]), \
         mock.patch.object(d.executor, "build_synthesis_repair_message",
                           repair_builder):
        d._run_synthesis(db, spec, impl, spec_dir, "swift_test", opts or {})
    repaired = calls["agent"] >= 2
    kwargs = repair_builder.call_args.kwargs if repair_builder.call_args else None
    return repaired, kwargs


def test_compiled_with_no_summary_now_gets_a_repair_round(db, synth_spec):
    """Compiled, no summary, no diagnostic — previously abandoned as an
    unexplained failure. The warning is the explanation."""
    spec, impl, spec_dir = synth_spec
    repaired, kwargs = _synth(db, spec, impl, spec_dir, COMPILED_NO_SUMMARY)
    assert repaired is True
    assert kwargs["build_diagnostic"] is None, "it compiled — do not claim otherwise"
    assert "World.swift:238:20" in kwargs["warning_diagnostic"]


def test_run9_output_now_takes_the_warning_path(db, synth_spec):
    """Run 9's real terminal output. This test previously pinned a LIMITATION:
    the harness printed `error: Process … signal code 5` after a clean build,
    _detect_build_failure matched that bare driver line, and the repair was
    told the code did not compile — false, and it buried the warning.

    DEV-548 made the ordering the discriminator, so the same bytes now reach
    the warning path. Kept and inverted rather than deleted, because the
    interesting thing about this fixture is that it *used* to go the other way.
    """
    spec, impl, spec_dir = synth_spec
    assert d._attributed_diagnostics(RUN9_TRAP_BUILD) == []
    assert d._detect_test_process_crash(RUN9_TRAP_BUILD) is not None
    repaired, kwargs = _synth(db, spec, impl, spec_dir, RUN9_TRAP_BUILD)
    assert repaired is True
    assert kwargs["build_diagnostic"] is None, "it compiled — do not claim otherwise"
    assert "World.swift:238:20" in kwargs["warning_diagnostic"]


def test_unexplained_failure_still_does_not_repair(db, synth_spec):
    """No summary, no diagnostic and nothing the compiler objected to is still
    the broken-runner case — do not spend the call."""
    spec, impl, spec_dir = synth_spec
    repaired, _ = _synth(db, spec, impl, spec_dir, NO_SIGNAL)
    assert repaired is False


def test_build_failure_still_takes_precedence_at_synthesis(db, synth_spec):
    spec, impl, spec_dir = synth_spec
    repaired, kwargs = _synth(db, spec, impl, spec_dir,
                              SWIFT_BUILD_FAILURE + RUN9_TRAP_BUILD)
    assert repaired is True
    assert kwargs["build_diagnostic"] is not None
    assert kwargs["warning_diagnostic"] is None


def test_near_miss_is_untouched_by_a_warning(db, synth_spec):
    """A measurable pass rate is still the near-miss path, warning or not."""
    spec, impl, spec_dir = synth_spec
    near_miss = ("Executed 20 tests\n18 passed, 2 failed in 1.2s\n"
                 "/wt/worktrees/s-1/Sources/A.swift:3:4: warning: value 'x' "
                 "was defined but never used\n")
    repaired, kwargs = _synth(db, spec, impl, spec_dir, near_miss)
    assert repaired is True
    assert kwargs["build_diagnostic"] is None
    assert kwargs["warning_diagnostic"] is None


def test_protected_path_warning_does_not_trigger_a_repair(db, synth_spec):
    spec, impl, spec_dir = synth_spec
    out = ("Build complete!\n/wt/worktrees/s-1/Sources/CentipedeCore/"
           "GameState.swift:12:9: warning: value 'q' was defined but never used\n")
    repaired, _ = _synth(db, spec, impl, spec_dir, out,
                         opts={"protected_paths":
                               ["Sources/CentipedeCore/GameState.swift"]})
    assert repaired is False


def test_kill_switch_also_covers_synthesis(db, synth_spec, monkeypatch):
    monkeypatch.setattr(d, "BLOCK_ON_BUILD_WARNINGS", False)
    spec, impl, spec_dir = synth_spec
    repaired, _ = _synth(db, spec, impl, spec_dir, COMPILED_NO_SUMMARY)
    assert repaired is False


# ── the repair prompt ────────────────────────────────────────────────────────

def _repair_text(**kw):
    msgs = executor.build_synthesis_repair_message(
        "# spec", "# design", [("A.swift", "struct A {}")], "output", **kw)
    return msgs[-1]["content"]


def test_warning_prompt_does_not_claim_either_existing_state():
    """Saying the build failed sends it hunting a syntax error that is not
    there; the near-miss wording is worse, because nothing here passed."""
    text = _repair_text(warning_diagnostic="  - A.swift:1:1: value 'x' unused")
    assert "does NOT compile" not in text
    assert "passes most tests" not in text
    assert "already passes most of its tests" not in text
    assert "COMPILED" in text
    assert "no usable test result" in text
    assert "A.swift:1:1" in text


def test_existing_two_prompts_are_byte_identical():
    """DEV-522's build prompt and DEV-406's near-miss prompt are pinned."""
    build_before = _repair_text(build_diagnostic="boom")
    near_before = _repair_text()
    # Passing the new kwarg alongside the old one changes nothing.
    assert _repair_text(build_diagnostic="boom",
                        warning_diagnostic="  - A.swift:1:1: x") == build_before
    assert "does NOT compile" in build_before
    assert "already passes most of its tests" in near_before
