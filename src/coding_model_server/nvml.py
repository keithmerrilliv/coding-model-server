"""One NVML session per process for code that just needs a VRAM number.

NVML reads replace ``nvidia-smi`` subprocesses: a fork per reading is a
fresh driver session each time, and one of those sessions panicked the
host (DEV-925). A handle held here costs microseconds per call and spawns
nothing. The dashboard sampler in metrics.py keeps its own handle because
its retry and backoff policy is part of what it reports; everything else
goes through here.

Thread-safe: the watchdog, teardown and request threads share one handle
under a lock. A failed read drops the handle so the next call runs
nvmlInit again, which is how a late or reloaded driver self-heals.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional, Tuple

# Soft dependency: without the bindings every reader returns None and the
# callers take their "GPU unreadable" branches, as they did when nvidia-smi
# was missing.
try:
    import pynvml
except ImportError:  # pragma: no cover - the tests patch this instead
    pynvml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_MIB = 1024 ** 2

_lock = threading.Lock()
_handle = None


def _attach_locked():
    """Return the device-0 handle, initialising NVML on first use."""
    global _handle
    if _handle is not None:
        return _handle
    if pynvml is None:
        return None
    try:
        pynvml.nvmlInit()
        _handle = pynvml.nvmlDeviceGetHandleByIndex(0)
    except (pynvml.NVMLError, OSError) as e:
        logger.debug("NVML init failed: %s", e)
        _detach_locked()
    return _handle


def _detach_locked() -> None:
    global _handle
    _handle = None
    if pynvml is None:
        return
    try:
        pynvml.nvmlShutdown()
    except (pynvml.NVMLError, OSError):
        # Never initialised, or already torn down with the driver.
        pass


def memory_mib() -> Optional[Tuple[int, int]]:
    """``(free, total)`` MiB for device 0, or None when the GPU can't be read.

    ``free`` is what a new CUDA context can actually allocate, so reserved
    memory is excluded; this matches what ``nvidia-smi --query-gpu=memory.free``
    reported to the callers that were ported from it.
    """
    with _lock:
        h = _attach_locked()
        if h is None:
            return None
        try:
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        except (pynvml.NVMLError, OSError) as e:
            # The driver went away under us (reload, GPU lost). Drop the
            # handle so the next call re-initialises instead of failing on
            # a stale one forever.
            logger.debug("NVML memory read failed: %s", e)
            _detach_locked()
            return None
        return int(mem.free // _MIB), int(mem.total // _MIB)
