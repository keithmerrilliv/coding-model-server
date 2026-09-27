"""Drive the real /v1/chat/completions handler with its singletons mocked.

Shared by the chat-route tests. It is a plain module rather than conftest.py
content because test modules import it by name, and ``from conftest import``
resolves to whichever conftest pytest loaded last (tests/seams/conftest.py).
"""
import asyncio
from unittest import mock

from coding_model_server.routes import chat
from coding_model_server.schemas import ChatCompletionRequest, ChatMessage


class FakeState:
    """An empty request.state: an attribute the handler reads without setting
    raises, as it would on starlette's State."""


class FakeRequest:
    """The handler only touches ``request.state``."""

    def __init__(self):
        self.state = FakeState()


def fake_llama_manager():
    """A llama_server_manager whose model is up, whose /tokenize counts four
    characters per token, and whose non-streaming proxy returns an empty
    completion."""
    mgr = mock.Mock()
    mgr.ensure_running.return_value = None
    mgr.tokenize.side_effect = lambda text: len(text or "") // 4
    mgr.proxy_sync.return_value = {"id": "ok", "choices": []}
    return mgr


def drive_chat(monkeypatch, *, model="implementer", messages=None, memory=None,
               collector=None, manager=None, **request_kwargs):
    """Run one non-streaming chat_completions call; return (result, manager).

    ``memory`` is the retrieval service (None turns RAG off), ``collector``
    replaces ``chat.rag_metrics`` when given, and ``manager`` replaces the
    default fake llama manager. ``messages`` defaults to one user "hello".
    """
    mgr = manager if manager is not None else fake_llama_manager()
    monkeypatch.setattr(chat, "llama_server_manager", mgr)
    monkeypatch.setattr(chat, "chat_admission", mock.Mock())
    if collector is not None:
        monkeypatch.setattr(chat, "rag_metrics", collector)
    monkeypatch.setattr(chat.runtime.services, "memory", memory, raising=False)
    if messages is None:
        messages = [ChatMessage(role="user", content="hello")]
    request = ChatCompletionRequest(
        model=model, messages=messages, stream=False, **request_kwargs)
    return asyncio.run(chat.chat_completions(request, FakeRequest())), mgr
