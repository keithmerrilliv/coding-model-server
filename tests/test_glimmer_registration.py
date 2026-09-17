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
    """The DEV-692 sweep picked ngl=36 --swa-full as the only rung that clears
    the ~1.4 GB reload floor while keeping prompt-cache reuse. A later edit that
    raises ngl without re-sweeping would OOM at load (ngl=44) or crash the
    prefill buffer on a revision pass (the DEV-616 class)."""
    cfg = Config.AGENTS["glimmer_architect"]["model_config"]
    assert cfg["n_gpu_layers"] == 36
    assert cfg["n_ctx"] == 131072
    assert cfg["type_k"] == 2 and cfg["type_v"] == 2
    assert "--swa-full" in cfg["server_extra_args"]


def test_the_window_is_readable_so_the_fit_check_arms():
    """DEV-676's eligible_agents skips any agent whose window it cannot read,
    which silently mis-routes it. The allocator reads n_ctx straight off the
    model config, so an int here is the whole requirement."""
    for agent in GLIMMER:
        assert int(Config.AGENTS[agent]["model_config"]["n_ctx"]) > 0


# ── not routed ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("agent", GLIMMER)
def test_glimmer_is_not_in_the_implementer_rotation(agent):
    assert agent not in _IMPLEMENTER_ROTATION


@pytest.mark.parametrize("agent", GLIMMER)
def test_glimmer_is_not_an_allowed_tier_recommendation(agent):
    """ALLOWED_IMPLEMENTER_AGENTS is the set the architect may recommend into.
    Membership would let a design put Glimmer on a production spec."""
    assert agent not in ALLOWED_IMPLEMENTER_AGENTS
    assert agent not in TIER_TO_IMPLEMENTER.values()


def test_no_spark_alias_exists():
    """Muse Spark is the hosted model this server has no path to. An alias by
    that name would resolve to a different model than the one it names."""
    assert "spark" not in Config.AGENT_ALIASES
    assert not any(k.endswith("spark") for k in Config.AGENT_ALIASES)


def test_the_glimmer_alias_resolves_to_a_registered_agent():
    assert Config.resolve_agent("glimmer") == "glimmer_implementer"
    assert Config.resolve_agent("glimmer") in Config.AGENTS
