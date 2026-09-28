"""DEV-900 — synthesis has its own roster entry, on deep_reviewer's model.

The synthesis agent used to be deep_reviewer itself, so repointing the
reviewer changed the synthesizer silently and usage telemetry counted every
synthesis call as a review. The entry is separate now, but until DEV-902's
eval says otherwise it must behave exactly as before: the same model, served
by the same llama-server child.
"""
import os

import pytest

import coding_model_server.orchestrator_daemon as d
from coding_model_server.config import Config
from coding_model_server.llama_server import LlamaServerManager


@pytest.mark.skipif("AUTONOMOUS_SYNTHESIS_AGENT" in os.environ,
                    reason="this environment overrides the synthesis agent")
def test_synthesis_defaults_to_the_synthesizer_agent():
    assert d._SYNTHESIS_AGENT == "synthesizer"


def test_the_synthesizer_is_a_served_agent():
    assert "synthesizer" in Config.AGENTS
    assert Config.resolve_agent("synthesizer") == "synthesizer"


def test_it_shares_deep_reviewers_model_and_process():
    syn = Config.AGENTS["synthesizer"]
    rev = Config.AGENTS["deep_reviewer"]
    # The same model config object: one set of measurements, one GGUF.
    assert syn["model_config"] is rev["model_config"]
    # The same runtime signature, so switching between them is no swap.
    sig = LlamaServerManager._runtime_signature
    assert sig(syn["model_config"]) == sig(rev["model_config"])
    # Same executor treatment (few-shot and fallback extraction), so a
    # synthesis call is served exactly as it was under deep_reviewer.
    assert syn.get("executor") == rev.get("executor")


def test_its_prompt_is_not_the_reviewers():
    # The pipeline sends its own synthesis system message, but a direct call
    # (the client) must not be told it is a reviewer.
    assert Config.AGENTS["synthesizer"]["system_prompt"] != \
        Config.AGENTS["deep_reviewer"]["system_prompt"]
