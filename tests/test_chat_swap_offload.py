"""Regression: model load/swap AND token-budget estimation must run off the
asyncio event loop.

DEV-112 — ensure_running() holds _swap_lock across a SIGTERM wait, a VRAM
poll, and a time.sleep() /health loop (up to ~140s of blocking work). Calling
it directly inside the async handler froze the whole FastAPI process: /health
timed out, the dashboard hung, and every concurrent SSE stream stalled
mid-token. It must run in a worker thread (asyncio.to_thread).

DEV-113 — the budget estimator round-trips to llama-server's /tokenize with a
synchronous requests.post, once per uncached string. On the loop thread those
jittered every concurrent stream and could stall up to 5s. They must run off
the loop too.

The invariant we pin: inside these calls, there is no running event loop.
asyncio.get_running_loop() succeeds only on the loop thread, so if a call were
still inline it would return the loop; offloaded to a worker thread it raises
RuntimeError. proxy_sync (chat.py already offloads it) is the positive control.
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


def _run_chat(monkeypatch, *, memory=None):
    """Drive chat_completions with mocked singletons; return the observed dict.

    observed records, for each blocking call, whether a running event loop was
    visible (True = ran inline on the loop = the bug).
    """
    observed = {"tokenize_on_loop": []}

    def _record_ensure_running(model_config, agent_id=None):
        observed["ensure_running_on_loop"] = _has_running_loop()

    def _record_tokenize(text):
        observed["tokenize_on_loop"].append(_has_running_loop())
        return len(text or "") // 4

    def _record_proxy_sync(*args, **kwargs):
        observed["proxy_sync_on_loop"] = _has_running_loop()
        return {"id": "ok", "choices": []}

    fake_mgr = mock.Mock()
    fake_mgr.ensure_running.side_effect = _record_ensure_running
    fake_mgr.tokenize.side_effect = _record_tokenize
    fake_mgr.proxy_sync.side_effect = _record_proxy_sync

    monkeypatch.setattr(chat, "llama_server_manager", fake_mgr)
    monkeypatch.setattr(chat, "chat_admission", mock.Mock())
    monkeypatch.setattr(chat.runtime.services, "memory", memory, raising=False)

    request = ChatCompletionRequest(
        model="implementer",
        messages=[ChatMessage(role="user", content="hello")],
        stream=False,
    )
    result = asyncio.run(chat.chat_completions(request, _FakeRequest()))
    return result, observed, fake_mgr


def test_ensure_running_is_offloaded_off_the_event_loop(monkeypatch):
    result, observed, fake_mgr = _run_chat(monkeypatch)

    assert result == {"id": "ok", "choices": []}
    fake_mgr.ensure_running.assert_called_once()
    # The bug: ensure_running observed the running loop (ran inline). The fix
    # runs it in a worker thread where no loop is running.
    assert observed.get("ensure_running_on_loop") is False, (
        "ensure_running must run off the event loop (asyncio.to_thread)"
    )
    # Positive control: the already-offloaded sync proxy sees the same.
    assert observed.get("proxy_sync_on_loop") is False


def test_tokenize_budget_estimation_is_offloaded_off_the_event_loop(monkeypatch):
    _, observed, _ = _run_chat(monkeypatch)

    # The estimator + guidance reserve both round-trip to /tokenize. Every one
    # of those calls must have run off the loop.
    assert observed["tokenize_on_loop"], "expected at least one tokenize round-trip"
    assert all(seen is False for seen in observed["tokenize_on_loop"]), (
        "every /tokenize round-trip must run off the event loop (DEV-113)"
    )


def test_rag_suffix_is_tokenized_apart_from_the_base_prompt(monkeypatch):
    """DEV-113 cache split: the volatile RAG block must be tokenized as its own
    string, never concatenated onto the stable base prompt (which would change
    the base's hash every retrieval and defeat the tokenize cache)."""
    fake_memory = mock.Mock()
    marker = "MEMORY-BLOCK-UNIQUE-abc123"
    fake_memory.get_context_string.return_value = marker

    _, observed, fake_mgr = _run_chat(monkeypatch, memory=fake_memory)

    tokenized = [c.args[0] for c in fake_mgr.tokenize.call_args_list]
    # The base agent prompt is tokenized as a standalone string...
    base_prompt = chat.Config.AGENTS["implementer"]["system_prompt"]
    assert base_prompt in tokenized, "base system prompt must be tokenized on its own"
    # ...and no tokenized string is the base with the memory block glued on.
    assert not any(marker in t and t != marker and base_prompt in t for t in tokenized), (
        "base prompt and RAG block must not be tokenized as one concatenated string"
    )
    # The RAG block itself is tokenized separately (it carries the marker).
    assert any(marker in t for t in tokenized), "RAG suffix must be tokenized separately"
