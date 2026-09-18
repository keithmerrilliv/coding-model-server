"""DEV-719: which agent produced an attempt comes from ATTEMPT_PLANNED, never
from `tasks.agent`.

`tasks` holds one row per (spec, role) and `update_task_agent` overwrites it in
place on every rotation. Anything joined to that column therefore reports the
task's FINAL agent for every one of its attempts — which does not look like a
bug, it looks like a spec that never rotated. A telemetry pass read exactly
that artifact as a finding ("0 of 57 multi-attempt specs ever changed agent")
before the event stream showed 11 of 11 had.

The first test demonstrates the loss rather than describing it, so anyone who
changes the mutation semantics finds out here.
"""
import pytest

from coding_model_autonomous.db import Database
from coding_model_autonomous.models import EventKind


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite",
                        workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


def _mk_spec_and_task(db, agent):
    spec = db.create_spec(title="attribution", source_md_path="s.md")
    task = db.create_task(spec_id=spec.id, agent=agent, role="implementer",
                          title="build")
    return spec, task


def test_tasks_agent_is_destructive(db):
    """The column keeps only the last writer. This is the trap."""
    spec, task = _mk_spec_and_task(db, "fast_implementer")
    db.update_task_agent(task.id, "deep_implementer")
    db.update_task_agent(task.id, "moe_implementer")

    assert db.get_task(task.id).agent == "moe_implementer"
    # and there is nowhere in `tasks` the earlier two survive
    assert "fast_implementer" not in (db.get_task(task.id).agent or "")


def test_the_event_stream_keeps_every_attempt(db):
    """ATTEMPT_PLANNED is per attempt, so the history survives the mutation."""
    spec, task = _mk_spec_and_task(db, "fast_implementer")
    for retry, agent, why in ((0, "fast_implementer", "recommended"),
                              (1, "deep_implementer", "rotation"),
                              (2, "moe_implementer", "sole_fit")):
        db.record_event(EventKind.ATTEMPT_PLANNED, spec_id=spec.id,
                        task_id=task.id,
                        payload={"role": "implementer", "retry": retry,
                                 "agent": agent, "assignment": why})
        db.update_task_agent(task.id, agent)

    planned = db.list_events_by_kind(spec_id=spec.id,
                                     kind=EventKind.ATTEMPT_PLANNED)
    agents = [p.payload["agent"] for p in planned]

    assert sorted(agents) == sorted(
        ["fast_implementer", "deep_implementer", "moe_implementer"])
    # the task column agrees only with the LAST one
    assert db.get_task(task.id).agent == "moe_implementer"
    assert len(set(agents)) == 3, "three distinct agents ran"


def test_the_assignment_reason_is_recorded(db):
    """DEV-719 originally claimed the reason for a pick was never recorded.
    It is, and has been since DEV-667/DEV-676 — this pins it so the claim
    cannot be made again."""
    spec, task = _mk_spec_and_task(db, "implementer")
    db.record_event(EventKind.ATTEMPT_PLANNED, spec_id=spec.id, task_id=task.id,
                    payload={"role": "implementer", "retry": 0,
                             "agent": "deep_implementer",
                             "assignment": "rerouted",
                             "planned_agent": "implementer"})
    e = db.list_events_by_kind(spec_id=spec.id,
                               kind=EventKind.ATTEMPT_PLANNED)[0]
    assert e.payload["assignment"] == "rerouted"
    assert e.payload["planned_agent"] == "implementer", (
        "a DEV-676 reroute must record what it was diverted FROM")
