"""DEV-491 — a deferred model swap must not cancel a spec.

`spec_837b167f` was cancelled after ~100s of retries because another spec's
planner pass had held the model for 20 minutes. The 503 it kept receiving was
not an error: the server was saying "another request is in flight against the
current model — retry shortly".

Two independent defects, and fixing either one alone makes things worse:

  1. The server sets `Retry-After` at the raise site, but the OpenAI-compat
     exception handler rebuilt the response and dropped `exc.headers`. So the
     client never saw it and fell back to the 10/30/60 CUDA-OOM schedule. Not
     one `retrying after 5.0s` appears in a full day of production logs.

  2. Honouring a 5s Retry-After on a fixed 4-attempt budget would have burned
     that budget in ~15s instead of ~100s — six times FASTER. The header fix
     alone would have made the cancellation more likely, not less.

So server-directed waits are now bounded by the caller's own task timeout
rather than by an attempt count.
"""
from unittest import mock

import pytest

import coding_model_autonomous._http as h


def _resp(status, headers=None, text="{}"):
    r = mock.Mock()
    r.status_code = status
    r.headers = headers or {}
    r.text = text
    return r


class _Clock:
    """Monotonic clock that only advances when the code under test sleeps."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def monotonic(self):
        return self.t


def _run(responses, timeout=1800, clock=None):
    clock = clock or _Clock()
    it = iter(responses)
    last = responses[-1]

    def fake_post(*a, **kw):
        try:
            return next(it)
        except StopIteration:
            return last

    with mock.patch.object(h._SESSION, "post", side_effect=fake_post), \
         mock.patch.object(h.time, "sleep", side_effect=clock.sleep), \
         mock.patch.object(h.time, "monotonic", side_effect=clock.monotonic):
        resp = h.post_chat_completion(
            "implementer", [{"role": "user", "content": "x"}],
            timeout=timeout, retry_5xx=True)
    return resp, clock


BUSY = {"Retry-After": "5"}
BUSY_BODY = '{"error":{"message":"model swap deferred: another request is in flight"}}'


class TestServerDirectedWait:
    def test_it_waits_far_past_the_old_100s_budget(self):
        """The scenario that cancelled the spec: a 20-minute generation."""
        busy = [_resp(503, BUSY, BUSY_BODY)] * 300
        resp, clock = _run(busy + [_resp(200)], timeout=1800)
        assert resp.status_code == 200, "must survive a 20-minute wait"
        assert sum(clock.sleeps) > 100, (
            "old behaviour gave up after ~100s — that is the bug")

    def test_a_busy_503_does_not_consume_the_failure_budget(self):
        """Attempts are for failures. A wait is not a failure."""
        resp, _ = _run([_resp(503, BUSY, BUSY_BODY)] * 10 + [_resp(200)])
        assert resp.status_code == 200

    def test_it_honours_the_servers_interval(self):
        resp, clock = _run([_resp(503, BUSY, BUSY_BODY)] * 3 + [_resp(200)])
        assert clock.sleeps[:3] == [5.0, 5.0, 5.0], (
            "should sleep the server's 5s, not the 10/30/60 OOM schedule")

    def test_it_gives_up_at_the_task_timeout(self):
        """Bounded, not infinite — the caller's own budget is the bound."""
        resp, clock = _run([_resp(503, BUSY, BUSY_BODY)] * 10_000, timeout=60)
        assert resp.status_code == 503
        assert sum(clock.sleeps) <= 60 + 5

    def test_a_longer_interval_is_respected(self):
        """InsufficientVram says 30s."""
        r = [_resp(503, {"Retry-After": "30"}, "vram")] * 2 + [_resp(200)]
        _, clock = _run(r)
        assert clock.sleeps[:2] == [30.0, 30.0]

    def test_absurd_intervals_are_clamped(self):
        _, clock = _run([_resp(503, {"Retry-After": "99999"}, "x")] * 2 + [_resp(200)])
        assert all(s <= 60.0 for s in clock.sleeps)


class TestOrdinaryFailuresKeepTheOldSchedule:
    def test_a_500_without_the_header_uses_the_oom_backoffs(self):
        """The CUDA-OOM path this was written for must not regress."""
        _, clock = _run([_resp(500, {}, "oom")] * 3 + [_resp(200)])
        assert clock.sleeps == [10.0, 30.0, 60.0]

    def test_a_500_storm_still_gives_up_after_four_attempts(self):
        resp, clock = _run([_resp(500, {}, "oom")] * 50)
        assert resp.status_code == 500
        assert clock.sleeps == [10.0, 30.0, 60.0], "must not wait forever on a real failure"

    def test_a_503_without_retry_after_is_a_failure_not_a_wait(self):
        """e.g. FileNotFoundError's 503 — no header, so no known end."""
        resp, clock = _run([_resp(503, {}, "model file missing")] * 50)
        assert resp.status_code == 503
        assert clock.sleeps == [10.0, 30.0, 60.0]

    def test_non_numeric_header_falls_back_to_the_schedule(self):
        resp, clock = _run([_resp(503, {"Retry-After": "Wed, 21 Oct 2026"}, "x")] * 50)
        assert resp.status_code == 503
        assert clock.sleeps == [10.0, 30.0, 60.0]

    def test_success_is_returned_immediately(self):
        resp, clock = _run([_resp(200)])
        assert resp.status_code == 200
        assert clock.sleeps == []

    def test_a_4xx_is_not_retried(self):
        resp, clock = _run([_resp(400, {}, "bad")] * 5)
        assert resp.status_code == 400
        assert clock.sleeps == []


class TestTransportErrorsStillRetry:
    def test_connection_errors_use_the_schedule_and_then_raise(self):
        import requests as rq

        clock = _Clock()
        with mock.patch.object(h._SESSION, "post", side_effect=rq.ConnectionError("boom")), \
             mock.patch.object(h.time, "sleep", side_effect=clock.sleep), \
             mock.patch.object(h.time, "monotonic", side_effect=clock.monotonic):
            with pytest.raises(rq.ConnectionError):
                h.post_chat_completion("implementer", [{"role": "user", "content": "x"}],
                                       timeout=60, retry_5xx=True)
        assert clock.sleeps == [10.0, 30.0, 60.0], "must not loop forever on a dead server"


class TestHeadersSurviveTheErrorEnvelope:
    """The defect that made all of the above unreachable in production."""

    def test_http_exception_headers_are_forwarded(self):
        import asyncio

        from fastapi import HTTPException

        from coding_model_server.server import http_exception_handler

        exc = HTTPException(status_code=503, detail="model swap deferred",
                            headers={"Retry-After": "5"})
        resp = asyncio.run(http_exception_handler(None, exc))
        assert resp.headers.get("Retry-After") == "5", (
            "the OpenAI-compat envelope dropped exc.headers, so the server's "
            "backpressure signal never reached the orchestrator")

    def test_the_openai_envelope_is_preserved(self):
        """Forwarding headers must not change the documented error shape."""
        import asyncio
        import json

        from fastapi import HTTPException

        from coding_model_server.server import http_exception_handler

        resp = asyncio.run(http_exception_handler(
            None, HTTPException(status_code=503, detail="busy")))
        body = json.loads(resp.body)
        assert body["error"]["code"] == 503
        assert body["error"]["type"] == "server_error"

    def test_an_exception_without_headers_still_works(self):
        import asyncio

        from fastapi import HTTPException

        from coding_model_server.server import http_exception_handler

        resp = asyncio.run(http_exception_handler(
            None, HTTPException(status_code=400, detail="bad")))
        assert resp.status_code == 400
