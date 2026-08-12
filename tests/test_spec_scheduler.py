"""DEV-393 — one spec's long agent call must not starve every other spec.

The tick loop called each state processor synchronously, so a single
retries-exhausted synthesis (~195k-token prompt, ~8 min of prefill) froze
the whole daemon: an already-approved plan gate sat unprocessed for 12+
minutes and the starved queue was indistinguishable from a hang. tick() now
hands each spec's pass to a per-spec worker pool (SpecScheduler); these
tests pin the scheduling contract, not the processors themselves.
"""
import threading
import time

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import SpecStatus


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite",
                        workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def scheduler():
    s = d.SpecScheduler(max_workers=4)
    yield s
    s.drain()


def _spec(db, status):
    return db.create_spec(title="demo", source_md_path="spec.md",
                          status=status)


class _Gate:
    """A processor stand-in that blocks until released, recording calls."""

    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.calls = []

    def __call__(self, db, spec):
        self.calls.append(spec.id)
        self.entered.set()
        assert self.release.wait(timeout=10), "gate never released"


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_slow_spec_does_not_block_other_specs(db, scheduler, monkeypatch):
    """The DEV-393 scenario: while one spec sits in a long execution pass,
    a tick must still process the other specs' passes."""
    _spec(db, SpecStatus.EXECUTING)  # the slow one, parked in the gate below
    fast = _spec(db, SpecStatus.PLAN_REVIEW)

    gate = _Gate()
    reviewed = []
    monkeypatch.setattr(d, "_process_executing", gate)
    monkeypatch.setattr(d, "_process_plan_review",
                        lambda db, spec: reviewed.append(spec.id))

    started = time.monotonic()
    d.tick(db, scheduler)
    tick_elapsed = time.monotonic() - started

    assert gate.entered.wait(timeout=5), "slow spec's pass never started"
    assert tick_elapsed < 2.0, (
        f"tick blocked {tick_elapsed:.1f}s on a spec's pass — the loop is "
        "still serial")
    assert _wait_for(lambda: reviewed == [fast.id]), (
        "the plan-review spec starved behind the executing spec's agent call")
    gate.release.set()


def test_in_flight_spec_is_not_double_processed(db, scheduler, monkeypatch):
    """Later ticks must skip a spec whose pass is still running — per-spec
    passes stay serial even though specs run concurrently."""
    spec = _spec(db, SpecStatus.EXECUTING)
    gate = _Gate()
    monkeypatch.setattr(d, "_process_executing", gate)

    d.tick(db, scheduler)
    assert gate.entered.wait(timeout=5)
    d.tick(db, scheduler)
    d.tick(db, scheduler)
    gate.release.set()
    scheduler.drain()

    assert gate.calls == [spec.id], (
        f"spec processed {len(gate.calls)} times while one pass was in "
        "flight")


def test_finished_spec_is_processed_again_next_tick(db, scheduler,
                                                    monkeypatch):
    """Once a pass completes, the next tick picks the spec up again."""
    spec = _spec(db, SpecStatus.EXECUTING)
    calls = []
    monkeypatch.setattr(d, "_process_executing",
                        lambda db, spec: calls.append(spec.id))

    d.tick(db, scheduler)
    assert _wait_for(lambda: calls == [spec.id])
    d.tick(db, scheduler)
    assert _wait_for(lambda: calls == [spec.id, spec.id]), (
        "a completed pass left the spec stuck in the in-flight registry")


def test_planner_error_still_fails_the_spec(db, scheduler, monkeypatch):
    """The per-state error policy must survive the move onto workers: a
    planner crash marks the spec FAILED, exactly as the serial loop did."""
    spec = _spec(db, SpecStatus.PENDING_PLAN)

    def boom(db, spec):
        raise RuntimeError("planner blew up")

    monkeypatch.setattr(d, "_process_pending_plan", boom)
    d.tick(db, scheduler)
    assert _wait_for(
        lambda: db.get_spec(spec.id).status is SpecStatus.FAILED), (
        "planner error no longer fails the spec")


def test_stale_queued_pass_is_skipped(db, monkeypatch):
    """A pass that waited in the queue while the spec moved on (gate
    decision, cancellation) must not run against the stale snapshot."""
    spec = _spec(db, SpecStatus.PENDING_PLAN)
    calls = []
    monkeypatch.setattr(d, "_process_pending_plan",
                        lambda db, spec: calls.append(spec.id))

    # Simulate the queue delay: the spec was listed as PENDING_PLAN, but by
    # the time the worker runs it an approval moved it to EXECUTING.
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    d._run_spec_pass(db, spec.id, SpecStatus.PENDING_PLAN, "planner",
                     d._process_pending_plan, SpecStatus.FAILED)

    assert calls == [], "stale pass ran against a spec that had moved on"
    assert db.get_spec(spec.id).status is SpecStatus.EXECUTING


def test_inline_tick_without_scheduler_is_synchronous(db, monkeypatch):
    """tick(db) with no scheduler keeps the old serial contract tests and
    one-shot callers rely on: every pass has completed by return."""
    s1 = _spec(db, SpecStatus.PLAN_REVIEW)
    s2 = _spec(db, SpecStatus.EXECUTING)
    order = []
    monkeypatch.setattr(d, "_process_plan_review",
                        lambda db, spec: order.append(("review", spec.id)))
    monkeypatch.setattr(d, "_process_executing",
                        lambda db, spec: order.append(("execute", spec.id)))

    d.tick(db)

    assert order == [("review", s1.id), ("execute", s2.id)]


# ── DEV-559: drain is bounded — a blocked model call must not hold the stop ──

def test_drain_returns_true_when_passes_finish_inside_the_grace():
    s = d.SpecScheduler(max_workers=2)
    s.submit("spec_a", "architect", lambda: time.sleep(0.05))
    assert s.drain(grace_seconds=5.0) is True


def test_drain_gives_up_after_the_grace_and_returns_false():
    """An 8-minute architect call used to make the graceful stop decorative:
    TimeoutStopSec=30 SIGKILLed the daemon on every mid-generation restart.
    The bounded drain exits the wait instead and leaves the pass to crash
    recovery (one recovery each, per DEV-558)."""
    import threading
    release = threading.Event()
    s = d.SpecScheduler(max_workers=2)
    s.submit("spec_b", "architect", lambda: release.wait(timeout=30))
    t0 = time.monotonic()
    try:
        assert s.drain(grace_seconds=0.5) is False
        assert time.monotonic() - t0 < 5.0, "drain must not wait for the call"
    finally:
        release.set()


def test_drain_cancels_queued_but_unstarted_passes():
    import threading
    release = threading.Event()
    started = []
    s = d.SpecScheduler(max_workers=1)
    s.submit("spec_c", "architect", lambda: release.wait(timeout=30))
    s.submit("spec_d", "planner", lambda: started.append("d"))
    try:
        assert s.drain(grace_seconds=0.3) is False
    finally:
        release.set()
    time.sleep(0.2)
    assert started == [], "a queued pass must be cancelled, not started"
