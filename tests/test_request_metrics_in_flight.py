"""DEV-504 — a busy endpoint never reads as idle.

Samples are recorded on completion, so a seven-minute generation used to
contribute nothing until it returned and the agent's row fell off the panel
at the five-minute idle cutoff: the machine read as idle precisely when it
was working hardest. The collector now marks a request at its start.
"""
from unittest import mock

from coding_model_server.metrics import RequestMetricsCollector


def _rows(snap):
    return {(e["method"], e["path"], e["subkey"]): e for e in snap["endpoints"]}


class TestInFlight:
    def test_a_started_request_is_a_row_before_it_completes(self):
        c = RequestMetricsCollector()
        c.begin("POST", "/v1/chat/completions", "implementer")
        rows = _rows(c.snapshot())
        row = rows[("POST", "/v1/chat/completions", "implementer")]
        assert row["in_flight"] == 1
        assert row["window_count"] == 0 and row["total_count"] == 0
        assert row["in_flight_oldest_seconds"] is not None

    def test_a_busy_endpoint_survives_the_idle_cutoff(self):
        """The ticket's measurement: last completion at T, still generating at
        T+6m55s. The row stays, and says it is busy."""
        c = RequestMetricsCollector()
        with mock.patch("coding_model_server.metrics.time") as t:
            t.time.return_value = 1_000.0
            c.record("POST", "/v1/chat/completions", "implementer", 1200.0, 200)
            t.time.return_value = 1_000.0 + 10
            c.begin("POST", "/v1/chat/completions", "implementer")
            t.time.return_value = 1_000.0 + 415  # 6m55s after the completion
            rows = _rows(c.snapshot())
        row = rows[("POST", "/v1/chat/completions", "implementer")]
        assert row["in_flight"] == 1
        assert row["in_flight_oldest_seconds"] == 405.0
        assert row["total_count"] == 1

    def test_an_idle_endpoint_is_still_evicted(self):
        c = RequestMetricsCollector()
        with mock.patch("coding_model_server.metrics.time") as t:
            t.time.return_value = 1_000.0
            c.record("GET", "/scanner/404", None, 1.0, 404)
            t.time.return_value = 1_000.0 + 400
            assert c.snapshot()["endpoints"] == []
            # Evicted from the dicts too, not only from the output.
            assert ("GET", "/scanner/404", None) not in c._rings

    def test_end_clears_the_marker_and_the_sample_takes_over(self):
        c = RequestMetricsCollector()
        token = c.begin("POST", "/v1/chat/completions", "implementer")
        c.end(token)
        c.record("POST", "/v1/chat/completions", "implementer", 5.0, 200)
        row = _rows(c.snapshot())[("POST", "/v1/chat/completions", "implementer")]
        assert row["in_flight"] == 0 and row["in_flight_oldest_seconds"] is None
        assert row["window_count"] == 1
        c.end(token)  # idempotent: a stream teardown after an exception path

    def test_retag_moves_the_marker_to_the_route_and_agent(self):
        """The middleware only knows the literal path at request start; the
        route template and the agent arrive later."""
        c = RequestMetricsCollector()
        token = c.begin("POST", "/v1/autonomous/specs/spec_123/cancel")
        c.retag(token, path="/v1/autonomous/specs/{spec_id}/cancel")
        c.retag(token, subkey="implementer")
        rows = _rows(c.snapshot())
        assert list(rows) == [("POST", "/v1/autonomous/specs/{spec_id}/cancel", "implementer")]
        c.retag(9999, subkey="x")  # unknown token: ignored

    def test_busy_rows_sort_first(self):
        c = RequestMetricsCollector()
        c.record("GET", "/health", None, 1.0, 200)
        c.record("GET", "/v1/models", None, 1.0, 200)
        c.begin("POST", "/v1/chat/completions", "deep_implementer")
        first = c.snapshot()["endpoints"][0]
        assert first["path"] == "/v1/chat/completions" and first["in_flight"] == 1

    def test_two_in_flight_report_the_oldest(self):
        c = RequestMetricsCollector()
        with mock.patch("coding_model_server.metrics.time") as t:
            t.time.return_value = 100.0
            c.begin("POST", "/v1/chat/completions", "implementer")
            t.time.return_value = 160.0
            c.begin("POST", "/v1/chat/completions", "implementer")
            t.time.return_value = 170.0
            row = _rows(c.snapshot())[("POST", "/v1/chat/completions", "implementer")]
        assert row["in_flight"] == 2 and row["in_flight_oldest_seconds"] == 70.0


class TestMiddlewareWiring:
    def test_a_slow_request_is_visible_while_it_runs(self):
        """Through the real app: the metrics endpoint, polled during a slow
        request, shows that request in flight."""
        import threading
        from fastapi.testclient import TestClient
        from coding_model_server import server as srv

        release = threading.Event()
        seen = {}

        @srv.app.get("/v1/_test_slow_dev504")
        def _slow():
            seen["snapshot"] = srv.request_metrics.snapshot()
            release.wait(timeout=5)
            return {"ok": True}

        with TestClient(srv.app) as client:
            r = client.get("/v1/_test_slow_dev504")
            release.set()
        assert r.status_code == 200
        rows = _rows(seen["snapshot"])
        busy = [k for k, e in rows.items() if e["in_flight"]]
        assert ("GET", "/v1/_test_slow_dev504", None) in busy
        after = _rows(srv.request_metrics.snapshot())
        assert after[("GET", "/v1/_test_slow_dev504", None)]["in_flight"] == 0
        assert after[("GET", "/v1/_test_slow_dev504", None)]["window_count"] >= 1
