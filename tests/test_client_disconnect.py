"""DEV-130 — a mid-stream disconnect must not report partial text as "stop".

If the server drops the connection after emitting content but before a
finish_reason chunk, get_completion used to break out with the default
finish_reason="stop" and return the partial text as complete. The
orchestrator's continuation logic only fires on finish_reason=="length" (or
a trailing <<<CONTINUE>>>), so the truncation was silent: no continuation,
no warning, truncated text saved to history.

Now the client reports "length" for that case — unless the server really
did send a finish_reason before the drop, which is a completed response
that merely lost its [DONE] trailer.
"""
from unittest import mock

import requests

from coding_model_client import completion


def _sse(chunks):
    for c in chunks:
        yield c


class _FakeResponse:
    """Streams the given SSE lines, then raises ConnectionError mid-read."""

    status_code = 200

    def __init__(self, lines, die_after=True):
        self._lines = lines
        self._die_after = die_after

    def iter_lines(self):
        for line in self._lines:
            yield line.encode("utf-8")
        if self._die_after:
            raise requests.exceptions.ConnectionError("connection reset by peer")

    def close(self):
        pass


def _content_chunk(text):
    return ('data: {"choices": [{"delta": {"content": "%s"}, '
            '"finish_reason": null}]}' % text)


def _finish_chunk(reason):
    return ('data: {"choices": [{"delta": {}, "finish_reason": "%s"}]}' % reason)


def _run(lines, die_after=True):
    fake = _FakeResponse(lines, die_after=die_after)
    with mock.patch.object(completion._SESSION, "post", return_value=fake):
        return completion.get_completion(
            [{"role": "user", "content": "hi"}], "implementer", "theme")


def test_midstream_disconnect_reports_length_for_continuation():
    text, finish_reason, tool_calls = _run(
        [_content_chunk("partial "), _content_chunk("answer")])
    assert text == "partial answer"
    assert finish_reason == "length", (
        "a disconnect after content but before finish_reason must surface as "
        "truncation so the continuation path recovers the rest"
    )
    assert tool_calls is None


def test_disconnect_after_real_finish_reason_keeps_it():
    # The server finished ("stop" arrived) and only the [DONE] trailer was
    # lost — that response is complete, not truncated.
    text, finish_reason, _ = _run(
        [_content_chunk("whole answer"), _finish_chunk("stop")])
    assert text == "whole answer"
    assert finish_reason == "stop"


def test_clean_stream_still_reports_server_finish_reason():
    text, finish_reason, _ = _run(
        [_content_chunk("done"), _finish_chunk("length"), "data: [DONE]"],
        die_after=False,
    )
    assert text == "done"
    assert finish_reason == "length"
