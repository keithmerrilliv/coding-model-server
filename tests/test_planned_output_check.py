"""DEV-645: single-call mode verifies the plan's implement outputs.

Manifest mode has checked its own declared file set since DEV-106. Single-call
mode checked nothing, so an attempt that dropped a planned file was scored
downstream as whatever the missing file happened to break: run 25's attempt 0
emitted the test and no edit for the module, and the new tests failed on
behaviour against the unmodified module; run 26's attempts 2-5 each produced
one of two files, and attempt 3's module did not even parse yet reached a
human gate labelled "implementer done" — with no test file, pytest collected
nothing and the build check reported "inconclusive".
"""
import json

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database

PLAN = {
    "phases": [
        {"name": "design", "role": "architect", "outputs": ["design.md"]},
        {"name": "implement", "role": "implementer",
         "outputs": ["src/pkg/mod.py", "tests/test_mod.py"]},
        {"name": "test", "role": "reviewer", "outputs": ["test_report.md"]},
    ]
}


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite",
                        workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def spec(db):
    import yaml
    s = db.create_spec(title="demo", source_md_path="spec.md")
    db.update_spec_status(s.id, d.SpecStatus.EXECUTING,
                          normalized_yaml=yaml.safe_dump(PLAN))
    spec_dir = db.spec_dir(s.id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    return db.get_spec(s.id)


def _write(spec_dir, rel, text="x = 1\n"):
    p = spec_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


# ── what counts as missing ───────────────────────────────────────────────────

def test_only_the_implement_phase_outputs_are_required(db, spec):
    """design.md and test_report.md belong to other phases and are not the
    implementer's to produce."""
    spec_dir = db.spec_dir(spec.id)
    _write(spec_dir, "src/pkg/mod.py")
    _write(spec_dir, "tests/test_mod.py")
    assert d._missing_planned_outputs(spec, spec_dir) == []


def test_a_dropped_output_is_named(db, spec):
    """Run 25's shape: the test landed, the module never did."""
    spec_dir = db.spec_dir(spec.id)
    _write(spec_dir, "tests/test_mod.py")
    assert d._missing_planned_outputs(spec, spec_dir) == ["src/pkg/mod.py"]


def test_the_other_half_of_run_26_is_caught_too(db, spec):
    """Attempts 2-5 alternated which file they dropped."""
    spec_dir = db.spec_dir(spec.id)
    _write(spec_dir, "src/pkg/mod.py")
    assert d._missing_planned_outputs(spec, spec_dir) == ["tests/test_mod.py"]


def test_both_missing_are_both_named(db, spec):
    spec_dir = db.spec_dir(spec.id)
    assert d._missing_planned_outputs(spec, spec_dir) == [
        "src/pkg/mod.py", "tests/test_mod.py"]


def test_existence_on_disk_is_the_test_not_the_response(db, spec):
    """A path the ledger refused, or that landed renamed after a collision, is
    missing from the workspace whatever the model claimed to produce — and the
    workspace is what the build check and the reviewer see."""
    spec_dir = db.spec_dir(spec.id)
    _write(spec_dir, "src/pkg/mod.py")
    _write(spec_dir, "tests/test_implementer_mod.py")   # the renamed landing
    assert d._missing_planned_outputs(spec, spec_dir) == ["tests/test_mod.py"]


# ── manifest mode keeps its own contract ─────────────────────────────────────

def test_manifest_mode_is_left_to_its_own_verification(db, spec):
    """DEV-106's `_verify_manifest_workspace` checks the ARCHITECT's file list
    and can restore a dropped file from a snapshot. It detects itself by
    manifest.json; this check stands aside on the same signal, so a manifest
    run behaves exactly as it did before DEV-645."""
    spec_dir = db.spec_dir(spec.id)
    (spec_dir / "manifest.json").write_text(json.dumps(
        [{"path": "src/pkg/other.py", "purpose": "x"}]))
    assert d._missing_planned_outputs(spec, spec_dir) == []


# ── a plan with nothing to check ─────────────────────────────────────────────

def test_a_plan_with_no_implement_outputs_requires_nothing(db):
    """Greenfield plans that do not enumerate outputs are unaffected — the
    check can only ever be as strong as the plan the operator approved."""
    import yaml
    s = db.create_spec(title="bare", source_md_path="spec.md")
    db.update_spec_status(s.id, d.SpecStatus.EXECUTING, normalized_yaml=yaml.safe_dump(
        {"phases": [{"name": "implement", "role": "implementer"}]}))
    spec_dir = db.spec_dir(s.id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    assert d._missing_planned_outputs(db.get_spec(s.id), spec_dir) == []
