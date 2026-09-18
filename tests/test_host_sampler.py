"""DEV-726: host CPU telemetry, and the rule that unmeasured stays unmeasured.

The panel this feeds exists because "what is this box drawing" could not be
answered on screen. The requirement that carries the weight is the same one
DEV-725 was filed for: a sensor that cannot be read reports null, never zero.
A permission error rendered as "0 W" is how two months of fictitious CPU
power got into the stats log.
"""
import pytest

from coding_model_server.metrics import HostSampler


@pytest.fixture
def sampler():
    return HostSampler(interval_s=0.05)


class TestUtilization:
    """Needs no privilege, so it must keep working when power does not."""

    def test_first_sample_has_no_utilization(self, sampler):
        # Utilization is a delta; one reading cannot produce one, and
        # reporting 0% would be a fabricated idle.
        assert sampler._sample_once()["util_cpu"] is None

    def test_second_sample_measures(self, sampler):
        import time as _t
        sampler._sample_once()
        # Jiffies advance at 100 Hz; two back-to-back reads see the same
        # totals, and a zero denominator correctly yields no measurement
        # rather than a fabricated 0%.
        _t.sleep(0.05)
        util = sampler._sample_once()["util_cpu"]
        assert util is not None
        assert 0.0 <= util <= 100.0

    def test_no_elapsed_jiffies_is_unknown_not_zero(self, sampler, monkeypatch):
        """d_total == 0. Reporting 0% busy there would be the same
        fabrication this panel guards against.

        The counters are stubbed rather than read back-to-back: on a loaded
        box two real reads can straddle a tick, so timing the calls would
        make this pass or fail with the machine's mood.
        """
        monkeypatch.setattr(sampler, "_read_jiffies", lambda: (100, 400))
        sampler._sample_once()
        assert sampler._sample_once()["util_cpu"] is None

    def test_unreadable_proc_stat_is_none_not_zero(self, sampler, monkeypatch):
        monkeypatch.setattr(sampler, "_read_jiffies", lambda: None)
        assert sampler._sample_once()["util_cpu"] is None


class TestPower:
    def test_unreadable_counter_reports_none_and_a_reason(self, sampler, monkeypatch):
        monkeypatch.setattr("coding_model_server.metrics._RAPL_PATH",
                            "/nonexistent/energy_uj")
        sampler._sample_once()
        sampler._sample_once()
        snap = sampler.snapshot()
        assert snap["cpu_power_available"] is False
        assert "FileNotFoundError" in (snap["cpu_power_error"] or "")
        assert all(s["power_w"] is None for s in snap["samples"])

    def test_a_readable_counter_produces_watts(self, sampler, tmp_path, monkeypatch):
        f = tmp_path / "energy_uj"
        f.write_text("0")
        monkeypatch.setattr("coding_model_server.metrics._RAPL_PATH", str(f))
        sampler._sample_once()
        f.write_text(str(50_000_000))          # 50 J since the last read
        import time as _t
        _t.sleep(0.05)
        sample = sampler._sample_once()
        assert sample["power_w"] is not None and sample["power_w"] > 0
        assert sampler.snapshot()["cpu_power_available"] is True

    def test_a_counter_that_went_backwards_is_unknown(self, sampler, tmp_path, monkeypatch):
        # A wrap or a reset is not a measurement, and must not surface as a
        # negative watt figure or collapse to zero.
        f = tmp_path / "energy_uj"
        f.write_text(str(10_000_000))
        monkeypatch.setattr("coding_model_server.metrics._RAPL_PATH", str(f))
        sampler._sample_once()
        f.write_text("5")
        assert sampler._sample_once()["power_w"] is None

    def test_power_absent_does_not_suppress_utilization(self, sampler, monkeypatch):
        """The expected state on an unprivileged host: half the card works."""
        import time as _t
        monkeypatch.setattr("coding_model_server.metrics._RAPL_PATH",
                            "/nonexistent/energy_uj")
        sampler._sample_once()
        _t.sleep(0.05)                 # let jiffies advance, as 1 Hz does
        s = sampler._sample_once()
        assert s["power_w"] is None
        assert s["util_cpu"] is not None


class TestSnapshotContract:
    """Same shape as gpu_stats so the panel polls both identically."""

    def _fill(self, sampler, n=4):
        for _ in range(n):
            sampler._ring.append(sampler._sample_once())

    def test_since_is_incremental(self, sampler):
        self._fill(sampler)
        newest = sampler.snapshot()["samples"][-1]["t"]
        assert sampler.snapshot(since=newest)["samples"] == []

    def test_limit_keeps_the_newest(self, sampler):
        self._fill(sampler, 6)
        full = sampler.snapshot()["samples"]
        assert sampler.snapshot(limit=2)["samples"] == full[-2:]

    def test_empty_ring_is_unavailable_not_an_error(self, sampler):
        snap = sampler.snapshot()
        assert snap["available"] is False
        assert snap["samples"] == []

    def test_reports_the_thread_count(self, sampler):
        assert sampler.snapshot()["cpu_count"] is None or \
            sampler.snapshot()["cpu_count"] > 0
