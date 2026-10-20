"""DEV-926 — a delivery the daemon owes is finished, not forgotten.

A push can outlive the shutdown grace, and a DONE spec is never walked by the
state machine again, so whether a delivery is still owed is read from the
spec's own events: delivery_requested, delivery_attempt, delivery (the
outcome). These tests drive the real daemon functions against a real
Database; only the git-touching step is replaced.
"""
import pytest
from fastapi import HTTPException

from coding_model_autonomous import delivery
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import EventKind, SpecStatus
from coding_model_server import orchestrator_daemon as daemon
from coding_model_server.routes import autonomous as routes


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "t.db", workspace_root=tmp_path)


def _done_spec(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    db.update_spec_status(spec.id, SpecStatus.DONE)
    return db.get_spec(spec.id)


def _roles(db, spec_id):
    """Delivery-related roles, oldest first."""
    evs = db.list_events_by_kind(spec_id=spec_id, kind=EventKind.AGENT_RAN, limit=-1)
    return [(e.payload or {}).get("role") for e in reversed(evs)
            if (e.payload or {}).get("role", "").startswith("delivery")]


def _last_outcome(db, spec_id):
    for e in db.list_events_by_kind(spec_id=spec_id, kind=EventKind.AGENT_RAN, limit=-1):
        if (e.payload or {}).get("role") == delivery.OUTCOME_ROLE:
            return e.payload
    return None


def _attempt(db, spec, n=1):
    daemon._anomaly(db, spec, None, delivery.ATTEMPT_ROLE, attempt=n, max_attempts=3)


class FakeDelivery:
    """Stands in for _deliver_completed_spec, the step that touches git."""

    def __init__(self, mode="pushed"):
        self.mode = mode
        self.calls = []

    def __call__(self, db, spec):
        self.calls.append(spec.id)
        if self.mode == "interrupted":
            return  # the process died here: attempt recorded, no outcome
        if self.mode == "crash":
            raise RuntimeError("remote hung up")
        daemon._record_delivery_outcome(
            db, spec, delivery.DeliveryResult("pushed", "ok", "pipeline/x"), [])


@pytest.fixture
def fake(monkeypatch):
    f = FakeDelivery()
    monkeypatch.setattr(daemon, "_deliver_completed_spec", f)
    monkeypatch.setattr(daemon.settings, "DELIVERY_MAX_ATTEMPTS", 3)
    return f


class TestDebt:
    def test_debt_follows_the_events(self, db):
        spec = _done_spec(db)
        assert delivery.delivery_debt(db, spec.id) == delivery.DeliveryDebt(False, 0)
        delivery.request_delivery(db, spec.id, "release")
        assert delivery.delivery_debt(db, spec.id) == delivery.DeliveryDebt(True, 0)
        _attempt(db, spec)
        _attempt(db, spec, 2)
        assert delivery.delivery_debt(db, spec.id) == delivery.DeliveryDebt(True, 2)
        daemon._record_delivery_outcome(
            db, spec, delivery.DeliveryResult("failed", "rejected"), [])
        # An outcome settles the debt, a failure included: it has a report.
        assert delivery.delivery_debt(db, spec.id) == delivery.DeliveryDebt(False, 0)
        delivery.request_delivery(db, spec.id, "operator")
        assert delivery.delivery_debt(db, spec.id).owed

    def test_one_debt_is_requested_once(self, db):
        spec = _done_spec(db)
        assert delivery.request_delivery(db, spec.id, "release") is True
        assert delivery.request_delivery(db, spec.id, "operator") is False
        assert _roles(db, spec.id) == ["delivery_requested"]

    def test_an_old_request_behind_a_long_tail_still_counts(self, db):
        """Every row is read: a newest-N window would lose the request."""
        spec = _done_spec(db)
        delivery.request_delivery(db, spec.id, "release")
        for _ in range(60):
            daemon._anomaly(db, spec, None, "something_else")
        assert delivery.delivery_debt(db, spec.id).owed


class TestInterruptedDelivery:
    def test_finished_on_the_next_start(self, db, fake):
        """The previous process recorded the attempt and died mid-push."""
        spec = _done_spec(db)
        delivery.request_delivery(db, spec.id, "release")
        _attempt(db, spec)

        daemon._deliver_owed(db)          # a fresh process: first tick

        assert fake.calls == [spec.id]
        assert _roles(db, spec.id) == [
            "delivery_requested", "delivery_attempt", "delivery_attempt", "delivery"]
        assert _last_outcome(db, spec.id)["status"] == "pushed"
        assert not delivery.delivery_debt(db, spec.id).owed
        daemon._deliver_owed(db)
        assert fake.calls == [spec.id], "settled debt is not delivered twice"

    def test_the_cap_turns_the_interruption_into_an_outcome(self, db, fake):
        spec = _done_spec(db)
        delivery.request_delivery(db, spec.id, "release")
        for n in (1, 2, 3):
            _attempt(db, spec, n)

        daemon._deliver_owed(db)

        assert fake.calls == [], "at the cap the daemon stops starting it"
        outcome = _last_outcome(db, spec.id)
        assert outcome["status"] == "failed"
        assert "3 time(s)" in outcome["detail"]
        assert f"redeliver {spec.id}" in outcome["detail"]
        assert not delivery.delivery_debt(db, spec.id).owed

    def test_a_delivery_that_failed_on_its_own_is_not_retried(self, db, fake):
        spec = _done_spec(db)
        delivery.request_delivery(db, spec.id, "release")
        _attempt(db, spec)
        daemon._record_delivery_outcome(
            db, spec, delivery.DeliveryResult("failed", "push rejected"), [])

        daemon._deliver_owed(db)
        assert fake.calls == []

    def test_a_crash_is_a_failed_outcome_not_an_interruption(self, db, fake):
        """Retrying a deterministic crash is the loop the cap exists to stop."""
        fake.mode = "crash"
        spec = _done_spec(db)
        delivery.request_delivery(db, spec.id, "release")

        daemon._deliver_owed(db)
        daemon._deliver_owed(db)

        assert fake.calls == [spec.id]
        outcome = _last_outcome(db, spec.id)
        assert outcome["status"] == "failed"
        assert "RuntimeError: remote hung up" in outcome["detail"]
        assert f"redeliver {spec.id}" in outcome["detail"]

    def test_a_non_done_spec_is_left_alone(self, db, fake):
        spec = db.create_spec(title="demo", source_md_path="spec.md")
        db.update_spec_status(spec.id, SpecStatus.EXECUTING)
        delivery.request_delivery(db, spec.id, "release")
        daemon._deliver_owed(db)
        assert fake.calls == []


class TestOperatorRequest:
    def test_picked_up_from_the_event_cursor(self, db, fake):
        """After the one-time scan only new events are read; a redeliver
        request is one of them."""
        spec = _done_spec(db)
        daemon._deliver_owed(db)              # scan: nothing owed
        assert fake.calls == []
        delivery.request_delivery(db, spec.id, "operator")
        daemon._deliver_owed(db)
        assert fake.calls == [spec.id]
        assert _last_outcome(db, spec.id)["status"] == "pushed"

    def test_cursor_walks_more_than_one_batch(self, db, fake, monkeypatch):
        monkeypatch.setattr(daemon, "_EVENT_BATCH", 5)
        spec = _done_spec(db)
        daemon._deliver_owed(db)
        for _ in range(12):
            daemon._anomaly(db, spec, None, "noise")
        delivery.request_delivery(db, spec.id, "operator")
        daemon._deliver_owed(db)
        assert fake.calls == [spec.id]


class FakeScheduler:
    def __init__(self, accept):
        self.accept = accept
        self.submitted = []

    def submit(self, spec_id, label, fn, *args):
        self.submitted.append((spec_id, label))
        if not self.accept:
            return False
        fn(*args)
        return True


class TestScheduler:
    def test_a_refused_submit_stays_pending_until_accepted(self, db, fake):
        """A pass on the same spec is in flight: the delivery waits its turn."""
        spec = _done_spec(db)
        delivery.request_delivery(db, spec.id, "release")
        _attempt(db, spec)
        sched = FakeScheduler(accept=False)
        daemon._deliver_owed(db, sched)
        assert sched.submitted == [(spec.id, "delivery")]
        assert fake.calls == []
        assert spec.id in daemon._delivery_watches[db].pending
        sched.accept = True
        daemon._deliver_owed(db, sched)
        assert fake.calls == [spec.id]
        assert spec.id not in daemon._delivery_watches[db].pending

    def test_tick_runs_the_owed_check(self, db, fake, monkeypatch):
        """It is part of tick(), after the passes, so nobody has to call it."""
        spec = _done_spec(db)
        delivery.request_delivery(db, spec.id, "operator")
        daemon.tick(db)
        assert fake.calls == [spec.id]


class TestRedeliverEndpoint:
    def test_404_409_then_queued_once(self, db, monkeypatch):
        monkeypatch.setattr(routes, "get_autonomous_db", lambda: db)
        with pytest.raises(HTTPException) as e:
            routes.redeliver_spec("spec_nope")
        assert e.value.status_code == 404

        running = db.create_spec(title="demo", source_md_path="spec.md")
        with pytest.raises(HTTPException) as e:
            routes.redeliver_spec(running.id)
        assert e.value.status_code == 409

        spec = _done_spec(db)
        assert routes.redeliver_spec(spec.id) == {"spec_id": spec.id, "queued": True}
        assert routes.redeliver_spec(spec.id) == {"spec_id": spec.id, "queued": False}
        evs = db.list_events_by_kind(spec_id=spec.id, kind=EventKind.AGENT_RAN, limit=-1)
        requests = [e.payload for e in evs if e.payload.get("role") == "delivery_requested"]
        assert len(requests) == 1 and requests[0]["trigger"] == "operator"
        assert requests[0]["model_call"] is False


class TestReleasePath:
    """The production path: the executing pass finds every task finished."""

    def _finish_all_tasks(self, db, monkeypatch):
        from types import SimpleNamespace
        from coding_model_autonomous.models import TaskStatus
        spec = db.create_spec(title="demo", source_md_path="spec.md")
        db.update_spec_status(spec.id, SpecStatus.EXECUTING)
        monkeypatch.setattr(db, "list_tasks_for_spec",
                            lambda spec_id: [SimpleNamespace(status=TaskStatus.DONE)])
        monkeypatch.setattr(daemon, "_find_current_task", lambda tasks: None)
        return db.get_spec(spec.id)

    def test_debt_is_recorded_before_done_and_settled_by_the_delivery(
            self, db, fake, monkeypatch):
        spec = self._finish_all_tasks(db, monkeypatch)
        daemon._process_executing(db, spec)
        assert db.get_spec(spec.id).status == SpecStatus.DONE
        assert fake.calls == [spec.id]
        assert _roles(db, spec.id) == ["delivery_requested", "delivery_attempt", "delivery"]
        evs = db.list_events_by_kind(spec_id=spec.id, kind=EventKind.AGENT_RAN, limit=-1)
        req = next(e for e in evs if e.payload.get("role") == "delivery_requested")
        assert req.payload["trigger"] == "release"
        assert not delivery.delivery_debt(db, spec.id).owed

    def test_a_restart_mid_push_is_finished_by_the_next_process(
            self, db, fake, monkeypatch, tmp_path):
        fake.mode = "interrupted"
        spec = self._finish_all_tasks(db, monkeypatch)
        daemon._process_executing(db, spec)
        assert db.get_spec(spec.id).status == SpecStatus.DONE
        assert delivery.delivery_debt(db, spec.id) == delivery.DeliveryDebt(True, 1)

        # The next process: same database file, nothing in memory.
        db2 = Database(tmp_path / "t.db", workspace_root=tmp_path)
        fake.mode = "pushed"
        daemon._deliver_owed(db2)
        assert fake.calls == [spec.id, spec.id]
        assert _last_outcome(db2, spec.id)["status"] == "pushed"
        assert not delivery.delivery_debt(db2, spec.id).owed
