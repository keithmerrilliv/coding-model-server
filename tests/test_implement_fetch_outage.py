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
            d._fetch_existing_files_for_spec(self._spec(), TABLE)

    def test_per_path_problems_degrade_soft(self, monkeypatch):
        self._plan(monkeypatch)
        monkeypatch.setattr(
            d.test_runner, "fetch_repo_files",
            lambda repo, paths, base_ref="HEAD", timeout=30:
            ([], ["src/big.py: not found at HEAD"]))
        assert d._fetch_existing_files_for_spec(self._spec(), TABLE) == []

    def test_no_candidates_never_fetches(self, monkeypatch):
        self._plan(monkeypatch)
        def boom(*a, **k):
            raise AssertionError("fetch should not run without candidates")
        monkeypatch.setattr(d.test_runner, "fetch_repo_files", boom)
        assert d._fetch_existing_files_for_spec(self._spec(), "no table") == []

    def test_healthy_fetch_unchanged(self, monkeypatch):
        self._plan(monkeypatch)
        monkeypatch.setattr(
            d.test_runner, "fetch_repo_files",
            lambda repo, paths, base_ref="HEAD", timeout=30:
            ([("src/big.py", "content")], []))
        assert d._fetch_existing_files_for_spec(self._spec(), TABLE) == [
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
        events = [e for e in db.list_events_by_kind(
                      spec_id=spec.id, kind=d.EventKind.TEST_RAN, limit=10)
                  if (e.payload or {}).get("phase") == "implement_existing_fetch"]
        assert len(events) == 1
        assert events[0].payload.get("runner_unreachable") is True

    def test_repeat_parks_do_not_spam_events(self, db):
        spec = db.create_spec(title="demo", source_md_path="spec.md")
        task = db.create_task(spec_id=spec.id, agent="implementer",
                              role="implementer", title="impl")
        for _ in range(5):
            d._requeue_implement_for_runner_outage(
                db, db.get_spec(spec.id), db.get_task(task.id), "down")
        events = [e for e in db.list_events_by_kind(
                      spec_id=spec.id, kind=d.EventKind.TEST_RAN, limit=50)
                  if (e.payload or {}).get("phase") == "implement_existing_fetch"]
        assert len(events) == 1  # first park only; next event at requeue 20
