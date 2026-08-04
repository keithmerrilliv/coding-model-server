"""POST /v1/memory must carry caller-supplied provenance through to storage.

Without this, every HTTP ingest fell through to add_memory's defaults, which
stamp source="manual". That is why ~89% of the 83k-chunk collection cannot be
attributed to a framework, URL or SDK version, and so cannot be selectively
refreshed — a doc scraper knows its real source, the default does not.
"""
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from coding_model_server import runtime
from coding_model_server.server import app

client = TestClient(app)  # no context form: lifespan (Chroma/MCP/GPU) stays off

DOC_META = {
    "source": "https://developer.apple.com/documentation/metal/mtldevice",
    "framework": "Metal",
    "symbol_kind": "protocol",
    "is_beta": False,
    "scraped_at": "2026-08-03",
}


@pytest.fixture
def fake_memory():
    m = mock.Mock()
    m.add_memory.return_value = {"status": "success", "id": "abc123"}
    m.add_memory_chunked.return_value = {"status": "success", "chunks": 3}
    with mock.patch.object(runtime.services, "memory", m):
        yield m


class TestMetadataPassthrough:
    def test_metadata_reaches_add_memory(self, fake_memory):
        r = client.post("/v1/memory", json={"text": "MTLDevice is...", "metadata": DOC_META},
                        headers={"X-Admin-Key": "test"})
        assert r.status_code == 200, r.text
        _, kwargs = fake_memory.add_memory.call_args
        assert kwargs["metadata"] == DOC_META, "provenance was dropped"

    def test_absent_metadata_is_none_not_missing(self, fake_memory):
        """Omitting metadata must still call add_memory(metadata=None) rather
        than blowing up or silently taking the chunked path."""
        r = client.post("/v1/memory", json={"text": "plain fact"},
                        headers={"X-Admin-Key": "test"})
        assert r.status_code == 200, r.text
        _, kwargs = fake_memory.add_memory.call_args
        assert kwargs.get("metadata") is None
        fake_memory.add_memory_chunked.assert_not_called()

    def test_source_still_routes_to_the_code_chunker(self, fake_memory):
        """`source` is a language hint for tree-sitter, NOT provenance. Prose
        docs must not be sent that way or they get chunked as source code."""
        r = client.post("/v1/memory", json={"text": "func f() {}", "source": "a.swift"},
                        headers={"X-Admin-Key": "test"})
        assert r.status_code == 200, r.text
        fake_memory.add_memory_chunked.assert_called_once()
        fake_memory.add_memory.assert_not_called()


class TestSchema:
    def test_metadata_field_exists_and_accepts_scalars(self):
        from coding_model_server.schemas import MemoryRequest
        req = MemoryRequest(text="t", metadata={"s": "x", "n": 1, "f": 1.5, "b": True})
        assert req.metadata["n"] == 1 and req.metadata["b"] is True

    def test_metadata_rejects_nested_objects(self):
        """Chroma stores scalars only; a dict value would fail at write time,
        so it should be rejected at the edge instead."""
        from pydantic import ValidationError

        from coding_model_server.schemas import MemoryRequest
        with pytest.raises(ValidationError):
            MemoryRequest(text="t", metadata={"platforms": {"iOS": "16.0"}})

    def test_metadata_defaults_to_none(self):
        from coding_model_server.schemas import MemoryRequest
        assert MemoryRequest(text="t").metadata is None
