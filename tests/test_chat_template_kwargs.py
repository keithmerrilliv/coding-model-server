"""The architect's reasoning is a budget line we never controlled — DEV-556.

`AUTONOMOUS_ARCHITECT_MAX_TOKENS` has always been a thinking-PLUS-design budget
of which only the design half is observable. Qwen3.6's template gates `<think>`
behind an `enable_thinking` conditional — `_probe_expects_thinking` says so at
llama_server.py:514 — and nothing in this repo ever sent that conditional, so
the template's default (on) applied to every architect call ever made. The
reasoning is generated, counted by the server's `usage`, and then dropped by
`strip_thinking` before `call_agent` sees a byte of it.

Run 12, one task, byte-identical prompts: attempt 1 spent 16000 completion
tokens and 30.5 minutes to emit ~500 tokens of design; attempt 2 wrote the whole
design in 5369 (DEV-543). Six of twenty architect calls across runs 10-12
truncated, every one exactly at the ceiling, whatever the ceiling was.

So the knob got built and the decision got measured: `dense_architect_nothink`,
the same GGUF with reasoning suppressed, was the second arm until thinking-on
became policy (2026-09-15); DEV-839 retired it on usage telemetry. The knob
stays, for the next eval arm that needs a template variable.

What these tests pin is that the knob is INERT until something opts in. The
whole roster runs through `_build_request_payload`, so a key appearing where it
did not before would change every agent's request at once.
"""
import pytest

from chat_harness import drive_chat
from coding_model_server.config import Config, _create_agent_config
from coding_model_server.llama_server import LlamaServerManager
from coding_model_server.schemas import ChatCompletionRequest, ChatMessage

MESSAGES = [{"role": "user", "content": "design it"}]


def _payload(chat_template_kwargs=..., stream=False):
    args = (MESSAGES, 4096, 0.2, stream, None, None, None, None)
    if chat_template_kwargs is ...:
        return LlamaServerManager._build_request_payload(*args)
    return LlamaServerManager._build_request_payload(*args, chat_template_kwargs)


# ── the payload is unchanged unless something opts in ────────────────────────

def test_the_key_is_absent_when_nothing_asks_for_it():
    """Every agent but one goes down this path; the request must not change."""
    assert "chat_template_kwargs" not in _payload()


def test_omitting_the_argument_and_passing_none_are_the_same_request():
    assert _payload() == _payload(None)


@pytest.mark.parametrize("empty", [None, {}])
def test_an_empty_mapping_sends_nothing(empty):
    """`{}` means 'no template variables', not 'send an empty object' — a model
    without --jinja must see the payload it saw before this existed."""
    assert "chat_template_kwargs" not in _payload(empty)


def test_the_kwargs_reach_the_payload_verbatim():
    assert _payload({"enable_thinking": False})["chat_template_kwargs"] == {
        "enable_thinking": False}


def test_a_false_value_inside_a_non_empty_mapping_survives():
    """The one that matters. `enable_thinking: False` is falsy, and a truthiness
    test on the VALUE rather than the mapping would drop the only setting this
    ticket exists to send."""
    payload = _payload({"enable_thinking": False})
    assert payload["chat_template_kwargs"]["enable_thinking"] is False


def test_streaming_and_sync_payloads_carry_it_alike():
    """DEV-119's suppression and DEV-87's buffering both live on the stream
    path; whatever we ask for must be asked for identically on both."""
    assert (_payload({"enable_thinking": False}, stream=True)["chat_template_kwargs"]
            == _payload({"enable_thinking": False})["chat_template_kwargs"])


def test_nothing_else_about_the_payload_moves():
    before, after = _payload(), _payload({"enable_thinking": False})
    assert set(after) - set(before) == {"chat_template_kwargs"}
    for key in before:
        assert after[key] == before[key], key


# ── the roster carries the decision ──────────────────────────────────────────

def test_the_incumbent_architect_opts_into_nothing():
    """Repointing is a separate, evidence-gated decision — until the eval says
    otherwise the architect must behave exactly as it does today."""
    assert Config.AGENTS["dense_architect"].get("chat_template_kwargs") is None


def test_the_agent_config_copies_the_mapping():
    """Two agents sharing one dict would let a mutation of either rewrite the
    other's request."""
    source = {"enable_thinking": False}
    cfg = _create_agent_config("d", "p", {"model": "m"}, chat_template_kwargs=source)
    source["enable_thinking"] = True
    assert cfg["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.parametrize("falsy", [None, {}])
def test_an_agent_that_passes_nothing_carries_no_key(falsy):
    cfg = _create_agent_config("d", "p", {"model": "m"}, chat_template_kwargs=falsy)
    assert "chat_template_kwargs" not in cfg


# ── the route resolves agent default vs request override ─────────────────────

NOTHINK = "test_architect_nothink"


@pytest.fixture
def nothink_arm(monkeypatch):
    """An eval arm the way one is registered: dense_architect's GGUF and
    prompt, with enable_thinking off by default."""
    base = Config.AGENTS["dense_architect"]
    monkeypatch.setitem(Config.AGENTS, NOTHINK, _create_agent_config(
        "test arm", base["system_prompt"], base["model_config"], executor=True,
        chat_template_kwargs={"enable_thinking": False}))
    return NOTHINK


def _resolve(monkeypatch, model, **kwargs):
    """Drive the real route and report what it handed the proxy."""
    _, fake_mgr = drive_chat(monkeypatch, model=model, messages=[
        ChatMessage(role="user", content="design it")], **kwargs)
    return fake_mgr.proxy_sync.call_args.kwargs["chat_template_kwargs"]


def test_the_agents_default_is_used_when_the_request_says_nothing(
        monkeypatch, nothink_arm):
    assert _resolve(monkeypatch, nothink_arm) == {"enable_thinking": False}


def test_an_agent_without_a_default_sends_nothing(monkeypatch):
    assert _resolve(monkeypatch, "dense_architect") is None


def test_an_explicit_request_value_overrides_the_agents_default(
        monkeypatch, nothink_arm):
    """So one call can be run the other way without a second roster entry —
    which is also how a thinking-off architect is re-enabled for one design."""
    assert _resolve(monkeypatch, nothink_arm,
                    chat_template_kwargs={"enable_thinking": True}) == {
        "enable_thinking": True}


def test_a_request_can_set_kwargs_on_an_agent_that_has_none(monkeypatch):
    assert _resolve(monkeypatch, "dense_architect",
                    chat_template_kwargs={"enable_thinking": False}) == {
        "enable_thinking": False}


# ── the schema ───────────────────────────────────────────────────────────────

def test_the_field_defaults_to_none():
    req = ChatCompletionRequest(messages=[ChatMessage(role="user", content="x")])
    assert req.chat_template_kwargs is None


def test_the_field_accepts_arbitrary_template_variables():
    """llama-server's Jinja renderer owns the vocabulary, not this schema —
    a template variable we have never heard of must not be rejected here."""
    req = ChatCompletionRequest(
        messages=[ChatMessage(role="user", content="x")],
        chat_template_kwargs={"enable_thinking": False, "some_future_flag": 3})
    assert req.chat_template_kwargs["some_future_flag"] == 3
