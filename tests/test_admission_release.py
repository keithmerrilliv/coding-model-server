"""DEV-117 — the chat admission slot must be released even when the SSE
generator is cancelled before its first iteration.

The slot release used to live only in the stream wrapper's ``finally``. A
sync generator that is never started never enters its ``try``, and close()
on an unstarted generator does not run the ``finally`` — so a client
disconnect already queued when the response started (a client timeout during
a loop-blocking swap) leaked one of CHAT_MAX_INFLIGHT=5 slots permanently.
Five of those and every chat request 503s until restart.

The fix: one idempotent release (``_once``) attached to BOTH the generator's
``finally`` and the response's Starlette background task, which runs on the
cancelled path too.
"""
import asyncio
from unittest import mock

from coding_model_server.routes import chat
from coding_model_server.runtime import AdmissionController
from coding_model_server.schemas import ChatCompletionRequest, ChatMessage


class _FakeState:
    pass


class _FakeRequest:
    def __init__(self):
        self.state = _FakeState()


def _drive_stream(monkeypatch, admission):
    """Run the streaming chat_completions path with mocked singletons and
    return the StreamingResponse."""
    fake_mgr = mock.Mock()
    fake_mgr.tokenize.side_effect = lambda text: len(text or "") // 4

    def _stream(*args, **kwargs):
        yield "data: one\n\n"
        yield "data: [DONE]\n\n"

    fake_mgr.proxy_stream.side_effect = _stream
    monkeypatch.setattr(chat, "llama_server_manager", fake_mgr)
    monkeypatch.setattr(chat, "chat_admission", admission)
    monkeypatch.setattr(chat.runtime.services, "memory", None, raising=False)

    request = ChatCompletionRequest(
        model="implementer",
        messages=[ChatMessage(role="user", content="hi")],
        stream=True,
    )
    return asyncio.run(chat.chat_completions(request, _FakeRequest()))


def test_once_wrapper_runs_exactly_once():
    calls = []
    f = chat._once(lambda: calls.append(1))
    f()
    f()
    assert len(calls) == 1


def test_close_on_unstarted_generator_skips_finally():
    # The mechanics behind the leak: an unstarted generator's close() never
    # enters the try, so the finally-based release alone cannot fire. This
    # is why the background-task hook must exist.
    released = []
    gen = chat._release_slot_on_stream_finish(iter(["x"]), lambda: released.append(1))
    gen.close()
    assert released == []


def test_background_task_releases_slot_when_stream_never_starts(monkeypatch):
    admission = AdmissionController(1)
    resp = _drive_stream(monkeypatch, admission)
    assert admission.in_flight == 1, "slot must be held while the stream is live"
    # Simulate the cancelled path: the generator is never iterated (its
    # finally never runs); Starlette still awaits the background task.
    assert resp.background is not None
    asyncio.run(resp.background())
    assert admission.in_flight == 0


def test_release_fires_exactly_once_when_both_hooks_run(monkeypatch):
    # Normal completion runs the generator finally AND the background task;
    # without the once-guard that would double-release, freeing a slot some
    # other request still holds.
    releases = []
    admission = mock.Mock()
    admission.release.side_effect = lambda: releases.append(1)
    resp = _drive_stream(monkeypatch, admission)

    async def _drain():
        async for _ in resp.body_iterator:
            pass

    asyncio.run(_drain())
    asyncio.run(resp.background())
    assert len(releases) == 1
