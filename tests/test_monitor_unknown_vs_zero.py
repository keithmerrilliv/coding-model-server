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


class TestGpuSensor:
    def test_gpu_failure_is_unknown_not_zero(self, mon, monkeypatch):
        monkeypatch.setattr(mon.subprocess, "check_output",
                            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
        mon._nvml_handle = None
        assert mon.get_gpu_power() is None

    def test_gpu_value_passes_through(self, mon, monkeypatch):
        monkeypatch.setattr(mon.subprocess, "check_output", lambda *a, **k: " 97.40 \n")
        mon._nvml_handle = None
        assert mon.get_gpu_power() == pytest.approx(97.40)


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
