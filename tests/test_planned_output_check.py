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


# ── DEV-677: what a targeted retry was told to leave alone ───────────────────

@pytest.fixture
def retry_task(db, spec):
    """An implementer task on its first retry, with attempt 0's workspace
    snapshotted the way _clean_spec_dir_for_retry leaves it."""
    task = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="implement")
    db.increment_task_retry(task.id)
    spec_dir = db.spec_dir(spec.id)
    _write(spec_dir, "retry_history/retry_0/src/pkg/mod.py", "old = 1\n")
    _write(spec_dir, "retry_history/retry_0/tests/test_mod.py", "def test(): pass\n")
    return db.get_task(task.id)


NOTES_CITING_MOD = ("## The code does not compile\n\n"
                    "    src/pkg/mod.py:3: error: name 'x' is not defined\n")


def test_an_uncited_planned_file_comes_forward_from_the_previous_attempt(
        db, spec, retry_task, caplog):
    """Run 32 retry 2: the feedback cited outcome.py only, the retry edited
    outcome.py only, and the test file it was told to leave alone was
    charged as missing."""
    spec_dir = db.spec_dir(spec.id)
    _write(spec_dir, "src/pkg/mod.py", "new = 1\n")
    with caplog.at_level("INFO"):
        carried = d._carry_forward_uncited_outputs(
            db, spec, retry_task, spec_dir, NOTES_CITING_MOD)
    assert carried == [("tests/test_mod.py", "def test(): pass\n")]
    assert (spec_dir / "tests/test_mod.py").read_text() == "def test(): pass\n"
    assert d._missing_planned_outputs(spec, spec_dir) == []
    assert "carried forward" in caplog.text and "DEV-677" in caplog.text
    ev = [json.loads(e.payload_json) for e in db.list_events_by_kind(
        spec_id=spec.id, kind=d.EventKind.AGENT_RAN)]
    anomaly = [e for e in ev if e.get("anomaly") == "outputs_carried_forward"]
    assert anomaly and anomaly[0]["carried"] == ["tests/test_mod.py"]
    assert anomaly[0]["cited"] == ["src/pkg/mod.py"]
    # The restore is on the ledger, attributed to this retry.
    from coding_model_autonomous.workspace import ACTION_RESTORED, ArtifactLedger
    rows = [e for e in ArtifactLedger.open(db, spec, spec_dir).entries
            if e.path == "tests/test_mod.py"]
    assert rows and rows[-1].action == ACTION_RESTORED and rows[-1].retry == 1


def test_a_cited_file_that_was_not_re_emitted_is_still_missing(db, spec, retry_task):
    """Asked for, not produced: DEV-645's verdict stands."""
    spec_dir = db.spec_dir(spec.id)
    _write(spec_dir, "tests/test_mod.py")
    carried = d._carry_forward_uncited_outputs(
        db, spec, retry_task, spec_dir, NOTES_CITING_MOD)
    assert carried == []
    assert d._missing_planned_outputs(spec, spec_dir) == ["src/pkg/mod.py"]


def test_a_file_no_attempt_ever_produced_is_still_missing(db, spec, retry_task):
    spec_dir = db.spec_dir(spec.id)
    (spec_dir / "retry_history/retry_0/tests/test_mod.py").unlink()
    _write(spec_dir, "src/pkg/mod.py")
    assert d._carry_forward_uncited_outputs(
        db, spec, retry_task, spec_dir, NOTES_CITING_MOD) == []
    assert d._missing_planned_outputs(spec, spec_dir) == ["tests/test_mod.py"]


def test_nothing_cited_carries_every_omitted_planned_file(db, spec, retry_task):
    """A human rejection that names no file ("tighten the error handling")
    tells the model to fix what it names and leave the rest; the rest comes
    forward."""
    spec_dir = db.spec_dir(spec.id)
    carried = d._carry_forward_uncited_outputs(
        db, spec, retry_task, spec_dir, "Please tighten the error handling.")
    assert sorted(p for p, _ in carried) == ["src/pkg/mod.py", "tests/test_mod.py"]
    assert d._missing_planned_outputs(spec, spec_dir) == []


def test_attempt_zero_has_nothing_to_carry(db, spec):
    spec_dir = db.spec_dir(spec.id)
    task = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="implement")
    _write(spec_dir, "retry_history/retry_0/tests/test_mod.py")
    assert d._carry_forward_uncited_outputs(db, spec, task, spec_dir, None) == []
    assert d._missing_planned_outputs(spec, spec_dir) == [
        "src/pkg/mod.py", "tests/test_mod.py"]


def test_manifest_mode_keeps_its_own_restore(db, spec, retry_task):
    spec_dir = db.spec_dir(spec.id)
    (spec_dir / "manifest.json").write_text(json.dumps(
        [{"path": "src/pkg/mod.py", "purpose": "x"}]))
    assert d._carry_forward_uncited_outputs(
        db, spec, retry_task, spec_dir, NOTES_CITING_MOD) == []
    assert not (spec_dir / "tests/test_mod.py").exists()


def test_the_feedback_no_longer_claims_the_workspace_is_reset(db, spec, retry_task, monkeypatch):
    """The two texts the model reads must agree (DEV-677): the retry prompt
    says leave uncited files untouched, so the missing-output feedback must
    not say every file has to come back."""
    captured = {}
    monkeypatch.setattr(d, "_dispose", lambda db_, s, t, f, **k: captured.setdefault("f", f))

    def no_fetch(*a, **k):
        raise RuntimeError("no fetch")
    monkeypatch.setattr(d, "_spec_context", no_fetch)
    d._route_missing_planned_outputs(db, spec, retry_task, "# spec", ["tests/test_mod.py"])
    fb = captured["f"].feedback
    assert "workspace is reset" not in fb
    assert "carried forward" in fb and "tests/test_mod.py" in fb
