"""Shared server runtime: singletons, lifespan-managed services, and the auth
dependency.

Route modules import from here (not from ``server``) so they can reach the
process-wide singletons and the lifespan-initialized services without a circular
import back to the app module.

The long-lived singletons (``llama_server_manager``, ``chat_admission``) are
created at import. The services that depend on heavy startup (memory, web
search, Apple Deep Docs MCP) are created in the app lifespan and published onto
the mutable ``services`` holder — routes read ``services.memory`` etc. at
call-time, after lifespan has populated them.
"""
import hmac
import logging
import os
import threading
from typing import Optional

from fastapi import Header, HTTPException, Request

from coding_model_server.config import Config
from coding_model_server.llama_server import LlamaServerManager

logger = logging.getLogger(__name__)


# ── Auth dependency ──────────────────────────────────────────────────────────

# Keys shipped in config templates. A bare "non-empty" check accepts them, and
# they are public knowledge — treat them the same as no key at all.
_PLACEHOLDER_ADMIN_KEYS = frozenset({'your-secret-key-here'})

# Bind addresses that keep an unauthenticated server off the network.
_LOOPBACK_BIND_HOSTS = frozenset({'127.0.0.1', '::1', 'localhost'})

# Request sources accepted when running without a key. 'testclient' is the
# fixed scope address starlette's TestClient stamps on every request.
_LOOPBACK_CLIENT_HOSTS = frozenset({'127.0.0.1', '::1', 'testclient'})


def _unauth_explicitly_allowed() -> bool:
    return os.getenv('CODING_MODEL_ALLOW_UNAUTH', '').lower() in ('1', 'true', 'yes')


def enforce_admin_key_config() -> None:
    """Refuse to serve with a missing or placeholder admin key (DEV-127).

    Called from the app lifespan so it covers every launch path — systemd,
    ``python -m coding_model_server.server``, and a bare
    ``uvicorn coding_model_server.server:app`` — not just the ``__main__``
    block, which a direct uvicorn launch skips.

    Startup proceeds only with a real key, or with no key when
    ``CODING_MODEL_ALLOW_UNAUTH=1`` AND the bind is loopback. The autonomous
    spec endpoints execute code by design, so an unauthenticated non-loopback
    bind is remote code execution for anyone who can reach the port; the
    supported LAN pattern is loopback + SSH tunnel (or a firewalled port with
    a strong key), never 0.0.0.0 unauthenticated.
    """
    key = Config.ADMIN_API_KEY
    if key in _PLACEHOLDER_ADMIN_KEYS:
        raise RuntimeError(
            "ADMIN_API_KEY is the well-known .env.example placeholder, which is "
            "no protection at all. Generate a real key: "
            "python3 -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    if key:
        return
    if not _unauth_explicitly_allowed():
        raise RuntimeError(
            "ADMIN_API_KEY is not set. Every endpoint — including the autonomous "
            "spec queue, which executes code — would be unauthenticated. Set "
            "ADMIN_API_KEY in ~/.config/coding-model-server/.env, or set "
            "CODING_MODEL_ALLOW_UNAUTH=1 to run key-less on loopback only."
        )
    if Config.HOST not in _LOOPBACK_BIND_HOSTS:
        raise RuntimeError(
            f"CODING_MODEL_ALLOW_UNAUTH=1 with HOST={Config.HOST!r}: "
            "unauthenticated operation is loopback-only. Bind 127.0.0.1 and use "
            "an SSH tunnel for LAN access, or set ADMIN_API_KEY."
        )
    logger.warning(
        "Running UNAUTHENTICATED (CODING_MODEL_ALLOW_UNAUTH=1, loopback bind). "
        "Non-loopback clients are rejected per-request as defense in depth."
    )


async def verify_admin_key(
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Verify admin API key if ADMIN_API_KEY is configured.

    Accepts either the legacy `X-Admin-Key` header (used by the bundled
    `coding_model_client`) or the OpenAI-standard `Authorization: Bearer <key>`
    header (used by third-party clients like qwen-code, OpenAI SDKs, etc.).

    Uses hmac.compare_digest for timing-safe comparison to prevent
    key extraction via timing side-channel attacks.

    With no key configured (reachable only via the explicit
    CODING_MODEL_ALLOW_UNAUTH opt-in — see enforce_admin_key_config), requests
    are accepted from loopback sources only. Config.HOST cannot be trusted to
    reflect the real bind under a bare `uvicorn --host 0.0.0.0` launch, so this
    per-request source check is what actually holds the loopback-only promise.
    """
    if not Config.ADMIN_API_KEY:
        client_host = request.client.host if request.client else ''
        if client_host not in _LOOPBACK_CLIENT_HOSTS:
            raise HTTPException(
                status_code=401,
                detail="Server runs without an admin key; only loopback clients are accepted.",
            )
        return
    candidate = x_admin_key
    if not candidate and authorization:
        scheme, _, token = authorization.partition(' ')
        if scheme.lower() == 'bearer' and token:
            candidate = token.strip()
    if not candidate or not hmac.compare_digest(candidate, Config.ADMIN_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing admin API key")


# ── Inference subprocess manager (long-lived singleton) ──────────────────────

llama_server_manager = LlamaServerManager()


# ── Chat admission control ───────────────────────────────────────────────────

class AdmissionController:
    """Bounded counter for concurrent chat completions.

    Caps total in-flight + queued requests. Beyond ``max_total`` the next
    call is rejected with 503+Retry-After instead of piling up behind
    ``LlamaServerManager.lock`` — under a retry storm that lock would
    otherwise queue dozens of waiting threads, each holding open an SSE
    connection until they get served, exhausting sockets and the
    autonomous orchestrator's patience.

    The lock here is a threading.Lock (not asyncio.Lock) so ``release`` is
    safely callable from a streaming-response generator's finally block,
    which runs in the response thread, not the event loop.
    """

    def __init__(self, max_total: int):
        self._max = max_total
        self._lock = threading.Lock()
        self._count = 0

    @property
    def in_flight(self) -> int:
        return self._count

    @property
    def max_inflight(self) -> int:
        return self._max

    def admit_or_503(self, *, retry_after_s: int = 5) -> None:
        with self._lock:
            if self._count >= self._max:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"server busy ({self._count} concurrent chat "
                        f"completions, max {self._max})"
                    ),
                    headers={"Retry-After": str(retry_after_s)},
                )
            self._count += 1

    def release(self) -> None:
        with self._lock:
            if self._count > 0:
                self._count -= 1


CHAT_MAX_INFLIGHT = int(os.getenv("CODING_MODEL_CHAT_MAX_INFLIGHT", "5"))
chat_admission = AdmissionController(CHAT_MAX_INFLIGHT)


# ── Lifespan-managed services holder ─────────────────────────────────────────

class _Services:
    """Mutable holder for services initialized in the app lifespan.

    Routes access these at call-time (``runtime.services.memory``), so they see
    whatever lifespan published rather than capturing ``None`` at import.
    """
    memory = None          # MemoryService
    web_search = None       # WebSearchService
    apple_deep_docs = None  # AppleDeepDocsService


services = _Services()


# ── Autonomous task store (lazy singleton) ───────────────────────────────────

_autonomous_db = None


def get_autonomous_db():
    """Return the process-wide autonomous Database, creating it on first use."""
    global _autonomous_db
    if _autonomous_db is None:
        from coding_model_autonomous import Database as _AutonomousDatabase
        _autonomous_db = _AutonomousDatabase()
        logger.info("Autonomous task store initialized at %s",
                    _autonomous_db.db_path)
    return _autonomous_db
