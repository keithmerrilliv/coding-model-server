"""DEV-116 — no swap may sneak in between ensure_running and the proxy call.

chat_completions returns a StreamingResponse whose generator isn't iterated
until after the route returns, so _active_requests used to stay 0 through
token counting. A second agent's ensure_running in that gap saw
has_active_requests()==False, passed the busy guard, and swapped the child —
the first request's generator then POSTed to a port serving the other model
and silently received wrong-model output labeled as its own agent.

The fix: ensure_running(reserve_slot=True) counts the caller's upcoming
proxy call in _active_requests before _swap_lock is released; the caller
owns that slot and releases it exactly once via release_slot().
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
    """A manager whose (fake) child is alive and serving model_config."""
    mgr = LlamaServerManager()
    proc = mock.Mock()
    proc.poll.return_value = None
    mgr.process = proc
    mgr._state = _STATE_RUNNING
    mgr.current_model_path = model_config["path"]
    mgr.current_model_config = model_config
    mgr.current_runtime_signature = mgr._runtime_signature(model_config)
    return mgr


def test_reserve_slot_counts_the_request_before_the_lock_is_released():
    mgr = _manager_running(MODEL_A)
    mgr.ensure_running(MODEL_A, agent_id="agent-a", reserve_slot=True)
    assert mgr.has_active_requests(), (
        "the reservation must be visible to the busy guard as soon as "
        "ensure_running returns"
    )
    mgr.release_slot()
    assert not mgr.has_active_requests()


def test_swap_is_refused_while_a_reservation_is_outstanding():
    # Agent A holds a reservation but hasn't POSTed yet (the TOCTOU gap).
    # Agent B needs a different runtime signature: the busy guard must defer
    # the swap instead of SIGTERMing the child A is about to talk to.
    mgr = _manager_running(MODEL_A)
    mgr.ensure_running(MODEL_A, agent_id="agent-a", reserve_slot=True)

    with pytest.raises(ModelBusyError):
        mgr.ensure_running(MODEL_B, agent_id="agent-b")

    # Once A's stream finishes and the slot is released, B may swap.
    mgr.release_slot()
    mgr._shutdown_unlocked = mock.Mock()
    mgr._wait_for_vram_release = mock.Mock()
    mgr._check_vram_or_raise = mock.Mock()
    mgr._gpu_free_mib = mock.Mock(return_value=None)
    mgr.start = mock.Mock()
    mgr.ensure_running(MODEL_B, agent_id="agent-b")
    mgr._shutdown_unlocked.assert_called_once()


def test_same_signature_reservations_stack_without_busy_error():
    # Two agents sharing one runtime signature don't need a swap, so both
    # may reserve concurrently.
    mgr = _manager_running(MODEL_A)
    mgr.ensure_running(MODEL_A, agent_id="agent-a", reserve_slot=True)
    mgr.ensure_running(MODEL_A, agent_id="agent-a2", reserve_slot=True)
    assert mgr._active_requests == 2
    mgr.release_slot()
    mgr.release_slot()
    assert mgr._active_requests == 0


def test_release_slot_floors_at_zero():
    mgr = LlamaServerManager()
    mgr.release_slot()
    assert mgr._active_requests == 0


def test_reserved_proxy_does_not_double_count():
    # With reserved=True the proxy must neither increment on entry nor
    # decrement on exit — the caller owns the slot.
    mgr = _manager_running(MODEL_A)
    mgr.ensure_running(MODEL_A, agent_id="agent-a", reserve_slot=True)

    resp = mock.Mock()
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    mgr._post_with_recovery = mock.Mock(return_value=resp)

    mgr.proxy_sync([], "system", "agent-a", 128, 0.2,
                   model_config=MODEL_A, reserved=True)
    assert mgr._active_requests == 1, "proxy_sync(reserved=True) must not touch the count"
    mgr.release_slot()
    assert mgr._active_requests == 0
