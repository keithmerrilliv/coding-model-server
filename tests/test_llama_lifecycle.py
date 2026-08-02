"""DEV-156/157/158 — VRAM-record self-correction, orphan handling, and
prompt upstream cancellation."""
import logging
import subprocess
from unittest import mock

import pytest

from coding_model_server.llama_server import (
    InsufficientVramError,
    LlamaServerManager,
    UpstreamCancel,
)


@pytest.fixture
def mgr():
    return LlamaServerManager()


# ── DEV-156: the VRAM record must be able to self-correct ────────────────────

class TestVramRecord:
    def test_three_refusals_clear_a_suspect_record(self, mgr):
        mgr._measured_vram_delta_mib["impl"] = 999_999  # inflated record
        with mock.patch.object(mgr, "_gpu_free_mib", return_value=10_000):
            with pytest.raises(InsufficientVramError):
                mgr._check_vram_or_raise("impl")
            with pytest.raises(InsufficientVramError):
                mgr._check_vram_or_raise("impl")
            # Third consecutive refusal: record cleared, load allowed.
            mgr._check_vram_or_raise("impl")
        assert "impl" not in mgr._measured_vram_delta_mib, (
            "an unfalsifiable record must clear itself instead of bricking "
            "the agent until a daemon restart"
        )

    def test_a_pass_resets_the_refusal_counter(self, mgr):
        mgr._measured_vram_delta_mib["impl"] = 8_000
        with mock.patch.object(mgr, "_gpu_free_mib", return_value=1_000):
            with pytest.raises(InsufficientVramError):
                mgr._check_vram_or_raise("impl")
        with mock.patch.object(mgr, "_gpu_free_mib", return_value=16_000):
            mgr._check_vram_or_raise("impl")  # passes; counter resets
        assert mgr._vram_refusals.get("impl") is None
        assert mgr._measured_vram_delta_mib["impl"] == 8_000, (
            "a record that passes again is trusted again"
        )

    def test_impossible_delta_is_not_recorded(self, mgr, caplog):
        # A measured delta exceeding the card's total VRAM cannot be the
        # model's footprint — another process moved memory between the two
        # readings. Recording it would guarantee eternal refusals (the
        # DEV-156 brick), so the recording arm in ensure_running must drop
        # it. The incoherent free readings are the point: that is what
        # cross-process interference looks like.
        mgr._wait_for_vram_release = mock.Mock()
        mgr._gpu_free_mib = mock.Mock(side_effect=[40_000, 10_000])
        mgr.start = mock.Mock()
        with mock.patch.object(mgr, "_gpu_total_mib", return_value=16_000), \
                caplog.at_level(logging.WARNING):
            mgr.ensure_running({"path": "/models/x.gguf", "n_ctx": 8192},
                               agent_id="impl")
        assert "impl" not in mgr._measured_vram_delta_mib, (
            "an impossible delta must be discarded, not recorded as a "
            "footprint that no future load can satisfy"
        )
        assert "cross-process interference" in caplog.text

    def test_gpu_total_is_cached(self, mgr):
        fake = mock.Mock(returncode=0, stdout="16384\n")
        with mock.patch.object(subprocess, "run", return_value=fake) as run:
            assert mgr._gpu_total_mib() == 16384
            assert mgr._gpu_total_mib() == 16384
        assert run.call_count == 1, "total VRAM never changes; query once"


# ── DEV-157: orphan reaping ──────────────────────────────────────────────────

class TestOrphanReaping:
    def test_foreign_server_on_the_port_refuses_start(self, mgr, tmp_path, monkeypatch):
        monkeypatch.setattr(LlamaServerManager, "_PIDFILE", tmp_path / "llama.pid")
        ok = mock.Mock(status_code=200)
        with mock.patch.object(mgr._session, "get", return_value=ok):
            with pytest.raises(RuntimeError, match="already answers /health"):
                mgr._reap_orphan_llama_server()

    def test_free_port_and_no_pidfile_is_a_noop(self, mgr, tmp_path, monkeypatch):
        import requests as http_requests
        monkeypatch.setattr(LlamaServerManager, "_PIDFILE", tmp_path / "llama.pid")
        with mock.patch.object(mgr._session, "get",
                               side_effect=http_requests.ConnectionError()):
            mgr._reap_orphan_llama_server()  # must not raise

    def test_pidfile_with_recycled_pid_is_not_killed(self, mgr, tmp_path, monkeypatch):
        # A pid-file pointing at a recycled pid whose cmdline is NOT
        # llama-server must never be signalled.
        import requests as http_requests
        pidfile = tmp_path / "llama.pid"
        pidfile.write_text("1\n")  # init — definitely not llama-server
        monkeypatch.setattr(LlamaServerManager, "_PIDFILE", pidfile)
        with mock.patch.object(mgr._session, "get",
                               side_effect=http_requests.ConnectionError()), \
             mock.patch("os.killpg") as killpg, \
             mock.patch("os.kill") as kill:
            mgr._reap_orphan_llama_server()
        killpg.assert_not_called()
        kill.assert_not_called()
        assert not pidfile.exists(), "stale pid-file is cleaned up"


# ── DEV-158: upstream cancel handle ──────────────────────────────────────────

class TestUpstreamCancel:
    def test_close_after_register_closes_the_response(self):
        h = UpstreamCancel()
        resp = mock.Mock()
        h.register(resp)
        h.close()
        resp.close.assert_called_once()

    def test_close_before_register_closes_on_arrival(self):
        # The client can vanish during ensure_running, before the upstream
        # POST even exists — the late-arriving response must still be cut.
        h = UpstreamCancel()
        h.close()
        resp = mock.Mock()
        h.register(resp)
        resp.close.assert_called_once()

    def test_close_is_idempotent(self):
        h = UpstreamCancel()
        resp = mock.Mock()
        h.register(resp)
        h.close()
        h.close()
        resp.close.assert_called_once()

    def test_proxy_stream_registers_the_live_response(self, mgr):
        handle = UpstreamCancel()
        resp = mock.Mock()
        resp.status_code = 200
        resp.iter_lines.return_value = iter(
            ['data: {"choices": [{"delta": {"content": "x"}, '
             '"finish_reason": "stop"}]}', "data: [DONE]"])
        resp.__enter__ = mock.Mock(return_value=resp)
        resp.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(mgr, "_post_with_recovery", return_value=resp):
            list(mgr.proxy_stream([], "sys", "m", 128, 0.2,
                                  model_config={"n_ctx": 1024},
                                  upstream_cancel=handle))
        handle.close()
        resp.close.assert_called_once()
