"""DEV-620: a dead runner at implement time parks the task, never blind-runs.

Run 19's retry: the Mac was off, fetch_repo_files returned no files and one
connection problem, nothing logged, edit mode disarmed itself, and
deep_implementer began regenerating an 8.5K-line surface from priors. The
fetch now raises RunnerOutageAtImplement transport-level failures BEFORE any
model call, and the implement runner parks the task with retry_count intact.
"""
from types import SimpleNamespace

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import test_runner
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import TaskStatus

TABLE = """
| Path | What changes here |
|---|---|
| `src/big.py` | guards |
"""

TRANSPORT_PROBLEM = "could not reach the runner's read path: connection refused"


def _ctx_files(spec, spec_md, extra_paths=(), *, role="implementer"):
    """DEV-632: the fetch is the context stage's; a role selects from it."""
    return d._spec_context(None, spec, spec_md, role=role,
                           extra_candidates=extra_paths).editable_files


class TestOutageClassifier:
    def test_transport_single_problem_is_outage(self):
        assert test_runner.problems_indicate_runner_outage(
            [TRANSPORT_PROBLEM], ["src/big.py"])

    def test_http_and_route_problems_are_outage(self):
        for p in ("read_files HTTP 500: boom",
                  "runner has no /v1/read_files route (needs redeploy)",
                  "read_files returned a non-JSON response"):
            assert test_runner.problems_indicate_runner_outage(p and [p], ["a.py"])

    def test_per_path_problem_is_not_outage(self):
        assert not test_runner.problems_indicate_runner_outage(
            ["src/big.py: not found at HEAD"], ["src/big.py"])

    def test_multiple_problems_are_not_outage(self):
        assert not test_runner.problems_indicate_runner_outage(
            ["a.py: not found", "b.py: not found"], ["a.py", "b.py"])

    def test_empty_problems_are_not_outage(self):
        assert not test_runner.problems_indicate_runner_outage([], ["a.py"])


class TestFetchRaisesOnOutage:
    def _spec(self):
        return SimpleNamespace(id="spec_test")

    def _plan(self, monkeypatch):
        monkeypatch.setattr(d, "_load_plan",
                            lambda spec: {"test_strategy": {"repo": "r"}})

    def test_transport_failure_with_candidates_raises(self, monkeypatch):
        self._plan(monkeypatch)
        monkeypatch.setattr(
            d.test_runner, "fetch_repo_files",
            lambda repo, paths, base_ref="HEAD", timeout=30:
            ([], [TRANSPORT_PROBLEM]))
        with pytest.raises(d.RunnerOutageAtImplement):
            _ctx_files(self._spec(), TABLE)

    def test_per_path_problems_degrade_soft(self, monkeypatch):
        self._plan(monkeypatch)
        monkeypatch.setattr(
            d.test_runner, "fetch_repo_files",
            lambda repo, paths, base_ref="HEAD", timeout=30:
            ([], ["src/big.py: not found at HEAD"]))
        assert _ctx_files(self._spec(), TABLE) == []

    def test_no_candidates_never_fetches(self, monkeypatch):
        self._plan(monkeypatch)
        def boom(*a, **k):
            raise AssertionError("fetch should not run without candidates")
        monkeypatch.setattr(d.test_runner, "fetch_repo_files", boom)
        assert _ctx_files(self._spec(), "no table") == []

    def test_healthy_fetch_unchanged(self, monkeypatch):
        self._plan(monkeypatch)
        monkeypatch.setattr(
            d.test_runner, "fetch_repo_files",
            lambda repo, paths, base_ref="HEAD", timeout=30:
            ([("src/big.py", "content")], []))
        assert _ctx_files(self._spec(), TABLE) == [
            ("src/big.py", "content")]


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite",
                        workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


class TestPark:
    def test_park_sets_pending_and_keeps_retry_count(self, db):
        spec = db.create_spec(title="demo", source_md_path="spec.md")
        task = db.create_task(spec_id=spec.id, agent="implementer",
                              role="implementer", title="impl")
        db.update_task_status(task.id, TaskStatus.RUNNING)
        before = db.get_task(task.id).retry_count

        d._requeue_implement_for_runner_outage(
            db, db.get_spec(spec.id), db.get_task(task.id), TRANSPORT_PROBLEM)

        after = db.get_task(task.id)
        assert after.status == TaskStatus.PENDING
        assert after.retry_count == before
        # DEV-652: a classified no-verdict, not a private TEST_RAN row.
        ev = [e.payload for e in db.list_events_by_kind(
                  spec_id=spec.id, kind=d.EventKind.FAILURE_CLASSIFIED, limit=10)]
        assert len(ev) == 1
        assert ev[0]["cls"] == "runner_outage"
        assert ev[0]["outcome"] == "no_verdict"
        assert ev[0]["phase"] == "existing_fetch"
        assert ev[0]["disposition"] == "requeue"

    def test_repeat_parks_are_uncapped_and_each_one_is_recorded(self, db):
        """DEV-652. This replaces a test that asserted the OPPOSITE, and the
        behaviour it asserted was a bug: the old anti-spam counted only the
        events it had itself written, so ``prior`` went 0 -> 1 and stuck
        there, and the "first park and every 20th thereafter" its docstring
        promised wrote exactly one event, ever.

        A fetch outage is uncapped by design (_CAPS gives
        (RUNNER_OUTAGE, "existing_fetch") no cap) because nothing has been
        spent and a powered-off Mac lasts hours — so every re-probe requeues
        and every requeue is now on the record.
        """
        spec = db.create_spec(title="demo", source_md_path="spec.md")
        task = db.create_task(spec_id=spec.id, agent="implementer",
                              role="implementer", title="impl")
        for _ in range(5):
            d._requeue_implement_for_runner_outage(
                db, db.get_spec(spec.id), db.get_task(task.id), "down")

        ev = [e.payload for e in db.list_events_by_kind(
                  spec_id=spec.id, kind=d.EventKind.FAILURE_CLASSIFIED, limit=50)]
        assert len(ev) == 5
        assert {e["disposition"] for e in ev} == {"requeue"}
        # never charged, never parked behind a gate
        assert db.get_task(task.id).retry_count == 0
        assert db.get_task(task.id).status == TaskStatus.PENDING
