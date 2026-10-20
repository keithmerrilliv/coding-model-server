"""DEV-925 — coding_model_server.nvml is the process-wide VRAM reader.

It replaced per-call ``nvidia-smi`` subprocesses in llama_server (free VRAM
is polled at 5 Hz during a swap). The contract: a (free, total) MiB pair
or None, one handle held across calls, re-initialised after a failure.
"""
from types import SimpleNamespace
from unittest import mock

import pytest

import coding_model_server.nvml as nvml


class FakeNvml:
    class NVMLError(Exception):
        pass

    def __init__(self, mode: str = "up") -> None:
        self.mode = mode
        self.inits = 0
        self.shutdowns = 0
        self.reads = 0

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

    def nvmlDeviceGetMemoryInfo(self, h):  # noqa: N802
        self.reads += 1
        self._check()
        return SimpleNamespace(free=14766 * 2**20, used=1536 * 2**20,
                               total=16303 * 2**20)


@pytest.fixture
def fake():
    f = FakeNvml()
    with mock.patch.object(nvml, "pynvml", f), \
            mock.patch.object(nvml, "_handle", None):
        yield f


def test_reads_free_and_total_in_mib(fake):
    assert nvml.memory_mib() == (14766, 16303)


def test_one_init_across_many_reads(fake):
    """The whole point: no per-call session, let alone a per-call process."""
    for _ in range(50):
        assert nvml.memory_mib() is not None
    assert fake.inits == 1
    assert fake.reads == 50


def test_none_without_bindings():
    with mock.patch.object(nvml, "pynvml", None), \
            mock.patch.object(nvml, "_handle", None):
        assert nvml.memory_mib() is None


def test_none_while_driver_is_down_then_recovers(fake):
    fake.mode = "down"
    assert nvml.memory_mib() is None
    assert nvml.memory_mib() is None
    fake.mode = "up"
    assert nvml.memory_mib() == (14766, 16303)
    # Every failed attempt tried nvmlInit again: that retry is the boot-race
    # fix (DEV-432) carried over from the sampler.
    assert fake.inits == 3


def test_read_failure_drops_the_handle(fake):
    assert nvml.memory_mib() is not None
    fake.mode = "down"
    assert nvml.memory_mib() is None
    assert fake.shutdowns >= 1 and nvml._handle is None
    fake.mode = "up"
    assert nvml.memory_mib() == (14766, 16303)
    assert fake.inits == 2
