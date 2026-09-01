"""A plan that cannot dispatch must never reach a human gate — DEV-426.

Three consecutive runs of the same spec produced a plan whose test_strategy
values lived only as prose inside `notes`, which nothing parses. Each cost a
review round. The most damaging case is silent: `protected_paths` dropped means
the scaffold protection (DEV-427) is disabled with no error at all.
"""
from unittest import mock

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import GateType, SpecStatus

SPEC_MD = """\
# Centipede slice 1

## test_strategy

    framework: swift_test
    required: true
    repo: centipede
    base_ref: main
    protected_paths:
      - Sources/CentipedeCore/GameState.swift
      - Package.swift
    notes: |
      Runs on the Mac runner.

## Risks
- something
"""

VALID = """\
test_strategy:
  framework: swift_test
  required: true
  repo: centipede
  base_ref: main
  protected_paths:
    - Sources/CentipedeCore/GameState.swift
    - Package.swift
"""

# What the planner actually emitted on run 3.
PROSE_ONLY = """\
test_strategy:
  framework: swift_test
  required: true
  notes: |
    Runs `swift test` against registered repo `centipede`.
    Protected scaffold files must remain untouched.
"""


# ── parsing the spec's own block ─────────────────────────────────────────────

def test_spec_block_is_parsed():
    got = d._spec_declared_test_strategy(SPEC_MD)
    assert got["repo"] == "centipede"
    assert got["base_ref"] == "main"
    assert "Package.swift" in got["protected_paths"]


def test_missing_or_unparseable_spec_block_is_not_fatal():
    assert d._spec_declared_test_strategy("# no strategy here") == {}
    assert d._spec_declared_test_strategy("") == {}
    assert d._spec_declared_test_strategy("## test_strategy\n\n    : : bad yaml\n") == {}


# ── the two rules ────────────────────────────────────────────────────────────

def test_framework_required_key_missing_is_a_problem():
    problems = d._validate_test_strategy(
        "test_strategy:\n  framework: swift_test\n", "")
    assert any("`repo` is required" in p for p in problems)


def test_xcodebuild_needs_repo_scheme_and_filter():
    problems = d._validate_test_strategy(
        "test_strategy:\n  framework: xcodebuild_test\n  repo: es\n", "")
    joined = " ".join(problems)
    assert "`scheme`" in joined and "`filter`" in joined
    assert "`repo`" not in joined  # supplied


def test_spec_declared_key_dropped_is_a_problem():
    """base_ref and protected_paths are not framework-required — and dropping
    them fails silently, which is worse."""
    plan = "test_strategy:\n  framework: swift_test\n  repo: centipede\n"
    problems = d._validate_test_strategy(plan, SPEC_MD)
    joined = " ".join(problems)
    assert "`base_ref`" in joined
    assert "`protected_paths`" in joined


def test_a_key_failing_both_rules_is_reported_once():
    problems = d._validate_test_strategy(PROSE_ONLY, SPEC_MD)
    assert sum(1 for p in problems if "`repo`" in p) == 1


def test_run_3s_actual_plan_is_rejected():
    problems = d._validate_test_strategy(PROSE_ONLY, SPEC_MD)
    joined = " ".join(problems)
    for key in ("`repo`", "`base_ref`", "`protected_paths`"):
        assert key in joined, key


def test_valid_plan_passes():
    assert d._validate_test_strategy(VALID, SPEC_MD) == []


def test_notes_and_required_are_never_demanded_as_keys():
    plan = ("test_strategy:\n  framework: swift_test\n  repo: centipede\n"
            "  base_ref: main\n  protected_paths: [Package.swift]\n")
    problems = d._validate_test_strategy(plan, SPEC_MD)
    assert problems == []


def test_non_apple_framework_is_left_alone():
    assert d._validate_test_strategy(
        "test_strategy:\n  framework: pytest\n  required: true\n", "") == []


def test_malformed_plan_yaml_is_not_our_job():
    """_bootstrap_tasks must fail loudly on that; we stay out of the way."""
    assert d._validate_test_strategy("test_strategy: [: :", SPEC_MD) == []
    assert d._validate_test_strategy("just a string", SPEC_MD) == []


# ── the transition ───────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


def _accept(db, yaml_text, spec_md=SPEC_MD):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    spec_dir = db.spec_dir(spec.id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text(spec_md)
    result = mock.Mock(yaml_text=yaml_text)
    d._accept_plan(db, db.get_spec(spec.id), spec_dir, result)
    return db.get_spec(spec.id)


# DEV-625 widened _OPERATOR_STRATEGY_KEYS to include repo, so every key
# SPEC_MD declares now self-heals via the DEV-573 overlay instead of costing
# a planner round. The bounce path still exists for spec-declared keys the
# overlay does not carry — this variant declares one.
SPEC_MD_WITH_CUSTOM_KEY = SPEC_MD.replace(
    "    base_ref: main\n",
    "    base_ref: main\n    xcode_scheme: CentipedeCI\n")


def test_operator_keys_self_heal_instead_of_bouncing(db):
    """Post-DEV-625: a plan that lost only operator-restorable keys is healed
    by the overlay and opens the gate — no planner round burned."""
    spec = _accept(db, PROSE_ONLY)
    gates = [g for g in db.list_gates_for_spec(spec.id)
             if g.gate_type is GateType.PLAN_APPROVAL]
    assert len(gates) == 1
    assert spec.status is SpecStatus.PLAN_REVIEW


def test_invalid_plan_never_opens_a_plan_approval_gate(db):
    spec = _accept(db, PROSE_ONLY, spec_md=SPEC_MD_WITH_CUSTOM_KEY)
    assert not [g for g in db.list_gates_for_spec(spec.id)
                if g.gate_type is GateType.PLAN_APPROVAL]
    # Bounced back to the planner via the same channel a human rejection uses.
    clar = [g for g in db.list_gates_for_spec(spec.id, GateType.CLARIFICATION)]
    assert len(clar) == 1
    assert "Plan rejection feedback" in clar[0].reviewer_notes
    assert "`xcode_scheme`" in clar[0].reviewer_notes
    assert spec.status is SpecStatus.PENDING_PLAN


def test_valid_plan_opens_the_gate_as_before(db):
    spec = _accept(db, VALID)
    gates = [g for g in db.list_gates_for_spec(spec.id)
             if g.gate_type is GateType.PLAN_APPROVAL]
    assert len(gates) == 1
    assert spec.status is SpecStatus.PLAN_REVIEW


def test_validation_rounds_are_capped(db):
    """A planner that cannot produce a valid block fails the spec, not loops."""
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    spec_dir = db.spec_dir(spec.id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text(SPEC_MD_WITH_CUSTOM_KEY)
    result = mock.Mock(yaml_text=PROSE_ONLY)

    for _ in range(d.PLAN_VALIDATION_MAX_ROUNDS):
        d._accept_plan(db, db.get_spec(spec.id), spec_dir, result)
        assert db.get_spec(spec.id).status is SpecStatus.PENDING_PLAN

    d._accept_plan(db, db.get_spec(spec.id), spec_dir, result)
    assert db.get_spec(spec.id).status is SpecStatus.FAILED


def test_a_missing_spec_file_does_not_break_acceptance(db):
    """Spec md unreadable → fall back to framework rules only, never crash."""
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    spec_dir = db.spec_dir(spec.id)
    spec_dir.mkdir(parents=True, exist_ok=True)
    d._accept_plan(db, db.get_spec(spec.id), spec_dir, mock.Mock(yaml_text=VALID))
    assert db.get_spec(spec.id).status is SpecStatus.PLAN_REVIEW
