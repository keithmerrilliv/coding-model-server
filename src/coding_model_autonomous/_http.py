"""Shared HTTP access to the coding-model-server inference API for autonomous agents.

planner, supervisor, and executor each POST to the same local
``/v1/chat/completions`` endpoint with the same host/port/admin-key handling.
This centralises the session, the URL, the auth header, and an optional
transient-5xx retry so those three stop duplicating it.

Server-side counterpart to ``coding_model_client/http.py`` — kept separate on purpose:
this package talks to the *local* server over loopback (``skip_memory`` defaulted
on for structured prompts), and must not depend on the client package.

The helper returns the raw :class:`requests.Response`; callers own status
checking and body parsing so each keeps its existing error semantics.
"""
import logging
import os
import time

import requests

# The orchestrator (planner/supervisor/executor) always runs on the SAME box as
# the inference server, so it reaches it over loopback. This is deliberately
# decoupled from CODING_MODEL_SERVER_IP: that var is the server's *externally
# advertised* LAN address (remote clients, CORS, dashboard) and moves whenever the
# box's DHCP lease or network changes. Binding internal calls to it once pointed
# the orchestrator at a dead LAN IP after a network move ("No route to host").
# Loopback never moves. CODING_MODEL_INTERNAL_HOST is honoured only for unusual
# split-host topologies (orchestrator and server on different machines); the
# default is always loopback.
# `.strip() or ...` so an empty/whitespace override (e.g. CODING_MODEL_INTERNAL_HOST=
# in an env file) falls back to loopback instead of yielding a hostless URL.
CODING_MODEL_INTERNAL_HOST = os.getenv("CODING_MODEL_INTERNAL_HOST", "").strip() or "127.0.0.1"
CODING_MODEL_SERVER_PORT = int(os.getenv("CODING_MODEL_SERVER_PORT", "5000"))
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

API_URL = f"http://{CODING_MODEL_INTERNAL_HOST}:{CODING_MODEL_SERVER_PORT}/v1/chat/completions"

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
    n_attempts = len(_BACKOFFS) + 1
    for attempt, delay in enumerate((0.0,) + _BACKOFFS):
        if delay:
            logger.info("post_chat_completion: retrying after %.0fs (attempt %d/%d)",
                        delay, attempt + 1, n_attempts)
            time.sleep(delay)
        try:
            resp = _SESSION.post(API_URL, json=payload, headers=headers, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            # A transport failure is the same transient class as a 5xx and must
            # back off identically — not raise straight out unretried. The
            # dominant cause is the redeploy race: scripts/redeploy.sh restarts
            # the server before the orchestrator, so an in-flight call dies with
            # ConnectionError while the child is down for a second or two (or a
            # read timeout while requests queue behind a slow model swap). The
            # callers turn any escaping exception into spec FAILED, discarding
            # approved work, so we retry here first and only re-raise once the
            # backoff schedule is exhausted.
            if attempt == len(_BACKOFFS):
                raise
            logger.warning(
                "post_chat_completion: %s (attempt %d/%d) — retrying",
                type(e).__name__, attempt + 1, n_attempts,
            )
            continue
        if resp.status_code < 500 or attempt == len(_BACKOFFS):
            break
        logger.warning(
            "post_chat_completion: %d from server (attempt %d/%d, body=%s)",
            resp.status_code, attempt + 1, n_attempts,
            resp.text[:200].replace("\n", " "),
        )
    return resp
