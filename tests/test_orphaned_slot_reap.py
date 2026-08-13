"""A leaked reservation must not deadlock every future swap — DEV-582.

Cancelling a spec whose model call is in flight leaves the route's
release_slot() hook unrun, so `_active_requests` stays pinned at 1. The swap
guard then defers EVERY subsequent swap ("another request is in flight"), the
next spec's planner never gets its model, and the pipeline stalls until
coding-model-server is restarted. Observed 2026-08-12: 15+ minutes of
`model swap for dense_architect deferred` every 5s with nothing running.

Time alone cannot detect this: the idle watchdog refreshes last_request_time
while `_active_requests > 0`, so a leaked count looks both busy AND fresh
forever. The discriminator is `_live_proxies` — a genuine in-flight request
always has a proxy_* executing behind its reservation; an abandoned one never
does. A leak is only reaped after it has persisted for ORPHAN_SLOT_REAP_S, so
the legitimate reserve→proxy gap (RAG + tokenize) is never disturbed.
"""
from unittest import mock

import pytest

from coding_model_server.llama_server import (
    LlamaServerManager,
    ModelBusyError,
    _STATE_RUNNING,
)

MODEL_A = {"path": "/models/a.gguf", "n_ctx": 32768}
MODEL_B = {"path": "/models/b.gguf", "n_ctx": 65536}


def _manager_running(model_config):
    mgr = LlamaServerManager()
    proc = mock.Mock()
    proc.poll.return_value = None
    mgr.process = proc
    mgr._state = _STATE_RUNNING
    mgr.current_model_path = model_config["path"]
    mgr.current_model_config = model_config
    mgr.current_runtime_signature = mgr._runtime_signature(model_config)
    return mgr


def _leaked(mgr):
    """The production shape: a reservation whose release never ran."""
    mgr.ensure_running(MODEL_A, agent_id="agent-a", reserve_slot=True)
    assert mgr.has_active_requests()
    assert mgr._live_proxies == 0, "nothing is executing behind the reservation"


# ── the deadlock is broken ──────────────────────────────────────────────────

def test_a_leaked_reservation_is_reaped_once_it_goes_stale(monkeypatch):
    """After the threshold, the swap proceeds instead of deferring forever."""
    mgr = _manager_running(MODEL_A)
    _leaked(mgr)

    # First encounter only starts the clock — still refuses.
    with pytest.raises(ModelBusyError):
        mgr.ensure_running(MODEL_B, agent_id="agent-b")

    # Age the observation past the threshold.
    mgr._orphan_slot_since -= (mgr.ORPHAN_SLOT_REAP_S + 1)

    with mock.patch.object(mgr, "_shutdown_unlocked"), \
            mock.patch.object(mgr, "_wait_for_vram_release"), \
            mock.patch.object(mgr, "_check_vram_or_raise"), \
            mock.patch.object(mgr, "start"), \
            mock.patch.object(mgr, "_restore_slot_state"), \
            mock.patch.object(mgr, "_gpu_free_mib", return_value=None):
        mgr.ensure_running(MODEL_B, agent_id="agent-b")

    assert mgr._active_requests == 0, "the leaked count must be cleared"


def test_the_reap_is_not_immediate(monkeypatch):
    """The reserve→proxy gap (RAG + tokenize) must never be reaped: a fresh
    reservation with no proxy yet still defers the swap."""
    mgr = _manager_running(MODEL_A)
    _leaked(mgr)

    for _ in range(3):
        with pytest.raises(ModelBusyError):
            mgr.ensure_running(MODEL_B, agent_id="agent-b")
    assert mgr.has_active_requests(), "a fresh reservation is still protected"


# ── real work is never reaped, however long it runs ─────────────────────────

def test_a_live_proxy_is_never_reaped_however_stale():
    """A 45-minute architect generation is genuine work behind its
    reservation. It must keep deferring the swap no matter how long it runs —
    reaping it would SIGTERM the child mid-generation."""
    mgr = _manager_running(MODEL_A)
    mgr.ensure_running(MODEL_A, agent_id="agent-a", reserve_slot=True)
    mgr._live_proxies = 1                      # a proxy is executing
    mgr._orphan_slot_since = 0.0               # ancient, if it were consulted

    with pytest.raises(ModelBusyError):
        mgr.ensure_running(MODEL_B, agent_id="agent-b")
    assert mgr._active_requests == 1, "live work must survive the guard"
    assert mgr._orphan_slot_since is None, "a live proxy resets the clock"


def test_a_proxy_starting_clears_a_pending_orphan_observation():
    """The clock started, then the proxy actually began: no longer a leak."""
    mgr = _manager_running(MODEL_A)
    _leaked(mgr)
    with pytest.raises(ModelBusyError):
        mgr.ensure_running(MODEL_B, agent_id="agent-b")
    assert mgr._orphan_slot_since is not None

    # proxy_sync/proxy_stream both do this on entry.
    with mgr.lock:
        mgr._live_proxies += 1
        mgr._orphan_slot_since = None

    with pytest.raises(ModelBusyError):
        mgr.ensure_running(MODEL_B, agent_id="agent-b")
    assert mgr._active_requests == 1


def test_release_slot_clears_the_orphan_clock():
    mgr = _manager_running(MODEL_A)
    _leaked(mgr)
    with pytest.raises(ModelBusyError):
        mgr.ensure_running(MODEL_B, agent_id="agent-b")
    assert mgr._orphan_slot_since is not None

    mgr.release_slot()
    assert mgr._orphan_slot_since is None
    assert not mgr.has_active_requests()
