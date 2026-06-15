#!/usr/bin/env python3
"""
Coding Model Multi-Agent Server (FastAPI)

OpenAI-compatible API for multi-agent LLM inference. This module is the app
assembly point: logging setup, lifespan, middleware, the exception handler, and
router registration. The actual endpoints live in coding_model_server.routes.*; shared
singletons and lifespan-managed services live in coding_model_server.runtime.
"""
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from coding_model_server import runtime
from coding_model_server.config import Config
from coding_model_server.logging_filters import (
    RoutePollAccessLines, build_poll_access_logger,
)
from coding_model_server.mcp_service import AppleDeepDocsService
from coding_model_server.memory_service import MemoryService
from coding_model_server.metrics import gpu_sampler, request_metrics
from coding_model_server.routes import admin, autonomous, chat, meta, memory
from coding_model_server.web_search_service import WebSearchService

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================================
# Lifespan
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager: install log routing, start the GPU sampler, and
    initialize the heavy services onto runtime.services for routes to use."""
    # Install access-log routing here, after uvicorn's dictConfig has run.
    # See RoutePollAccessLines docstring for why module-load doesn't work.
    # Strip any prior copies first — addFilter doesn't dedupe, so a lifespan
    # re-run (test harness, hot reload) would otherwise stack filters.
    access_logger = logging.getLogger("uvicorn.access")
    for existing in list(access_logger.filters):
        if isinstance(existing, RoutePollAccessLines):
            access_logger.removeFilter(existing)
    access_logger.addFilter(RoutePollAccessLines(build_poll_access_logger()))

    # Background nvidia-smi sampler for the dashboard's GPU panel. Polls at
    # 1 Hz regardless of how many dashboard tabs are watching. No-op without
    # nvidia-smi.
    gpu_sampler.start()

    logger.info("Server starting up...")

    try:
        logger.info("Initializing Memory Service...")
        runtime.services.memory = MemoryService()
        logger.info("Memory Service initialized successfully")
    except Exception as e:
        logger.error("Failed to initialize memory service: %s", e)
        runtime.services.memory = None

    try:
        logger.info("Initializing Web Search Service...")
        runtime.services.web_search = WebSearchService()
        logger.info("Web Search Service initialized successfully")
    except Exception as e:
        logger.error("Failed to initialize web search service: %s", e)
        runtime.services.web_search = None

    try:
        logger.info("Initializing Apple Deep Docs Service...")
        mcp_path = os.path.join(os.getcwd(), "tools/appledeepdoc-mcp")
        svc = AppleDeepDocsService(mcp_path)
        if svc.start():
            runtime.services.apple_deep_docs = svc
            logger.info("Apple Deep Docs Service initialized successfully")
        else:
            logger.error("Apple Deep Docs Service failed to start")
            runtime.services.apple_deep_docs = None
    except Exception as e:
        logger.error("Failed to initialize Apple Deep Docs Service: %s", e)
        runtime.services.apple_deep_docs = None

    yield

    logger.info("Server shutting down - unloading models...")
    gpu_sampler.stop()
    runtime.llama_server_manager.shutdown()
    if runtime.services.apple_deep_docs:
        runtime.services.apple_deep_docs.stop()
    logger.info("Server shutdown complete.")


# ============================================================================
# FastAPI Application
# ============================================================================

app = FastAPI(
    title="Coding Model Multi-Agent Server",
    description="OpenAI-compatible API for Coding Model LLM with multi-agent support",
    version="2.0",
    lifespan=lifespan
)

_cors_origins_raw = os.getenv("CORS_ORIGINS", "localhost,127.0.0.1").split(",")
_cors_origins = []
for origin in _cors_origins_raw:
    origin = origin.strip()
    if not origin:
        continue
    if origin == "*":
        _cors_origins = ["*"]
        break
    # Ensure origins have a scheme — bare hostnames don't match in CORS
    if not origin.startswith("http"):
        _cors_origins.append(f"http://{origin}")
    else:
        _cors_origins.append(origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials="*" not in _cors_origins,
    # Restricted method/header allowlist: with allow_credentials=True a
    # browser-loaded malicious site can submit cross-origin POST with the
    # user's bearer if we wildcard headers. Pin to what the dashboard +
    # OpenAI-compatible clients actually use.
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["X-Admin-Key", "Authorization", "Content-Type", "Accept"],
)


# ============================================================================
# Middleware + exception handler
# ============================================================================

# Skip recording the metrics endpoints themselves: the dashboard polls them
# at 1 Hz, which would otherwise dominate the chart. Also skip CORS preflights.
_METRICS_SKIP_PATHS = (
    "/v1/admin/metrics",
    "/v1/admin/gpu_stats",
    "/v1/admin/active_model",
)

_REQ_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _assign_req_id(request: Request) -> str:
    """Echo a client-supplied X-Request-ID if it's safe; otherwise generate.

    The validation regex caps length and rejects exotic chars so a hostile
    client can't smuggle log-injection payloads through what becomes a log
    prefix on every line for this request. Generated form: ``req_<8 hex>``.
    """
    incoming = request.headers.get("x-request-id", "")
    if incoming and _REQ_ID_RE.match(incoming):
        return incoming
    return f"req_{uuid.uuid4().hex[:8]}"


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    # Correlation ID is set on every request, even ones we skip metrics
    # for, so the response always echoes X-Request-ID and downstream
    # handlers can include it in their logs.
    req_id = _assign_req_id(request)
    request.state.req_id = req_id

    if request.method == "OPTIONS":
        response = await call_next(request)
        response.headers["X-Request-ID"] = req_id
        return response
    path = request.url.path
    if any(path.startswith(p) for p in _METRICS_SKIP_PATHS):
        response = await call_next(request)
        response.headers["X-Request-ID"] = req_id
        return response
    start = time.perf_counter()
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception:
        # Record the failure as a 500 before re-raising so it shows up on the
        # error sparkline. The exception handler downstream produces the body.
        duration_ms = (time.perf_counter() - start) * 1000.0
        route = request.scope.get("route")
        route_path = getattr(route, "path", path) if route else path
        subkey = getattr(request.state, "metric_subkey", None)
        category = getattr(request.state, "error_category", None)
        request_metrics.record(
            request.method, route_path, subkey, duration_ms, 500, category
        )
        raise
    duration_ms = (time.perf_counter() - start) * 1000.0
    # Record the matched route template (e.g. /v1/autonomous/specs/{spec_id})
    # rather than the literal path — keeps cardinality bounded. 404s have no
    # matched route; fall back to the literal path so they still show.
    route = request.scope.get("route")
    route_path = getattr(route, "path", path) if route else path
    subkey = getattr(request.state, "metric_subkey", None)
    category = getattr(request.state, "error_category", None)
    request_metrics.record(
        request.method, route_path, subkey, duration_ms, status, category
    )
    response.headers["X-Request-ID"] = req_id
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Custom HTTP exception handler for OpenAI-compatible error format"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.detail,
                "type": "invalid_request_error" if exc.status_code < 500 else "server_error",
                "code": exc.status_code
            }
        }
    )


# ============================================================================
# Routes
# ============================================================================

app.include_router(meta.router)
app.include_router(memory.router)
app.include_router(autonomous.router)
app.include_router(admin.router)
app.include_router(chat.router)


# ============================================================================
# Entrypoint
# ============================================================================

if __name__ == "__main__":
    import sys
    import uvicorn

    # Refuse to start with no admin key unless explicitly opted out.
    # Empty ADMIN_API_KEY previously left every admin endpoint open — combined
    # with HOST=0.0.0.0 that meant a default install was fully unauthenticated.
    if not Config.ADMIN_API_KEY and os.getenv('CODING_MODEL_ALLOW_UNAUTH', '').lower() not in ('1', 'true', 'yes'):
        logger.error(
            "ADMIN_API_KEY is not set. All admin endpoints would be unauthenticated. "
            "Set ADMIN_API_KEY in ~/.config/coding-model-server/.env, or set "
            "CODING_MODEL_ALLOW_UNAUTH=1 to explicitly permit unauthenticated operation."
        )
        sys.exit(1)

    # Validate configuration
    errors = Config.validate()
    if errors:
        for error in errors:
            logger.error(error)
        # Don't exit — allow the server to start even if some models are
        # missing; their endpoints just fail for those specific models.
        logger.warning("Starting server with configuration errors...")

    uvicorn.run(
        "coding_model_server.server:app",
        host=Config.HOST,
        port=Config.PORT,
        log_level="info",
        reload=False,
        loop="asyncio"  # Force standard asyncio loop for stability
    )
