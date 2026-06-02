"""Smoke tests for the FastAPI app's route surface.

These exist as a regression guard for the server.py module split: they assert
the app still exposes the same routes and that the side-effect-free endpoints
(/health, /v1/models, and the OpenAPI schema) keep working after routes move
into separate modules. They deliberately avoid endpoints that need a live model
or the lifespan-initialized services.

TestClient is used WITHOUT its context-manager form so the lifespan (GPU
sampler, MCP subprocess, ChromaDB) does not run — we only exercise pure-read
routes plus the route table itself.
"""
from fastapi.testclient import TestClient

from qwen_server.server import app

client = TestClient(app)


# The full set of registered paths. If the split drops or renames a route,
# this snapshot catches it.
EXPECTED_PATHS = {
    "/",
    "/health",
    "/v1/models",
    "/v1/chat/completions",
    "/v1/memory",
    "/v1/memory/search",
    "/v1/memory/ingest",
    "/v1/tools/search",
    "/v1/tools/apple_deep_docs",
    "/v1/files/upload",
    "/v1/autonomous/specs",
    "/v1/autonomous/specs/{spec_id}",
    "/v1/autonomous/specs/{spec_id}/events",
    "/v1/autonomous/gates",
    "/v1/autonomous/gates/{gate_id}",
    "/v1/autonomous/gates/{gate_id}/respond",
    "/v1/admin/metrics",
    "/v1/admin/gpu_stats",
    "/v1/admin/active_model",
}


def test_all_expected_routes_registered():
    registered = {r.path for r in app.routes}
    missing = EXPECTED_PATHS - registered
    assert not missing, f"routes missing after split: {missing}"


def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert "agents" in body and isinstance(body["agents"], list)


def test_list_models_returns_agents():
    r = client.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    ids = {m["id"] for m in body["data"]}
    # A couple of stable agents that should always be present.
    assert "implementer" in ids
    assert "reviewer" in ids


def test_root_metadata():
    r = client.get("/")
    assert r.status_code == 200


def test_openapi_schema_builds():
    # Exercises every route's signature/response model — a broken extraction
    # (bad type, dangling dependency) surfaces here.
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert r.json()["info"]["title"]


def test_lifespan_publishes_services_to_runtime():
    """Guard the runtime.services indirection introduced by the split.

    Routes read runtime.services.memory at call-time, populated by the app
    lifespan. This drives the full path: lifespan publishes a (stubbed)
    service, and a route reads it and returns its result. Heavy inits are
    mocked so no ChromaDB / MCP subprocess / GPU sampler is touched.
    """
    from unittest import mock

    import qwen_server.server as srv
    from qwen_server import runtime

    fake_mem = mock.Mock()
    fake_mem.search_memory.return_value = [{"text": "hit"}]
    with mock.patch.object(srv, "MemoryService", return_value=fake_mem), \
            mock.patch.object(srv, "WebSearchService", return_value=mock.Mock()), \
            mock.patch.object(srv, "AppleDeepDocsService") as ADD, \
            mock.patch.object(srv.gpu_sampler, "start"), \
            mock.patch.object(srv.gpu_sampler, "stop"), \
            mock.patch.object(runtime.llama_server_manager, "shutdown"):
        ADD.return_value.start.return_value = False
        with TestClient(srv.app) as lifespan_client:  # context form runs lifespan
            assert runtime.services.memory is fake_mem
            r = lifespan_client.post("/v1/memory/search", json={"query": "x"})
            assert r.status_code == 200
            assert r.json() == {"results": [{"text": "hit"}]}
