"""DEV-118 — the idle watchdog must survive a fast swap.

The old design shared one boolean: a swap's _shutdown_unlocked cleared it,
and the old watchdog only noticed at its next 30s wake. If the new model
became healthy before that wake (typical for 5-15s loads of 30B-class
agents), _start_watchdog saw the old thread still alive and early-returned
WITHOUT re-arming the flag. The old thread then woke, saw the flag false,
and exited — leaving the new child with no watchdog, so the 30-minute idle
unload never fired and ~14 GiB of VRAM stayed pinned until the next manual
swap.

Now each watchdog thread is bound to a generation token: shutdown bumps it
to invalidate the incumbent, and _start_watchdog ALWAYS binds a fresh
thread to a fresh generation instead of trusting a possibly-dying survivor.
"""
import time
from unittest import mock

from coding_model_server import llama_server
from coding_model_server.llama_server import LlamaServerManager, _STATE_RUNNING


def _running_manager():
    mgr = LlamaServerManager()
    proc = mock.Mock()
    proc.poll.return_value = None
    mgr.process = proc
    mgr._state = _STATE_RUNNING
    mgr.last_request_time = time.time() - mgr.IDLE_TIMEOUT - 1  # long idle
    return mgr


def _capture_threads(monkeypatch):
    """Replace Thread so watchdog threads are recorded, not started."""
    started = []

    class _FakeThread:
        def __init__(self, target=None, args=(), daemon=None):
            self.target, self.args = target, args
            started.append(self)

        def start(self):
            pass

        def is_alive(self):
            return True  # simulate the old thread still one wake from exit

    monkeypatch.setattr(llama_server, "Thread", _FakeThread)
    return started


def test_start_watchdog_always_starts_a_fresh_generation(monkeypatch):
    # The regression: an alive-but-doomed old thread must not suppress the
    # new child's watchdog.
    started = _capture_threads(monkeypatch)
    mgr = _running_manager()

    mgr._start_watchdog()          # child 1's watchdog (gen 1)
    mgr._shutdown_unlocked()       # swap teardown invalidates gen 1
    mgr._state = _STATE_RUNNING    # fast swap: new child healthy quickly
    mgr.process = mock.Mock(poll=mock.Mock(return_value=None))
    mgr._start_watchdog()          # must NOT early-return on the alive gen-1 thread

    assert len(started) == 2, "each child must get its own watchdog thread"
    assert started[1].args == (mgr._watchdog_generation,), (
        "the new thread must be bound to the current generation"
    )


def test_stale_generation_exits_without_killing(monkeypatch):
    monkeypatch.setattr(llama_server.time, "sleep", lambda s: None)
    mgr = _running_manager()
    mgr._watchdog_generation = 5
    mgr._shutdown_unlocked = mock.Mock()

    mgr._idle_watchdog(gen=4)  # superseded thread wakes up

    mgr._shutdown_unlocked.assert_not_called()


def test_current_generation_still_unloads_an_idle_child(monkeypatch):
    # The feature itself keeps working: a current-generation watchdog with
    # an idle child past IDLE_TIMEOUT tears it down.
    monkeypatch.setattr(llama_server.time, "sleep", lambda s: None)
    mgr = _running_manager()
    mgr._watchdog_generation = 3
    mgr._shutdown_unlocked = mock.Mock()

    mgr._idle_watchdog(gen=3)

    mgr._shutdown_unlocked.assert_called_once()


def test_in_flight_reservation_defers_the_kill(monkeypatch):
    # An ensure_running reservation (DEV-116) counts as activity: the
    # watchdog must treat it like an in-flight request, refresh idle time,
    # and keep the child alive. The loop would spin forever on continue, so
    # the second sleep raises to stop the test.
    calls = {"n": 0}

    def _sleep(_s):
        calls["n"] += 1
        if calls["n"] > 1:
            raise TimeoutError("stop loop")

    monkeypatch.setattr(llama_server.time, "sleep", _sleep)
    mgr = _running_manager()
    mgr._watchdog_generation = 1
    mgr._active_requests = 1
    mgr._shutdown_unlocked = mock.Mock()

    try:
        mgr._idle_watchdog(gen=1)
    except TimeoutError:
        pass

    mgr._shutdown_unlocked.assert_not_called()
    assert mgr.last_request_time > time.time() - 5, (
        "in-flight work must refresh the idle clock"
    )
