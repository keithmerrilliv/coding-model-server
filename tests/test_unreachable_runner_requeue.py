"""An unreachable runner must requeue, not open a human gate (DEV-538).

Run 8 opened a code_review gate on a build check that had never run, and the
pipeline sat on it for 3930 minutes across three reboots. The runner comes back
on its own; nothing about that needs a human.
"""
import pytest
import requests

from coding_model_autonomous import test_runner
from coding_model_server import orchestrator_daemon as od


# ── the transport signal ────────────────────────────────────────────────────

def test_transport_failure_is_distinguishable_from_a_test_failure():
    unreachable = (
        "mac-runner unreachable at http://127.0.0.1:5050/v1/run_tests: "
        "HTTPConnectionPool(host='127.0.0.1', port=5050): Read timed out.")
    assert test_runner.is_runner_unreachable(unreachable)

    # A real build failure — the compiler spoke — must not read as transport.
    real = "World.swift:42:52: error: cannot find 'lc' in scope\n"
    assert not test_runner.is_runner_unreachable(real)
    # Nor must an empty or missing output.
    assert not test_runner.is_runner_unreachable("")
    assert not test_runner.is_runner_unreachable(None)


def test_run_8_output_verbatim_is_recognised():
    """The exact string that stalled run 8 for 65 hours."""
    assert test_runner.is_runner_unreachable(
        "mac-runner unreachable at http://127.0.0.1:5050/v1/run_tests: "
        "HTTPConnectionPool(host='127.0.0.1', port=5050): Read timed out. "
        "(read timeout=330)")


# ── retry policy ────────────────────────────────────────────────────────────

def test_connection_errors_retry_with_backoff(monkeypatch):
    """A sleeping Mac fails fast, so retrying it is nearly free."""
    slept, attempts = [], {"n": 0}
    monkeypatch.setattr(test_runner.time, "sleep", slept.append)

    def always_refused(*a, **kw):
        attempts["n"] += 1
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(test_runner._SESSION, "post", always_refused)
    with pytest.raises(requests.ConnectionError):
        test_runner._dispatch_with_retry("http://x/v1/run_tests", {}, {}, 30)

    assert attempts["n"] == len(test_runner._DISPATCH_CONNECT_BACKOFFS) + 1
    assert slept == list(test_runner._DISPATCH_CONNECT_BACKOFFS)


def test_connection_error_that_recovers_returns_the_response(monkeypatch):
    monkeypatch.setattr(test_runner.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def refuse_once(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.ConnectionError("asleep")
        return "the-response"

    monkeypatch.setattr(test_runner._SESSION, "post", refuse_once)
    assert test_runner._dispatch_with_retry("u", {}, {}, 30) == "the-response"
    assert calls["n"] == 2


def test_read_timeout_is_retried_only_once(monkeypatch):
    """A read timeout has already burned its full window.

    Run 8's was 330s. Retrying it the way a fast connection failure is retried
    would block a worker for many minutes for no added information.
    """
    monkeypatch.setattr(test_runner.time, "sleep", lambda _s: None)
    attempts = {"n": 0}

    def always_timeout(*a, **kw):
        attempts["n"] += 1
        raise requests.Timeout("read timed out")

    monkeypatch.setattr(test_runner._SESSION, "post", always_timeout)
    with pytest.raises(requests.Timeout):
        test_runner._dispatch_with_retry("u", {}, {}, 330)
    assert attempts["n"] == test_runner._DISPATCH_READ_TIMEOUT_RETRIES + 1 == 2


# ── requeue decision ────────────────────────────────────────────────────────

class _FakeDB:
    def __init__(self, prior_unreachable=0):
        self._events = [
            _Ev({"phase": "pre_gate_build_check", "runner_unreachable": True})
            for _ in range(prior_unreachable)
        ]
        self.recorded = []
        self.status_updates = []

    def list_events_by_kind(self, *, spec_id, kind, limit=20):
        return list(self._events)

    def record_event(self, kind, *, spec_id=None, task_id=None, payload=None):
        self.recorded.append(payload or {})

    def update_task_status(self, task_id, status):
        self.status_updates.append((task_id, status))


class _Ev:
    def __init__(self, payload):
        self.payload = payload


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _spec_and_task(retry_count=2):
    return (_Obj(id="spec_63a31526"),
            _Obj(id="task_1", retry_count=retry_count))


def test_first_unreachable_dispatch_requeues_instead_of_gating():
    db = _FakeDB(prior_unreachable=0)
    spec, task = _spec_and_task()
    assert od._requeue_for_unreachable_runner(db, spec, task) is True
    assert db.status_updates == [("task_1", od.TaskStatus.PENDING)]
    assert db.recorded[-1]["runner_unreachable"] is True
    assert db.recorded[-1]["requeue"] == 1


def test_requeue_does_not_burn_an_implementer_attempt():
    """The retry budget belongs to the implementer, not to a sleeping Mac."""
    db = _FakeDB(prior_unreachable=1)
    spec, task = _spec_and_task(retry_count=2)
    od._requeue_for_unreachable_runner(db, spec, task)
    # retry_count is reported, never incremented, and no increment API is used.
    assert db.recorded[-1]["retry"] == 2
    assert task.retry_count == 2


def test_escalates_to_a_human_once_the_cap_is_reached():
    db = _FakeDB(prior_unreachable=od._MAX_UNREACHABLE_REQUEUES)
    spec, task = _spec_and_task()
    assert od._requeue_for_unreachable_runner(db, spec, task) is False
    assert db.status_updates == [], "must not requeue past the cap"
    assert db.recorded == [], "the caller opens the gate; this records nothing"


def test_unrelated_test_ran_events_do_not_count_toward_the_cap():
    db = _FakeDB(prior_unreachable=0)
    db._events = [
        _Ev({"phase": "pre_gate_build_check", "passed": False}),   # real failure
        _Ev({"phase": "synthesis_repair", "runner_unreachable": True}),
        _Ev({"phase": "pre_gate_build_check", "runner_unreachable": True}),
    ]
    spec, task = _spec_and_task()
    assert od._requeue_for_unreachable_runner(db, spec, task) is True
    assert db.recorded[-1]["requeue"] == 2  # only the one matching event


# ── gate wording, for the case that does reach a human ──────────────────────

def test_gate_says_the_runner_was_unreachable_not_review_this_code():
    line = od._build_check_line(
        False,
        "mac-runner unreachable at http://127.0.0.1:5050/v1/run_tests: "
        "Read timed out.",
        "swift_test")
    assert "unreachable" in line
    assert "infrastructure fault" in line
    assert "do not approve it" in line.lower()


def test_a_genuinely_inconclusive_output_keeps_the_dev_477_wording():
    line = od._build_check_line(False, "some unrecognised runner noise",
                                "swift_test")
    assert "inconclusive" in line
    assert "unreachable" not in line
