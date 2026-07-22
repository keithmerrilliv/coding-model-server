"""Regression: the model load/swap must not run on the asyncio event loop.

DEV-112 — ensure_running() holds _swap_lock across a SIGTERM wait, a VRAM
poll, and a time.sleep() /health loop (up to ~140s of blocking work). Calling
it directly inside the async handler froze the whole FastAPI process: /health
timed out, the dashboard hung, and every concurrent SSE stream stalled
mid-token. It must run in a worker thread (asyncio.to_thread).

The invariant we pin: inside ensure_running, there is no running event loop.
asyncio.get_running_loop() succeeds only on the loop thread, so if the call
were still inline it would return the loop; offloaded to a worker thread it
raises RuntimeError. proxy_sync (chat.py already offloads it) is used as the
positive control — it must observe the same thread-with-no-loop.
"""
import asyncio
from unittest import mock

from coding_model_server.routes import chat
from coding_model_server.schemas import ChatCompletionRequest, ChatMessage


def _has_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


class _FakeState:
    pass


class _FakeRequest:
    def __init__(self):
        self.state = _FakeState()


def test_ensure_running_is_offloaded_off_the_event_loop(monkeypatch):
    observed = {}

    def _record_ensure_running(model_config, agent_id=None):
        observed["ensure_running_on_loop"] = _has_running_loop()

    def _record_proxy_sync(*args, **kwargs):
        observed["proxy_sync_on_loop"] = _has_running_loop()
        return {"id": "ok", "choices": []}

    fake_mgr = mock.Mock()
    fake_mgr.ensure_running.side_effect = _record_ensure_running
    fake_mgr.tokenize.side_effect = lambda text: len(text or "") // 4
    fake_mgr.proxy_sync.side_effect = _record_proxy_sync

    fake_admission = mock.Mock()

    monkeypatch.setattr(chat, "llama_server_manager", fake_mgr)
    monkeypatch.setattr(chat, "chat_admission", fake_admission)
    # No memory service → RAG injection is a no-op, keeping the path minimal.
    monkeypatch.setattr(chat.runtime.services, "memory", None, raising=False)

    request = ChatCompletionRequest(
        model="implementer",
        messages=[ChatMessage(role="user", content="hello")],
        stream=False,
    )

    result = asyncio.run(chat.chat_completions(request, _FakeRequest()))

    assert result == {"id": "ok", "choices": []}
    fake_mgr.ensure_running.assert_called_once()
    # The bug: ensure_running observed the running loop (ran inline). The fix
    # runs it in a worker thread where no loop is running.
    assert observed.get("ensure_running_on_loop") is False, (
        "ensure_running must run off the event loop (asyncio.to_thread)"
    )
    # Positive control: the already-offloaded sync proxy sees the same.
    assert observed.get("proxy_sync_on_loop") is False
