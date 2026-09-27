"""retry_policy.choose_agent: the one place an implementer is picked — DEV-838.

The composition used to live inline in the daemon's _run_implementer; these
pin its order without going through a whole implementer pass.
"""
import json
import re
from pathlib import Path

import pytest

from coding_model_autonomous import retry_policy
from coding_model_autonomous.models import EventKind, SpecStatus
from coding_model_autonomous.retry_policy import choose_agent

CHAIN = ["implementer", "glimmer_implementer", "moe_implementer",
         "fast_implementer", "deep_implementer"]
WINDOWS = {"implementer": 131_072, "glimmer_implementer": 131_072,
           "moe_implementer": 118_784, "fast_implementer": 65_536,
           "deep_implementer": 262_144}


@pytest.fixture
def attempt(db, monkeypatch):
    monkeypatch.setattr(retry_policy, "_IMPLEMENTER_ROTATION", list(CHAIN))
    monkeypatch.setattr(retry_policy, "ROTATION_RANDOM_FRACTION", 0.0)
    spec = db.create_spec(title="demo", source_md_path="spec.md",
                          status=SpecStatus.EXECUTING)
    task = db.create_task(spec_id=spec.id, agent="whatever_was_written_last",
                          role="implementer", title="impl")
    spec_dir = db.spec_dir(spec.id)
    spec_dir.mkdir(parents=True, exist_ok=True)

    def at_retry(n):
        for _ in range(n - db.get_task(task.id).retry_count):
            db.increment_task_retry(task.id)
        return db.get_task(task.id)
    return db, spec, spec_dir, at_retry


def _choose(db, spec, spec_dir, task, completion=8_000):
    return choose_agent(db, spec.id, task, spec_dir,
                        default_agent="implementer",
                        completion_tokens=completion, window_of=WINDOWS.get)


def test_attempt_zero_takes_the_architects_recommendation(attempt):
    db, spec, spec_dir, at_retry = attempt
    (spec_dir / "complexity.json").write_text(
        json.dumps({"tier": "low", "recommended_agent": "moe_implementer"}))
    c = _choose(db, spec, spec_dir, at_retry(0))
    assert (c.agent, c.assignment) == ("moe_implementer", "recommended")


def test_without_a_recommendation_the_anchor_is_the_role_default_not_the_task(
        attempt):
    """DEV-640: task.agent is whatever the last pick wrote; anchoring on it
    re-based the chain every retry."""
    db, spec, spec_dir, at_retry = attempt
    c = _choose(db, spec, spec_dir, at_retry(0))
    assert (c.agent, c.assignment) == ("implementer", "rotation")
    assert [_choose(db, spec, spec_dir, at_retry(n)).agent
            for n in (1, 2, 3)] == CHAIN[1:4]


def test_a_prompt_only_one_window_holds_is_a_sole_fit(attempt):
    db, spec, spec_dir, at_retry = attempt
    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id,
                    payload={"role": "implementer", "agent": "implementer",
                             "prompt_tokens": 150_000, "calls": 1})
    c = _choose(db, spec, spec_dir, at_retry(1))
    assert c.needed_tokens == 158_000
    assert c.eligible == ["deep_implementer"]
    assert (c.agent, c.assignment) == ("deep_implementer", "sole_fit")


def test_the_random_arm_is_recorded_as_random(attempt, monkeypatch):
    db, spec, spec_dir, at_retry = attempt
    monkeypatch.setattr(retry_policy, "random_rotation_pick",
                        lambda: "fast_implementer")
    c = _choose(db, spec, spec_dir, at_retry(1))
    assert (c.agent, c.assignment) == ("fast_implementer", "random")


def test_the_daemon_does_not_compose_the_pick_itself():
    daemon = (Path(__file__).resolve().parents[1] / "src" / "coding_model_server"
              / "orchestrator_daemon.py").read_text()
    for helper in ("_rotation_pick", "eligible_agents", "random_rotation_pick",
                   "_select_implementer_agent", "previous_prompt_tokens"):
        assert not re.search(rf"\b{helper}\(", daemon), helper
    assert daemon.count("choose_agent(") == 1
