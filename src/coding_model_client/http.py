"""Shared HTTP session + chat-completion request builder for the client.

One keepalive ``Session`` and one place that assembles the chat-completion
payload, auth headers, and target URL, so call sites stop re-rolling them.
Centralising this is exactly what would have prevented the
``compaction._SESSION`` regression: there was no shared session for that module
to use, so it referenced one it never defined.

The helper returns the raw :class:`requests.Response`; the caller owns status
checking and body parsing, so each keeps its existing error semantics
(``raise_for_status`` vs. a ``status_code`` check, and its own return shape).
"""
import requests

from coding_model_client.config import config

# Module-level session: keepalive across the client's many server calls saves
# TCP+TLS setup per request.
_SESSION = requests.Session()


def get_session() -> requests.Session:
    """Return the shared keepalive session (for tests / advanced callers)."""
    return _SESSION


def post_chat_completion(model, messages, *, timeout, stream=False, **params):
    """POST a chat completion to the server and return the raw ``Response``.

    Args:
        model: Agent/model name routed by the server.
        messages: OpenAI-style message list.
        timeout: Per-request timeout in seconds (caller-chosen — values vary
            widely across call sites, so there is deliberately no default).
        stream: Whether to request a streamed response.
        **params: Extra payload fields merged in (e.g. ``temperature``,
            ``max_tokens``, ``skip_memory``).
    """
    payload = {"model": model, "messages": messages, "stream": stream, **params}
    return _SESSION.post(
        config.API_URL,
        json=payload,
        headers={"Content-Type": "application/json", **config.auth_headers},
        timeout=timeout,
        stream=stream,
    )
