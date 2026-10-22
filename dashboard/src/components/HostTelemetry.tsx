import React, { useEffect, useRef, useState } from 'react';
import { fetchHostStats } from '../api/client';
import Sparkline from './Sparkline';
import type { HostStatsResponse, HostSample } from '../types/api';

// The sampler runs at 1 Hz. Polling faster buys nothing; polling slower makes
// the sparkline lie about its own time axis.
const POLL_MS = 1000;
const KEEP = 60;

const mean = (xs: (number | null)[]): number | null => {
  // Nulls are SKIPPED, not counted as zero. That substitution is the whole
  // reason this panel exists — see the note in the render.
  const got = xs.filter((x): x is number => x != null);
  return got.length ? got.reduce((a, b) => a + b, 0) / got.length : null;
};

const gib = (mib: number | null | undefined, decimals = 1): string =>
  mib == null ? '—' : (mib / 1024).toFixed(decimals);

const pct = (part: number | null | undefined, whole: number | null | undefined): string =>
  part != null && whole ? ` (${((part / whole) * 100).toFixed(0)}%)` : '';

// Full-width rows with a tall sparkline, the GPU panel's shape (DEV-939):
// CPU utilization and memory are what this box spends on MoE expert
// offload, and they read at a glance only when they are as large as the
// GPU's own utilization row beside them.
const HostTelemetry: React.FC = () => {
  const [meta, setMeta] = useState<HostStatsResponse | null>(null);
  const [samples, setSamples] = useState<HostSample[]>([]);
  const [error, setError] = useState<string | null>(null);
  const newest = useRef<string | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      const res = await fetchHostStats(newest.current);
      if (cancelled) return;
      if (res.error) { setError(res.error); return; }
      if (!res.data) return;
      setError(null);
      setMeta(res.data);
      if (res.data.samples.length) {
        newest.current = res.data.samples[res.data.samples.length - 1].t;
        setSamples(prev => [...prev, ...res.data!.samples].slice(-KEEP));
      }
    };
    poll();
    const id = setInterval(poll, POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  if (error) return (
    <div className="card">
      <h2>Host · CPU and memory</h2>
      <div className="api-error">{error}</div>
    </div>
  );

  const last = samples.length ? samples[samples.length - 1] : null;
  const util = samples.map(s => s.util_cpu);
  const power = samples.map(s => s.power_w);
  const used = samples.map(s => s.mem_used_mib);
  const cached = samples.map(s => s.mem_cached_mib);
  const meanPower = mean(power);
  const powerOff = meta ? !meta.cpu_power_available : false;
  const memTotal = meta?.mem_total_mib ?? last?.mem_total_mib ?? null;
  const memOff = meta != null && samples.length > 0 && memTotal == null;
  const swapNone = last?.swap_total_mib === 0;

  return (
    <div className="card">
      <h2>
        Host · CPU and memory
        <span className="metric-window-hint">
          live · 1 Hz · last {samples.length}s
          {meta?.cpu_count ? ` · ${meta.cpu_count} threads` : ''}
        </span>
      </h2>

      <div className="metric-row">
        <div className="metric-row-head">
          <span className="metric-label">CPU util</span>
          <span className="metric-value">
            <span style={{ color: '#0d6efd' }}>
              {last?.util_cpu != null ? `${last.util_cpu.toFixed(1)}%` : '—'}
            </span>
          </span>
        </div>
        <div className="metric-row-spark">
          <Sparkline values={util} yMin={0} yMax={100} color="#0d6efd" fill />
        </div>
        <div className="metric-row-hint">
          {last?.load1 != null && last?.load5 != null && last?.load15 != null
            ? `load ${last.load1.toFixed(2)} · ${last.load5.toFixed(2)} · ${last.load15.toFixed(2)} (1m · 5m · 15m)`
            : 'load averages unavailable'}
          {' · '}expert offload runs here, so this and GPU mem util climb together on the cpu_moe rungs
        </div>
      </div>

      <div className="metric-row">
        <div className="metric-row-head">
          <div>
            <span className="metric-label">Memory used</span>{' '}
            <span className="metric-label-secondary">/ total</span>
          </div>
          <span className="metric-value">
            {memOff ? 'unavailable' : `${gib(last?.mem_used_mib)} / ${gib(memTotal, 0)} GiB`}
            {!memOff && <span className="metric-pct">{pct(last?.mem_used_mib, memTotal)}</span>}
          </span>
        </div>
        <div className="metric-row-spark">
          <Sparkline values={used} yMin={0} yMax={memTotal ?? undefined} color="#198754" fill />
        </div>
        <div className="metric-row-hint">
          used = total − available; the page cache below is not counted here
        </div>
      </div>

      <div className="metric-row">
        <div className="metric-row-head">
          <span className="metric-label">Page cache</span>
          <span className="metric-value">
            {memOff ? 'unavailable' : `${gib(last?.mem_cached_mib)} GiB`}
            {!memOff && <span className="metric-pct">{pct(last?.mem_cached_mib, memTotal)}</span>}
          </span>
        </div>
        <div className="metric-row-spark">
          <Sparkline values={cached} yMin={0} yMax={memTotal ?? undefined} color="#fd7e14" fill />
        </div>
        <div className="metric-row-hint">
          the mmapped model weights live here; a rung that fits stays cached between calls, two that do not take turns evicting each other
        </div>
      </div>

      <div className="metric-row">
        <div className="metric-row-head">
          <span className="metric-label">CPU package power</span>
          <span className="metric-value" style={{ color: powerOff ? '#6c757d' : '#d63384' }}>
            {/* An unreadable sensor must never render as 0 W. That exact
                substitution put two months of fictitious zeroes into the
                stats log (DEV-725), and a panel is a louder place to repeat it. */}
            {powerOff ? 'unavailable'
              : last?.power_w != null ? `${last.power_w.toFixed(1)} W` : '—'}
          </span>
        </div>
        {!powerOff && (
          <div className="metric-row-spark">
            <Sparkline values={power} yMin={0} color="#d63384" fill height={36} />
          </div>
        )}
      </div>

      <div className="metric-tile-grid">
        <Tile label="available" value={memOff ? '—' : `${gib(last?.mem_available_mib)} GiB`} />
        <Tile label="dirty" value={memOff ? '—' : `${gib(last?.mem_dirty_mib)} GiB`} />
        <Tile label="swap used"
          value={memOff ? '—' : swapNone ? 'none' : `${gib(last?.swap_used_mib)} / ${gib(last?.swap_total_mib, 0)} GiB`} />
        <Tile label={`mean power ${samples.length}s`}
          value={meanPower != null ? `${meanPower.toFixed(1)} W` : '—'} />
      </div>

      {powerOff && (
        <p className="host-note">
          CPU power is <strong>not being measured</strong>. The RAPL energy
          counter is root-only by default — that restriction is the mitigation
          for PLATYPUS (CVE-2020-8694). Utilization, memory and load above need
          no privilege and are live. To enable power, run{' '}
          <code>sudo bash scripts/enable_rapl_reading.sh --execute</code> and
          restart the server.
          {meta?.cpu_power_error && (
            <><br /><span className="host-note-reason">{meta.cpu_power_error}</span></>
          )}
        </p>
      )}

      <p className="host-note">
        Package power only. Nothing here measures the motherboard, RAM,
        drives, fans or PSU loss, so treat it as a floor on what the wall
        meter sees rather than an estimate of it.
      </p>
    </div>
  );
};

const Tile: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <div className="metric-tile">
    <div className="metric-tile-label">{label}</div>
    <div className="metric-tile-value">{value}</div>
  </div>
);

export default HostTelemetry;
