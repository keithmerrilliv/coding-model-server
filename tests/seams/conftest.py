"""Seam-tier fixtures (DEV-634).

Every knob the daemon reads from the environment at import is pinned here,
so a seam test means the same thing on the main tree (where the daemon's
import loads ``.env``) as in CI (where it does not). The pins are the
shipped defaults, not this box's overrides.
"""
from __future__ import annotations

import os

import pytest

# The daemon loads .env into os.environ at import (orchestrator_daemon.py:54).
# This directory collects before tests/test_*.py, so without care that import
# would be the suite's first and this box's .env — ADMIN_API_KEY included —
# would be what Config captures. Import Config first (the order the rest of
# the suite already relies on) and keep the daemon's .env out of the process
# environment: the seam tier pins every knob it needs below.
import coding_model_server.config  # noqa: F401  — capture Config before .env
_env_before = dict(os.environ)
import coding_model_server.orchestrator_daemon as d  # noqa: E402
for _k in set(os.environ) - set(_env_before):
    del os.environ[_k]
os.environ.update(_env_before)

from coding_model_autonomous import adversarial, executor, outcome, planner, retry_policy  # noqa: E402
from coding_model_autonomous.db import Database  # noqa: E402

from seam_fakes import FakeModelServer, FakeRunner  # noqa: E402
from seam_harness import DAEMON_PATH, DAEMON_STUB  # noqa: E402

ROTATION = ["implementer", "deep_implementer", "moe_implementer", "fast_implementer"]


@pytest.fixture(autouse=True)
def seam_env(monkeypatch):
    """Pin the daemon's env-derived globals to the shipped defaults."""
    # Routing stacks and optional phases.
    monkeypatch.setattr(d, "SUPERVISOR_ENABLED", False)
    monkeypatch.setattr(adversarial, "ADVERSARIAL_TESTS_ENABLED", False)
    monkeypatch.setattr(adversarial, "generate_adversarial_tests",
                        lambda *a, **k: [])
    monkeypatch.setattr(executor, "DESIGN_REVIEW_ENABLED", True)
    monkeypatch.setattr(executor, "DESIGN_REVIEW_MAX_REVISIONS", 1)
    monkeypatch.setattr(executor, "TESTABILITY_CHECK_ENABLED", True)
    monkeypatch.setattr(executor, "AUTONOMOUS_MEMORY_ROLES", set())
    # Implementer shape: single-call, whole-file unless a test arms edit mode.
    monkeypatch.setattr(executor, "DIFF_BASED_EDITS", False)
    monkeypatch.setattr(executor, "IMPLEMENTER_MODE", "auto")
    monkeypatch.setattr(executor, "MANIFEST_FILE_THRESHOLD", 8)
    # Token budgets: the shipped defaults, so max_tokens assertions hold on a
    # box whose .env raises them.
    for role, n in (("architect", 8000), ("implementer", 16000), ("reviewer", 16000)):
        monkeypatch.setitem(executor.ROLE_TO_MAX_TOKENS, role, n)
    monkeypatch.setattr(executor, "ARCHITECT_MAX_TOKENS", 8000)
    monkeypatch.setattr(executor, "IMPLEMENTER_MAX_TOKENS", 16000)
    monkeypatch.setattr(executor, "REVIEWER_MAX_TOKENS", 16000)
    monkeypatch.setattr(executor, "IMPLEMENTER_MAX_TOKENS_CEILING", 48000)
    monkeypatch.setattr(executor, "IMPLEMENTER_TOKENS_PER_FILE", 1500)
    monkeypatch.setattr(executor, "IMPLEMENTER_TOKENS_BASE", 4000)
    monkeypatch.setattr(executor, "DESIGN_REVIEW_MAX_TOKENS", 8000)
    # Budgets. MAX_RETRIES is imported by name into the daemon — pin both.
    monkeypatch.setattr(executor, "MAX_RETRIES", 5)
    monkeypatch.setattr(d, "MAX_RETRIES", 5)
    monkeypatch.setattr(executor, "ARCHITECT_PARSE_RETRIES", 2)
    monkeypatch.setattr(executor, "REVIEWER_PARSE_RETRIES", 1)
    monkeypatch.setattr(planner, "PLANNER_PARSE_RETRIES", 1)
    monkeypatch.setattr(d, "PLAN_VALIDATION_MAX_ROUNDS", 2)
    monkeypatch.setattr(d, "BUILD_FAILURE_ARCHITECT_THRESHOLD", 1)
    # DEV-652: the runner-outage requeue is capped by outcome._CAPS now, not
    # by a local constant. NO_VERDICT_CAP is its env-derived fallback and the
    # tier already depends on the shipped 5 (the architect park asserts "x6").
    monkeypatch.setattr(outcome, "NO_VERDICT_CAP", 5)
    # DEV-631: also env-derived. Pinned to the shipped default so the
    # tier exercises the real contract; the rotation cases below opt out
    # explicitly, because truncating the walk is the whole point of it.
    monkeypatch.setattr(outcome, "INVARIANT_AGENTS", 2)
    monkeypatch.setattr(d, "BLOCK_ON_BUILD_WARNINGS", False)
    monkeypatch.setattr(d, "ALLOW_UNREAD_FILE_MODIFICATION", False)
    # Agents, so assertions can name models.
    monkeypatch.setattr(executor, "ROLE_TO_AGENT", {
        "architect": "dense_architect", "implementer": "implementer",
        "reviewer": "reviewer"})
    monkeypatch.setattr(executor, "DESIGN_REVIEW_AGENT", "reviewer")
    monkeypatch.setattr(planner, "PLANNER_AGENT", "dense_architect")
    monkeypatch.setattr(d, "_SYNTHESIS_AGENT", "deep_reviewer")
    monkeypatch.setattr(retry_policy, "_IMPLEMENTER_ROTATION", list(ROTATION))
    monkeypatch.setattr(d, "_IMPLEMENTER_ROTATION", list(ROTATION))
    # Delivery: no remote → skipped, never touches git.
    monkeypatch.delenv("AUTONOMOUS_DELIVERY_REMOTES", raising=False)
    yield


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "seam.sqlite",
                        workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


@pytest.fixture
def model(monkeypatch) -> FakeModelServer:
    return FakeModelServer().install(monkeypatch)


@pytest.fixture
def runner(monkeypatch) -> FakeRunner:
    return FakeRunner({DAEMON_PATH: DAEMON_STUB}).install(monkeypatch)


@pytest.fixture
def edit_mode(monkeypatch):
    """Arm diff-based edits (the DEV-581 / DEV-638 path)."""
    monkeypatch.setattr(executor, "DIFF_BASED_EDITS", True)
