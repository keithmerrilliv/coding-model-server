"""DEV-692: Muse-Glimmer is a retry-only implementer.

It rotates into retries but can never make attempt 0: no complexity tier or
architect recommendation reaches it, because attempt 0 is the only attempt
comparable across runs (DEV-431). These tests pin both halves, and the swept
serving config behind it. The architect arm (`glimmer_architect`) and the
`glimmer` alias were retired on usage telemetry (DEV-839).
"""
import pytest

from coding_model_server.config import Config
from coding_model_autonomous.executor import (
    ALLOWED_IMPLEMENTER_AGENTS, TIER_TO_IMPLEMENTER,
)
from coding_model_autonomous.retry_policy import _IMPLEMENTER_ROTATION

GLIMMER = ("glimmer_implementer",)


# ── registered ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("agent", GLIMMER)
def test_the_glimmer_arms_are_registered(agent):
    assert agent in Config.AGENTS


def test_the_arms_carry_the_swept_config():
    """DEV-727 re-swept at 65,536 and moved the pick to ngl=40 --swa-full: the
    rung with the most headroom that still gains layers over 36 (1,884 MiB free
    against the old 1,763, +4 layers, +21% decode). ngl=44 drops to 780 MiB,
    under the floor, and 48/52 do not load. A later edit that raises ngl without
    re-sweeping would OOM at load or crash the prefill buffer on a revision pass
    (the DEV-616 class); one that restores the 131K window pays double the KV
    for context the DEV-633 fit check never budgets against."""
    cfg = Config.AGENTS["glimmer_implementer"]["model_config"]
    assert cfg["n_gpu_layers"] == 40
    assert cfg["n_ctx"] == 65536
    assert cfg["type_k"] == 2 and cfg["type_v"] == 2
    assert "--swa-full" in cfg["server_extra_args"]


def test_glimmer_is_served_with_jinja():
    """--no-jinja is the tempting response to DEV-727's peg-parse 500, and it
    does not work: llama-server refuses this model outright with "this custom
    template is not supported, try using --jinja" (tested 2026-09-18), so the
    arm 502s on every call instead of one in six. The harmony template has no
    legacy path."""
    cfg = Config.AGENTS["glimmer_implementer"]["model_config"]
    assert "--jinja" in cfg["server_extra_args"]
    assert "--no-jinja" not in cfg["server_extra_args"]


def test_the_window_is_readable_so_the_fit_check_arms():
    """DEV-676's eligible_agents skips any agent whose window it cannot read,
    which silently mis-routes it. The allocator reads n_ctx straight off the
    model config, so an int here is the whole requirement."""
    for agent in GLIMMER:
        assert int(Config.AGENTS[agent]["model_config"]["n_ctx"]) > 0


# ── not routed ───────────────────────────────────────────────────────────────

def test_the_rotation_is_ordered_by_what_an_attempt_costs():
    """The walk is failure-triggered, so every slot ahead of the one that
    delivers is paid in full — and Glimmer averages 1,041 s a call at 11 tok/s
    against `implementer`'s 165 s at 64, with MiniMax at 6.6-9 tok/s between
    them. The fast decoders therefore take the early retries and Glimmer the
    last one before the window fallback. It is still retry-only (below)."""
    assert _IMPLEMENTER_ROTATION == [
        "implementer", "fast_implementer", "ornith", "moe_implementer",
        "glimmer_implementer", "deep_implementer"]


def test_a_failed_first_attempt_is_retried_on_the_cheap_agents_first():
    from coding_model_autonomous import retry_policy as rp
    assert [rp._rotation_pick("implementer", n) for n in (1, 2, 3, 4, 5)] == [
        "fast_implementer", "ornith", "moe_implementer", "glimmer_implementer",
        "deep_implementer"]
    # Whoever made attempt 0, Glimmer is never the first retry and deep is
    # never ahead of it.
    for first in ("fast_implementer", "moe_implementer"):
        walk = [rp._rotation_pick(first, n) for n in (1, 2, 3, 4, 5)]
        assert walk[0] == "implementer"
        assert walk[-2:] == ["glimmer_implementer", "deep_implementer"]


def test_escalation_for_size_reaches_the_cheapest_window_that_fits():
    """The rotation is also plan_dispatch's candidate order: a prompt too big
    for the chosen agent goes to the first candidate that holds it. With the
    live windows that is moe_implementer's 116K, ahead of anything slower."""
    from coding_model_autonomous import context as ctx
    windows = {a: int(Config.AGENTS[a]["model_config"]["n_ctx"])
               for a in _IMPLEMENTER_ROTATION}
    files = [(f"src/f{i}.py", "x" * 10_000) for i in range(24)]   # 80K tokens
    alloc = ctx.plan_dispatch(
        "spec_t", role="implementer", fixed_chars=0, completion_tokens=8_000,
        agent="implementer", candidates=_IMPLEMENTER_ROTATION,
        window_of=windows.get,
        sections=[ctx.Section(ctx.SECTION_EDITABLE, files, 400_000)])
    assert alloc.agent == "moe_implementer"
    assert not alloc.dropped_any


def test_deep_is_the_window_fallback_not_a_retry_slot():
    """DEV-821: deep_implementer delivered 0 of 38 attempts (DEV-720), so it is
    last in the chain and no complexity tier defaults to it. It stays in the
    rotation because its 256K window is the only fit for some prompts."""
    from coding_model_autonomous import retry_policy as rp
    assert _IMPLEMENTER_ROTATION[-1] == "deep_implementer"
    assert "deep_implementer" not in TIER_TO_IMPLEMENTER.values()
    windows = {"implementer": 65536, "glimmer_implementer": 131072,
               "moe_implementer": 65536, "fast_implementer": 65536,
               "deep_implementer": 262144}
    # A prompt only deep's window holds still reaches it, and only it.
    assert rp.eligible_agents(200_000, windows.get) == ["deep_implementer"]
    assert rp._rotation_pick("implementer", 1,
                             eligible=["deep_implementer"]) == "deep_implementer"


@pytest.mark.parametrize("agent", GLIMMER)
def test_glimmer_is_not_an_allowed_tier_recommendation(agent):
    """ALLOWED_IMPLEMENTER_AGENTS is the set the architect may recommend into,
    and TIER_TO_IMPLEMENTER is what a complexity tier defaults to. Membership in
    either would put Glimmer on ATTEMPT 1, which is the only attempt whose agent
    is chosen rather than rotated into and therefore the only one comparable
    across runs (DEV-431). Rotation membership is retry-only on purpose."""
    assert agent not in ALLOWED_IMPLEMENTER_AGENTS
    assert agent not in TIER_TO_IMPLEMENTER.values()


def test_implementer_calls_are_single_turn():
    """The load-bearing precondition for Glimmer being in the rotation at all.

    DEV-727: Glimmer 500s inside llama-server whenever it emits a tool call in
    harmony recipient syntax, which it starts doing once a conversation carries
    a tool RESULT — round 1 of the architect's DEV-714 tool loop. The implementer
    never builds that shape: each call is a fresh [system, user] pair. If the
    implementer ever gains a tool loop, this test fails, and Glimmer's rotation
    membership has to be reconsidered before that loop ships."""
    from coding_model_autonomous.executor import (
        build_manifest_message, build_per_file_message, ManifestEntry,
    )
    entry = ManifestEntry(path="a.py", purpose="thing", exports="")
    for msgs in (
        build_manifest_message("spec", "design"),
        build_per_file_message("spec", "design", [entry], entry, ""),
    ):
        assert [m["role"] for m in msgs] == ["system", "user"]


def test_no_spark_alias_exists():
    """Muse Spark is the hosted model this server has no path to. An alias by
    that name would resolve to a different model than the one it names."""
    assert "spark" not in Config.AGENT_ALIASES
    assert not any(k.endswith("spark") for k in Config.AGENT_ALIASES)

