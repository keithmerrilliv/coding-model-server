"""In-memory request KPI + GPU telemetry for the dashboard.

Two collectors:
  - RequestMetricsCollector: HTTP middleware feeds it (method, path,
    optional subkey, duration, status). Bucketed snapshots feed live
    sparklines on the dashboard.
  - GpuSampler: 1 Hz background thread, shells out to nvidia-smi, keeps
    a rolling buffer of GPU utilisation/VRAM/power/clocks/temp.

Neither collector is admin-key gated at this layer; the snapshot endpoints
in server.py are. Everything is in-memory — no persistence. Cardinality is
bounded because the middleware records the matched route template, not the
raw URL, and the only handler-set subkey is `agent_id` on chat completions.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Per-endpoint ring of (monotonic_ts, duration_ms, status_code).
# 600 ≈ 60 s @ 10 req/s — generous; trims itself anyway.
_PER_ENDPOINT_RING_SIZE = 600

# 1-Hz GPU samples. 120 = 2 min of history (we render the last 60).
_GPU_RING_SIZE = 120

_EndpointKey = Tuple[str, str, Optional[str]]


def _default_error_category(status: int) -> str:
    """Status-code-only classification — used when the handler didn't
    stamp a more specific category on the request.

    Categories are stable strings consumed by the dashboard, so they should
    only ever be added to, not renamed.
    """
    if status in (401, 403):
        return "4xx_auth"
    if status == 404:
        return "4xx_not_found"
    if status == 429:
        return "4xx_rate_limit"
    if 400 <= status < 500:
        return "4xx_other"
    if status == 503:
        return "5xx_unavailable"
    if status == 504:
        return "5xx_timeout"
    if 500 <= status < 600:
        return "5xx_other"
    return "other"


class RequestMetricsCollector:
    """Per-(method, path, subkey) ring buffer of recent request samples.

    `error_category` is an optional, free-form string the request handler
    can stamp on the request to override status-code-only classification
    (e.g. "5xx_proxy_disconnected" vs the generic "5xx_other"). The
    collector accumulates a per-endpoint counter so the dashboard can
    show the breakdown next to the aggregate non-2xx rate.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rings: Dict[_EndpointKey, Deque[Tuple[float, float, int]]] = (
            defaultdict(lambda: deque(maxlen=_PER_ENDPOINT_RING_SIZE))
        )
        self._totals: Dict[_EndpointKey, int] = defaultdict(int)
        self._last_seen: Dict[_EndpointKey, float] = {}
        # Per-endpoint, per-category running count of non-2xx responses.
        # Categories are derived from status + an optional override the
        # request handler stamps on request.state.error_category.
        self._error_breakdown: Dict[_EndpointKey, Dict[str, int]] = (
            defaultdict(lambda: defaultdict(int))
        )

    def record(self, method: str, path: str, subkey: Optional[str],
               duration_ms: float, status: int,
               error_category: Optional[str] = None) -> None:
        key: _EndpointKey = (method, path, subkey)
        now = time.time()
        with self._lock:
            self._rings[key].append((now, duration_ms, status))
            self._totals[key] += 1
            self._last_seen[key] = now
            if status >= 400:
                category = error_category or _default_error_category(status)
                self._error_breakdown[key][category] += 1

    def snapshot(self, window_seconds: int = 60) -> dict:
        """Bucketed summary suitable for direct chart consumption.

        For each endpoint we emit `window_seconds` 1-second buckets so the
        x-axis stays uniform whether the endpoint fired or not. Endpoints
        idle for more than 5 min and with zero in-window activity are
        dropped from the snapshot AND evicted from the underlying ring/total
        dicts on the way out — without eviction, scanner traffic against
        unmatched FastAPI routes (server.py falls back to the literal path
        for 404s) would create permanent dict entries.
        """
        now = time.time()
        cutoff = now - window_seconds
        idle_drop = now - 300.0
        out: List[dict] = []
        evict: List[_EndpointKey] = []
        with self._lock:
            for key, ring in self._rings.items():
                samples_in_window = [s for s in ring if s[0] >= cutoff]
                last_seen = self._last_seen.get(key, 0.0)
                if not samples_in_window and last_seen < idle_drop:
                    evict.append(key)
                    continue
                method, path, subkey = key
                breakdown = dict(self._error_breakdown.get(key, {}))
                out.append({
                    "method": method,
                    "path": path,
                    "subkey": subkey,
                    "total_count": self._totals[key],
                    "last_seen_seconds_ago": (
                        round(now - last_seen, 2) if last_seen else None
                    ),
                    "window_count": len(samples_in_window),
                    "buckets": _build_buckets(samples_in_window, now, window_seconds),
                    "error_breakdown": breakdown,
                })
            # Evict idle keys so unbounded 404 / scanner traffic can't grow
            # the dicts forever. Done inside the same lock as the iteration
            # to avoid a concurrent record() racing the delete.
            for key in evict:
                self._rings.pop(key, None)
                self._totals.pop(key, None)
                self._last_seen.pop(key, None)
                self._error_breakdown.pop(key, None)
        out.sort(key=lambda e: (
            e["last_seen_seconds_ago"]
            if e["last_seen_seconds_ago"] is not None else 1e9
        ))
        return {
            "now": datetime.now(timezone.utc).isoformat(),
            "window_seconds": window_seconds,
            "endpoints": out,
        }


def _build_buckets(samples: List[Tuple[float, float, int]],
                   now: float, window_seconds: int) -> List[dict]:
    buckets: List[dict] = []
    if window_seconds <= 0:
        return buckets
    # Pre-bucket samples by floored 1-s offset for O(N) instead of O(N*W).
    by_offset: Dict[int, List[Tuple[float, int]]] = defaultdict(list)
    for ts, dur, status in samples:
        offset = int(now - ts)  # 0 = current second, window_seconds-1 = oldest
        if 0 <= offset < window_seconds:
            by_offset[offset].append((dur, status))
    for i in range(window_seconds):
        # Emit oldest → newest so the array reads left-to-right on the chart.
        offset = window_seconds - 1 - i
        bucket = by_offset.get(offset, [])
        if not bucket:
            buckets.append({
                "t_offset": -offset,
                "count": 0,
                "p50_ms": None,
                "p95_ms": None,
                "errors": 0,
            })
            continue
        durations = sorted(d for d, _ in bucket)
        errors = sum(1 for _, s in bucket if s >= 400)
        buckets.append({
            "t_offset": -offset,
            "count": len(bucket),
            "p50_ms": _percentile(durations, 0.5),
            "p95_ms": _percentile(durations, 0.95),
            "errors": errors,
        })
    return buckets


def _percentile(sorted_values: List[float], q: float) -> Optional[float]:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return round(sorted_values[0], 1)
    k = (len(sorted_values) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = k - lo
    return round(sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac, 1)


_GPU_QUERY_FIELDS = (
    "utilization.gpu",
    "utilization.memory",
    "memory.used",
    "memory.total",
    "power.draw",
    "power.limit",
    "clocks.gr",
    "clocks.mem",
    "temperature.gpu",
    "pstate",
)


class GpuSampler:
    """1-Hz nvidia-smi sampler with a ring-buffered history."""

    def __init__(self, interval_s: float = 1.0) -> None:
        self._interval = interval_s
        self._ring: Deque[dict] = deque(maxlen=_GPU_RING_SIZE)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._available = self._probe()
        # Cached per-process; the GPU power cap doesn't change.
        self._static_power_limit: Optional[float] = None
        self._static_vram_total: Optional[float] = None

    @staticmethod
    def _probe() -> bool:
        try:
            subprocess.run(
                ["nvidia-smi", "--version"],
                capture_output=True, timeout=2.0, check=True,
            )
            return True
        except (FileNotFoundError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired, OSError):
            return False

    def start(self) -> None:
        if not self._available:
            logger.warning("GpuSampler: nvidia-smi unavailable, GPU stats disabled")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="GpuSampler",
        )
        self._thread.start()
        logger.info("GpuSampler started (interval=%.1fs)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            sample = self._sample_once()
            if sample is not None:
                with self._lock:
                    self._ring.append(sample)
            self._stop.wait(self._interval)

    def _sample_once(self) -> Optional[dict]:
        try:
            out = subprocess.run(
                [
                    "nvidia-smi",
                    f"--query-gpu={','.join(_GPU_QUERY_FIELDS)}",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True, text=True, timeout=2.0, check=True,
            ).stdout.strip()
        except (FileNotFoundError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired, OSError) as e:
            logger.debug("nvidia-smi sample failed: %s", e)
            return None
        # Single-GPU rig — first row is the only row. Multi-GPU support
        # would enumerate rows here.
        first_row = out.splitlines()[0] if out else ""
        cells = [c.strip() for c in first_row.split(",")]
        if len(cells) != len(_GPU_QUERY_FIELDS):
            logger.debug("Unexpected nvidia-smi output: %r", first_row)
            return None

        def _f(s: str) -> Optional[float]:
            # nvidia-smi emits "[N/A]" for unsupported metrics on some
            # cards — coerce to None so the JSON stays clean.
            try:
                return float(s)
            except ValueError:
                return None

        sample = {
            "t": datetime.now(timezone.utc).isoformat(),
            "util_gpu": _f(cells[0]),
            "util_mem": _f(cells[1]),
            "vram_used_mib": _f(cells[2]),
            "vram_total_mib": _f(cells[3]),
            "power_w": _f(cells[4]),
            "power_limit_w": _f(cells[5]),
            "clock_gr_mhz": _f(cells[6]),
            "clock_mem_mhz": _f(cells[7]),
            "temp_c": _f(cells[8]),
            "pstate": cells[9] or None,
        }
        if self._static_power_limit is None and sample["power_limit_w"]:
            self._static_power_limit = sample["power_limit_w"]
        if self._static_vram_total is None and sample["vram_total_mib"]:
            self._static_vram_total = sample["vram_total_mib"]
        return sample

    def snapshot(self, since: "str | None" = None,
                 limit: "int | None" = None) -> dict:
        """Ring snapshot, optionally incremental (DEV-159).

        ``since``: ISO-8601 timestamp — only samples strictly newer are
        returned. Lexicographic comparison is correct because samples carry
        UTC ISO strings of uniform format. The dashboard passes its newest
        seen timestamp so 1Hz polls carry one new sample instead of the
        whole 120-entry ring (~99% redundant JSON per tab).
        ``limit``: keep only the newest N — the panel renders 60, so
        shipping the other half of the ring was pure waste.
        """
        with self._lock:
            samples = list(self._ring)
        if since:
            samples = [s for s in samples if s["t"] > since]
        if limit is not None and len(samples) > limit:
            samples = samples[-limit:]
        return {
            "available": self._available,
            "interval_s": self._interval,
            "power_limit_w": self._static_power_limit,
            "vram_total_mib": self._static_vram_total,
            "samples": samples,
        }


# Module-level singletons. server.py imports + drives these.
request_metrics = RequestMetricsCollector()
gpu_sampler = GpuSampler(interval_s=1.0)
