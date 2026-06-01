"""Behavioural tests for the reviewer stage of the orchestrator daemon.

_run_reviewer was refactored from one 306-line function into a thin driver plus
four named helpers. These exercise the driver end-to-end with the LLM call, test
runner, and adversarial phase mocked, pinning the two outcomes that must not
drift:

  * reviewer PASS + tests pass  -> task BLOCKED_ON_REVIEW + a release_approval gate
  * reviewer FAIL               -> a retry is attempted (no release gate)

plus the structural-guard path (a clean exit with no summary line must not be
trusted). Everything runs against a throwaday DB under tmp_path; no network, no
sandbox, no live task store.
"""
from unittest import mock

import pytest

import qwen_server.orchestrator_daemon as d
from qwen_autonomous.db import Database
from qwen_autonomous.executor import ParseError, ReviewerResult
from qwen_autonomous.models import ArtifactKind, GateType, SpecStatus, TaskStatus


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def spec_and_task(db):
    """A spec with one CODE artifact and a reviewer task, set up on disk."""
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    spec_dir = db.spec_dir(spec.id)
    (spec_dir / "spec.md").write_text("# spec\nbuild a thing")
    (spec_dir / "design.md").write_text("# design")
    (spec_dir / "impl.py").write_text("def f():\n    return 1\n")
    db.create_artifact(spec_id=spec.id, kind=ArtifactKind.CODE, path="impl.py")
    # Plan with a pytest strategy so tests are "required". normalized_yaml is
    # only settable via update_spec_status.
    db.update_spec_status(
        spec.id, SpecStatus.EXECUTING,
        normalized_yaml="test_strategy:\n  framework: pytest\n  required: true\n",
    )
    task = db.create_task(
        spec_id=spec.id, agent="reviewer", role="reviewer", title="review demo",
    )
    # refetch so normalized_yaml/status reflect the update
    return db.get_spec(spec.id), db.get_task(task.id), spec_dir


def _reviewer_result(verdict):
    return ReviewerResult(
        test_files=[("tests/test_demo.py", "def test_ok():\n    assert True\n")],
        review_md="No issues found.",
        verdict=verdict,
        raw="<<<REVIEW>>>...",
    )


def _patches(verdict, *, tests_pass, test_output):
    """Patch the daemon's reviewer collaborators for one run."""
    return {
        "call_agent": mock.patch.object(d, "call_agent", return_value="raw"),
        "parse": mock.patch.object(d, "parse_reviewer_response",
                                   return_value=_reviewer_result(verdict)),
        "build": mock.patch.object(d, "build_reviewer_message", return_value=[]),
        "run_tests": mock.patch.object(d, "run_tests",
                                       return_value=(tests_pass, test_output)),
        # keep phase-b out of the way unless a test opts in
        "adv": mock.patch.object(d.executor, "ADVERSARIAL_TESTS_ENABLED", False),
    }


def _run(db, spec, task, spec_dir, **kw):
    patches = _patches(**kw)
    with patches["call_agent"], patches["parse"], patches["build"], \
            patches["run_tests"], patches["adv"]:
        d._run_reviewer(db, spec, task, spec_dir)


def test_pass_with_passing_tests_creates_release_gate(db, spec_and_task):
    spec, task, spec_dir = spec_and_task
    _run(db, spec, task, spec_dir,
         verdict="PASS", tests_pass=True, test_output="1 passed in 0.01s")

    gates = db.list_open_gates(spec.id)
    assert len(gates) == 1
    assert gates[0].gate_type is GateType.RELEASE_APPROVAL
    assert db.get_task(task.id).status is TaskStatus.BLOCKED_ON_REVIEW


def test_fail_verdict_attempts_retry_and_no_gate(db, spec_and_task):
    spec, task, spec_dir = spec_and_task
    with mock.patch.object(d, "_attempt_retry") as retry:
        _run(db, spec, task, spec_dir,
             verdict="FAIL", tests_pass=True, test_output="1 passed in 0.01s")
    retry.assert_called_once()
    assert db.list_open_gates(spec.id) == []


def test_structural_guard_blocks_hallucinated_pass(db, spec_and_task):
    # Reviewer says PASS and the runner exits "clean" but emits no summary
    # line — the guard must force a retry, not a release gate.
    spec, task, spec_dir = spec_and_task
    with mock.patch.object(d, "_attempt_retry") as retry:
        _run(db, spec, task, spec_dir,
             verdict="PASS", tests_pass=True, test_output="(no tests ran)")
    retry.assert_called_once()
    assert db.list_open_gates(spec.id) == []


def test_review_report_artifact_written(db, spec_and_task):
    spec, task, spec_dir = spec_and_task
    _run(db, spec, task, spec_dir,
         verdict="PASS", tests_pass=True, test_output="1 passed in 0.01s")
    assert (spec_dir / "review_report.md").exists()
    kinds = {a.kind for a in db.list_artifacts(spec.id)}
    assert ArtifactKind.REVIEW_REPORT in kinds


def test_parse_error_persists_raw_and_fails(db, spec_and_task):
    # Guards a latent bug: the parse-error branch referenced result.text, but
    # ParseError only has .reason/.raw — it would AttributeError exactly when
    # an operator needs the failed-response dump.
    spec, task, spec_dir = spec_and_task
    perr = ParseError(reason="no <<<REVIEW>>> block", raw="garbage model output")
    with mock.patch.object(d, "call_agent", return_value="raw"), \
            mock.patch.object(d, "build_reviewer_message", return_value=[]), \
            mock.patch.object(d, "parse_reviewer_response", return_value=perr):
        d._run_reviewer(db, spec, task, spec_dir)

    dump = spec_dir / "reviewer_failed_response.txt"
    assert dump.exists()
    assert "garbage model output" in dump.read_text()
    assert db.get_task(task.id).status is TaskStatus.FAILED
    assert db.list_open_gates(spec.id) == []
