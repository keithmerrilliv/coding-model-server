"""GpuSampler reads the GPU through NVML, and its availability is live.

Two invariants, each learned the hard way:

  - DEV-432: availability is derived from the current sample, never latched
    at startup. A server that came up before the nvidia driver was ready
    used to report no GPU for its entire lifetime.
  - DEV-925: the sampler never forks ``nvidia-smi``. One of the ~86k daily
    forks panicked the host (kernel double fault in the driver's ioctl
    path), so reads go through nvidia-ml-py in-process.
"""
import logging
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

import coding_model_server.metrics as metrics
from coding_model_server.metrics import GpuSampler


class FakeNvml:
    """Just enough of nvidia-ml-py for GpuSampler.

    ``mode`` is consulted on every call, so a test can flip the driver
    between "up" and "down" mid-run and watch the sampler follow it.
    """

    class NVMLError(Exception):
        pass

    class NVMLError_NotSupported(NVMLError):  # noqa: N801 - mirrors pynvml
        pass

    NVML_CLOCK_GRAPHICS = 0
    NVML_CLOCK_MEM = 1
    NVML_TEMPERATURE_GPU = 0
    NVML_PSTATE_UNKNOWN = 32

    def __init__(self, mode: str = "up") -> None:
        self.mode = mode
        self.inits = 0
        self.shutdowns = 0

    def _check(self) -> None:
        if self.mode == "down":
            raise self.NVMLError("Driver Not Loaded")

    def nvmlInit(self):  # noqa: N802
        self.inits += 1
        self._check()

    def nvmlShutdown(self):  # noqa: N802
        self.shutdowns += 1

    def nvmlDeviceGetHandleByIndex(self, index):  # noqa: N802
        self._check()
        return f"gpu{index}"

    def nvmlDeviceGetUtilizationRates(self, h):  # noqa: N802
        self._check()
        return SimpleNamespace(gpu=42, memory=17)

    def nvmlDeviceGetMemoryInfo(self, h):  # noqa: N802
        self._check()
        return SimpleNamespace(used=8000 * 2**20, total=15751 * 2**20)

    def nvmlDeviceGetPowerUsage(self, h):  # noqa: N802
        self._check()
        return 210_500  # mW

    def nvmlDeviceGetEnforcedPowerLimit(self, h):  # noqa: N802
        self._check()
        return 360_000  # mW

    def nvmlDeviceGetClockInfo(self, h, which):  # noqa: N802
        self._check()
        return 2400 if which == self.NVML_CLOCK_GRAPHICS else 10501

    def nvmlDeviceGetTemperature(self, h, sensor):  # noqa: N802
        self._check()
        return 61

    def nvmlDeviceGetPerformanceState(self, h):  # noqa: N802
        self._check()
        return 2


@pytest.fixture
def nvml():
    fake = FakeNvml()
    with mock.patch.object(metrics, "pynvml", fake):
        yield fake


@pytest.fixture
def sampler():
    s = GpuSampler(interval_s=0.01)
    yield s
    s.stop()


def _run_ticks(sampler: GpuSampler, ticks: int, before_tick=None,
               expect_available: bool = True) -> None:
    """Drive the real loop for `ticks` samples, then wait for state to settle.

    ``before_tick(n)`` runs ahead of tick n, which is where a test flips the
    fake driver. Counting wraps _sample_once rather than any one NVML call,
    so the count is one per loop iteration whatever the driver's state.
    """
    seen = threading.Event()
    calls = {"n": 0}
    real = sampler._sample_once

    def counted():
        calls["n"] += 1
        if before_tick:
            before_tick(calls["n"])
        result = real()
        if calls["n"] >= ticks:
            seen.set()
        return result

    with mock.patch.object(sampler, "_sample_once", side_effect=counted), \
            mock.patch.object(metrics, "_GPU_RETRY_INTERVAL_S", 0.01):
        sampler.start()
        assert seen.wait(timeout=5.0), f"loop did not reach {ticks} ticks"
        # Let the tick that satisfied the wait finish updating state.
        for _ in range(200):
            if sampler.snapshot()["available"] == expect_available:
                break
            threading.Event().wait(0.01)


class TestBootRace:
    def test_start_runs_even_when_driver_is_down(self, nvml, sampler):
        """The old code returned early here and never started the thread."""
        nvml.mode = "down"
        sampler.start()
        assert sampler._thread is not None and sampler._thread.is_alive()
        assert sampler.snapshot()["available"] is False

    def test_recovers_once_the_driver_comes_up(self, nvml, sampler):
        """Fail like a not-yet-ready driver, then succeed: no restart needed."""
        def before_tick(n):
            nvml.mode = "down" if n <= 2 else "up"

        _run_ticks(sampler, ticks=4, before_tick=before_tick)

        snap = sampler.snapshot()
        assert snap["available"] is True
        assert len(snap["samples"]) >= 1
        assert snap["samples"][-1]["util_gpu"] == 42.0
        assert snap["vram_total_mib"] == 15751.0
        # Each failed attach counted as an init; the successful one too.
        assert nvml.inits >= 3

    def test_goes_unavailable_again_if_driver_dies(self, nvml, sampler):
        """Driver reloaded under a long-running server."""
        def before_tick(n):
            nvml.mode = "up" if n <= 2 else "down"

        _run_ticks(sampler, ticks=4, before_tick=before_tick,
                   expect_available=False)

        assert sampler.snapshot()["available"] is False
        # The stale handle was dropped, so the next tick re-initialises
        # rather than reading a dead handle forever.
        assert sampler._handle is None
        assert nvml.shutdowns >= 1


class TestFieldMapping:
    def test_sample_matches_the_dashboard_contract(self, nvml):
        s = GpuSampler(interval_s=1.0)
        sample = s._sample_once()
        s.stop()
        assert sample is not None
        assert sample["util_gpu"] == 42.0
        assert sample["util_mem"] == 17.0
        assert sample["vram_used_mib"] == 8000.0
        assert sample["vram_total_mib"] == 15751.0
        assert sample["power_w"] == 210.5
        assert sample["power_limit_w"] == 360.0
        assert sample["clock_gr_mhz"] == 2400.0
        assert sample["clock_mem_mhz"] == 10501.0
        assert sample["temp_c"] == 61.0
        # The dashboard tile renders nvidia-smi's spelling, not NVML's int.
        assert sample["pstate"] == "P2"
        assert s.snapshot()["power_limit_w"] == 360.0

    def test_unsupported_field_is_none_not_a_failed_sample(self, nvml):
        """nvidia-smi printed "[N/A]" for these; the sample must still land."""
        def not_supported(h):
            raise nvml.NVMLError_NotSupported("Not Supported")
        nvml.nvmlDeviceGetPowerUsage = not_supported

        s = GpuSampler(interval_s=1.0)
        sample = s._sample_once()
        s.stop()
        assert sample is not None
        assert sample["power_w"] is None
        assert sample["temp_c"] == 61.0

    def test_unknown_pstate_is_none(self, nvml):
        nvml.nvmlDeviceGetPerformanceState = lambda h: nvml.NVML_PSTATE_UNKNOWN
        s = GpuSampler(interval_s=1.0)
        sample = s._sample_once()
        s.stop()
        assert sample is not None and sample["pstate"] is None


class TestLogging:
    def test_missing_gpu_still_explains_itself_once(self, nvml, sampler, caplog):
        """A GPU-less host must say why the panel is empty, exactly once."""
        nvml.mode = "down"
        with caplog.at_level(logging.WARNING, logger="coding_model_server.metrics"), \
                mock.patch.object(metrics, "_GPU_RETRY_INTERVAL_S", 0.01):
            sampler.start()
            threading.Event().wait(0.2)

        warnings = [r for r in caplog.records if "not responding" in r.message]
        assert len(warnings) == 1, f"expected one warning, got {len(warnings)}"

    def test_missing_bindings_explain_themselves_once(self, sampler, caplog):
        """No nvidia-ml-py installed: unavailable, said once, no crash."""
        with mock.patch.object(metrics, "pynvml", None), \
                caplog.at_level(logging.WARNING, logger="coding_model_server.metrics"), \
                mock.patch.object(metrics, "_GPU_RETRY_INTERVAL_S", 0.01):
            sampler.start()
            threading.Event().wait(0.2)
            assert sampler.snapshot()["available"] is False

        warnings = [r for r in caplog.records if "nvidia-ml-py missing" in r.message]
        assert len(warnings) == 1


class TestNoSubprocess:
    def test_never_forks_nvidia_smi(self):
        """The whole point: nothing in the metrics module spawns a process."""
        assert not hasattr(metrics, "subprocess")
        assert not hasattr(metrics, "_GPU_QUERY_FIELDS")

    def test_no_probe_method_remains(self):
        """The latching probe is gone, not merely bypassed."""
        assert not hasattr(GpuSampler, "_probe")
