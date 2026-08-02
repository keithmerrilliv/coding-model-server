"""DEV-406 — a near-passing synthesized artifact gets ONE repair round.

spec_96d7e07f's synthesis scored 15/17 and the spec hard-failed a 2h47m run.
_run_synthesis now measures the pass rate on a failing synthesis run and,
above the threshold, makes one targeted repair call scoped to the failing
tests before giving up. Strictly bounded: one extra call, threshold-gated,
and a converted pass still flows through the caller's release_approval gate
(DEV-121 semantics untouched).
"""
from types import SimpleNamespace
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import SpecStatus

NEAR_MISS_TAP = "# tests 17\n# pass 15\n# fail 2\n"
DISASTER_TAP = "# tests 17\n# pass 3\n# fail 14\n"
ALL_PASS = "# tests 17\n# pass 17\n# fail 0\n"


# ── the rate parser ──────────────────────────────────────────────────────

def test_pass_rate_from_tap():
    assert d._test_pass_rate(NEAR_MISS_TAP) == pytest.approx(15 / 17)


def test_pass_rate_from_pytest():
    assert d._test_pass_rate("15 passed, 2 failed in 3.2s") == pytest.approx(15 / 17)


def test_pass_rate_unparseable_is_none():
    """None, not 0.0 — an unreadable summary must not qualify for repair."""
    assert d._test_pass_rate("no summary here") is None


# ── the repair round ─────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path):
    db = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    spec_dir = db.spec_dir(spec.id)
    (spec_dir / "spec.md").write_text("# spec")
    (spec_dir / "design.md").write_text("# design")
    (spec_dir / "impl.py").write_text("v1")
    db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    impl_task = db.create_task(spec_id=spec.id, agent="implementer",
                               role="implementer", title="build")
    yield db, db.get_spec(spec.id), impl_task, spec_dir
    db.close_all()


def _run_synthesis(db, spec, impl_task, spec_dir, *, guard_results,
                   repair_result=None):
    """Drive _run_synthesis with the agent + test guard mocked.

    guard_results: successive (passed, output) tuples from the test guard.
    repair_result: parsed repair response; defaults to a one-file overlay.
    """
    synth = SimpleNamespace(files=[("impl.py", "v2"), ("t.test.js", "t")])
    repair = repair_result or SimpleNamespace(files=[("impl.py", "v3")])
    calls = {"agent": 0}

    def fake_call_agent(*a, **k):
        calls["agent"] += 1
        return "raw"

    with mock.patch.object(d, "call_agent", side_effect=fake_call_agent), \
            mock.patch.object(d, "build_synthesis_message", return_value=[]), \
            mock.patch.object(d.executor, "build_synthesis_repair_message",
                              return_value=[]), \
            mock.patch.object(d.executor, "implementer_max_tokens_for",
                              return_value=1000), \
            mock.patch.object(d, "parse_implementer_response",
                              side_effect=[synth, repair]), \
            mock.patch.object(d, "_run_tests_with_guard",
                              side_effect=guard_results):
        passed, output = d._run_synthesis(
            db, spec, impl_task, spec_dir, "node_test", {})
    return passed, output, calls["agent"]


def test_near_miss_gets_one_repair_and_can_convert(env):
    db, spec, impl_task, spec_dir = env
    passed, output, agent_calls = _run_synthesis(
        db, spec, impl_task, spec_dir,
        guard_results=[(False, NEAR_MISS_TAP), (True, ALL_PASS)])
    assert passed is True, "a converted repair is a synthesis pass"
    assert agent_calls == 2, "exactly one extra agent call"
    assert (spec_dir / "impl.py").read_text() == "v3", "repair overlaid"
    assert (spec_dir / "t.test.js").read_text() == "t", (
        "files the repair didn't emit stay from synthesis")


def test_disaster_rate_gets_no_repair(env):
    db, spec, impl_task, spec_dir = env
    passed, output, agent_calls = _run_synthesis(
        db, spec, impl_task, spec_dir,
        guard_results=[(False, DISASTER_TAP)])
    assert passed is False
    assert agent_calls == 1, "3/17 is not a near-miss — no repair call"


def test_unparseable_summary_gets_no_repair(env):
    db, spec, impl_task, spec_dir = env
    passed, _output, agent_calls = _run_synthesis(
        db, spec, impl_task, spec_dir,
        guard_results=[(False, "guard forced FAIL, no summary")])
    assert passed is False
    assert agent_calls == 1


def test_failed_repair_still_fails(env):
    db, spec, impl_task, spec_dir = env
    passed, output, agent_calls = _run_synthesis(
        db, spec, impl_task, spec_dir,
        guard_results=[(False, NEAR_MISS_TAP), (False, NEAR_MISS_TAP)])
    assert passed is False, "one round only — a failed repair fails the spec"
    assert agent_calls == 2


def test_synthesis_pass_skips_repair_entirely(env):
    db, spec, impl_task, spec_dir = env
    passed, _output, agent_calls = _run_synthesis(
        db, spec, impl_task, spec_dir,
        guard_results=[(True, ALL_PASS)])
    assert passed is True
    assert agent_calls == 1
