"""DEV-657 part 2 — a retrieval outcome must outlive the process that made it.

The in-memory ring (DEV-501) answers "how is RAG doing right now" and nothing
else: it holds 50 entries, it is per-process, and every restart erases it.
Between 2026-09-08 and 2026-09-15 retrieval ran 116 times on this box and not
one outcome survived to be queried — the ring was wiped by a routine restart
mid-investigation, which is precisely how the question "is the Apple-docs
corpus earning its keep" has stayed unanswerable.

The fix is to put the outcome where the rest of an attempt's telemetry already
lives: on the AGENT_RAN event. That crosses a process boundary — retrieval
happens in the server, AGENT_RAN is written by the orchestrator — so the
outcome has to ride the HTTP response. These tests pin both halves and the
invariant that they agree.
"""
import asyncio
from unittest import mock

from coding_model_server.metrics import RagRetrievalCollector
from coding_model_server.routes import chat
from coding_model_server.schemas import ChatCompletionRequest, ChatMessage
from coding_model_autonomous.executor import agent_event_fields


class _FakeState:
    pass


class _FakeRequest:
    def __init__(self):
        self.state = _FakeState()


def _run_chat(monkeypatch, memory, collector, **kwargs):
    """Drive the real handler and RETURN the completion, so the wire shape is
    assertable. The sibling harness in test_rag_metrics discards it."""
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
    return asyncio.run(chat.chat_completions(request, _FakeRequest()))


def _memory_returning(text, hits=None, best_distance=None):
    memory = mock.Mock()

    def ctx(query, max_tokens=1000, stats=None):
        if stats is not None:
            if hits is not None:
                stats["hits"] = hits
            if best_distance is not None:
                stats["best_distance"] = best_distance
        return text

    memory.get_context_string.side_effect = ctx
    return memory


class TestOutcomeRidesTheResponse:
    def test_injection_is_reported_on_the_wire(self, monkeypatch):
        memory = _memory_returning("MEMORY", hits=3, best_distance=0.31)
        resp = _run_chat(monkeypatch, memory, RagRetrievalCollector())
        assert resp["rag"] == {
            "outcome": "injected", "agent": "implementer",
            "hits": 3, "best_distance": 0.31,
        }

    def test_empty_is_reported_and_is_not_silence(self, monkeypatch):
        """'Ran and matched nothing' is the healthy state for a centipede spec
        against a corpus of Apple API docs — but only if it is visible."""
        memory = _memory_returning("", hits=0, best_distance=0.57)
        resp = _run_chat(monkeypatch, memory, RagRetrievalCollector())
        assert resp["rag"]["outcome"] == "empty"
        assert resp["rag"]["hits"] == 0
        assert resp["rag"]["best_distance"] == 0.57

    def test_skipped_is_reported(self, monkeypatch):
        memory = _memory_returning("MEMORY")
        resp = _run_chat(monkeypatch, memory, RagRetrievalCollector(),
                         skip_memory=True)
        assert resp["rag"]["outcome"] == "skipped"

    def test_no_memory_service_attaches_nothing(self, monkeypatch):
        """An absent key and a recorded 'skipped' are different facts: one is a
        server with retrieval switched off, the other is a caller declining it.
        DEV-488 hid for nine architect calls in exactly that gap."""
        resp = _run_chat(monkeypatch, None, RagRetrievalCollector())
        assert "rag" not in resp

    def test_ring_and_wire_report_the_same_numbers(self, monkeypatch):
        """The whole reason _record_rag exists: two call sites cannot drift."""
        collector = RagRetrievalCollector()
        memory = _memory_returning("MEMORY", hits=7, best_distance=0.42)
        resp = _run_chat(monkeypatch, memory, collector)
        ring = collector.snapshot()["recent"][0]
        assert resp["rag"]["outcome"] == ring["outcome"]
        assert resp["rag"]["hits"] == ring["hits"]
        assert resp["rag"]["best_distance"] == ring["best_distance"]
        assert resp["rag"]["agent"] == ring["agent"]

    def test_query_text_never_rides_the_response(self, monkeypatch):
        """The ring truncates a query because it feeds a dashboard poll. The
        event payload should not carry it at all: a spec paragraph in every
        AGENT_RAN row is a lot of bytes to store to answer a question nobody
        asked, and it is user content in a telemetry record."""
        memory = _memory_returning("MEMORY", hits=1, best_distance=0.3)
        resp = _run_chat(monkeypatch, memory, RagRetrievalCollector())
        assert "query" not in resp["rag"]


class TestOutcomeReachesTheEvent:
    def test_rag_is_carried_onto_agent_ran(self):
        out = agent_event_fields({
            "agent": "implementer", "duration_ms": 12,
            "rag": {"outcome": "injected", "hits": 3, "best_distance": 0.31},
        })
        assert out["rag"]["outcome"] == "injected"
        assert out["rag"]["hits"] == 3

    def test_absent_rag_is_omitted_not_nulled(self):
        """An event predating this change and one whose retrieval never ran
        must look the same to a reader — the rule the rest of this function
        already follows (DEV-528)."""
        out = agent_event_fields({"agent": "implementer", "duration_ms": 12})
        assert "rag" not in out

    def test_empty_rag_dict_is_omitted(self):
        out = agent_event_fields({"agent": "implementer", "rag": {}})
        assert "rag" not in out

    def test_non_dict_rag_is_ignored(self):
        """A server that returns something unexpected must not poison the
        payload — event rows are queried, not eyeballed."""
        out = agent_event_fields({"agent": "implementer", "rag": "injected"})
        assert "rag" not in out


class TestFinishReasonReachesTheEvent:
    """DEV-691, architect sub-case. `truncated` alone cannot distinguish a
    model that stopped early from a budget overrun; run 39's architect ended
    its turn at 5,361 and 3,352 completion tokens against a 10,000 budget and
    the event could not say so."""

    def test_finish_reason_is_carried(self):
        out = agent_event_fields({"agent": "dense_architect",
                                  "finish_reason": "stop"})
        assert out["finish_reason"] == "stop"

    def test_length_is_carried_alongside_truncated(self):
        out = agent_event_fields({"agent": "x", "finish_reason": "length",
                                  "truncated": True})
        assert out["finish_reason"] == "length"
        assert out["truncated"] is True

    def test_absent_finish_reason_is_omitted(self):
        out = agent_event_fields({"agent": "x"})
        assert "finish_reason" not in out

    def test_none_finish_reason_is_omitted(self):
        """call_agent writes `fr or None`, so None is the unreported case."""
        out = agent_event_fields({"agent": "x", "finish_reason": None})
        assert "finish_reason" not in out
