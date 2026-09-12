"""DEV-649: synthesis is not dispatched when it provably cannot answer.

Synthesis and its repair emit whole `<<<FILE:>>>` blocks — they never got the
implementer's edit mode (DEV-581) — so an existing file costs its full size in
OUTPUT tokens. Run 28 asked deep_reviewer to merge a 145,825-char executor.py
inside a 32,000-token budget. Re-emitting it needs ~48,600, so every possible
response was a stub; DEV-636's shrink guard refused the 484-line answer and
then the 156-line repair, 77 minutes and a 213K-token prompt after dispatch.

Manifest mode settles the same arithmetic up front with
MANIFEST_WHOLE_FILE_MAX_CHARS (DEV-604). This is its counterpart for the
exhaustion escape hatch.
"""
import yaml
import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous import context as ctx
from coding_model_autonomous import executor as ex
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import SpecStatus
from coding_model_autonomous.outcome import FailureClass

PLAN = {
    "test_strategy": {"framework": "pytest", "repo": "demo"},
    "phases": [
        {"name": "implement", "role": "implementer",
         "outputs": ["src/pkg/big.py", "tests/test_big.py"]},
    ],
}

# The run-28 shape: one existing planned output far past what the budget can
# re-emit, and one new file.
RUN28_CHARS = 145_825


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite",
                        workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def spec(db):
    s = db.create_spec(title="demo", source_md_path="spec.md")
    db.update_spec_status(s.id, SpecStatus.EXECUTING,
                          normalized_yaml=yaml.safe_dump(PLAN))
    spec_dir = db.spec_dir(s.id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text("# Spec\n")
    (spec_dir / "design.md").write_text("# Design\n\n## File Structure\n\n- x\n")
    return db.get_spec(s.id)


def _pin_context(monkeypatch, existing):
    """Pin the context stage so no runner is involved."""
    monkeypatch.setattr(
        d, "_spec_context",
        lambda *a, **k: ctx.SpecContext.from_files("spec_t", editable=existing))


# ── the estimator ────────────────────────────────────────────────────────────

def test_emission_cost_is_the_files_own_size():
    """It is the whole file that has to come back out, not a diff."""
    assert ex.whole_file_emission_tokens(
        [("a.py", "x" * 30_000)]) == 30_000 // ctx.CHARS_PER_TOKEN


def test_emission_cost_sums_every_file():
    got = ex.whole_file_emission_tokens([("a.py", "x" * 300), ("b.py", "y" * 600)])
    assert got == 900 // ctx.CHARS_PER_TOKEN


def test_run_28_needed_more_than_its_whole_budget():
    """The arithmetic the run spent 77 minutes on."""
    needed = ex.whole_file_emission_tokens([("executor.py", "x" * RUN28_CHARS)])
    assert needed > 32_000                      # the budget it actually had
    assert needed > int(32_000 * ex.SYNTHESIS_EMIT_HEADROOM)


# ── the guard ────────────────────────────────────────────────────────────────

def test_an_unanswerable_merge_is_refused_before_dispatch(db, spec, monkeypatch):
    _pin_context(monkeypatch, [("src/pkg/big.py", "x" * RUN28_CHARS)])
    failure = d._synthesis_cannot_emit(db, spec, db.spec_dir(spec.id))
    assert failure is not None
    assert failure.cls is FailureClass.SYNTHESIS_FAILED
    # The operator has to see the arithmetic, not just a refusal.
    assert "src/pkg/big.py" in failure.detail
    assert str(RUN28_CHARS) in failure.detail
    assert "output tokens" in failure.detail


def test_a_merge_that_fits_is_dispatched(db, spec, monkeypatch):
    """Run 26's shape — a 24K file — is well inside the budget."""
    _pin_context(monkeypatch, [("src/pkg/big.py", "x" * 24_222)])
    assert d._synthesis_cannot_emit(db, spec, db.spec_dir(spec.id)) is None


def test_only_planned_outputs_are_counted(db, spec, monkeypatch):
    """A huge file in the context that the plan does NOT ask the merge to
    produce is context, not output — it costs nothing on the way out."""
    _pin_context(monkeypatch, [("src/pkg/unrelated.py", "x" * RUN28_CHARS)])
    assert d._synthesis_cannot_emit(db, spec, db.spec_dir(spec.id)) is None


def test_a_greenfield_merge_is_never_refused(db, spec, monkeypatch):
    """Nothing exists yet, so there is nothing to re-emit."""
    _pin_context(monkeypatch, [])
    assert d._synthesis_cannot_emit(db, spec, db.spec_dir(spec.id)) is None


def test_the_check_can_be_disabled(db, spec, monkeypatch):
    _pin_context(monkeypatch, [("src/pkg/big.py", "x" * RUN28_CHARS)])
    monkeypatch.setattr(ex, "SYNTHESIS_EMIT_HEADROOM", 0.0)
    assert d._synthesis_cannot_emit(db, spec, db.spec_dir(spec.id)) is None


def test_a_context_failure_does_not_block_the_merge(db, spec, monkeypatch):
    """The guard is an optimisation, not a gate: if it cannot size the
    emission it must let the escape hatch run."""
    def _boom(*a, **k):
        raise RuntimeError("context unavailable")
    monkeypatch.setattr(d, "_spec_context", _boom)
    assert d._synthesis_cannot_emit(db, spec, db.spec_dir(spec.id)) is None
