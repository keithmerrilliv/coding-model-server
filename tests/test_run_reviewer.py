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

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.executor import ParseError, ReviewerResult
from coding_model_autonomous.models import ArtifactKind, GateType, SpecStatus, TaskStatus


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


class TestVerifyReviewCitations:
    """The cite-check regex must treat URLs / IP:port as non-citations.

    Regression: `http://192.168.50.101:3001/` matched `50.101:3001` as a
    `file.ext:line` cite, annotating mid-URL and logging a spurious 100%
    unverified rate on clean reviews.
    """

    def test_url_with_ip_port_is_not_a_citation(self, tmp_path):
        md = "Evidence: server runs at http://192.168.50.101:3001/ and works."
        annotated, checked, unverified = d._verify_review_citations(md, tmp_path)
        assert checked == 0          # nothing looked like a cite
        assert unverified == 0
        assert annotated == md       # URL left untouched, no mid-string annotation

    def test_hostname_port_is_not_a_citation(self, tmp_path):
        md = "See http://example.com:8080/path for the dashboard."
        annotated, checked, _ = d._verify_review_citations(md, tmp_path)
        assert checked == 0
        assert annotated == md

    def test_real_missing_cite_is_annotated(self, tmp_path):
        md = "Bug at src/converter.py:42 — off-by-one."
        annotated, checked, unverified = d._verify_review_citations(md, tmp_path)
        assert checked == 1          # real path:line cite detected
        assert unverified == 1       # file doesn't exist in the empty spec dir
        assert "[unverified — file not in spec dir]" in annotated

    def test_real_existing_cite_not_annotated(self, tmp_path):
        (tmp_path / "converter.py").write_text("x = 1\n")
        md = "Looks fine at converter.py:1."
        annotated, checked, unverified = d._verify_review_citations(md, tmp_path)
        assert checked == 1
        assert unverified == 0
        assert "unverified" not in annotated


def test_parse_error_persists_raw_and_retries(db, spec_and_task):
    # Guards a latent bug: the parse-error branch referenced result.text, but
    # ParseError only has .reason/.raw — it would AttributeError exactly when
    # an operator needs the failed-response dump.
    # Also pins the robustness fix: a single unparseable/truncated review must
    # NOT kill the spec — it re-runs the reviewer (the 122B reviewer's
    # degenerate truncation is intermittent).
    spec, task, spec_dir = spec_and_task
    perr = ParseError(reason="no <<<REVIEW>>> block", raw="garbage model output")
    with mock.patch.object(d, "call_agent", return_value="raw"), \
            mock.patch.object(d, "build_reviewer_message", return_value=[]), \
            mock.patch.object(d, "parse_reviewer_response", return_value=perr):
        d._run_reviewer(db, spec, task, spec_dir)

    dump = spec_dir / "reviewer_failed_response.txt"
    assert dump.exists()
    assert "garbage model output" in dump.read_text()
    # First failure re-runs the reviewer rather than failing the spec.
    assert db.get_task(task.id).status is TaskStatus.PENDING
    assert db.get_spec(spec.id).status is SpecStatus.EXECUTING
    assert db.list_open_gates(spec.id) == []


def test_parse_error_soft_fails_to_retry_when_exhausted(db, spec_and_task):
    # When reviewer re-runs are exhausted, an unparseable review becomes a soft
    # FAIL routed through _attempt_retry (supervisor → implementer/design) — NOT
    # a direct spec failure (the old behavior that killed specs on one truncation).
    spec, task, spec_dir = spec_and_task
    perr = ParseError(reason="truncated", raw="...")
    with mock.patch.object(d.executor, "REVIEWER_PARSE_RETRIES", 0), \
            mock.patch.object(d, "call_agent", return_value="raw"), \
            mock.patch.object(d, "build_reviewer_message", return_value=[]), \
            mock.patch.object(d, "parse_reviewer_response", return_value=perr), \
            mock.patch.object(d, "_attempt_retry") as attempt:
        d._run_reviewer(db, spec, task, spec_dir)

    attempt.assert_called_once()
    # _run_reviewer itself must not have hard-failed the spec.
    assert db.get_spec(spec.id).status is SpecStatus.EXECUTING
