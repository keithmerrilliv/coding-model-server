"""routes/meta.py: what /health and /v1/models report, not just that they 200.

test_server_routes.py proves the routes are registered and answer. These pin
the fields an operator reads: whether a model is loaded, how many review gates
are waiting on a human (DEV-430), and that a broken task store degrades
/health to "unknown" instead of failing it. The app is driven through
TestClient without its context manager, so the lifespan (llama-server, GPU
sampler, MCP, ChromaDB) never starts; the two singletons meta reads are
replaced on the module.
"""
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from coding_model_autonomous.models import GateType
from coding_model_server.config import Config
from coding_model_server.routes import meta
from coding_model_server.server import app

client = TestClient(app)


@pytest.fixture
def manager(monkeypatch):
    mgr = mock.Mock()
    mgr.is_running.return_value = False
    monkeypatch.setattr(meta, "llama_server_manager", mgr)
    return mgr


@pytest.fixture
def store(db, monkeypatch):
    monkeypatch.setattr(meta, "get_autonomous_db", lambda: db)
    return db


def _open_gate(db):
    spec = db.create_spec(title="demo", source_md_path="spec.md")
    return db.create_gate(spec_id=spec.id, gate_type=GateType.CODE_REVIEW,
                          prompt_md="## review")


# ── /health ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("running", [True, False])
def test_health_reports_whether_a_model_is_loaded(manager, store, running):
    manager.is_running.return_value = running
    body = client.get("/health").json()
    assert body["status"] == "healthy"
    assert body["model_loaded"] is running


def test_health_counts_only_gates_still_waiting(manager, store):
    _open_gate(store)
    answered = _open_gate(store)
    _open_gate(store)
    store.respond_to_gate(answered.id, "approved")

    assert client.get("/health").json()["open_review_gates"] == 2


def test_health_with_nothing_waiting_says_zero(manager, store):
    assert client.get("/health").json()["open_review_gates"] == 0


def test_an_unavailable_store_reads_as_unknown_not_as_an_outage(manager, monkeypatch):
    """None means 'could not ask', which is different from 0 waiting."""
    def broken():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(meta, "get_autonomous_db", broken)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"
    assert r.json()["open_review_gates"] is None


def test_health_lists_the_configured_agents(manager, store):
    assert client.get("/health").json()["agents"] == list(Config.AGENTS)


# ── /v1/models ───────────────────────────────────────────────────────────────

def test_models_is_the_openai_list_shape_of_the_roster():
    body = client.get("/v1/models").json()
    assert body["object"] == "list"
    assert [m["id"] for m in body["data"]] == list(Config.AGENTS)
    for m in body["data"]:
        assert m["object"] == "model"
        assert m["owned_by"] == "coding-model-server"
        assert m["description"] == Config.AGENTS[m["id"]]["description"]
        assert isinstance(m["created"], int)


def test_root_names_the_health_and_models_endpoints():
    endpoints = client.get("/").json()["endpoints"]
    assert endpoints["health"] == "/health"
    assert endpoints["models"] == "/v1/models"
