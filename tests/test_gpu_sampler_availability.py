"""DEV-432 — GpuSampler availability is live, not latched at startup.

The sampler used to probe nvidia-smi once in __init__ and refuse to start
when that failed, so a server that came up before the nvidia driver was
ready reported no GPU for its entire lifetime.
"""
import logging
import subprocess
import threading
from unittest import mock

import pytest

from coding_model_server.metrics import GpuSampler

# One CSV row in the order of _GPU_QUERY_FIELDS.
GOOD_ROW = "42, 17, 8000, 15751, 210.5, 360.0, 2400, 10501, 61, P2\n"


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["nvidia-smi"], returncode=0, stdout=stdout)


@pytest.fixture
def sampler():
    s = GpuSampler(interval_s=0.01)
    yield s
    s.stop()


def _run_ticks(sampler: GpuSampler, side_effect, ticks: int) -> None:
    """Drive the real loop until it has called nvidia-smi `ticks` times."""
    seen = threading.Event()
    calls = {"n": 0}

    def fake_run(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= ticks:
            seen.set()
        result = side_effect(calls["n"])
        if isinstance(result, Exception):
            raise result
        return result

    with mock.patch.object(subprocess, "run", side_effect=fake_run):
        sampler.start()
        assert seen.wait(timeout=5.0), f"loop did not reach {ticks} ticks"
        # Let the tick that satisfied the wait finish updating state.
        for _ in range(200):
            if sampler.snapshot()["available"] == (
                not isinstance(side_effect(calls["n"]), Exception)
            ):
                break
            threading.Event().wait(0.01)


class TestBootRace:
    def test_start_runs_even_when_nvidia_smi_is_down(self, sampler):
        """The old code returned early here and never started the thread."""
        with mock.patch.object(
            subprocess, "run",
            side_effect=subprocess.CalledProcessError(9, "nvidia-smi"),
        ):
            sampler.start()
            assert sampler._thread is not None and sampler._thread.is_alive()
        assert sampler.snapshot()["available"] is False

    def test_recovers_once_the_driver_comes_up(self, sampler):
        """Fail like a not-yet-ready driver, then succeed: no restart needed."""
        def side_effect(n):
            if n <= 2:
                return subprocess.CalledProcessError(9, "nvidia-smi")
            return _completed(GOOD_ROW)

        # Retry backoff would make this slow; the interval is what gets
        # capped, so shrink it for the test.
        with mock.patch("coding_model_server.metrics._GPU_RETRY_INTERVAL_S", 0.01):
            _run_ticks(sampler, side_effect, ticks=4)

        snap = sampler.snapshot()
        assert snap["available"] is True
        assert len(snap["samples"]) >= 1
        assert snap["samples"][-1]["util_gpu"] == 42.0
        assert snap["vram_total_mib"] == 15751.0

    def test_goes_unavailable_again_if_nvidia_smi_dies(self, sampler):
        """Driver reloaded under a long-running server."""
        def side_effect(n):
            if n <= 2:
                return _completed(GOOD_ROW)
            return FileNotFoundError("nvidia-smi")

        with mock.patch("coding_model_server.metrics._GPU_RETRY_INTERVAL_S", 0.01):
            _run_ticks(sampler, side_effect, ticks=4)

        assert sampler.snapshot()["available"] is False


class TestLogging:
    def test_missing_gpu_still_explains_itself_once(self, sampler, caplog):
        """A GPU-less host must say why the panel is empty, exactly once."""
        with caplog.at_level(logging.WARNING, logger="coding_model_server.metrics"), \
                mock.patch("coding_model_server.metrics._GPU_RETRY_INTERVAL_S", 0.01), \
                mock.patch.object(
                    subprocess, "run", side_effect=FileNotFoundError("nvidia-smi")):
            sampler.start()
            threading.Event().wait(0.2)

        warnings = [r for r in caplog.records if "not responding" in r.message]
        assert len(warnings) == 1, f"expected one warning, got {len(warnings)}"

    def test_no_probe_method_remains(self):
        """The latching probe is gone, not merely bypassed."""
        assert not hasattr(GpuSampler, "_probe")
