"""Gates waiting on a human must be visible — DEV-430.

A spec blocked on review was indistinguishable from an idle daemon: the
blocked-on-review branch returns silently every tick and the health endpoint
stayed green. On DEV-102 that read as "nothing to do" twice in one afternoon
— 18 minutes after a restart, and 12 minutes with no restart involved,
because the reviewer had no signal that a gate had opened.
"""
import logging
from datetime import datetime, timedelta, timezone

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import GateType


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


def _spec_with_pending_gate(db, gate_type=GateType.CODE_REVIEW):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    gate = db.create_gate(spec_id=spec.id, task_id=None, gate_type=gate_type,
                          prompt_md="## Code review: demo")
    return spec, gate


def test_startup_announces_every_pending_gate(db, caplog):
    spec, gate = _spec_with_pending_gate(db)
    with caplog.at_level(logging.WARNING, logger=d.logger.name):
        count = d.report_open_gates(db, startup=True)

    assert count == 1
    logged = caplog.text
    assert gate.id in logged
    assert spec.id in logged
    assert "code_review" in logged
    assert "startup" in logged


def test_periodic_report_uses_the_still_waiting_wording(db, caplog):
    _spec_with_pending_gate(db)
    with caplog.at_level(logging.WARNING, logger=d.logger.name):
        d.report_open_gates(db)
    assert "still waiting" in caplog.text


def test_no_gates_is_quiet_except_at_startup(db, caplog):
    with caplog.at_level(logging.WARNING, logger=d.logger.name):
        assert d.report_open_gates(db) == 0
    assert caplog.text.strip() == ""

    # At startup the absence is itself worth stating once.
    with caplog.at_level(logging.INFO, logger=d.logger.name):
        assert d.report_open_gates(db, startup=True) == 0
    assert "no review gates" in caplog.text


def test_answered_gates_are_not_reported(db, caplog):
    spec, gate = _spec_with_pending_gate(db)
    db.respond_to_gate(gate.id, "approved", notes="ok")
    with caplog.at_level(logging.WARNING, logger=d.logger.name):
        assert d.report_open_gates(db) == 0


def test_multiple_gates_all_reported(db, caplog):
    _spec_with_pending_gate(db)
    _spec_with_pending_gate(db, GateType.DESIGN_APPROVAL)
    with caplog.at_level(logging.WARNING, logger=d.logger.name):
        assert d.report_open_gates(db) == 2
    assert "design_approval" in caplog.text
    assert "code_review" in caplog.text


def test_reporting_never_raises_when_the_store_is_broken(caplog):
    """A logging aid must not be able to kill the daemon loop."""
    class _Broken:
        def list_open_gates(self):
            raise RuntimeError("db gone")

    with caplog.at_level(logging.ERROR, logger=d.logger.name):
        assert d.report_open_gates(_Broken()) == 0


# ── age ──────────────────────────────────────────────────────────────────────

class _Gate:
    def __init__(self, created_at):
        self.created_at = created_at


def test_age_is_reported_in_minutes():
    created = datetime.now(timezone.utc) - timedelta(minutes=18)
    assert 17.0 < d._gate_age_minutes(_Gate(created)) < 19.0


def test_age_accepts_an_iso_string():
    created = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    assert 4.0 < d._gate_age_minutes(_Gate(created)) < 6.0


def test_unusable_timestamp_degrades_to_zero():
    for bad in (None, "not-a-date", 12345):
        assert d._gate_age_minutes(_Gate(bad)) == 0.0
