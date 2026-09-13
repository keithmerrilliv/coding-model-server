"""DEV-583 / DEV-493: a first-class operator cancel.

One call sets CANCELLED (terminal against in-flight writers, DEV-567),
cancels every open gate, closes every task row, records the reason — and a
gate a late pass opens afterwards is born cancelled rather than orphaned.
The admin swap reset clears leaked reservations without a restart.
"""
import logging

import pytest
from fastapi import HTTPException

import coding_model_server.routes.autonomous as routes
from coding_model_autonomous.db import Database, SpecAlreadyTerminal
from coding_model_autonomous.jira_client import FakeJiraClient
from coding_model_autonomous.jira_sync import JiraSync
from coding_model_autonomous.models import (
    CancelSpecRequest, EventKind, GateStatus, GateType, SpecStatus, TaskStatus,
)


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def running(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    (db.spec_dir(spec.id) / "spec.md").write_text("# demo")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    impl = db.create_task(spec_id=spec.id, agent="implementer", role="implementer", title="b")
    rev = db.create_task(spec_id=spec.id, agent="reviewer", role="reviewer", title="r")
    db.update_task_status(impl.id, TaskStatus.RUNNING)
    gate = db.create_gate(spec_id=spec.id, gate_type=GateType.CODE_REVIEW, prompt_md="## review")
    return spec, impl, rev, gate


class TestCancelSpec:
    def test_one_call_leaves_the_store_consistent(self, db, running, caplog):
        spec, impl, rev, gate = running
        with caplog.at_level(logging.WARNING):
            out = db.cancel_spec(spec.id, reason="spiralling on access-control errors")
        assert db.get_spec(spec.id).status is SpecStatus.CANCELLED
        assert db.get_gate(gate.id).status is GateStatus.CANCELLED
        assert db.get_task(impl.id).status is TaskStatus.SKIPPED
        assert db.get_task(rev.id).status is TaskStatus.SKIPPED
        assert out["gates_cancelled"] == [gate.id]
        assert set(out["tasks_closed"]) == {impl.id, rev.id}
        assert out["in_flight"] == ["implementer"]
        assert out["previous_status"] == "executing"
        assert any("CANCELLED by operator" in r.getMessage() for r in caplog.records)

    def test_the_reason_is_on_the_events(self, db, running):
        spec, *_ = running
        db.cancel_spec(spec.id, reason="doomed")
        evs = db.list_events_by_kind(spec_id=spec.id, kind=EventKind.SPEC_STATUS_CHANGED)
        payloads = [e.payload for e in evs]
        assert any(p.get("cancelled_by") == "operator" and p.get("reason") == "doomed"
                   for p in payloads)

    def test_a_terminal_spec_refuses(self, db, running):
        spec, *_ = running
        db.cancel_spec(spec.id)
        with pytest.raises(SpecAlreadyTerminal):
            db.cancel_spec(spec.id)

    def test_an_unknown_spec_refuses(self, db):
        with pytest.raises(ValueError):
            db.cancel_spec("spec_nope")

    def test_a_late_pass_cannot_resurrect_or_orphan(self, db, running):
        """DEV-567 + DEV-567 item 2: the in-flight pass finishes after the
        cancel — its status write is discarded and its gate is born
        cancelled, so nothing waits on a human."""
        spec, impl, *_ = running
        db.cancel_spec(spec.id)
        assert db.update_spec_status(spec.id, SpecStatus.EXECUTING) is False
        late = db.create_gate(spec_id=spec.id, gate_type=GateType.CODE_REVIEW, prompt_md="late")
        assert late.status is GateStatus.CANCELLED
        assert db.get_gate(late.id).status is GateStatus.CANCELLED
        assert db.list_open_gates(spec.id) == []

    def test_the_reason_reaches_the_jira_epic(self, db, running):
        spec, *_ = running
        client = FakeJiraClient()
        sync = JiraSync(db, client)
        sync.tick()
        db.cancel_spec(spec.id, reason="duplicate submission")
        sync.tick()
        epic = client.get_issue(db.get_spec(spec.id).jira_epic_key)
        assert epic.status == "Cancelled"
        assert any("Cancelled by operator" in c and "duplicate submission" in c
                   for c in epic.comments)


class TestTheEndpoint:
    def test_cancel(self, db, running, monkeypatch):
        spec, *_ = running
        monkeypatch.setattr(routes, "get_autonomous_db", lambda: db)
        out = routes.cancel_spec(spec.id, CancelSpecRequest(reason="x"))
        assert out["status"] == "cancelled" and out["reason"] == "x"

    def test_404_and_409(self, db, running, monkeypatch):
        spec, *_ = running
        monkeypatch.setattr(routes, "get_autonomous_db", lambda: db)
        with pytest.raises(HTTPException) as e:
            routes.cancel_spec("spec_nope", None)
        assert e.value.status_code == 404
        routes.cancel_spec(spec.id, None)
        with pytest.raises(HTTPException) as e:
            routes.cancel_spec(spec.id, None)
        assert e.value.status_code == 409


class TestSwapReset:
    def _manager(self, active, live):
        from threading import Lock
        from coding_model_server.llama_server import LlamaServerManager
        m = LlamaServerManager.__new__(LlamaServerManager)
        m.lock = Lock()
        m._active_requests = active
        m._live_proxies = live
        m._orphan_slot_since = 123.0
        return m

    def test_clears_a_leak(self):
        m = self._manager(active=1, live=0)
        out = m.reset_swap_state()
        assert out == {"reset": True, "live_proxies": 0, "active_requests_cleared": 1, "forced": False}
        assert m._active_requests == 0 and m._orphan_slot_since is None

    def test_refuses_real_work_unless_forced(self):
        m = self._manager(active=1, live=1)
        assert m.reset_swap_state()["reset"] is False
        assert m._active_requests == 1
        assert m.reset_swap_state(force=True)["forced"] is True
        assert m._active_requests == 0

    def test_the_endpoint_maps_refusal_to_409(self, monkeypatch):
        import coding_model_server.routes.admin as admin
        monkeypatch.setattr(admin, "llama_server_manager", self._manager(active=1, live=1))
        with pytest.raises(HTTPException) as e:
            admin.admin_swap_reset()
        assert e.value.status_code == 409
        assert admin.admin_swap_reset(force=True)["reset"] is True
