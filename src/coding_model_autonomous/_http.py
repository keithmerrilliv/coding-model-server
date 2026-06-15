"""Shared HTTP access to the coding-model-server inference API for autonomous agents.

planner, supervisor, and executor each POST to the same local
``/v1/chat/completions`` endpoint with the same host/port/admin-key handling.
This centralises the session, the URL, the auth header, and an optional
transient-5xx retry so those three stop duplicating it.

Server-side counterpart to ``coding_model_client/http.py`` — kept separate on purpose:
this package talks to the *local* server (``CODING_MODEL_SERVER_IP``/``CODING_MODEL_SERVER_PORT``,
``skip_memory`` defaulted on for structured prompts), and must not depend on the
client package.

The helper returns the raw :class:`requests.Response`; callers own status
checking and body parsing so each keeps its existing error semantics.
"""
import logging
import os
import time

import requests

CODING_MODEL_SERVER_HOST = os.getenv("CODING_MODEL_SERVER_IP", "127.0.0.1")
CODING_MODEL_SERVER_PORT = int(os.getenv("CODING_MODEL_SERVER_PORT", "5000"))
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

API_URL = f"http://{CODING_MODEL_SERVER_HOST}:{CODING_MODEL_SERVER_PORT}/v1/chat/completions"

# Module-level session: reuses TCP+TLS across the architect → implementer →
# reviewer → supervisor sequence, saving 5-30 ms per call.
_SESSION = requests.Session()

logger = logging.getLogger("orchestrator.http")

# Transient-5xx backoff schedule (seconds) for retry_5xx=True. Model-swap CUDA
# OOM is the dominant cause of 500s here (VRAM not fully released between
# models — see project_model_swap_oom): the next process can't allocate its
# compute buffer until the kernel reaps the previous CUDA context. Generous
# because VRAM release on Blackwell can take 10-30s after a llama-server exits.
_BACKOFFS = (10.0, 30.0, 60.0)


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if ADMIN_API_KEY:
        headers["X-Admin-Key"] = ADMIN_API_KEY
    return headers


def post_chat_completion(model, messages, *, timeout, skip_memory=True,
                         retry_5xx=False, **params):
    """POST a chat completion to the local coding-model-server; return raw Response.

    Args:
        model: Agent/model name routed by the server.
        messages: OpenAI-style message list.
        timeout: Per-request timeout in seconds (callers' values vary widely).
        skip_memory: Opt out of server-side RAG injection (default True — chat
            memory is noise for autonomous structured prompts).
        retry_5xx: If True, retry transient 5xx responses on the _BACKOFFS
            schedule (used by call_agent to survive model-swap OOMs).
        **params: Extra payload fields (temperature, max_tokens, tools, ...).
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "skip_memory": skip_memory,
        **params,
    }
    headers = _headers()

    if not retry_5xx:
        return _SESSION.post(API_URL, json=payload, headers=headers, timeout=timeout)

    resp = None
    for attempt, delay in enumerate((0.0,) + _BACKOFFS):
        if delay:
            logger.info("post_chat_completion: retrying after %.0fs (attempt %d/%d)",
                        delay, attempt + 1, len(_BACKOFFS) + 1)
            time.sleep(delay)
        resp = _SESSION.post(API_URL, json=payload, headers=headers, timeout=timeout)
        if resp.status_code < 500 or attempt == len(_BACKOFFS):
            break
        logger.warning(
            "post_chat_completion: %d from server (attempt %d/%d, body=%s)",
            resp.status_code, attempt + 1, len(_BACKOFFS) + 1,
            resp.text[:200].replace("\n", " "),
        )
    return resp
