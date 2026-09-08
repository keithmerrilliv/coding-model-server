"""DEV-629: one classifier, one disposition — the budget is charged only for
a verdict on real output, and only the terminal branch ends a spec."""
from __future__ import annotations

import json

import pytest
import requests

from coding_model_autonomous import GateStatus, GateType, SpecStatus, TaskStatus
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import EventKind
from coding_model_autonomous.outcome import (
    Failure, FailureClass, Hooks, Outcome, classify_exception,
    classify_model_output, classify_test_run, consecutive_no_verdicts,
    dispose, repo_packages, rotation_offset,
)


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def spec_tasks(db):
    spec = db.create_spec(title="t", source_md_path="spec.md", status=SpecStatus.EXECUTING)
    arch = db.create_task(spec_id=spec.id, agent="dense_architect", role="architect", title="design")
    impl = db.create_task(spec_id=spec.id, agent="implementer", role="implementer", title="implement")
    rev = db.create_task(spec_id=spec.id, agent="reviewer", role="reviewer", title="test")
    db.update_task_status(arch.id, TaskStatus.DONE)
    return spec, db.get_task(impl.id), db.get_task(rev.id)


def hooks(max_retries=5, synthesize=None, supervisor=None):
    return Hooks(max_retries=lambda: max_retries, synthesize=synthesize,
                 supervisor=supervisor, reviewer_parse_retries=lambda: 1)


def classified(db, spec_id):
    return [json.loads(e.payload_json or "{}") for e in
            db.list_events_by_kind(spec_id=spec_id, kind=EventKind.FAILURE_CLASSIFIED, limit=50)]


class TestClassifyException:
    def test_transport(self):
        f = classify_exception(requests.ConnectionError("refused"), role="implementer")
        assert f.cls is FailureClass.TRANSPORT and f.outcome is Outcome.NO_VERDICT
        assert not f.rotate

    def test_http_4xx_rotates_5xx_does_not(self):
        r413 = requests.Response(); r413.status_code = 413
        f = classify_exception(requests.HTTPError("413", response=r413), role="implementer")
        assert f.cls is FailureClass.HTTP_REFUSAL and f.rotate and f.extra["status"] == 413
        r502 = requests.Response(); r502.status_code = 502
        g = classify_exception(requests.HTTPError("502", response=r502), role="implementer")
        assert g.cls is FailureClass.HTTP_REFUSAL and not g.rotate

    def test_missing_choices_is_server_malformed(self):
        f = classify_exception(RuntimeError("Server response missing choices/content: x"),
                               role="architect")
        assert f.cls is FailureClass.SERVER_MALFORMED and f.outcome is Outcome.NO_VERDICT

    def test_unknown_is_no_verdict_from_the_daemon(self):
        f = classify_exception(KeyError("oops"), role="reviewer")
        assert f.cls is FailureClass.UNKNOWN_EXCEPTION and f.source == "daemon"
        assert f.outcome is Outcome.NO_VERDICT

    def test_shutdown_and_runner_outage_by_name(self):
        class ShutdownRequested(RuntimeError): ...
        class RunnerOutageAtImplement(RuntimeError): ...
        assert classify_exception(ShutdownRequested("x"), role="implementer").cls is FailureClass.SHUTDOWN
        f = classify_exception(RunnerOutageAtImplement("down"), role="implementer")
        assert f.cls is FailureClass.RUNNER_OUTAGE and f.phase == "existing_fetch" and f.cap is None


class TestClassifyOutput:
    def test_truncated_is_no_verdict_and_rotates(self):
        f = classify_model_output("<<<FILE: a>>>partial", {"truncated": True, "agent": "implementer",
                                                           "max_tokens": 16000},
                                  role="implementer", parse_reason="no END_FILE")
        assert f.cls is FailureClass.TRUNCATED and f.rotate and "16000" in f.detail

    def test_empty_after_think_stripping(self):
        f = classify_model_output("<think>\nstuck", {"agent": "a"}, role="architect",
                                  parse_reason="No DESIGN", strip_thinking=lambda s: "")
        assert f.cls is FailureClass.EMPTY_COMPLETION and f.rotate

    def test_real_output_that_does_not_parse_is_a_verdict(self):
        f = classify_model_output("here is prose", {}, role="implementer", parse_reason="No FILE blocks")
        assert f.cls is FailureClass.PARSE_FAILURE and f.outcome is Outcome.VERDICT

    def test_fine_output_is_none(self):
        assert classify_model_output("ok", {}, role="implementer") is None


class TestClassifyTests:
    def test_packages(self):
        assert repo_packages(["src/coding_model_server/x.py", "tests/test_a.py", "app/main.py"]) == {
            "coding_model_server", "tests", "app"}

    def test_unreachable(self):
        f = classify_test_run("mac-runner unreachable at http://x", role="implementer", passed=False,
                              build_reason=None, unreachable=True)
        assert f.cls is FailureClass.RUNNER_OUTAGE and f.cap == 3

    def test_missing_repo_package_is_provisioning(self):
        f = classify_test_run("E   ModuleNotFoundError: No module named 'coding_model_server'",
                              role="implementer", passed=False,
                              build_reason="E   ModuleNotFoundError: No module named 'coding_model_server'",
                              unreachable=False, packages={"coding_model_server"})
        assert f.cls is FailureClass.SANDBOX_PROVISIONING and f.outcome is Outcome.NO_VERDICT

    def test_missing_src_prefixed_module_is_a_build_failure(self):
        reason = "E   ModuleNotFoundError: No module named 'src.coding_model_autonomous.executor'"
        f = classify_test_run(reason, role="implementer", passed=False, build_reason=reason,
                              unreachable=False, packages={"coding_model_autonomous"})
        assert f.cls is FailureClass.BUILD_FAILURE and f.outcome is Outcome.VERDICT

    def test_red_tests_and_green(self):
        f = classify_test_run("1 failed", role="reviewer", passed=False, build_reason=None, unreachable=False)
        assert f.cls is FailureClass.TESTS_FAILED
        assert classify_test_run("4 passed", role="reviewer", passed=True, build_reason=None,
                                 unreachable=False) is None


class TestDisposeNoVerdict:
    def test_requeue_keeps_budget_and_spec(self, db, spec_tasks):
        spec, impl, _ = spec_tasks
        db.update_task_status(impl.id, TaskStatus.RUNNING)
        d = dispose(db, spec, db.get_task(impl.id),
                    classify_exception(requests.Timeout("t"), role="implementer"), hooks())
        assert d.action == "requeue" and d.consecutive == 1
        fresh = db.get_task(impl.id)
        assert fresh.status == TaskStatus.PENDING and fresh.retry_count == 0
        assert db.get_spec(spec.id).status == SpecStatus.EXECUTING
        ev = classified(db, spec.id)[0]
        assert ev["outcome"] == "no_verdict" and ev["cls"] == "transport" and ev["disposition"] == "requeue"
        assert db.list_gates_for_spec(spec.id) == []

    def test_rotate_advances_offset_without_charging(self, db, spec_tasks):
        spec, impl, _ = spec_tasks
        r = requests.Response(); r.status_code = 413
        f = classify_exception(requests.HTTPError("413", response=r), role="implementer")
        dispose(db, spec, db.get_task(impl.id), f, hooks())
        assert rotation_offset(db, spec.id, db.get_task(impl.id)) == 1
        assert db.get_task(impl.id).retry_count == 0

    def test_cap_parks_behind_an_infrastructure_gate(self, db, spec_tasks):
        spec, impl, _ = spec_tasks
        f = classify_exception(requests.ConnectionError("down"), role="implementer")
        for _ in range(5):
            assert dispose(db, spec, db.get_task(impl.id), f, hooks()).action == "requeue"
        d = dispose(db, spec, db.get_task(impl.id), f, hooks())
        assert d.action == "park" and d.consecutive == 6
        task = db.get_task(impl.id)
        assert task.status == TaskStatus.BLOCKED_ON_REVIEW and task.retry_count == 0
        gates = db.list_gates_for_spec(spec.id, GateType.CLARIFICATION)
        assert len(gates) == 1 and gates[0].task_id == impl.id
        assert "transport ×6" in gates[0].prompt_md and "no retry was charged" in gates[0].prompt_md
        assert db.get_spec(spec.id).status == SpecStatus.EXECUTING

    def test_count_resets_when_retry_count_advances(self, db, spec_tasks):
        spec, impl, _ = spec_tasks
        f = classify_exception(requests.ConnectionError("down"), role="implementer")
        for _ in range(4):
            dispose(db, spec, db.get_task(impl.id), f, hooks())
        db.increment_task_retry(impl.id)
        assert consecutive_no_verdicts(db, spec.id, db.get_task(impl.id)) == 0

    def test_uncapped_fetch_outage_never_parks(self, db, spec_tasks):
        spec, impl, _ = spec_tasks
        class RunnerOutageAtImplement(RuntimeError): ...
        f = classify_exception(RunnerOutageAtImplement("down"), role="implementer")
        for _ in range(12):
            assert dispose(db, spec, db.get_task(impl.id), f, hooks()).action == "requeue"


class TestDisposeVerdict:
    def test_implementer_charged_with_synthetic_gate(self, db, spec_tasks):
        spec, impl, _ = spec_tasks
        f = Failure(FailureClass.PARSE_FAILURE, "implementer", "parse", "No FILE blocks",
                    feedback="Re-emit ALL files.")
        d = dispose(db, spec, db.get_task(impl.id), f, hooks())
        assert d.action == "charge"
        task = db.get_task(impl.id)
        assert task.retry_count == 1 and task.status == TaskStatus.PENDING
        g = db.list_gates_for_spec(spec.id, GateType.CODE_REVIEW)
        assert len(g) == 1 and g[0].status == GateStatus.REJECTED
        assert g[0].prompt_md == "## Automated parse-failure retry" and g[0].reviewer_notes == "Re-emit ALL files."

    def test_reviewer_stage_failure_charges_implementer_and_resets_reviewer(self, db, spec_tasks):
        spec, impl, rev = spec_tasks
        db.update_task_status(impl.id, TaskStatus.DONE)
        db.update_task_status(rev.id, TaskStatus.RUNNING)
        f = Failure(FailureClass.TESTS_FAILED, "reviewer", "tests", "1 failed",
                    feedback="Reviewer verdict: FAIL", charge_role="implementer")
        d = dispose(db, spec, db.get_task(rev.id), f, hooks())
        assert d.action == "charge"
        assert db.get_task(impl.id).retry_count == 1
        assert db.get_task(impl.id).status == TaskStatus.PENDING
        assert db.get_task(rev.id).status == TaskStatus.PENDING

    def test_exhaustion_goes_to_synthesis_for_every_class(self, db, spec_tasks):
        spec, impl, rev = spec_tasks
        for _ in range(5):
            db.increment_task_retry(impl.id)
        calls = []
        def synth(db_, spec_, impl_task, reviewer_task, feedback):
            calls.append((impl_task.id, reviewer_task.id, feedback)); return None
        f = Failure(FailureClass.PARSE_FAILURE, "implementer", "parse", "unparseable")
        d = dispose(db, spec, db.get_task(impl.id), f, hooks(synthesize=synth))
        assert d.action == "synthesize" and calls == [(impl.id, rev.id, "unparseable")]
        assert db.get_spec(spec.id).status == SpecStatus.EXECUTING

    def test_synthesis_failure_is_terminal_and_closes_every_task(self, db, spec_tasks):
        spec, impl, rev = spec_tasks
        for _ in range(5):
            db.increment_task_retry(impl.id)
        db.update_task_status(impl.id, TaskStatus.RUNNING)
        def synth(*a):
            return Failure(FailureClass.SYNTHESIS_FAILED, "implementer", "tests", "still red")
        f = Failure(FailureClass.TESTS_FAILED, "reviewer", "tests", "red", charge_role="implementer")
        d = dispose(db, spec, db.get_task(rev.id), f, hooks(synthesize=synth))
        assert d.action == "terminal"
        assert db.get_spec(spec.id).status == SpecStatus.FAILED
        statuses = {t.role: t.status for t in db.list_tasks_for_spec(spec.id)}
        assert statuses["implementer"] == TaskStatus.FAILED  # DEV-532: not left RUNNING
        assert statuses["reviewer"] in (TaskStatus.FAILED, TaskStatus.SKIPPED)
        assert statuses["architect"] == TaskStatus.DONE
        ev = classified(db, spec.id)[0]
        assert ev["outcome"] == "terminal" and ev["cls"] == "synthesis_failed"

    def test_architect_charged_then_design_exhausted(self, db, spec_tasks):
        spec, _, _ = spec_tasks
        arch = db.list_tasks_for_spec_by_role(spec.id, "architect")[0]
        f = Failure(FailureClass.REVIEW_REJECTED, "architect", "gate", "no", feedback="fix the API")
        dispose(db, spec, db.get_task(arch.id), f, hooks(max_retries=1))
        assert db.get_task(arch.id).retry_count == 1
        assert (db.spec_dir(spec.id) / "design_review_feedback.md").read_text() == "fix the API"
        d = dispose(db, spec, db.get_task(arch.id), f, hooks(max_retries=1))
        assert d.action == "terminal" and d.failure.cls is FailureClass.DESIGN_EXHAUSTED
        assert db.get_spec(spec.id).status == SpecStatus.FAILED

    def test_reviewer_parse_verdict_rerun_then_implementer_pays(self, db, spec_tasks):
        spec, impl, rev = spec_tasks
        f = Failure(FailureClass.PARSE_FAILURE, "reviewer", "parse", "no REVIEW block")
        d1 = dispose(db, spec, db.get_task(rev.id), f, hooks())
        assert d1.action == "charge" and db.get_task(rev.id).retry_count == 1
        assert db.get_task(impl.id).retry_count == 0
        d2 = dispose(db, spec, db.get_task(rev.id), f, hooks())
        assert d2.action == "charge" and db.get_task(impl.id).retry_count == 1

    def test_supervisor_strategy_is_consulted_for_gate_and_tests_only(self, db, spec_tasks):
        spec, impl, rev = spec_tasks
        seen = []
        def strategy(db_, spec_, task, failure):
            seen.append(failure.source); return True
        h = hooks(supervisor=strategy)
        dispose(db, spec, db.get_task(rev.id),
                Failure(FailureClass.REVIEW_REJECTED, "reviewer", "gate", "no", charge_role="implementer"), h)
        dispose(db, spec, db.get_task(impl.id),
                Failure(FailureClass.PARSE_FAILURE, "implementer", "parse", "no"), h)
        assert seen == ["gate"]
        assert db.get_task(impl.id).retry_count == 1  # the parse verdict took the default path
