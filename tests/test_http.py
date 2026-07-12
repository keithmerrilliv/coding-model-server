"""Tests for the shared client chat-completion helper.

The helper centralises payload + header + session assembly that compaction and
review previously each rolled by hand. These lock in the request shape — a
regression here silently changes what every migrated call site sends — and in
particular guard the auth-header behaviour that review.py used to build by hand
(``X-Admin-Key`` when ``ADMIN_API_KEY`` is set), now sourced from
``config.auth_headers``.
"""
from unittest import mock

import requests

import coding_model_client.http as client_http
from coding_model_client.config import config


def _call(**kwargs):
    """Invoke post_chat_completion with the shared session's .post mocked out.

    Returns the mock so the test can assert on how it was called.
    """
    with mock.patch.object(client_http._SESSION, "post") as post:
        post.return_value = mock.sentinel.response
        result = client_http.post_chat_completion(**kwargs)
    assert result is mock.sentinel.response  # returns the raw Response untouched
    return post


def test_builds_basic_payload_and_targets_api_url():
    post = _call(model="reviewer", messages=[{"role": "user", "content": "hi"}], timeout=60)
    args, kwargs = post.call_args
    assert args[0] == config.API_URL
    assert kwargs["json"] == {
        "model": "reviewer",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }
    assert kwargs["timeout"] == 60


def test_extra_params_merged_into_payload():
    post = _call(model="m", messages=[], timeout=120, max_tokens=2000, temperature=0.3)
    payload = post.call_args.kwargs["json"]
    assert payload["max_tokens"] == 2000
    assert payload["temperature"] == 0.3
    assert payload["stream"] is False


def test_review_specific_skip_memory_flag_passes_through():
    # review.py relies on skip_memory; ensure arbitrary payload flags survive.
    post = _call(model="m", messages=[], timeout=10, skip_memory=True)
    assert post.call_args.kwargs["json"]["skip_memory"] is True


def test_headers_always_include_content_type():
    post = _call(model="m", messages=[], timeout=10)
    assert post.call_args.kwargs["headers"]["Content-Type"] == "application/json"


def test_auth_header_present_when_admin_key_set():
    with mock.patch.object(type(config), "ADMIN_API_KEY", "secret-key"):
        post = _call(model="m", messages=[], timeout=10)
    headers = post.call_args.kwargs["headers"]
    # Equivalent to review.py's old manual `X-Admin-Key` block.
    assert headers["X-Admin-Key"] == "secret-key"


def test_no_auth_header_when_admin_key_unset():
    with mock.patch.object(type(config), "ADMIN_API_KEY", ""):
        post = _call(model="m", messages=[], timeout=10)
    assert "X-Admin-Key" not in post.call_args.kwargs["headers"]


def test_stream_flag_sets_payload_and_request_stream():
    post = _call(model="m", messages=[], timeout=10, stream=True)
    assert post.call_args.kwargs["json"]["stream"] is True
    assert post.call_args.kwargs["stream"] is True


def test_non_stream_is_the_default():
    post = _call(model="m", messages=[], timeout=10)
    assert post.call_args.kwargs["stream"] is False


def test_uses_the_shared_session():
    # All callers must share one keepalive session, not spin up their own.
    assert isinstance(client_http.get_session(), requests.Session)
    assert client_http.get_session() is client_http._SESSION
