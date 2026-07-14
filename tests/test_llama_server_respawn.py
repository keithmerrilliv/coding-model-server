"""Regression tests: ensure_running must respawn a child that died out of band.

A CUDA crash, the OOM-killer, or an external kill (scripts/_llama_bench.py frees
the GPU exactly this way) terminates the llama-server child without anything
transitioning the manager's _state, which stays RUNNING. ensure_running used to
decide "already correct" from _state alone, so it early-returned without
respawning; the proxy then hit a dead port and every request 502'd until the
unit was restarted by hand. The server was wedged, permanently, by a dead child.

is_running() has always consulted process.poll(). These pin ensure_running to
the same definition of "running".
"""
from unittest import mock

import pytest

from coding_model_server.llama_server import (
    _STATE_IDLE,
    _STATE_RUNNING,
    LlamaServerManager,
    ModelBusyError,
)

MODEL_CONFIG = {"path": "/models/x.gguf", "n_ctx": 8192, "n_gpu_layers": 10}


@pytest.fixture
def mgr():
    # No side effects in __init__; safe to construct directly.
    m = LlamaServerManager()
    m._wait_for_vram_release = mock.Mock()
    m._check_vram_or_raise = mock.Mock()
    m._gpu_free_mib = mock.Mock(return_value=99_999)
    m.start = mock.Mock()
    return m


def _attach_child(mgr, returncode):
    """Put the manager in RUNNING with a child whose poll() returns `returncode`.

    returncode None => alive; an int => already exited.
    """
    proc = mock.Mock()
    proc.poll.return_value = returncode
    mgr.process = proc
    mgr._state = _STATE_RUNNING
    mgr.current_model_path = MODEL_CONFIG["path"]
    mgr.current_runtime_signature = mgr._runtime_signature(MODEL_CONFIG)
    return proc


def test_dead_child_is_respawned_even_though_state_says_running(mgr):
    """The wedge: state RUNNING + matching signature + dead child."""
    _attach_child(mgr, returncode=0)  # exited cleanly, e.g. SIGTERM from pkill

    mgr.ensure_running(MODEL_CONFIG, agent_id="implementer")

    mgr.start.assert_called_once_with(MODEL_CONFIG)


def test_crashed_child_is_respawned(mgr):
    """Same, for a non-zero exit (CUDA crash / OOM-killer)."""
    _attach_child(mgr, returncode=-9)

    mgr.ensure_running(MODEL_CONFIG, agent_id="implementer")

    mgr.start.assert_called_once_with(MODEL_CONFIG)


def test_live_child_with_matching_signature_is_left_alone(mgr):
    """The hot path must stay a no-op — no needless reload on every request."""
    _attach_child(mgr, returncode=None)  # alive

    mgr.ensure_running(MODEL_CONFIG, agent_id="implementer")

    mgr.start.assert_not_called()
    assert mgr.current_agent_id == "implementer"


def test_live_child_with_different_config_still_swaps(mgr):
    """A genuine runtime swap is unaffected by the liveness check."""
    _attach_child(mgr, returncode=None)
    other = dict(MODEL_CONFIG, n_ctx=32768)

    mgr.ensure_running(other, agent_id="reviewer")

    mgr.start.assert_called_once_with(other)


def test_cold_start_from_idle(mgr):
    """No child at all: start, and don't trip over process=None."""
    assert mgr._state == _STATE_IDLE and mgr.process is None

    mgr.ensure_running(MODEL_CONFIG, agent_id="implementer")

    mgr.start.assert_called_once_with(MODEL_CONFIG)


def test_dead_child_matches_is_running_view(mgr):
    """ensure_running and is_running must not disagree about 'running'."""
    _attach_child(mgr, returncode=0)

    # is_running() already reported False here — that disagreement (health said
    # model_loaded: false while ensure_running believed it was up) was the bug.
    assert mgr.is_running() is False
    mgr.ensure_running(MODEL_CONFIG, agent_id="implementer")
    mgr.start.assert_called_once()


# ── swap guard (DEV-17 / HIGH-05) ────────────────────────────────────────────
def test_swap_refused_while_live_child_has_active_request(mgr):
    """A swap that would SIGTERM a live child mid-stream must be refused, not
    silently kill the in-flight request."""
    _attach_child(mgr, returncode=None)  # alive, serving agent A
    mgr._active_requests = 1              # another agent's request is streaming
    other = dict(MODEL_CONFIG, n_ctx=32768)  # forces a swap

    with pytest.raises(ModelBusyError):
        mgr.ensure_running(other, agent_id="reviewer")

    mgr.start.assert_not_called()        # child was NOT torn down
    assert mgr.process.terminate.call_count == 0


def test_swap_allowed_when_no_active_requests(mgr):
    """With the child idle, the same swap proceeds normally."""
    _attach_child(mgr, returncode=None)
    mgr._active_requests = 0
    other = dict(MODEL_CONFIG, n_ctx=32768)

    mgr.ensure_running(other, agent_id="reviewer")

    mgr.start.assert_called_once_with(other)


def test_crash_recovery_not_blocked_by_its_own_active_request(mgr):
    """_post_with_recovery calls ensure_running from inside a request that has
    already incremented _active_requests, but the child is DEAD there — the
    guard is gated on child_alive, so recovery must still respawn."""
    _attach_child(mgr, returncode=-9)    # crashed child
    mgr._active_requests = 1             # the recovering request itself

    mgr.ensure_running(MODEL_CONFIG, agent_id="implementer")

    mgr.start.assert_called_once_with(MODEL_CONFIG)


def test_has_active_requests_is_wired(mgr):
    """Promoted from dead code to the swap guard."""
    mgr._active_requests = 0
    assert mgr.has_active_requests() is False
    mgr._active_requests = 2
    assert mgr.has_active_requests() is True
