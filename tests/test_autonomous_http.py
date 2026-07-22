"""Tests for the autonomous package's shared chat-completion helper.

planner / supervisor / executor were each hand-rolling the same POST to the
local coding-model-server. These pin the consolidated helper: payload shape, the
skip_memory default, extra-param passthrough (tools/tool_choice for supervisor),
and — most importantly — the opt-in transient-5xx backoff that executor.call_agent
relies on to survive model-swap CUDA OOMs.

time.sleep is patched out so the backoff path is exercised without real waits.
"""
import importlib
from unittest import mock

import requests

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


# ── DEV-120: transport errors are the same transient class as a 5xx ───────────

def test_retry_retries_connection_error_then_reraises():
    """A ConnectionError every attempt (server down through the whole backoff
    window) must be retried on the schedule, then re-raised — not swallowed."""
    with mock.patch.object(http, "time") as t, \
            mock.patch.object(http._SESSION, "post") as post:
        post.side_effect = requests.ConnectionError("connection refused")
        try:
            http.post_chat_completion("m", [], timeout=10, retry_5xx=True)
            raised = False
        except requests.ConnectionError:
            raised = True
    assert raised, "must re-raise after exhausting retries"
    assert post.call_count == 4          # initial + 3 backoff retries
    assert [c.args[0] for c in t.sleep.call_args_list] == [10.0, 30.0, 60.0]


def test_retry_recovers_from_transient_connection_error():
    """The redeploy race: the server is briefly down (ConnectionError), then the
    restart comes back within the backoff window and the retry succeeds."""
    with mock.patch.object(http, "time") as t, \
            mock.patch.object(http._SESSION, "post") as post:
        post.side_effect = [requests.ConnectionError("mid-restart"), _resp(200)]
        resp = http.post_chat_completion("m", [], timeout=10, retry_5xx=True)
    assert resp.status_code == 200
    assert post.call_count == 2          # one failed connect, then success
    assert t.sleep.call_count == 1


def test_retry_retries_read_timeout():
    """A read Timeout (requests queued behind a slow model swap) is transient."""
    with mock.patch.object(http, "time") as t, \
            mock.patch.object(http._SESSION, "post") as post:
        post.side_effect = [requests.Timeout("read timed out"), _resp(200)]
        resp = http.post_chat_completion("m", [], timeout=10, retry_5xx=True)
    assert resp.status_code == 200
    assert post.call_count == 2
    assert t.sleep.call_count == 1


def test_transport_error_not_retried_when_retry_disabled():
    """Without retry_5xx the transport error propagates immediately (unchanged
    behaviour for the non-retry callers)."""
    with mock.patch.object(http._SESSION, "post") as post:
        post.side_effect = requests.ConnectionError("refused")
        try:
            http.post_chat_completion("m", [], timeout=10)
            raised = False
        except requests.ConnectionError:
            raised = True
    assert raised
    assert post.call_count == 1


# ── internal calls bind to loopback, decoupled from the advertised LAN IP ─────
# Regression for the network-move outage: CODING_MODEL_SERVER_IP is the server's
# externally advertised address; when the box changed subnets it went stale and,
# because internal calls used to read it, the orchestrator dialed a dead LAN IP
# ("No route to host"). Internal calls must ride loopback regardless.

def test_internal_url_is_loopback_by_default(monkeypatch):
    monkeypatch.delenv("CODING_MODEL_INTERNAL_HOST", raising=False)
    monkeypatch.delenv("CODING_MODEL_SERVER_IP", raising=False)
    reloaded = importlib.reload(http)
    try:
        assert reloaded.API_URL == "http://127.0.0.1:5000/v1/chat/completions"
    finally:
        importlib.reload(http)


def test_internal_url_ignores_server_ip(monkeypatch):
    # The advertised LAN IP must NOT redirect internal calls.
    monkeypatch.delenv("CODING_MODEL_INTERNAL_HOST", raising=False)
    monkeypatch.setenv("CODING_MODEL_SERVER_IP", "10.0.0.123")
    reloaded = importlib.reload(http)
    try:
        assert "10.0.0.123" not in reloaded.API_URL
        assert reloaded.API_URL == "http://127.0.0.1:5000/v1/chat/completions"
    finally:
        importlib.reload(http)


def test_internal_host_empty_falls_back_to_loopback(monkeypatch):
    # An empty/whitespace override must not produce a hostless URL.
    monkeypatch.setenv("CODING_MODEL_INTERNAL_HOST", "  ")
    reloaded = importlib.reload(http)
    try:
        assert reloaded.API_URL == "http://127.0.0.1:5000/v1/chat/completions"
    finally:
        monkeypatch.undo()
        importlib.reload(http)


def test_internal_host_explicit_override(monkeypatch):
    # Split-host topologies can still opt in via the dedicated var.
    monkeypatch.setenv("CODING_MODEL_INTERNAL_HOST", "192.168.1.9")
    reloaded = importlib.reload(http)
    try:
        assert reloaded.API_URL == "http://192.168.1.9:5000/v1/chat/completions"
    finally:
        # Restore env FIRST, then reload, so the module is left on the loopback
        # default instead of leaking 192.168.1.9 to later test modules.
        monkeypatch.undo()
        importlib.reload(http)
