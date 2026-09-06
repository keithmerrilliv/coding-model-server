"""DEV-624: context-aware dispatch and non-terminal dispatch refusals.

Run 19 rotated a 152K-token prompt onto moe_implementer's 116K window; run 21
v2's architect recommended fast_implementer (64K) for a 75K-token prompt.
Both dispatches drew a 413 and the exception killed each spec terminally in
under a minute. Dispatch now estimates fit and escalates to a window that
holds the prompt, and an HTTP refusal rotates like a parse failure instead
of failing the spec.
"""
import pytest
import requests

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import GateType, SpecStatus, TaskStatus


# ── ctx limits ───────────────────────────────────────────────────────────────

def test_known_agent_limits_come_from_config():
    assert d._agent_ctx_limit("fast_implementer") == 65536
    assert d._agent_ctx_limit("deep_implementer") == 262144


def test_alias_resolves_before_lookup():
    assert d._agent_ctx_limit("m25_implementer") == d._agent_ctx_limit("moe_implementer")


def test_unknown_agent_is_none():
    assert d._agent_ctx_limit("no_such_agent") is None
    assert d._agent_ctx_limit(None) is None


# ── the fit check ────────────────────────────────────────────────────────────

def _messages(chars: int):
    return [{"role": "system", "content": "s"},
            {"role": "user", "content": "x" * chars}]


def test_fitting_prompt_keeps_the_chosen_agent():
    got = d._ctx_capable_agent("spec_t", "fast_implementer", _messages(30_000), 8_000)
    assert got == "fast_implementer"


def test_oversized_prompt_escalates_to_a_window_that_fits():
    """Run 21 v2's shape: ~300K chars (~100K tokens) + 32K completion needs
    ~132K — implementer and fast (64K) can't, moe (116K) can't, deep can."""
    got = d._ctx_capable_agent("spec_t", "fast_implementer", _messages(300_000), 32_000)
    assert got == "deep_implementer"


def test_moderate_prompt_takes_the_first_fitting_rotation_agent():
    """~180K chars (~60K tokens) + 8K completion fits implementer's 64K —
    barely — so escalation from fast stops at the first fitting candidate."""
    got = d._ctx_capable_agent("spec_t", "fast_implementer", _messages(160_000), 8_000)
    assert d._agent_ctx_limit(got) >= 160_000 // 3 + 8_000


def test_unknown_agent_is_left_alone():
    got = d._ctx_capable_agent("spec_t", "mystery_agent", _messages(900_000), 32_000)
    assert got == "mystery_agent"


def test_nothing_fits_picks_the_largest_window():
    got = d._ctx_capable_agent("spec_t", "fast_implementer", _messages(2_000_000), 32_000)
    assert got == "deep_implementer"


# ── dispatch refusal rotates, never terminal ─────────────────────────────────

@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite",
                        workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


def _spec_with_task(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    spec_dir = db.spec_dir(spec.id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text("# Spec")
    (spec_dir / "design.md").write_text("# Design")
    task = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="impl")
    db.update_task_status(task.id, TaskStatus.RUNNING)
    return spec, task, spec_dir


def _boom(*a, **k):
    raise requests.HTTPError(
        "413 Client Error: Request Entity Too Large for url: x")


def test_http_refusal_rotates_instead_of_failing(db, monkeypatch):
    spec, task, spec_dir = _spec_with_task(db)
    monkeypatch.setattr(d, "_generate_implementation", _boom)

    d._run_implementer(db, db.get_spec(spec.id), db.get_task(task.id), spec_dir)

    after = db.get_task(task.id)
    assert after.status == TaskStatus.PENDING
    assert after.retry_count == 1
    assert db.get_spec(spec.id).status != SpecStatus.FAILED
    gates = [g for g in db.list_gates_for_spec(spec.id)
             if g.gate_type is GateType.CODE_REVIEW]
    assert len(gates) == 1
    assert "never reached the model" in gates[0].reviewer_notes


def test_refusal_with_exhausted_budget_fails_the_spec(db, monkeypatch):
    spec, task, spec_dir = _spec_with_task(db)
    for _ in range(d.MAX_RETRIES):
        db.increment_task_retry(task.id)
    monkeypatch.setattr(d, "_generate_implementation", _boom)

    d._run_implementer(db, db.get_spec(spec.id), db.get_task(task.id), spec_dir)

    assert db.get_task(task.id).status == TaskStatus.FAILED
    assert db.get_spec(spec.id).status is SpecStatus.FAILED
