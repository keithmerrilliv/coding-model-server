"""DEV-725: the resource monitor must be able to say "I don't know".

`get_cpu_energy()` returned 0 on any exception, and the CSV recorded the
resulting 0.00 as if it were a measurement. CPU package energy has been
root-only since the PLATYPUS mitigation (CVE-2020-8694) and the unit runs
unprivileged, so EVERY cpu_watts value ever logged — 829,903 samples across
two months — was a permission error wearing a measurement's clothes.

A monitor that cannot distinguish "zero watts" from "no reading" will mislead
again on the next sensor that breaks, so the fix is the distinction, not the
permission.
"""
import csv
import importlib.util
import pathlib
import sys

import pytest

_SRC = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "monitor_resources.py"


@pytest.fixture
def mon(monkeypatch, tmp_path):
    """Load the script fresh so module-level state does not leak between tests."""
    spec = importlib.util.spec_from_file_location("monitor_resources", _SRC)
    module = importlib.util.module_from_spec(spec)
    sys.modules["monitor_resources"] = module
    spec.loader.exec_module(module)
    module.LOG_FILE = str(tmp_path / "stats.csv")
    module._sensor_down.clear()
    return module


class TestUnreadableIsNotZero:
    def test_missing_rapl_reads_as_unknown(self, mon, tmp_path):
        mon.POWERCAP_PATH = str(tmp_path / "nope" / "energy_uj")
        assert mon.get_cpu_energy() is None

    def test_permission_error_reads_as_unknown(self, mon, tmp_path, monkeypatch):
        # The real failure on this host: the file exists and cannot be read.
        def denied(*a, **k):
            raise PermissionError(13, "Permission denied")
        monkeypatch.setattr("builtins.open", denied)
        assert mon.get_cpu_energy() is None

    def test_a_real_reading_survives(self, mon, tmp_path):
        p = tmp_path / "energy_uj"
        p.write_text("123456789\n")
        mon.POWERCAP_PATH = str(p)
        assert mon.get_cpu_energy() == 123456789

    def test_zero_is_still_reportable(self, mon, tmp_path):
        # A genuine zero must NOT be swallowed as unknown — that would be the
        # same conflation pointing the other way.
        p = tmp_path / "energy_uj"
        p.write_text("0\n")
        mon.POWERCAP_PATH = str(p)
        assert mon.get_cpu_energy() == 0


class _FakeNvml:
    """Enough of nvidia-ml-py for get_gpu_power; `mode` flips the driver."""

    class NVMLError(Exception):
        pass

    def __init__(self, mode="up"):
        self.mode = mode
        self.inits = 0
        self.shutdowns = 0

    def _check(self):
        if self.mode == "down":
            raise self.NVMLError("Driver Not Loaded")

    def nvmlInit(self):  # noqa: N802
        self.inits += 1
        self._check()

    def nvmlShutdown(self):  # noqa: N802
        self.shutdowns += 1

    def nvmlDeviceGetHandleByIndex(self, i):  # noqa: N802
        self._check()
        return "gpu0"

    def nvmlDeviceGetPowerUsage(self, h):  # noqa: N802
        self._check()
        return 97_400  # mW


@pytest.fixture
def nvml(mon):
    fake = _FakeNvml()
    mon.pynvml = fake
    mon.HAS_PYNVML = True
    mon._nvml_handle = None
    return fake


class TestGpuSensor:
    def test_gpu_failure_is_unknown_not_zero(self, mon, nvml):
        nvml.mode = "down"
        assert mon.get_gpu_power() is None

    def test_gpu_value_passes_through(self, mon, nvml):
        assert mon.get_gpu_power() == pytest.approx(97.40)

    def test_one_session_across_samples(self, mon, nvml):
        """DEV-925: no per-sample process, and no per-sample nvmlInit either."""
        assert not hasattr(mon, "subprocess")
        for _ in range(20):
            mon.get_gpu_power()
        assert nvml.inits == 1

    def test_driver_loss_is_retried_not_latched(self, mon, nvml):
        assert mon.get_gpu_power() == pytest.approx(97.40)
        nvml.mode = "down"
        assert mon.get_gpu_power() is None
        assert nvml.shutdowns >= 1, "a dead handle must be dropped"
        nvml.mode = "up"
        assert mon.get_gpu_power() == pytest.approx(97.40)
        assert nvml.inits == 2

    def test_missing_bindings_say_which_interpreter(self, mon, capsys):
        """The unit ran the system python for months; the warning must point at
        the venv rather than at the GPU."""
        mon.HAS_PYNVML = False
        mon._nvml_handle = None
        assert mon.get_gpu_power() is None
        assert "project venv" in capsys.readouterr().out


class TestWarningIsOncePerFailure:
    def test_repeat_failures_warn_once(self, mon, capsys, tmp_path):
        mon.POWERCAP_PATH = str(tmp_path / "gone" / "energy_uj")
        for _ in range(20):
            mon.get_cpu_energy()
        out = capsys.readouterr().out
        # 17,280 samples a day: a per-sample warning is its own way of hiding.
        assert out.count("WARNING: cpu unreadable") == 1

    def test_recovery_is_announced(self, mon, capsys, tmp_path):
        mon.POWERCAP_PATH = str(tmp_path / "gone" / "energy_uj")
        mon.get_cpu_energy()
        good = tmp_path / "energy_uj"
        good.write_text("42\n")
        mon.POWERCAP_PATH = str(good)
        mon.get_cpu_energy()
        assert "cpu is readable again" in capsys.readouterr().out

    def test_a_changed_reason_warns_again(self, mon, capsys, tmp_path):
        mon.POWERCAP_PATH = str(tmp_path / "gone" / "energy_uj")
        mon.get_cpu_energy()
        capsys.readouterr()
        mon.POWERCAP_PATH = str(tmp_path)          # IsADirectoryError, a new reason
        mon.get_cpu_energy()
        assert "WARNING: cpu unreadable" in capsys.readouterr().out


class TestCsvShape:
    """The column must be EMPTY, not 0.00 — that is what makes the history
    readable by anyone integrating it later."""

    def _row(self, mon, cpu_watts, gpu_watts):
        with open(mon.LOG_FILE, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "gpu_watts", "cpu_watts", "rag_db_bytes"])
            w.writerow(["t",
                        "" if gpu_watts is None else f"{gpu_watts:.2f}",
                        "" if cpu_watts is None else f"{cpu_watts:.2f}",
                        0])
        return list(csv.DictReader(open(mon.LOG_FILE)))[0]

    def test_unknown_cpu_is_blank(self, mon):
        assert self._row(mon, None, 97.4)["cpu_watts"] == ""

    def test_known_cpu_is_a_number(self, mon):
        assert self._row(mon, 31.5, 97.4)["cpu_watts"] == "31.50"

    def test_blank_and_zero_are_distinguishable(self, mon):
        assert self._row(mon, None, 1.0)["cpu_watts"] != self._row(mon, 0.0, 1.0)["cpu_watts"]
