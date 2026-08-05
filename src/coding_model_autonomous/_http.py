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

# A 503 carrying Retry-After is not a failure — it is the server saying "wait,
# then ask again", and the wait has a known end. Two of them mean another spec
# is mid-generation and the model cannot be swapped yet; those generations run
# 10-20 minutes on this box, so a fixed 3-attempt budget is an order of
# magnitude too short and exhaustion is guaranteed rather than exceptional.
# spec_837b167f was cancelled that way after ~100s of waiting on a 20-minute
# planner pass, discarding an approved plan, an approved design and two human
# reviews (DEV-491).
#
# So server-directed waits get a deadline instead of an attempt count, bounded
# by the caller's own per-role timeout (architect 2700s, implementer 1800s) —
# the budget the task already has. They do not consume the _BACKOFFS budget,
# which stays reserved for genuine transient 5xx.
_BUSY_WAIT_CAP = float(os.getenv("AUTONOMOUS_BUSY_WAIT_CAP", "0")) or None


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
    next_delay = 0.0
    # Server-directed waits (503 + Retry-After) are bounded by wall clock, not
    # by attempts, so one spec's long generation can no longer cancel another
    # (DEV-491). Everything else keeps the fixed schedule.
    busy_budget = _BUSY_WAIT_CAP or timeout
    busy_deadline = time.monotonic() + busy_budget
    attempt = 0
    busy_waits = 0
    while attempt < n_attempts:
        if next_delay:
            logger.info("post_chat_completion: retrying after %.1fs (attempt %d/%d)",
                        next_delay, attempt + 1, n_attempts)
            time.sleep(next_delay)
        # Default for the NEXT round: the CUDA-OOM schedule this was written
        # for. A Retry-After below overrides it (DEV-137).
        next_delay = _BACKOFFS[attempt] if attempt < len(_BACKOFFS) else 0.0
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
            attempt += 1
            continue
        if resp.status_code < 500:
            break
        # Honor Retry-After, bounded (DEV-137). Every autonomous stage
        # transition is a model swap; the server's busy-swap 503 says
        # "Retry-After: 5" for a drain that clears in seconds, and sleeping
        # the 10/30/60 OOM schedule instead added tens of seconds of dead
        # air per spec. 500s without the header keep the generous schedule.
        retry_after = resp.headers.get("Retry-After")
        wait_s = None
        if retry_after:
            try:
                wait_s = min(max(float(retry_after), 1.0), 60.0)
            except (TypeError, ValueError):
                wait_s = None  # non-numeric header — keep the schedule delay

        # A 503 that names its own retry interval is a wait, not a failure: the
        # server is refusing *for now* to protect an in-flight stream, and will
        # accept once that finishes. Spending the 4-attempt failure budget on it
        # is what cancelled spec_837b167f (DEV-491), and honouring the 5s
        # interval on a fixed budget would have exhausted it ~6x FASTER. So
        # these wait against a deadline and never consume an attempt.
        if resp.status_code == 503 and wait_s is not None:
            remaining = busy_deadline - time.monotonic()
            if remaining <= 0:
                logger.error(
                    "post_chat_completion: server still busy after %.0fs "
                    "(the task's own budget) — giving up: %s",
                    busy_budget, resp.text[:160].replace("\n", " "),
                )
                break
            next_delay = min(wait_s, remaining)
            busy_waits += 1
            # Logged sparsely: at 5s intervals a 20-minute wait is ~240 rounds,
            # and a line each would bury everything else in the journal.
            if busy_waits == 1 or busy_waits % 12 == 0:
                logger.info(
                    "post_chat_completion: server busy, waiting %.1fs "
                    "(%d so far, %.0fs of %.0fs budget left): %s",
                    next_delay, busy_waits, remaining, busy_budget,
                    resp.text[:120].replace("\n", " "),
                )
            continue
        if attempt == len(_BACKOFFS):
            break
        if wait_s is not None:
            next_delay = wait_s
        logger.warning(
            "post_chat_completion: %d from server (attempt %d/%d, body=%s)",
            resp.status_code, attempt + 1, n_attempts,
            resp.text[:200].replace("\n", " "),
        )
        attempt += 1
    return resp
