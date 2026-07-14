"""The VRAM pre-flight guard: refuse loads that don't fit, allow loads that do.

The guard used to demand `free >= recorded + margin`, where `recorded` is the
footprint measured across a load that SUCCEEDED. For an agent whose footprint
lands within `margin` of the card's capacity, that asks for VRAM the card does
not have, so no amount of freeing can satisfy it — the GPU is already idle and
there is nothing left to free.

`implementer` at n_cpu_moe=18 was exactly that agent: it loaded fine, left ~500
MiB free, and then every reload 503'd. The first load in a process is exempt
(nothing recorded yet), so the server looked healthy until the idle watchdog
reaped the child ~30 minutes in — after which it bricked itself until a human
restarted it. The workaround was to back every agent off its measured-best
n_cpu_moe, which is decode thrown away to avoid a cliff that shouldn't exist.

These pin the guard to the only thing it can honestly assert: a model that was
measured at `recorded` MiB fits in `recorded` MiB, because we watched it happen.
"""
import logging
from unittest import mock

import pytest

from coding_model_server.llama_server import (
    InsufficientVramError,
    LlamaServerManager,
)

# The real shape of the bug, in round numbers. An RTX 5080 with ~15,900 MiB free
# when idle, loading an agent that leaves ~500 MiB free.
IDLE_FREE = 15_900
FOOTPRINT = 15_400  # what the successful load actually consumed
AGENT = "implementer"


@pytest.fixture
def mgr():
    return LlamaServerManager()  # no side effects in __init__


def _measured(m, agent=AGENT, footprint=FOOTPRINT, free_now=IDLE_FREE):
    """Manager that has already loaded `agent` once and recorded its footprint."""
    m._measured_vram_delta_mib[agent] = footprint
    m._gpu_free_mib = mock.Mock(return_value=free_now)
    return m


class TestGuardAllowsWhatAlreadyFit:
    def test_reload_allowed_when_footprint_leaves_less_than_the_margin(self, mgr):
        """The brick, in one assertion.

        The GPU is idle and as free as it has ever been, and the model is the
        same one that loaded here minutes ago. free (15,850) exceeds the measured
        footprint (15,400), so it fits. The old check wanted 15,400 + 500 = 15,900
        and refused — permanently, since nothing could ever free the missing 50 MiB.
        """
        _measured(mgr, free_now=15_850)
        mgr._check_vram_or_raise(AGENT)  # must not raise

    def test_exactly_enough_is_enough(self, mgr):
        """free == recorded. It fit in exactly this much last time, by measurement."""
        _measured(mgr, free_now=FOOTPRINT)
        mgr._check_vram_or_raise(AGENT)

    def test_warns_when_it_fits_but_the_margin_does_not(self, mgr, caplog):
        _measured(mgr, free_now=FOOTPRINT + 100)
        with caplog.at_level(logging.WARNING):
            mgr._check_vram_or_raise(AGENT)
        assert "100 MiB to spare" in caplog.text

    def test_silent_when_the_margin_is_comfortable(self, mgr, caplog):
        _measured(mgr, footprint=8_000, free_now=15_000)
        with caplog.at_level(logging.WARNING):
            mgr._check_vram_or_raise(AGENT)
        assert caplog.text == ""


class TestGuardStillRefusesWhatDoesNotFit:
    def test_refuses_when_another_process_holds_the_vram_we_need(self, mgr):
        """The guard's actual job, and it must survive the fix.

        Not a margin shortfall — the model does not fit at the size we measured
        it at. Something else is holding VRAM; loading would OOM the child.
        """
        _measured(mgr, free_now=FOOTPRINT - 1)
        with pytest.raises(InsufficientVramError, match="only 15399 MiB free"):
            mgr._check_vram_or_raise(AGENT)

    def test_refuses_when_the_gpu_is_badly_oversubscribed(self, mgr):
        _measured(mgr, free_now=2_000)
        with pytest.raises(InsufficientVramError):
            mgr._check_vram_or_raise(AGENT)


class TestGuardAbstains:
    def test_first_load_is_exempt(self, mgr):
        """Nothing recorded yet, so there is nothing to check against."""
        mgr._gpu_free_mib = mock.Mock(return_value=10)
        mgr._check_vram_or_raise("never_loaded")  # must not raise

    def test_no_agent_id_is_exempt(self, mgr):
        _measured(mgr, free_now=0)
        mgr._check_vram_or_raise(None)

    def test_nvidia_smi_unavailable_proceeds(self, mgr):
        """No reading is not the same as a bad reading. Don't block the load."""
        _measured(mgr)
        mgr._gpu_free_mib = mock.Mock(return_value=None)
        mgr._check_vram_or_raise(AGENT)


class TestFirstLoadAnnouncesATightFit:
    """The load that creates the problem should be the load that reports it.

    Before, a doomed-tight config was silent at load and only spoke up 30 minutes
    later as a 503 from a different code path, by which point the config that
    caused it was no longer the obvious suspect.
    """

    def _drive_one_load(self, m, free_before, free_after):
        m._wait_for_vram_release = mock.Mock()
        m._gpu_free_mib = mock.Mock(side_effect=[free_before, free_after])
        m.start = mock.Mock()
        m.ensure_running({"path": "/models/x.gguf", "n_ctx": 8192}, agent_id=AGENT)

    def test_warns_when_the_first_load_leaves_no_cushion(self, mgr, caplog):
        with caplog.at_level(logging.WARNING):
            self._drive_one_load(mgr, free_before=IDLE_FREE, free_after=480)
        assert "leaves only 480 MiB free" in caplog.text
        assert mgr._measured_vram_delta_mib[AGENT] == IDLE_FREE - 480

    def test_quiet_when_the_first_load_leaves_room(self, mgr, caplog):
        with caplog.at_level(logging.WARNING):
            self._drive_one_load(mgr, free_before=IDLE_FREE, free_after=2_400)
        assert "leaves only" not in caplog.text
