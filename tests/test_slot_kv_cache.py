"""DEV-408 — slot-KV persistence across model swaps.

The DEV-393 incident measured ~478s of prefill for one 195k-token synthesis
prompt, and every model swap threw that KV away. The manager now passes
llama-server's cache flags (--cache-ram, --slot-save-path) and parks/reloads
the live slot's KV around swaps: save on graceful teardown, restore after a
start of the same runtime signature. These tests pin the choreography with
the HTTP session and child process faked — no real llama-server involved.
"""
import types
from unittest import mock

import pytest

from coding_model_server.llama_server import LlamaServerManager


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    m = LlamaServerManager()
    monkeypatch.setattr(
        type(m), "_slot_save_dir",
        property(lambda self: tmp_path / "kv_cache"))
    (tmp_path / "kv_cache").mkdir()
    return m


def _fake_session(slots_json=None, post_status=200):
    session = mock.Mock()
    get_resp = mock.Mock()
    get_resp.json.return_value = slots_json if slots_json is not None else []
    session.get.return_value = get_resp
    post_resp = mock.Mock()
    post_resp.status_code = post_status
    post_resp.text = "err"
    session.post.return_value = post_resp
    return session

def _live_child(mgr, signature=("m.gguf", 131072)):
    mgr.current_runtime_signature = signature
    proc = mock.Mock()
    proc.poll.return_value = None
    mgr.process = proc
    return proc


# ── argv flags ───────────────────────────────────────────────────────────

def test_cache_ram_and_slot_save_path_in_args(mgr):
    cmd = mgr._build_server_args("llama-server", {"path": "m.gguf"})
    assert "--cache-ram" in cmd
    assert cmd[cmd.index("--cache-ram") + 1] == str(mgr.CACHE_RAM_MIB)
    assert "--slot-save-path" in cmd
    assert cmd[cmd.index("--slot-save-path") + 1] == str(mgr._slot_save_dir)


def test_slot_save_path_absent_when_disabled(mgr, monkeypatch):
    monkeypatch.setattr(type(mgr), "SLOT_SAVE_ENABLED", False)
    cmd = mgr._build_server_args("llama-server", {"path": "m.gguf"})
    assert "--slot-save-path" not in cmd


# ── cache-key discipline ─────────────────────────────────────────────────

def test_cache_filename_changes_with_runtime_signature(mgr):
    """llama-server hard-fails restores across model/quant/KV-type changes,
    so any signature difference must produce a different file name."""
    a = mgr._slot_cache_filename(("m.gguf", 131072, 8, 8))
    b = mgr._slot_cache_filename(("m.gguf", 262144, 8, 8))
    c = mgr._slot_cache_filename(("m.gguf", 131072, 8, 8))
    assert a != b
    assert a == c
    assert a.endswith(".bin")


# ── save side ────────────────────────────────────────────────────────────

def test_save_posts_when_context_is_large(mgr):
    # n_prompt_tokens is what the 2026-06 build actually reports (verified
    # live against /slots); n_past is the older name, covered below.
    _live_child(mgr)
    mgr._session = _fake_session([{"n_prompt_tokens": 150_000}])
    mgr._save_slot_state()
    mgr._session.post.assert_called_once()
    url = mgr._session.post.call_args.args[0]
    assert "/slots/0?action=save" in url
    fname = mgr._session.post.call_args.kwargs["json"]["filename"]
    assert fname == mgr._slot_cache_filename(mgr.current_runtime_signature)


def test_save_skipped_below_token_threshold(mgr):
    """Tiny contexts re-prefill faster than a multi-GB file writes."""
    _live_child(mgr)
    mgr._session = _fake_session([{"n_prompt_tokens": 100}])
    mgr._save_slot_state()
    mgr._session.post.assert_not_called()


def test_save_accepts_legacy_n_past_field(mgr):
    _live_child(mgr)
    mgr._session = _fake_session([{"n_past": 150_000}])
    mgr._save_slot_state()
    mgr._session.post.assert_called_once()


def test_save_skipped_with_no_live_child(mgr):
    mgr.current_runtime_signature = ("m.gguf",)
    mgr.process = None
    mgr._session = _fake_session()
    mgr._save_slot_state()
    mgr._session.get.assert_not_called()


def test_save_never_raises(mgr):
    """An error escaping the save would break the swap/shutdown path."""
    _live_child(mgr)
    mgr._session = mock.Mock()
    mgr._session.get.side_effect = RuntimeError("connection torn down")
    mgr._save_slot_state()  # must not raise


def test_unhealthy_shutdown_does_not_attempt_save(mgr, monkeypatch):
    """The health-timeout path passes save_slot=False — an HTTP save against
    a wedged child would hang the recovery."""
    _live_child(mgr)
    saves = []
    monkeypatch.setattr(mgr, "_save_slot_state",
                        lambda: saves.append(True))
    mgr._shutdown_unlocked(save_slot=False)
    assert saves == []


def test_graceful_shutdown_saves_first(mgr, monkeypatch):
    _live_child(mgr)
    order = []
    monkeypatch.setattr(mgr, "_save_slot_state", lambda: order.append("save"))
    real_poll = mgr.process.poll
    mgr.process.terminate = lambda: order.append("term")
    mgr.process.wait = lambda timeout=None: order.append("wait")
    mgr.process.poll = lambda: real_poll()
    mgr._shutdown_unlocked()
    assert order and order[0] == "save", "KV must be parked before SIGTERM"


# ── restore side ─────────────────────────────────────────────────────────

def test_restore_posts_when_file_exists(mgr):
    sig = ("m.gguf", 131072)
    fname = mgr._slot_cache_filename(sig)
    (mgr._slot_save_dir / fname).write_bytes(b"kv")
    mgr._session = _fake_session()
    mgr._restore_slot_state(sig)
    url = mgr._session.post.call_args.args[0]
    assert "/slots/0?action=restore" in url
    assert mgr._session.post.call_args.kwargs["json"]["filename"] == fname


def test_restore_noop_without_file(mgr):
    mgr._session = _fake_session()
    mgr._restore_slot_state(("never-saved.gguf", 1))
    mgr._session.post.assert_not_called()


def test_rejected_restore_discards_the_file(mgr):
    """One bad save must not fail every future start of that model."""
    sig = ("m.gguf", 131072)
    fname = mgr._slot_cache_filename(sig)
    path = mgr._slot_save_dir / fname
    path.write_bytes(b"stale")
    mgr._session = _fake_session(post_status=400)
    mgr._restore_slot_state(sig)
    assert not path.exists()


# ── retention ────────────────────────────────────────────────────────────

def test_trim_removes_oldest_over_budget(mgr, monkeypatch):
    monkeypatch.setattr(type(mgr), "SLOT_SAVE_MAX_TOTAL_GIB",
                        3 / (1024 ** 3))  # 3-byte budget
    older = mgr._slot_save_dir / "old.bin"
    newer = mgr._slot_save_dir / "new.bin"
    older.write_bytes(b"xx")
    newer.write_bytes(b"yy")
    import os
    os.utime(older, (1, 1))
    mgr._trim_slot_cache_dir()
    assert not older.exists(), "oldest file must be evicted first"
    assert newer.exists()
