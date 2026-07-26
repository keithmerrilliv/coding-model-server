"""DEV-161/162/163 — chunk-boundary correctness, atomic memory dedup, and
DNS-rebinding-proof fetches."""
from unittest import mock

import pytest

from coding_model_client import services
from coding_model_server.code_chunker import CodeChunker


# ── DEV-161: byte offsets must index bytes, not str ──────────────────────────

class TestChunkerMultibyte:
    def test_non_ascii_source_chunks_are_not_garbled(self):
        chunker = CodeChunker()
        if chunker.get_parser_for_ext(".py") is None:
            pytest.skip("tree-sitter python parser unavailable")
        # The accented comment is 30+ multibyte chars; every chunk boundary
        # after it used to shift by the byte/char delta, slicing functions
        # mid-token and storing garbage into the RAG store.
        src = (
            "# coración años niño über — ünïcödé commentary\n"
            "def first_function():\n"
            "    return 'alpha'\n"
            "\n"
            "def second_function():\n"
            "    return 'beta'\n"
        )
        chunks = chunker.chunk_text(src, ".py")
        texts = [c["text"] for c in chunks]
        joined = "\n".join(texts)
        assert "def second_function():" in joined
        assert "return 'beta'" in joined
        # Every chunk must be a clean substring of the source — a byte/char
        # mismatch produces slices that appear nowhere in the original.
        for t in texts:
            assert t in src, f"garbled chunk not present in source: {t!r}"

    def test_ascii_source_still_chunks(self):
        chunker = CodeChunker()
        if chunker.get_parser_for_ext(".py") is None:
            pytest.skip("tree-sitter python parser unavailable")
        src = "def f():\n    return 1\n\ndef g():\n    return 2\n"
        chunks = chunker.chunk_text(src, ".py")
        assert chunks
        for c in chunks:
            assert c["text"] in src


# ── DEV-162: dedup is atomic (hash is the id) ────────────────────────────────

class _FakeCollection:
    """Minimal Chroma stand-in: dict keyed by id, upsert overwrites."""

    def __init__(self):
        self.rows: dict = {}

    def get(self, ids=None, where=None, limit=None):
        if ids:
            found = [i for i in ids if i in self.rows]
            return {"ids": found, "metadatas": [self.rows[i] for i in found]}
        return {"ids": list(self.rows), "metadatas": list(self.rows.values())}

    def upsert(self, documents, embeddings, metadatas, ids):
        for i, meta in zip(ids, metadatas):
            self.rows[i] = meta

    def add(self, documents, embeddings, metadatas, ids):  # pragma: no cover
        raise AssertionError("add() bypasses upsert dedup — use upsert")

    def count(self):
        return len(self.rows)


def _service_with(collection):
    from coding_model_server.memory_service import MemoryService
    svc = MemoryService.__new__(MemoryService)  # skip __init__/_init_db
    svc._collection = collection
    svc._embedding_model = mock.Mock()
    svc._embedding_model.encode.return_value = mock.Mock(
        tolist=lambda: [0.1, 0.2, 0.3])
    return svc


class TestMemoryDedup:
    def test_document_id_is_the_content_hash(self):
        col = _FakeCollection()
        svc = _service_with(col)
        res = svc.add_memory("a durable fact")
        assert res["status"] == "success"
        assert res["id"] == svc._content_hash("a durable fact")
        assert list(col.rows) == [res["id"]]

    def test_racing_identical_writes_yield_one_row(self):
        # Simulate the race the get-then-add version lost: the "existing"
        # check sees nothing (both threads passed it), then both write.
        col = _FakeCollection()
        svc = _service_with(col)
        with mock.patch.object(col, "get", return_value={"ids": []}):
            svc.add_memory("same content")
            svc.add_memory("same content")
        assert col.count() == 1, "upsert on the content hash collapses the race"

    def test_second_identical_add_reports_duplicate(self):
        col = _FakeCollection()
        svc = _service_with(col)
        svc.add_memory("hello world")
        again = svc.add_memory("hello world")
        assert again["status"] == "duplicate"

    def test_large_collection_warns(self, caplog):
        import logging
        col = _FakeCollection()
        svc = _service_with(col)
        with mock.patch.object(type(svc), "MEMORY_COUNT_WARN_THRESHOLD", 1), \
             caplog.at_level(logging.WARNING):
            svc.add_memory("one fact")
        assert any("warn threshold" in r.getMessage() for r in caplog.records)


# ── DEV-163: connect to the validated IP, not a re-resolved name ─────────────

class TestPinnedFetch:
    def test_http_connects_to_the_validated_ip_with_host_header(self, monkeypatch):
        get = mock.Mock(return_value=mock.Mock(status_code=200, headers={}))
        monkeypatch.setattr(services, "_SESSION", mock.Mock(get=get))
        monkeypatch.setattr(
            services, "_validate_public_url",
            lambda url: (True, "", ["93.184.216.34"]))

        services._get_revalidating_redirects("http://example.com/doc", timeout=5)

        called_url = get.call_args.args[0]
        assert "93.184.216.34" in called_url, "must dial the validated IP"
        assert get.call_args.kwargs["headers"]["Host"] == "example.com"

    def test_https_keeps_the_hostname_for_tls_identity(self, monkeypatch):
        # Pinning the IP would break SNI/cert validation; TLS already binds
        # the response to the certified hostname.
        get = mock.Mock(return_value=mock.Mock(status_code=200, headers={}))
        monkeypatch.setattr(services, "_SESSION", mock.Mock(get=get))
        monkeypatch.setattr(
            services, "_validate_public_url",
            lambda url: (True, "", ["93.184.216.34"]))

        services._get_revalidating_redirects("https://example.com/doc", timeout=5)

        assert get.call_args.args[0] == "https://example.com/doc"

    def test_port_is_preserved_when_pinning(self, monkeypatch):
        get = mock.Mock(return_value=mock.Mock(status_code=200, headers={}))
        monkeypatch.setattr(services, "_SESSION", mock.Mock(get=get))
        monkeypatch.setattr(
            services, "_validate_public_url",
            lambda url: (True, "", ["93.184.216.34"]))

        services._get_revalidating_redirects("http://example.com:8080/x", timeout=5)

        assert "93.184.216.34:8080" in get.call_args.args[0]
