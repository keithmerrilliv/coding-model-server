"""Incomplete build diagnostics are flagged as incomplete — DEV-435.

`swift build` reports a failed emit-module job as a bare
"error: emit-module command failed with exit code 1 (use -v to see invocation)"
and swallows the real diagnostic. Everything downstream then reasons from the
cascade errors that DID carry a location — on spec_ead8f7fc those were the test
files reporting "cannot find 'World' in scope" because the module was never
produced. Seven attempts rewrote test files while the actual defect sat
untouched in the sources.
"""
import coding_model_server.orchestrator_daemon as d

# Trimmed from retry_history/retry_6/build_failure.txt of spec_ead8f7fc.
REAL_OUTPUT = """\
Building for debugging...
[6/12] Compiling CentipedeCore World.swift
[9/12] Compiling CentipedeCore GameState.swift
[10/12] Emitting module CentipedeCore
error: emit-module command failed with exit code 1 (use -v to see invocation)
/Users/youruser/.../Tests/CentipedeCoreTests/FallOffTests.swift:7:17: error: cannot find 'World' in scope
 7 |     let world = World(seed: 1)
   |                 `- error: cannot find 'World' in scope
"""


def test_bare_error_is_detected():
    assert d._unattributed_errors(REAL_OUTPUT) == [
        "emit-module command failed with exit code 1 (use -v to see invocation)"
    ]


def test_located_errors_are_not_counted_as_unattributed():
    located = "/x/y/Foo.swift:3:5: error: cannot find 'Bar' in scope\n"
    assert d._unattributed_errors(located) == []


def test_duplicate_bare_errors_are_deduped():
    out = "error: boom\nerror: boom\nerror: other\n"
    assert d._unattributed_errors(out) == ["boom", "other"]


def test_empty_output_has_none():
    assert d._unattributed_errors("") == []
    assert d._unattributed_errors(None) == []


def test_note_warns_that_located_errors_may_be_consequences():
    note = d._diagnostic_completeness_note(REAL_OUTPUT)
    assert "INCOMPLETE DIAGNOSTICS" in note
    assert "emit-module command failed" in note
    # The actionable steer: do not chase the test files.
    assert "consequences" in note
    assert "fixing the tests will not help" in note
    assert "module's own sources" in note


def test_note_when_nothing_is_located_at_all():
    note = d._diagnostic_completeness_note("error: something broke\n")
    assert "INCOMPLETE DIAGNOSTICS" in note
    assert "failing file is unknown" in note
    assert "consequences" not in note


def test_clean_output_produces_no_note():
    located = "/x/Foo.swift:3:5: error: cannot find 'Bar' in scope\n"
    assert d._diagnostic_completeness_note(located) == ""
    assert d._diagnostic_completeness_note("") == ""


def test_note_is_prepended_to_the_retry_feedback(tmp_path, monkeypatch):
    """The implementer's retry prompt must carry the warning, not just the log."""
    from unittest import mock
    from coding_model_autonomous.db import Database
    from coding_model_autonomous.executor import ImplementerResult
    from coding_model_autonomous.models import GateStatus, SpecStatus

    db = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    try:
        spec = db.create_spec(title="demo", source_md_path="spec.md")
        db.update_spec_status(spec.id, SpecStatus.EXECUTING)
        task = db.create_task(spec_id=spec.id, agent="implementer",
                              role="implementer", title="build")
        spec_dir = db.spec_dir(spec.id)
        spec_dir.mkdir(parents=True, exist_ok=True)
        (spec_dir / "spec.md").write_text("# demo\n")
        (spec_dir / "design.md").write_text("# design\n")

        result = ImplementerResult(files=[("Sources/A.swift", "struct A {}")], raw="")
        with mock.patch.object(d, "_generate_implementation", return_value=result), \
             mock.patch.object(d, "_load_plan",
                               return_value={"test_strategy": {"framework": "swift_test",
                                                               "repo": "centipede"}}), \
             mock.patch.object(d, "_run_tests_with_guard",
                               return_value=(False, REAL_OUTPUT)):
            d._run_implementer(db, db.get_spec(spec.id), db.get_task(task.id), spec_dir)

        rejected = [g for g in db.list_gates_for_spec(spec.id)
                    if g.status is GateStatus.REJECTED]
        assert len(rejected) == 1
        assert "INCOMPLETE DIAGNOSTICS" in rejected[0].reviewer_notes
        assert "fixing the tests will not help" in rejected[0].reviewer_notes
    finally:
        db.close_all()
