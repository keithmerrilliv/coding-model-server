"""DEV-823: the rotation fit check estimates the next prompt per CALL, not per attempt.

Run 61 (spec_360d8d96): fast_implementer's attempt 0 was five manifest-mode
calls, 71,399 prompt tokens in SUM. The fit check read that as one prompt,
added the 16,000 completion budget, ruled out every 64K agent and left
[moe, deep]; `_rotation_pick` then skipped moe and sent retry 1 to
deep_implementer. The retry's own budget line said ~18,357 prompt tokens —
it fit everywhere.
"""
import pytest

from coding_model_autonomous import executor
from coding_model_autonomous import retry_policy as rp
from coding_model_autonomous.db import Database
from coding_model_autonomous.models import EventKind

WINDOWS = {"implementer": 65536, "glimmer_implementer": 65536,
           "moe_implementer": 118784, "fast_implementer": 65536,
           "deep_implementer": 262144}
COMPLETION = 16000
RUN61_CALLS = [9_100, 18_357, 14_600, 14_742, 14_600]      # sums to 71,399


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=tmp_path / "t.sqlite", workspace_root=tmp_path / "ws")
    yield database
    database.close_all()


def _record(db, payload):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id,
                    payload={"role": "implementer", **payload})
    return spec.id


# ── the tally keeps the largest call ─────────────────────────────────────────

def test_the_tally_keeps_the_sum_and_the_largest_call():
    tally: dict = {}
    for n in RUN61_CALLS:
        executor.accumulate_agent_fields(tally, {"agent": "fast_implementer",
                                                 "prompt_tokens": n})
    assert sum(RUN61_CALLS) == 71_399
    assert tally["prompt_tokens"] == 71_399          # cost per attempt: unchanged
    assert tally["max_call_prompt_tokens"] == 18_357
    assert tally["calls"] == 5
    assert executor.agent_event_fields(tally)["max_call_prompt_tokens"] == 18_357


# ── the estimate reads it ────────────────────────────────────────────────────

def test_a_manifest_attempt_is_estimated_by_its_largest_call(db):
    spec_id = _record(db, {"prompt_tokens": 71_399, "calls": 5,
                           "max_call_prompt_tokens": 18_357})
    assert rp.previous_prompt_tokens(db, spec_id, "implementer") == 18_357


def test_a_multi_call_event_without_the_figure_cannot_tell(db):
    """Recorded before DEV-823: the sum is the only number, and it is wrong."""
    spec_id = _record(db, {"prompt_tokens": 71_399, "calls": 5})
    assert rp.previous_prompt_tokens(db, spec_id, "implementer") is None


def test_a_single_call_event_is_unchanged(db):
    spec_id = _record(db, {"prompt_tokens": 40_000})
    assert rp.previous_prompt_tokens(db, spec_id, "implementer") == 40_000


# ── run 61, end to end ───────────────────────────────────────────────────────

def test_run61_retry_now_fits_every_agent_and_rotates_normally():
    eligible = rp.eligible_agents(18_357 + COMPLETION, WINDOWS.get)
    assert set(eligible) == set(WINDOWS)
    assert rp._rotation_pick("fast_implementer", 1, eligible=eligible) == "implementer"


def test_run61_as_it_was_now_picks_moe_not_deep():
    """Even with the inflated estimate, the first agent that fits goes first."""
    eligible = rp.eligible_agents(71_399 + COMPLETION, WINDOWS.get)
    assert set(eligible) == {"moe_implementer", "deep_implementer"}
    assert rp._rotation_pick("fast_implementer", 1, eligible=eligible) == "moe_implementer"


def test_an_eligible_initial_agent_keeps_the_old_offset():
    """Control: when attempt 0's agent still fits, retry 1 is the next one."""
    eligible = list(WINDOWS)
    assert rp._rotation_pick("implementer", 1, eligible=eligible) == "glimmer_implementer"
    assert rp._rotation_pick("fast_implementer", 1, eligible=eligible) == "implementer"


def test_a_prompt_only_deep_holds_still_reaches_deep():
    """DEV-821's window fallback is untouched."""
    eligible = rp.eligible_agents(200_000, WINDOWS.get)
    assert eligible == ["deep_implementer"]
    assert rp._rotation_pick("fast_implementer", 1, eligible=eligible) == "deep_implementer"
