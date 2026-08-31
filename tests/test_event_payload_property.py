"""DEV-622: Event.payload — the read-side twin of record_event(payload=...).

The daemon's DEV-538 requeue counters read `e.payload` with no accessor
behind it; the second unreachable-runner requeue on one spec died on
AttributeError. The property parses payload_json defensively.
"""
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import Event, EventKind


def test_payload_round_trips_through_the_db(tmp_path):
    db = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    try:
        spec = db.create_spec(title="demo", source_md_path="spec.md")
        db.record_event(EventKind.TEST_RAN, spec_id=spec.id,
                        payload={"phase": "pre_gate_build_check",
                                 "runner_unreachable": True})
        events = db.list_events_by_kind(
            spec_id=spec.id, kind=EventKind.TEST_RAN, limit=5)
        assert (events[0].payload or {}).get("runner_unreachable") is True
    finally:
        db.close_all()


def test_payload_none_when_absent():
    assert Event(kind=EventKind.DAEMON_TICK).payload is None


def test_payload_none_when_unparseable():
    assert Event(kind=EventKind.DAEMON_TICK, payload_json="{not json").payload is None


def test_payload_none_when_not_a_dict():
    assert Event(kind=EventKind.DAEMON_TICK, payload_json="[1, 2]").payload is None
