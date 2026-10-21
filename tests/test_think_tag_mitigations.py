"""DEV-934: a model that stops dead on think tags gets two mitigations.

The payload builder rewrites the literals a model config lists, and a
reasoning-only completion (DEV-617) is retried once with thinking off when
the loaded template takes enable_thinking.
"""
from unittest import mock

import pytest
from fastapi import HTTPException

from coding_model_server.llama_server import LlamaServerManager

ESCAPES = {"<think>": "< think >", "</think>": "< /think >"}


def _payload(messages, model_config, **kw):
    return LlamaServerManager._build_request_payload(
        messages, 64, 0.0, False, model_config, None, None, None, **kw)


def test_escape_in_prompt_rewrites_every_message_and_leaves_the_input_alone():
    msgs = [{"role": "system", "content": "strip <think> blocks"},
            {"role": "user", "content": "ends with </think>\n"},
            {"role": "assistant", "content": None}]
    out = _payload(msgs, {"escape_in_prompt": ESCAPES})["messages"]
    assert [m["content"] for m in out] == [
        "strip < think > blocks", "ends with < /think >\n", None]
    assert msgs[0]["content"] == "strip <think> blocks"   # copied, not mutated


def test_no_escape_config_sends_the_text_verbatim():
    msgs = [{"role": "user", "content": "<think>"}]
    assert _payload(msgs, {})["messages"] is msgs
    assert _payload(msgs, {"escape_in_prompt": {}})["messages"][0]["content"] == "<think>"


def _resp(content, finish="stop", completion_tokens=24):
    r = mock.Mock()
    r.status_code = 200
    r.json.return_value = {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": completion_tokens,
                  "total_tokens": 10 + completion_tokens},
    }
    return r


@pytest.fixture
def mgr():
    m = LlamaServerManager()
    m.current_expects_thinking = True
    m.current_thinking_toggle = True
    return m


def _complete(m, **kw):
    with mock.patch.object(m, "_confirm_restore"):
        return m.proxy_sync([{"role": "user", "content": "q"}], "sys", "agent", 64, 0.0,
                            model_config={"n_ctx": 1024}, req_id="req_t", **kw)


def test_reasoning_only_is_retried_once_with_thinking_off(mgr):
    # First pass: cut off mid-reasoning (finish length, no </think>) — the
    # DEV-617 shape. Second pass answers.
    posts = [_resp("<think>\nstill thinking", finish="length"),
             _resp("the answer")]
    with mock.patch.object(mgr, "_post_with_recovery", side_effect=posts) as post:
        out = _complete(mgr, chat_template_kwargs={"reasoning_effort": "medium"})
    assert out["choices"][0]["message"]["content"] == "the answer"
    assert out["usage"]["thinking_fallback"] == 1
    assert post.call_count == 2
    first, second = (c.args[1] for c in post.call_args_list)
    assert first["chat_template_kwargs"] == {"reasoning_effort": "medium"}
    assert second["chat_template_kwargs"] == {"reasoning_effort": "medium",
                                              "enable_thinking": False}


def test_a_visible_answer_is_never_retried_and_carries_no_flag(mgr):
    with mock.patch.object(mgr, "_post_with_recovery", return_value=_resp("fine")) as post:
        out = _complete(mgr)
    assert post.call_count == 1
    assert "thinking_fallback" not in out["usage"]


def test_a_template_without_the_toggle_gets_the_502_as_before(mgr):
    mgr.current_thinking_toggle = False
    with mock.patch.object(mgr, "_post_with_recovery",
                           return_value=_resp("<think>\nx", finish="length")) as post:
        with pytest.raises(HTTPException) as exc:
            _complete(mgr)
    assert exc.value.status_code == 502 and "reasoning-only" in exc.value.detail
    assert post.call_count == 1


def test_a_call_that_already_turned_thinking_off_is_not_retried(mgr):
    with mock.patch.object(mgr, "_post_with_recovery",
                           return_value=_resp("<think>\nx", finish="length")) as post:
        with pytest.raises(HTTPException):
            _complete(mgr, chat_template_kwargs={"enable_thinking": False})
    assert post.call_count == 1


def test_a_second_reasoning_only_pass_is_the_502(mgr):
    posts = [_resp("<think>\na", finish="length"), _resp("<think>\nb", finish="length")]
    with mock.patch.object(mgr, "_post_with_recovery", side_effect=posts) as post:
        with pytest.raises(HTTPException):
            _complete(mgr)
    assert post.call_count == 2


def test_flash_next_lists_the_two_tags():
    from coding_model_server.roster import AGENTS
    for name in ("flashnext_reviewer", "flashnext_architect"):
        assert AGENTS[name]["model_config"]["escape_in_prompt"] == ESCAPES
    assert "escape_in_prompt" not in AGENTS["deep_reviewer"]["model_config"]
