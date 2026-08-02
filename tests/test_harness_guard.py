"""DEV-404 — a broken test harness must not burn implementer retries.

spec_96d7e07f's reviewer tests did `import { assert } from 'node:test'` (no
such export), so all 19 tests died on TypeError before exercising a line of
implementation — and the retry loop consumed logic attempts on it. The
daemon now detects the zero-pass/harness-error signature and issues a free,
pointed harness-fix retry instead, capped so it can't loop forever.
"""
import json

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import SpecStatus, TaskStatus

BROKEN_HARNESS_TAP = """\
not ok 1 - deterministic_seeded_rng
  error: 'assert.deepStrictEqual is not a function'
not ok 2 - mushroom_damage
  error: 'assert.ok is not a function'
not ok 3 - suite wrapper
  error: '2 subtests failed'
# tests 19
# suites 2
# pass 0
# fail 19
"""

GENUINE_FAILURES_TAP = """\
not ok 1 - head_hit_scoring
  error: 'Expected 900 to equal 600'
not ok 2 - reset_restores
  error: 'AssertionError: entities not cleared'
# tests 17
# pass 0
# fail 17
"""

MIXED_RESULTS_TAP = """\
not ok 1 - one_broken
  error: 'assert.ok is not a function'
# tests 17
# pass 15
# fail 2
"""


def test_detects_the_dogfood_signature():
    reason = d._detect_harness_defect(BROKEN_HARNESS_TAP, "node_test")
    assert reason is not None
    assert "assert.ok is not a function" in reason


def test_genuine_assertion_failures_are_not_harness_defects():
    assert d._detect_harness_defect(GENUINE_FAILURES_TAP, "node_test") is None


def test_any_passing_test_disqualifies():
    """If anything passed, the harness demonstrably works — the failures
    are logic failures and must cost budget as usual."""
    assert d._detect_harness_defect(MIXED_RESULTS_TAP, "node_test") is None


def test_pytest_collection_error_is_a_harness_defect():
    out = "ImportError while importing test module 'tests/test_x.py'\n1 error"
    assert d._detect_harness_defect(out, "pytest") is not None


def test_pytest_with_passes_is_not():
    out = "ModuleNotFoundError mentioned in a docstring\n3 passed in 0.1s"
    assert d._detect_harness_defect(out, "pytest") is None


def test_unparseable_output_is_not_a_harness_defect():
    assert d._detect_harness_defect("garbage", "node_test") is None


# ── the free-retry mechanics ─────────────────────────────────────────────

@pytest.fixture
def env(tmp_path):
    db = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    spec = db.create_spec(title="demo", source_md_path="spec.md",
                          status=SpecStatus.EXECUTING)
    impl = db.create_task(spec_id=spec.id, agent="implementer",
                          role="implementer", title="impl")
    rev = db.create_task(spec_id=spec.id, agent="reviewer",
                         role="reviewer", title="review")
    db.update_task_status(impl.id, TaskStatus.DONE)
    db.update_task_status(rev.id, TaskStatus.RUNNING)
    spec_dir = db.workspace_root / spec.id
    spec_dir.mkdir(parents=True, exist_ok=True)
    yield db, db.get_spec(spec.id), db.get_task(impl.id), \
        db.get_task(rev.id), spec_dir
    db.close_all()


def test_harness_retry_costs_no_budget(env):
    db, spec, impl, rev, spec_dir = env
    ok = d._harness_retry(db, spec, rev, spec_dir, "broken assert import",
                          BROKEN_HARNESS_TAP, "node_test")
    assert ok is True
    fresh = db.get_task(impl.id)
    assert fresh.retry_count == 0, "a harness retry must not burn the budget"
    assert fresh.status is TaskStatus.PENDING, "implementer re-runs"
    assert db.get_task(rev.id).status is TaskStatus.PENDING, \
        "downstream reviewer resets too"
    report = (spec_dir / "failure_report.md").read_text()
    assert "TEST HARNESS DEFECT" in report
    assert "node:assert/strict" in report, "the fix instruction must name it"


def test_free_retries_are_capped(env):
    db, spec, impl, rev, spec_dir = env
    for i in range(d._HARNESS_FREE_RETRIES):
        assert d._harness_retry(db, spec, rev, spec_dir, "r",
                                BROKEN_HARNESS_TAP, "node_test") is True
    assert d._harness_retry(db, spec, rev, spec_dir, "r",
                            BROKEN_HARNESS_TAP, "node_test") is False, \
        "past the cap the normal budgeted path must take over"
    used = json.loads((spec_dir / "harness_retries.json").read_text())["used"]
    assert used == d._HARNESS_FREE_RETRIES
