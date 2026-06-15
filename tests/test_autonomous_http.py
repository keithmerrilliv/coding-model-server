"""Tests for the autonomous package's shared chat-completion helper.

planner / supervisor / executor were each hand-rolling the same POST to the
local coding-model-server. These pin the consolidated helper: payload shape, the
skip_memory default, extra-param passthrough (tools/tool_choice for supervisor),
and — most importantly — the opt-in transient-5xx backoff that executor.call_agent
relies on to survive model-swap CUDA OOMs.

time.sleep is patched out so the backoff path is exercised without real waits.
"""
from unittest import mock

import coding_model_autonomous._http as http


def _resp(status):
    r = mock.Mock()
    r.status_code = status
    r.text = "body"
    return r


def test_basic_payload_and_url():
    with mock.patch.object(http._SESSION, "post") as post:
        post.return_value = _resp(200)
        http.post_chat_completion("reviewer", [{"role": "user", "content": "hi"}],
                                  timeout=60)
    args, kwargs = post.call_args
    assert args[0] == http.API_URL
    assert kwargs["json"]["model"] == "reviewer"
    assert kwargs["json"]["stream"] is False
    assert kwargs["json"]["skip_memory"] is True   # default on
    assert kwargs["timeout"] == 60


def test_skip_memory_can_be_disabled():
    with mock.patch.object(http._SESSION, "post") as post:
        post.return_value = _resp(200)
        http.post_chat_completion("m", [], timeout=10, skip_memory=False)
    assert post.call_args.kwargs["json"]["skip_memory"] is False


def test_extra_params_passed_through():
    with mock.patch.object(http._SESSION, "post") as post:
        post.return_value = _resp(200)
        http.post_chat_completion("m", [], timeout=10, max_tokens=1500,
                                  temperature=0.1, tools=[{"x": 1}],
                                  tool_choice={"type": "function"})
    payload = post.call_args.kwargs["json"]
    assert payload["max_tokens"] == 1500
    assert payload["tools"] == [{"x": 1}]
    assert payload["tool_choice"] == {"type": "function"}


def test_admin_key_header_when_set(monkeypatch):
    monkeypatch.setattr(http, "ADMIN_API_KEY", "secret")
    with mock.patch.object(http._SESSION, "post") as post:
        post.return_value = _resp(200)
        http.post_chat_completion("m", [], timeout=10)
    assert post.call_args.kwargs["headers"]["X-Admin-Key"] == "secret"


def test_no_admin_key_header_when_unset(monkeypatch):
    monkeypatch.setattr(http, "ADMIN_API_KEY", "")
    with mock.patch.object(http._SESSION, "post") as post:
        post.return_value = _resp(200)
        http.post_chat_completion("m", [], timeout=10)
    assert "X-Admin-Key" not in post.call_args.kwargs["headers"]


# ── retry_5xx behaviour (the executor.call_agent contract) ────────────────────

def test_no_retry_by_default():
    with mock.patch.object(http._SESSION, "post") as post:
        post.return_value = _resp(500)
        http.post_chat_completion("m", [], timeout=10)
    assert post.call_count == 1   # 5xx not retried unless asked


def test_retry_5xx_retries_then_gives_up():
    with mock.patch.object(http, "time") as t, \
            mock.patch.object(http._SESSION, "post") as post:
        post.return_value = _resp(500)  # always 500
        http.post_chat_completion("m", [], timeout=10, retry_5xx=True)
    # initial attempt + 3 backoff retries = 4 total
    assert post.call_count == 4
    # slept on the 3 retries, with the documented schedule
    assert [c.args[0] for c in t.sleep.call_args_list] == [10.0, 30.0, 60.0]


def test_retry_5xx_stops_on_first_success():
    with mock.patch.object(http, "time") as t, \
            mock.patch.object(http._SESSION, "post") as post:
        post.side_effect = [_resp(500), _resp(200), _resp(200)]
        resp = http.post_chat_completion("m", [], timeout=10, retry_5xx=True)
    assert resp.status_code == 200
    assert post.call_count == 2          # one retry, then success
    assert t.sleep.call_count == 1       # slept once before the retry


def test_retry_5xx_does_not_retry_4xx():
    with mock.patch.object(http, "time") as t, \
            mock.patch.object(http._SESSION, "post") as post:
        post.return_value = _resp(400)
        http.post_chat_completion("m", [], timeout=10, retry_5xx=True)
    assert post.call_count == 1          # 4xx is a real error, not transient
    assert t.sleep.call_count == 0
