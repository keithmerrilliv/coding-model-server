"""Tests for DEV-653: terminal specs retire their open review gates."""
import logging

import pytest

from coding_model_autonomous.db import Database, TERMINAL_SPEC_STATUSES
from coding_model_autonomous.models import (
    EventKind,
    GateStatus,
    GateType,
    SpecStatus,
)


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


def _spec_with_gates(db: Database, n: int):
    """Create a spec with one implementer task and *n* open code-review gates.

    Returns ``(spec, [gates])``.
    """
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    # Write the spec markdown so spec_dir() has something.
    db.spec_dir(spec.id).joinpath("spec.md").write_text("# Demo\n")
    # Set it EXECUTING so gates can be opened on it.
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    # Create an implementer task (required before opening CODE_REVIEW gate).
    db.create_task(spec_id=spec.id, agent="implementer", role="implementer", title="b")
    # Open the requested number of gates.
    gates = []
    for i in range(n):
        g = db.create_gate(
            spec_id=spec.id,
            gate_type=GateType.CODE_REVIEW,
            prompt_md=f"## review {i}",
        )
        gates.append(g)
    return spec, gates


def cancelled_events(db: Database, spec_id: str) -> list:
    """Return GATE_RESPONDED events whose payload['decision'] == 'cancelled'."""
    all_events = db.list_events_by_kind(spec_id=spec_id, kind=EventKind.GATE_RESPONDED)
    return [
        ev
        for ev in all_events
        if ev.payload is not None and ev.payload.get("decision") == "cancelled"
    ]


# T1 — TERMINAL_SPEC_STATUSES constant has correct value
def test_terminal_spec_statuses_constant():
    assert TERMINAL_SPEC_STATUSES == (
        SpecStatus.DONE,
        SpecStatus.FAILED,
        SpecStatus.CANCELLED,
    )


# T2 — FAILED status retires two open gates
def test_failed_retires_two_gates(db):
    spec, gates = _spec_with_gates(db, 2)

    result = db.update_spec_status(spec.id, SpecStatus.FAILED)

    assert result is True
    assert db.list_open_gates(spec.id) == []
    for g in gates:
        gate = db.get_gate(g.id)
        assert gate.status is GateStatus.CANCELLED

    cancelled = cancelled_events(db, spec.id)
    assert len(cancelled) == 2
    cancelled_ids = {ev.gate_id for ev in cancelled}
    assert cancelled_ids == {gates[0].id, gates[1].id}


# T3 — CANCELLED status with one open gate
def test_cancelled_retires_one_gate(db):
    spec, gates = _spec_with_gates(db, 1)

    result = db.update_spec_status(spec.id, SpecStatus.CANCELLED)

    assert result is True
    assert db.list_open_gates(spec.id) == []

    gate = db.get_gate(gates[0].id)
    assert gate.status is GateStatus.CANCELLED

    cancelled = cancelled_events(db, spec.id)
    assert len(cancelled) == 1


# T4 — DONE status retires the open gate
def test_done_retires_gate(db):
    spec, gates = _spec_with_gates(db, 1)

    result = db.update_spec_status(spec.id, SpecStatus.DONE)

    assert result is True
    gate = db.get_gate(gates[0].id)
    assert gate.status is GateStatus.CANCELLED


# T5 — Non-terminal write leaves gate PENDING and records no cancellation
def test_nonterminal_keeps_gate_pending(db):
    spec, gates = _spec_with_gates(db, 1)

    result = db.update_spec_status(spec.id, SpecStatus.PLAN_REVIEW)

    assert result is True
    gate = db.get_gate(gates[0].id)
    assert gate.status is GateStatus.PENDING

    cancelled = cancelled_events(db, spec.id)
    assert len(cancelled) == 0


# T6 — retire_open_gates on already-retired returns empty list
def test_retire_already_retired_returns_empty(db):
    spec, gates = _spec_with_gates(db, 1)

    # First call: terminal transition.
    db.update_spec_status(spec.id, SpecStatus.FAILED)

    initial_count = len(cancelled_events(db, spec.id))

    # Second call: explicit retire (no-op).
    retired = db.retire_open_gates(spec.id)

    assert retired == []
    assert len(cancelled_events(db, spec.id)) == initial_count


# T7 — isolation between specs
def test_isolation_between_specs(db):
    spec_a, gates_a = _spec_with_gates(db, 1)
    spec_b, gates_b = _spec_with_gates(db, 1)

    db.update_spec_status(spec_a.id, SpecStatus.FAILED)

    gate_b = db.get_gate(gates_b[0].id)
    assert gate_b.status is GateStatus.PENDING


# T8 — cancel_spec still produces exactly ONE cancellation per gate
def test_cancel_spec_no_double_cancel(db):
    spec, gates = _spec_with_gates(db, 1)

    db.cancel_spec(spec.id, reason="drill")

    cancelled = cancelled_events(db, spec.id)
    assert len(cancelled) == 1
    assert cancelled[0].gate_id == gates[0].id


# T9 — WARNING log contains expected strings after terminal write; no new record after no-op
def test_warning_logging(db, caplog):
    with caplog.at_level(logging.WARNING):
        spec, gates = _spec_with_gates(db, 2)

        # First: terminal transition logs.
        db.update_spec_status(spec.id, SpecStatus.FAILED)

        dev653_records = [r for r in caplog.records if "DEV-653" in r.getMessage()]
        assert len(dev653_records) >= 1
        assert any("retired on terminal status" in r.getMessage() for r in dev653_records)

        # Capture count before the explicit call.
        pre_count = sum(1 for r in caplog.records if "DEV-653" in r.getMessage())

        # Second: explicit retire (no-op) should NOT add a DEV-653 line.
        db.retire_open_gates(spec.id)

        post_dev653_records = [
            r for r in caplog.records if "DEV-653" in r.getMessage()
        ]
        assert len(post_dev653_records) == pre_count


# T10 — SPEC_STATUS_CHANGED event recorded BEFORE cancelled gate events
def test_event_ordering(db):
    spec, gates = _spec_with_gates(db, 2)

    db.update_spec_status(spec.id, SpecStatus.FAILED)

    status_events = db.list_events_by_kind(
        spec_id=spec.id, kind=EventKind.SPEC_STATUS_CHANGED
    )
    assert len(status_events) > 0

    # Newest first; find one with new_status == 'failed'.
    status_ev = next((e for e in status_events if e.payload and e.payload.get("new_status") == "failed"), None)
    assert status_ev is not None, f"No FAILED status event found among {status_events}"

    cancelled = cancelled_events(db, spec.id)
    assert len(cancelled) >= 1

    # Every GATE_RESPONDED decision=cancelled must have id greater than the status change's id.
    for ev in cancelled:
        assert ev.id is not None
        assert ev.id > status_ev.id, (
            f"Cancelled event (id={ev.id}) should be after status event "
            f"(id={status_ev.id}); got reversed order"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
