"""DEV-350 — no RAG retrieval for requests whose context it can never reach.

chat_completions started the retrieval task unconditionally, then computed
client_has_system and zeroed the result: when the client supplies its own
system message, _build_openai_messages drops the agent system prompt — and
the RAG block riding on it — so the request paid the embedding encode + HNSW
query (up to its 2s cap, on CPU exactly during prefill) for a context block
that was guaranteed to be discarded, while the log claimed memory context was
injected. Third-party OpenAI-compatible clients all send their own system
message and cannot know the skip_memory flag.
"""
import asyncio
from unittest import mock

from coding_model_server.routes import chat
from coding_model_server.schemas import ChatCompletionRequest, ChatMessage


class _FakeState:
    pass


class _FakeRequest:
    def __init__(self):
        self.state = _FakeState()


def _run_chat(monkeypatch, messages, memory):
    fake_mgr = mock.Mock()
    fake_mgr.ensure_running.return_value = None
    fake_mgr.tokenize.side_effect = lambda text: len(text or "") // 4
    fake_mgr.proxy_sync.return_value = {"id": "ok", "choices": []}

    monkeypatch.setattr(chat, "llama_server_manager", fake_mgr)
    monkeypatch.setattr(chat, "chat_admission", mock.Mock())
    monkeypatch.setattr(chat.runtime.services, "memory", memory, raising=False)

    request = ChatCompletionRequest(
        model="implementer", messages=messages, stream=False)
    return asyncio.run(chat.chat_completions(request, _FakeRequest()))


def test_client_system_message_skips_retrieval(monkeypatch):
    fake_memory = mock.Mock()
    fake_memory.get_context_string.return_value = "MEMORY-BLOCK"

    result = _run_chat(monkeypatch, [
        ChatMessage(role="system", content="I am my own system prompt"),
        ChatMessage(role="user", content="hello"),
    ], memory=fake_memory)

    assert result == {"id": "ok", "choices": []}, "request must still succeed"
    fake_memory.get_context_string.assert_not_called()


def test_agent_prompt_requests_still_get_retrieval(monkeypatch):
    """Positive control: without a client system message the agent prompt
    ships, so retrieval must still run — the skip must not over-reach."""
    fake_memory = mock.Mock()
    fake_memory.get_context_string.return_value = "MEMORY-BLOCK"

    result = _run_chat(monkeypatch, [
        ChatMessage(role="user", content="hello"),
    ], memory=fake_memory)

    assert result == {"id": "ok", "choices": []}
    fake_memory.get_context_string.assert_called_once()
