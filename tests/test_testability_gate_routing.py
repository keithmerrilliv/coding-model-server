"""A design that strands its own criteria goes back to the architect — DEV-481.

The check is worth nothing if a stranded design still reaches a human. It runs
BEFORE the design review and before the design_approval gate, has its own retry
budget so it cannot consume the design review's single revision (DEV-440), and
is capped so it cannot loop.
"""
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import GateType, SpecStatus

STRANDED = """\
# Architecture: Demo

## Data Models
- `Mushroom`: `{ hits: Int }`
- `MushroomField`: `{ cells: [Position: Mushroom] }`

## Acceptance Criteria Checklist
- [ ] Same seed produces identical field

## Criterion Seams
- Same seed | setup: `World(seed: 1)` | act: `w.field.cells` \
| assert: `w1.field.cells == w2.field.cells`
"""

SOUND = STRANDED.replace("- `Mushroom`: `{ hits: Int }`",
                         "- `Mushroom`: `{ hits: Int }` — Equatable")


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite",
                        workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def arch(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    spec_dir = db.spec_dir(spec.id)
    (spec_dir / "spec.md").write_text("# spec")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    task = db.create_task(spec_id=spec.id, agent="architect",
                          role="architect", title="design")
    return db.get_spec(spec.id), db.get_task(task.id), spec_dir


def _run_architect(db, spec, task, spec_dir, design_md):
    parsed = mock.Mock(design_md=design_md, complexity=None)
    with mock.patch.object(d, "call_agent", return_value="raw"), \
         mock.patch.object(d, "build_architect_message", return_value=[]), \
         mock.patch.object(d, "parse_architect_response", return_value=parsed), \
         mock.patch.object(d, "_run_design_review", return_value=("PASS", "")):
        d._run_architect(db, spec, task, spec_dir)


def _gates(db, spec):
    return [g for g in db.list_gates_for_spec(spec.id)
            if g.gate_type == GateType.DESIGN_APPROVAL]


class TestStrandedDesignBounces:
    def test_no_design_approval_gate_is_opened(self, db, arch):
        spec, task, spec_dir = arch
        _run_architect(db, spec, task, spec_dir, STRANDED)
        assert _gates(db, spec) == []

    def test_feedback_is_written_for_the_architect(self, db, arch):
        spec, task, spec_dir = arch
        _run_architect(db, spec, task, spec_dir, STRANDED)
        fb = spec_dir / "design_review_feedback.md"
        assert fb.is_file()
        assert "Mushroom" in fb.read_text()

    def test_the_architect_retry_is_spent(self, db, arch):
        spec, task, spec_dir = arch
        _run_architect(db, spec, task, spec_dir, STRANDED)
        assert db.get_task(task.id).retry_count == 1


class TestSoundDesignProceeds:
    def test_the_gate_opens(self, db, arch):
        spec, task, spec_dir = arch
        _run_architect(db, spec, task, spec_dir, SOUND)
        assert len(_gates(db, spec)) == 1

    def test_no_feedback_is_left_behind(self, db, arch):
        """A stale feedback file would bleed into a later architect run."""
        spec, task, spec_dir = arch
        _run_architect(db, spec, task, spec_dir, SOUND)
        assert not (spec_dir / "design_review_feedback.md").is_file()


class TestBounded:
    def test_the_check_stops_after_its_budget(self, db, arch, monkeypatch):
        """Past the round cap a stranded design proceeds rather than looping —
        the human gate is still downstream."""
        spec, task, spec_dir = arch
        monkeypatch.setattr(d.executor, "TESTABILITY_CHECK_MAX_ROUNDS", 0)
        _run_architect(db, spec, task, spec_dir, STRANDED)
        assert len(_gates(db, spec)) == 1

    def test_it_can_be_disabled(self, db, arch, monkeypatch):
        spec, task, spec_dir = arch
        monkeypatch.setattr(d.executor, "TESTABILITY_CHECK_ENABLED", False)
        _run_architect(db, spec, task, spec_dir, STRANDED)
        assert len(_gates(db, spec)) == 1

    def test_a_checker_crash_never_kills_the_spec(self, db, arch, monkeypatch):
        """Fail-open. This is regex parsing of LLM prose sitting in the
        architect path; a crash must cost the check, not the spec."""
        spec, task, spec_dir = arch
        monkeypatch.setattr(d.design_testability, "check_design_testability",
                            mock.Mock(side_effect=RuntimeError("boom")))
        _run_architect(db, spec, task, spec_dir, STRANDED)
        assert len(_gates(db, spec)) == 1
        assert db.get_spec(spec.id).status == SpecStatus.EXECUTING
