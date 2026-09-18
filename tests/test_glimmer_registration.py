"""DEV-692: Muse-Glimmer is registered for evaluation but deliberately not routed.

Registration and routing are separate decisions here, and the separation is the
whole point: an unevaluated model that the rotation or a complexity-tier
recommendation can reach is an unevaluated model in production. These tests pin
that boundary in both directions — Glimmer must be addressable (so the eval can
run it and so the allocator knows its window), and it must not be reachable by
any automatic path until the pairwise eval returns a verdict.
"""
import pytest

from coding_model_server.config import Config
from coding_model_autonomous.executor import (
    ALLOWED_IMPLEMENTER_AGENTS, TIER_TO_IMPLEMENTER,
)
from coding_model_autonomous.retry_policy import _IMPLEMENTER_ROTATION

GLIMMER = ("glimmer_architect", "glimmer_implementer")


# ── registered ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("agent", GLIMMER)
def test_the_glimmer_arms_are_registered(agent):
    assert agent in Config.AGENTS


def test_both_arms_are_the_same_model():
    """One GGUF over two prompts, so evaluating the architect and implementer
    slots needs no model swap between them."""
    a, b = Config.AGENTS["glimmer_architect"], Config.AGENTS["glimmer_implementer"]
    assert a["model_config"] is b["model_config"]
    assert a["system_prompt"] != b["system_prompt"]


def test_the_arms_carry_the_swept_config():
    """DEV-727 re-swept at 65,536 and moved the pick to ngl=40 --swa-full: the
    rung with the most headroom that still gains layers over 36 (1,884 MiB free
    against the old 1,763, +4 layers, +21% decode). ngl=44 drops to 780 MiB,
    under the floor, and 48/52 do not load. A later edit that raises ngl without
    re-sweeping would OOM at load or crash the prefill buffer on a revision pass
    (the DEV-616 class); one that restores the 131K window pays double the KV
    for context the DEV-633 fit check never budgets against."""
    cfg = Config.AGENTS["glimmer_architect"]["model_config"]
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
    cfg = Config.AGENTS["glimmer_architect"]["model_config"]
    assert "--jinja" in cfg["server_extra_args"]
    assert "--no-jinja" not in cfg["server_extra_args"]


def test_the_window_is_readable_so_the_fit_check_arms():
    """DEV-676's eligible_agents skips any agent whose window it cannot read,
    which silently mis-routes it. The allocator reads n_ctx straight off the
    model config, so an int here is the whole requirement."""
    for agent in GLIMMER:
        assert int(Config.AGENTS[agent]["model_config"]["n_ctx"]) > 0


# ── not routed ───────────────────────────────────────────────────────────────

def test_the_architect_arm_is_not_in_the_implementer_rotation():
    assert "glimmer_architect" not in _IMPLEMENTER_ROTATION


def test_the_implementer_arm_sits_directly_behind_deep():
    """DEV-692 item 3: Keith's placement. Third in the chain, which is also the
    position that separates the two Qwen families instead of stacking a fourth
    consecutive one."""
    assert _IMPLEMENTER_ROTATION.index("glimmer_implementer") == \
        _IMPLEMENTER_ROTATION.index("deep_implementer") + 1


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


def test_the_glimmer_alias_resolves_to_a_registered_agent():
    assert Config.resolve_agent("glimmer") == "glimmer_implementer"
    assert Config.resolve_agent("glimmer") in Config.AGENTS
