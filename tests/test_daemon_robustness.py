"""DEV-140/142/143/144 — daemon robustness against malformed plans, double
pollers, artifact-row accumulation, and unbounded event growth."""
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import (
    ArtifactKind,
    EventKind,
    SpecStatus,
    TaskStatus,
)


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


# ── DEV-140: non-mapping plan phases ─────────────────────────────────────────

def test_string_phases_fail_the_spec_instead_of_looping(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md",
                          status=SpecStatus.EXECUTING)
    db.update_spec_status(spec.id, SpecStatus.EXECUTING,
                          normalized_yaml="phases: [design, implement, test]\n")

    d._process_executing(db, db.get_spec(spec.id))

    assert db.get_spec(spec.id).status is SpecStatus.FAILED, (
        "a plausible-LLM plan shape must fail once, not retry-log forever"
    )
    assert db.list_tasks_for_spec(spec.id) == []


def test_valid_phases_still_bootstrap(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md",
                          status=SpecStatus.EXECUTING)
    db.update_spec_status(
        spec.id, SpecStatus.EXECUTING,
        normalized_yaml="phases:\n  - name: build\n    role: implementer\n",
    )
    with mock.patch.object(d, "_start_task"):
        d._process_executing(db, db.get_spec(spec.id))
    tasks = db.list_tasks_for_spec(spec.id)
    assert len(tasks) == 1 and tasks[0].role == "implementer"


# ── DEV-142: CAS claim + single instance ─────────────────────────────────────

def test_claim_task_is_compare_and_set(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    task = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="t")
    assert db.claim_task(task.id) is True, "first poller wins"
    assert db.claim_task(task.id) is False, "second poller must lose"
    assert db.get_task(task.id).status is TaskStatus.RUNNING


def test_start_task_skips_an_already_claimed_task(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md",
                          status=SpecStatus.EXECUTING)
    task = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="t")
    db.claim_task(task.id)  # the other poller got here first

    with mock.patch.object(d, "_run_implementer") as run:
        d._start_task(db, db.get_spec(spec.id), db.get_task(task.id))
    run.assert_not_called()


# ── DEV-143: reviewer artifact dedup ─────────────────────────────────────────

def test_reviewer_sees_each_path_once_after_retries(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    task = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="t")
    spec_dir = db.spec_dir(spec.id)
    (spec_dir / "app.py").write_text("final version")
    # Three retries re-inserted the same path three times.
    for _ in range(3):
        db.create_artifact(spec_id=spec.id, task_id=task.id,
                           kind=ArtifactKind.CODE, path="app.py")

    files = d._collect_reviewer_code_files(db, spec.id, spec_dir)

    assert files == [("app.py", "final version")], (
        "each on-disk file must appear exactly once in the reviewer prompt"
    )


# ── DEV-144: event retention + index ─────────────────────────────────────────

def test_prune_removes_only_old_heartbeats(db):
    db.record_event(EventKind.DAEMON_TICK)
    db.record_event(EventKind.SPEC_SUBMITTED)
    # Backdate both far past the retention window.
    db._conn().execute("UPDATE events SET created_at = '2020-01-01T00:00:00+00:00'")
    db._conn().commit()
    db.record_event(EventKind.DAEMON_TICK)  # fresh tick — kept

    removed = db.prune_daemon_ticks(older_than_days=14)

    assert removed == 1
    kinds = [r["kind"] for r in
             db._conn().execute("SELECT kind FROM events").fetchall()]
    assert kinds.count(EventKind.DAEMON_TICK.value) == 1, "fresh tick kept"
    assert EventKind.SPEC_SUBMITTED.value in kinds, "real events never swept"


def test_redundant_events_id_index_is_gone(db):
    rows = db._conn().execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_events_id'"
    ).fetchall()
    assert rows == [], "idx_events_id duplicates the rowid btree"
