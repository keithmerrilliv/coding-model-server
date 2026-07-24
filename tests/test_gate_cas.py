"""Compare-and-set on review-gate responses (DEV-123).

respond_to_gate used to be an unguarded UPDATE ... WHERE id=?: if a human
decided via CLI/HTTP while the Jira reverse-sync thread was mid round-trip,
the sync overwrote the decision (last-writer-wins), emitting two contradictory
GATE_RESPONDED events and flipping the gate state the implementer retry and
forward sync had already acted on. These pin the guarded behavior:

  * first decision wins; the second raises GateAlreadyDecidedError
  * the loser writes nothing — no second GATE_RESPONDED event
  * HTTP route surfaces the lost race as 409, not a silent overwrite
  * Jira reverse sync treats it as an informational no-op
"""
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi import HTTPException

import coding_model_server.routes.autonomous as routes
from coding_model_autonomous.db import Database, GateAlreadyDecidedError
from coding_model_autonomous.jira_sync import JiraSync
from coding_model_autonomous.models import (
    EventKind,
    GateRespondRequest,
    GateStatus,
    GateType,
    SpecStatus,
)


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def pending_gate(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    gate = db.create_gate(spec_id=spec.id, gate_type=GateType.CODE_REVIEW,
                          prompt_md="## review me")
    return spec, gate


def _gate_responded_events(db, spec_id):
    return [e for e in db.list_recent_events(spec_id=spec_id, limit=100)
            if e.kind is EventKind.GATE_RESPONDED]


def test_first_decision_wins_second_raises(db, pending_gate):
    spec, gate = pending_gate
    db.respond_to_gate(gate.id, "rejected", notes="needs work")

    with pytest.raises(GateAlreadyDecidedError) as exc:
        db.respond_to_gate(gate.id, "approved", notes="LGTM from Jira")

    # The standing decision is intact and the error carries it.
    assert exc.value.gate.status is GateStatus.REJECTED
    stored = db.get_gate(gate.id)
    assert stored.status is GateStatus.REJECTED
    assert stored.reviewer_notes == "needs work"
    # Exactly one GATE_RESPONDED event — the loser recorded nothing.
    assert len(_gate_responded_events(db, spec.id)) == 1


def test_missing_gate_still_raises_keyerror(db):
    with pytest.raises(KeyError):
        db.respond_to_gate("gate_ffffffff", "approved")


def test_route_returns_409_on_lost_race(db, pending_gate):
    spec, gate = pending_gate
    db.respond_to_gate(gate.id, "approved")

    with mock.patch.object(routes, "get_autonomous_db", return_value=db):
        with pytest.raises(HTTPException) as exc:
            routes.respond_to_gate(
                gate.id, GateRespondRequest(decision="rejected", notes="too late"))

    assert exc.value.status_code == 409
    assert "already approved" in exc.value.detail
    assert db.get_gate(gate.id).status is GateStatus.APPROVED


def test_reverse_sync_lost_race_is_noop(db, pending_gate):
    spec, gate = pending_gate
    db.respond_to_gate(gate.id, "rejected", notes="human said no")

    sync = JiraSync.__new__(JiraSync)  # only .db is touched by this path
    sync.db = db
    issue = SimpleNamespace(key="AUTO-7", comments=["looks good, shipping"])
    # Must not raise and must not overwrite the CLI decision.
    sync._respond_from_jira(gate.id, "approved", issue)

    stored = db.get_gate(gate.id)
    assert stored.status is GateStatus.REJECTED
    assert stored.reviewer_notes == "human said no"
    assert len(_gate_responded_events(db, spec.id)) == 1
