"""Shared HTTP access to the coding-model-server inference API for autonomous agents.

planner and executor each POST to the same local
``/v1/chat/completions`` endpoint with the same host/port/admin-key handling.
This centralises the session, the URL, the auth header, and an optional
transient-5xx retry so those three stop duplicating it.

Server-side counterpart to ``coding_model_client/http.py`` — kept separate on purpose:
this package talks to the *local* server over loopback (``skip_memory`` defaulted
on for structured prompts), and must not depend on the client package.

The helper returns the raw :class:`requests.Response`; callers own status
checking and body parsing so each keeps its existing error semantics.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import requests

from . import settings
from .settings import retrieval_decision, role_to_agent

# The orchestrator (planner/executor) always runs on the SAME box as
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
# reviewer sequence, saving 5-30 ms per call.
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


def is_reasoning_only(resp) -> bool:
    """A 502 from DEV-617's guard: tokens spent, nothing visible (DEV-760).

    Matched on the server's own wording, because every other 502 here
    ("upstream inference error", a crashed child) is transient and must keep
    its backoff."""
    return (getattr(resp, "status_code", None) == 502
            and "reasoning-only response" in (getattr(resp, "text", "") or ""))


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
        if is_reasoning_only(resp):
            # DEV-760: the model spent its whole budget thinking. That is
            # deterministic, not transient — run 46 re-sent the identical call
            # three more times and got the identical 502 each time, 40 minutes
            # of GPU for nothing. Hand it back at once so the caller's own
            # retry, which can change the budget or the prompt, decides.
            logger.warning(
                "post_chat_completion: reasoning-only 502 is budget exhaustion, "
                "not a transient failure — not re-sending the same request: %s",
                resp.text[:200].replace("\n", " "))
            break
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


# ── Agent calling ────────────────────────────────────────────────────────────

def call_agent(
    role: str,
    messages: list[dict[str, str]],
    *,
    agent: str | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
    meta: Optional[dict] = None,
    memory_query: str | None = None,
    language: str | None = None,
) -> str:
    """Call an agent via the coding-model-server inference API.

    Returns the raw content string from the model's response.
    Raises on transport or HTTP errors so the caller can decide
    whether to retry or fail.

    If *meta* is provided, it is populated with response metadata the caller
    can inspect after the call:
      - ``meta["agent"]``: the resolved model name, so the caller can attribute
        an event to it without re-deriving the role→agent mapping.
      - ``meta["duration_ms"]``: wall-clock for the call, retry backoff included.
      - ``meta["prompt_tokens"]`` / ``["completion_tokens"]`` / ``["total_tokens"]``:
        usage as reported by the server; absent when it reported none.
      - ``meta["finish_reason"]``: the model's stop reason ("stop", "length", …)
      - ``meta["truncated"]``: True when finish_reason == "length", i.e. the
        output hit ``max_tokens`` and was cut off mid-stream. The implementer
        path uses this to emit an OUTPUT_TRUNCATED event instead of letting a
        half-written file surface as a bogus reviewer "missing file" FAIL.

    ``agent_event_fields(meta)`` packages the first four for an event payload.

    *memory_query* is what RAG retrieves on, for roles that opted in. Without
    it the server embeds the last user message, which for these prompts leads
    with the entire spec and design — and the embedding model truncates at 256
    tokens, so the actual ask at the end never reaches retrieval (DEV-489).
    Ignored when the role has not opted into memory.
    """
    agent = agent or role_to_agent(role)
    max_tokens = max_tokens or settings.ROLE_TO_MAX_TOKENS.get(role, 8000)
    timeout = timeout or settings.ROLE_TO_TIMEOUT.get(role, 1800)

    # RAG opt-in: the role AND the spec's language (DEV-657 part 1). Either
    # one alone was never the right condition — a corpus is about a subject,
    # and a role is not a subject.
    use_memory, rag_reason = retrieval_decision(role, language)

    logger.info("calling agent=%s, role=%s, msg_count=%d, max_tokens=%d, "
                "rag=%s (%s, language=%s)", agent, role, len(messages),
                max_tokens, "on" if use_memory else "off", rag_reason,
                language or "unknown")
    if rag_reason == "language_unknown" and role.lower() in settings.AUTONOMOUS_MEMORY_ROLES:
        # A role that opted in and got nothing is worth a line above INFO: it
        # is the one way this gate could silently switch retrieval off for
        # everything and look like a quiet success.
        logger.warning("role %s opted into retrieval but the spec's language "
                       "is unknown — RAG skipped (DEV-657)", role)

    # retry_5xx=True: model-swap CUDA OOM is the dominant cause of 500s here
    # (VRAM not fully released between models — see project_model_swap_oom.md):
    # the next process can't allocate its compute buffer until the kernel
    # reaps the previous CUDA context. Without this retry a single transient
    # swap-OOM cancels the whole spec (_start_task catches Exception → task
    # failed → spec FAILED, no rotation). The generous backoff schedule lives
    # in _http._BACKOFFS (Blackwell VRAM release takes 10-30s after exit).
    # Only sent when the role actually retrieves, so the payload doesn't carry
    # a query the server would ignore.
    extra = {}
    if use_memory and memory_query:
        extra["memory_query"] = memory_query
    # Wall-clock over the whole call, backoff included (DEV-528). retry_5xx
    # means a model-swap OOM can spend 10-30s sleeping before the request that
    # finally succeeds, and that is real time the spec sat waiting — so it
    # belongs in the figure a "cost per attempt" query reads. Measured here
    # rather than at the ~20 event call sites so every agent is timed the same
    # way; monotonic because the box runs NTP and a step would give a negative.
    t0 = time.monotonic()
    resp = post_chat_completion(
        agent, messages, timeout=timeout, max_tokens=max_tokens,
        temperature=0.2, retry_5xx=True, skip_memory=not use_memory,
        **extra,
    )
    duration_ms = int((time.monotonic() - t0) * 1000)
    resp.raise_for_status()
    data = resp.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(
            f"Server response missing choices/content: {e}"
        ) from e

    if meta is not None:
        meta["agent"] = agent  # resolved model — lets callers attribute events
        meta["duration_ms"] = duration_ms
        # The server returns OpenAI-style `usage` on every non-streaming
        # completion (streaming.build_completion_response). llama_server
        # substitutes zeros when a backend omits it, so a zero total means
        # "not reported" rather than a free call — record nothing in that
        # case, because a missing number and a genuine 0 must not read alike.
        usage = data.get("usage") or {}
        if usage.get("total_tokens"):
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                val = usage.get(key)
                if isinstance(val, int) and val >= 0:
                    meta[key] = val
        # DEV-760: reasoning versus visible output, measured by the server.
        for key in ("reasoning_chars", "visible_chars"):
            val = usage.get(key)
            if isinstance(val, int) and val >= 0:
                meta[key] = val
        try:
            fr = (data["choices"][0].get("finish_reason") or "").lower()
        except (KeyError, IndexError, AttributeError):
            fr = ""
        meta["finish_reason"] = fr or None
        meta["truncated"] = fr == "length"
        # DEV-657 part 2: the server reports what retrieval did for this call.
        # Absent on a server that predates it, which must stay distinguishable
        # from a recorded "skipped".
        #
        # DEV-657 part 1 adds `gate`: the server can say retrieval was skipped
        # but not WHY, because the language decision is made here. Without it
        # "skipped" pools the role opt-out, the language gate and an unreadable
        # plan into one value, and the part 3 A/B cannot tell which arm a call
        # was actually in. The synthesised branch does not blur the old
        # distinction — `not_requested` is a new value that no old server ever
        # wrote, so absence still means "server predates this".
        rag = data.get("rag")
        if isinstance(rag, dict) and rag:
            meta["rag"] = {**rag, "gate": rag_reason}
        elif not use_memory:
            meta["rag"] = {"outcome": "not_requested", "gate": rag_reason}
        if meta["truncated"]:
            logger.warning(
                "agent=%s role=%s OUTPUT TRUNCATED (finish_reason=length) at "
                "max_tokens=%d — response was cut off mid-stream",
                agent, role, max_tokens,
            )

    return content


def agent_event_fields(meta: Optional[dict]) -> dict:
    """Telemetry subset of a ``call_agent`` *meta*, for an AGENT_RAN payload.

    Every AGENT_RAN event that records a real model call routes its
    attribution through here, so the fields are spelled identically at all
    of them (DEV-528). A query that groups by agent cannot work when one
    site writes ``agent`` and another writes ``model``, which is how the
    adversarial path drifted.

    Keys absent from *meta* are omitted rather than stored as None: an event
    predating this change and one whose usage went unreported should look the
    same to a reader, and neither should be mistaken for a measured zero.
    """
    if not meta:
        return {}
    out = {
        key: meta[key]
        for key in ("agent", "duration_ms", "prompt_tokens",
                    "completion_tokens", "total_tokens", "calls",
                    "max_call_prompt_tokens", "budget_needed_tokens",
                    "reasoning_chars", "visible_chars")
        if meta.get(key) is not None
    }
    # Carried because it changes how an attempt reads: a truncated response is
    # a budget failure, not a model failure, and the two should not pool.
    if meta.get("truncated"):
        out["truncated"] = True
    # DEV-691: `truncated` alone cannot tell "the model stopped early" from
    # "the call died" — both arrive as a parse failure with truncated absent.
    # Run 39's architect ended its turn at 5,361 and 3,352 completion tokens
    # against a 10,000 budget; without finish_reason the record could not say
    # that was a premature stop rather than a budget overrun, and the retry
    # pulled the wrong lever. call_agent already computes it; carry it.
    if meta.get("finish_reason"):
        out["finish_reason"] = meta["finish_reason"]
    # DEV-657 part 2: retrieval outcome, so "how useful is RAG" is a query
    # over events rather than a 50-entry ring that every restart erases.
    rag = meta.get("rag")
    if isinstance(rag, dict) and rag:
        out["rag"] = rag
    return out


def accumulate_agent_fields(tally: dict, meta: Optional[dict]) -> dict:
    """Fold one ``call_agent`` *meta* into a running *tally*, and return it.

    Manifest mode builds one implementer attempt out of 1 + N model calls —
    the manifest, then a file at a time. DEV-528 asks for cost "per attempt",
    so those sum: the question "what did this attempt cost" has one answer,
    not thirty.

    The tally uses the same key names as a *meta*, so ``agent_event_fields``
    renders either. ``calls`` is kept alongside because a 3-call attempt and a
    30-call one that happen to total the same are not the same event, and the
    mean per call is only recoverable if the count survives.
    """
    if not meta:
        return tally
    # Last writer wins. Within one attempt every call uses the same agent —
    # rotation happens between attempts, not inside one — so this is stable;
    # it is a fallback for the case where an early call reported nothing.
    if meta.get("agent"):
        tally["agent"] = meta["agent"]
    for key in ("duration_ms", "prompt_tokens", "completion_tokens",
                "total_tokens"):
        val = meta.get(key)
        if val is not None:
            tally[key] = tally.get(key, 0) + val
    tally["calls"] = tally.get("calls", 0) + 1
    # DEV-823: the SUM answers "what did this attempt cost"; it does not answer
    # "will the next prompt fit a window", which is a per-call question. Run
    # 61's fit check read 71,399 summed over five calls as one prompt, ruled
    # out every 64K agent, and sent an ~18K-token retry to deep_implementer.
    if meta.get("prompt_tokens") is not None:
        tally["max_call_prompt_tokens"] = max(
            tally.get("max_call_prompt_tokens", 0), meta["prompt_tokens"])
    # Any truncated call taints the attempt: the file it was writing is
    # half-finished regardless of how the remaining calls went.
    if meta.get("truncated"):
        tally["truncated"] = True
    # DEV-657 part 2, completing it: retrieval outcomes were carried from a
    # single `meta` but dropped here, so every role whose event is built from
    # a tally recorded nothing. That is the IMPLEMENTER — 22 implementer
    # calls since part 2 landed, zero retrieval records, while the architect
    # scored 15 of 15. Retrieval had been running the whole time; only the
    # record was missing, and the missing one is the role part 3 exists to
    # measure.
    #
    # Counted rather than collapsed to one value: an attempt is 1 + N calls
    # in manifest mode and they do not share an outcome. "3 injected, 2 empty"
    # is the truth; any single outcome for the attempt would be invented.
    rag = meta.get("rag")
    if isinstance(rag, dict) and rag:
        acc = tally.setdefault("rag", {})
        acc["calls"] = acc.get("calls", 0) + 1
        outcomes = acc.setdefault("outcomes", {})
        outcome = str(rag.get("outcome") or "unknown")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if isinstance(rag.get("hits"), int):
            acc["hits"] = acc.get("hits", 0) + rag["hits"]
        # DEV-834: which documents, and the closest one, across the attempt.
        if isinstance(rag.get("best_distance"), (int, float)):
            prior = acc.get("best_distance")
            acc["best_distance"] = (rag["best_distance"] if prior is None
                                    else min(prior, rag["best_distance"]))
        if isinstance(rag.get("sources"), list):
            kept = acc.setdefault("sources", [])
            for src in rag["sources"]:
                if src not in kept and len(kept) < 20:
                    kept.append(src)
        if rag.get("gate"):
            gates = acc.setdefault("gates", {})
            gates[rag["gate"]] = gates.get(rag["gate"], 0) + 1
    return tally
