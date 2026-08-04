"""POST /v1/memory/delete — the collection is no longer append-only.

Until this existed the only way to remove anything was to archive the whole
store and rebuild it, which is literally what removing one test row cost on
2026-08-03. It also left the provenance work half-finished: chunks record their
framework and source URL, but nothing could act on that to refresh one
framework at a time.

The guard tests matter most. chromadb treats an empty filter as "match all",
so a caller who meant to pass a filter and passed {} would wipe 23k chunks —
the exact failure this endpoint exists to make recoverable.
"""
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from coding_model_server import runtime
from coding_model_server.schemas import MemoryDeleteRequest
from coding_model_server.server import app

client = TestClient(app)  # no context form: lifespan stays off
H = {"X-Admin-Key": "test"}


@pytest.fixture
def fake_memory():
    m = mock.Mock()
    m.delete_memories.return_value = {"status": "success", "deleted": 3, "remaining": 100}
    with mock.patch.object(runtime.services, "memory", m):
        yield m


class TestRoute:
    def test_delete_by_ids(self, fake_memory):
        r = client.post("/v1/memory/delete", json={"ids": ["a", "b"]}, headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] == 3
        assert fake_memory.delete_memories.call_args.kwargs["ids"] == ["a", "b"]

    def test_delete_by_framework_filter(self, fake_memory):
        """The per-framework refresh this was built for."""
        r = client.post("/v1/memory/delete",
                        json={"where": {"framework": "Metal"}}, headers=H)
        assert r.status_code == 200, r.text
        assert fake_memory.delete_memories.call_args.kwargs["where"] == {"framework": "Metal"}

    def test_unfiltered_delete_is_a_400_not_a_500(self, fake_memory):
        """A refusal is the caller's mistake, so it must not read as a server fault."""
        fake_memory.delete_memories.return_value = {
            "error": "refusing to delete with no ids and no filter; pass allow_delete_all"}
        r = client.post("/v1/memory/delete", json={}, headers=H)
        assert r.status_code == 400, r.text

    def test_backend_failure_is_a_500(self, fake_memory):
        fake_memory.delete_memories.return_value = {"error": "chroma exploded"}
        r = client.post("/v1/memory/delete", json={}, headers=H)
        assert r.status_code == 500, r.text

    def test_route_is_registered(self):
        # Reuse the suite's flattener: since fastapi 0.13x, include_router()
        # leaves routes inside an _IncludedRouter wrapper with no .path.
        from test_server_routes import _registered_paths
        assert "/v1/memory/delete" in _registered_paths(app.routes)


class TestServiceGuard:
    """Exercise the real MemoryService method with a stub collection."""

    def _svc(self, count_before=10, count_after=7):
        from coding_model_server.memory_service import MemoryService
        svc = MemoryService.__new__(MemoryService)      # bypass __init__/Chroma
        col = mock.Mock()
        col.count.side_effect = [count_before, count_after]
        svc._collection = col
        return svc, col

    def test_no_ids_no_filter_is_refused(self):
        svc, col = self._svc()
        out = svc.delete_memories()
        assert "error" in out and "refusing" in out["error"]
        col.delete.assert_not_called()

    def test_empty_dict_filter_is_still_refused(self):
        """{} is falsy AND means match-all in chromadb — the dangerous case."""
        svc, col = self._svc()
        out = svc.delete_memories(where={})
        assert "error" in out
        col.delete.assert_not_called()

    def test_allow_delete_all_permits_the_wipe(self):
        svc, col = self._svc(10, 0)
        out = svc.delete_memories(allow_delete_all=True)
        assert out["deleted"] == 10
        # No ids/where passed through: chromadb rejects an empty where dict.
        assert col.delete.call_args.kwargs == {}

    def test_empty_where_is_not_forwarded_to_chroma(self):
        svc, col = self._svc(10, 5)
        svc.delete_memories(ids=["x"], where={})
        assert "where" not in col.delete.call_args.kwargs

    def test_counts_are_derived_from_the_collection(self):
        svc, col = self._svc(100, 91)
        out = svc.delete_memories(where={"framework": "Metal"})
        assert out == {"status": "success", "deleted": 9, "remaining": 91}


class TestSchema:
    def test_defaults(self):
        req = MemoryDeleteRequest()
        assert req.ids is None and req.where is None
        assert req.allow_delete_all is False
