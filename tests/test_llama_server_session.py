"""The manager reuses one HTTP session for all llama-server calls (DEV-28),
and the keep-alive write is locked + throttled (DEV-31)."""
from unittest import mock

import requests

from coding_model_server.llama_server import LlamaServerManager


def test_manager_has_a_pooled_session():
    m = LlamaServerManager()
    assert isinstance(m._session, requests.Session)
    adapter = m._session.get_adapter("http://localhost")
    # pool sized to the admission cap, never below the floor
    assert adapter._pool_maxsize >= 8


def test_bump_last_request_time_throttles_and_locks(monkeypatch):
    m = LlamaServerManager()
    times = iter([1000.0, 1000.5, 1002.0, 1010.0])
    monkeypatch.setattr("coding_model_server.llama_server.time.time", lambda: next(times))

    m.last_request_time = 0.0
    m._bump_last_request_time()          # t=1000.0, gap huge -> writes
    assert m.last_request_time == 1000.0
    m._bump_last_request_time()          # t=1000.5, gap 0.5s < 5s -> throttled
    assert m.last_request_time == 1000.0
    m._bump_last_request_time()          # t=1002.0, gap 1.5s < 5s -> throttled
    assert m.last_request_time == 1000.0
    m._bump_last_request_time()          # t=1010.0, gap 10s -> writes
    assert m.last_request_time == 1010.0


def test_bump_takes_the_lock_on_write(monkeypatch):
    m = LlamaServerManager()
    monkeypatch.setattr("coding_model_server.llama_server.time.time", lambda: 5000.0)
    m.last_request_time = 0.0
    with mock.patch.object(m, "lock", wraps=m.lock) as spy:
        m._bump_last_request_time()
    spy.__enter__.assert_called_once()
