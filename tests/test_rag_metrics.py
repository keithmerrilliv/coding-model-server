"""DEV-501 — retrieval outcomes must be distinguishable on the dashboard.

Two RAG defects hid in production with nothing surfacing them:

  - DEV-488: retrieval never ran for any autonomous agent. Nine architect calls
    logged rag=on and injected nothing.
  - DEV-494: retrieval ran and returned confident nonsense at distance 0.508,
    under the 0.6 ceiling, for a query it had no business matching.

Both look identical to a single "injections" counter — the first reports zero,
and the second reports a healthy-looking number. These tests pin the
distinctions that make them tellable apart.
"""
import asyncio
from unittest import mock

from coding_model_server.metrics import RagRetrievalCollector
from coding_model_server.routes import chat
from coding_model_server.schemas import ChatCompletionRequest, ChatMessage


class TestCollector:
    def test_outcomes_are_counted_separately(self):
        c = RagRetrievalCollector()
        c.record("skipped")
        c.record("empty", hits=0)
        c.record("injected", hits=3, best_distance=0.3)
        snap = c.snapshot()
        assert snap["counts"] == {"skipped": 1, "empty": 1, "injected": 1}

    def test_skipped_is_excluded_from_attempted(self):
        """A skip_memory caller never ran retrieval; counting it as an attempt
        would make the hit rate look bad for a correctly-configured system."""
        c = RagRetrievalCollector()
        for _ in range(5):
            c.record("skipped")
        c.record("injected", hits=1, best_distance=0.3)
        snap = c.snapshot()
        assert snap["attempted"] == 1
        assert snap["hit_rate"] == 1.0

    def test_hit_rate_is_none_when_nothing_attempted(self):
        """DEV-488's signature. A rate of 0.0 would read as 'ran and missed';
        None reads as 'never ran', which is the actual defect."""
        c = RagRetrievalCollector()
        c.record("skipped")
        assert c.snapshot()["hit_rate"] is None

    def test_hit_rate_zero_means_ran_and_found_nothing(self):
        """The healthy state DEV-494's fix produces — must NOT look like None."""
        c = RagRetrievalCollector()
        c.record("empty", hits=0)
        assert c.snapshot()["hit_rate"] == 0.0

    def test_best_distance_is_retained(self):
        """The number that made DEV-494 legible."""
        c = RagRetrievalCollector()
        c.record("injected", query="q", hits=3, best_distance=0.508)
        assert c.snapshot()["recent"][0]["best_distance"] == 0.508

    def test_recent_is_newest_first(self):
        c = RagRetrievalCollector()
        c.record("injected", query="older")
        c.record("injected", query="newer")
        assert c.snapshot()["recent"][0]["query"] == "newer"

    def test_long_queries_are_truncated(self):
        """A query can be a whole spec paragraph; this rides a dashboard poll."""
        c = RagRetrievalCollector()
        c.record("injected", query="x" * 5000)
        assert len(c.snapshot()["recent"][0]["query"]) <= 200

    def test_ring_is_bounded(self):
        c = RagRetrievalCollector()
        for i in range(500):
            c.record("injected", query=str(i))
        snap = c.snapshot(limit=50)
        assert len(snap["recent"]) <= 50
        assert snap["counts"]["injected"] == 500, "counters must survive ring eviction"


class _FakeState:
    pass


class _FakeRequest:
    def __init__(self):
        self.state = _FakeState()


def _run_chat(monkeypatch, memory, collector, **kwargs):
    fake_mgr = mock.Mock()
    fake_mgr.ensure_running.return_value = None
    fake_mgr.tokenize.side_effect = lambda text: len(text or "") // 4
    fake_mgr.proxy_sync.return_value = {"id": "ok", "choices": []}
    monkeypatch.setattr(chat, "llama_server_manager", fake_mgr)
    monkeypatch.setattr(chat, "chat_admission", mock.Mock())
    monkeypatch.setattr(chat, "rag_metrics", collector)
    monkeypatch.setattr(chat.runtime.services, "memory", memory, raising=False)
    request = ChatCompletionRequest(
        model="implementer", stream=False,
        messages=[ChatMessage(role="user", content="hello")], **kwargs)
    asyncio.run(chat.chat_completions(request, _FakeRequest()))


class TestRecordedFromTheRealPath:
    """The collector is only useful if the retrieval path actually feeds it."""

    def test_injection_is_recorded(self, monkeypatch):
        memory = mock.Mock()

        def ctx(query, max_tokens=1000, stats=None):
            if stats is not None:
                stats["hits"] = 3
                stats["best_distance"] = 0.31
            return "MEMORY"

        memory.get_context_string.side_effect = ctx
        c = RagRetrievalCollector()
        _run_chat(monkeypatch, memory, c)
        snap = c.snapshot()
        assert snap["injected"] == 1
        assert snap["recent"][0]["hits"] == 3
        assert snap["recent"][0]["best_distance"] == 0.31

    def test_empty_result_is_recorded_as_empty_not_missing(self, monkeypatch):
        memory = mock.Mock()
        memory.get_context_string.side_effect = (
            lambda query, max_tokens=1000, stats=None: "")
        c = RagRetrievalCollector()
        _run_chat(monkeypatch, memory, c)
        snap = c.snapshot()
        assert snap["counts"].get("empty") == 1
        assert snap["injected"] == 0
        assert snap["attempted"] == 1, "it ran — that is the point"

    def test_skip_memory_is_recorded_as_skipped(self, monkeypatch):
        memory = mock.Mock()
        c = RagRetrievalCollector()
        _run_chat(monkeypatch, memory, c, skip_memory=True)
        snap = c.snapshot()
        assert snap["counts"].get("skipped") == 1
        assert snap["attempted"] == 0
        memory.get_context_string.assert_not_called()


class TestMemoryServiceStatsOutParam:
    def test_stats_are_populated_without_changing_the_return(self):
        """Out-param, matching call_agent(..., meta=meta), so the many callers
        and mocks that just want the string keep working."""
        from coding_model_server.memory_service import MemoryService

        svc = MemoryService.__new__(MemoryService)
        svc.search_memory = lambda q, n_results=5: [
            {"document": "a", "distance": 0.42},
            {"document": "b", "distance": 0.55},
        ]
        stats: dict = {}
        out = svc.get_context_string("q", stats=stats)
        assert isinstance(out, str) and "a" in out
        assert stats["hits"] == 2
        assert stats["best_distance"] == 0.42, "best = lowest cosine distance"

    def test_stats_on_no_hits(self):
        from coding_model_server.memory_service import MemoryService

        svc = MemoryService.__new__(MemoryService)
        svc.search_memory = lambda q, n_results=5: []
        stats: dict = {}
        assert svc.get_context_string("q", stats=stats) == ""
        assert stats["hits"] == 0
        assert stats["best_distance"] is None
