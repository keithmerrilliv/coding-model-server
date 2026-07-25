import React, { useEffect, useRef, useState } from 'react';
import { fetchGpuStats } from '../api/client';
import Sparkline from './Sparkline';
import type { GpuStatsResponse, GpuSample } from '../types/api';

const GPU_POLL_MS = 1000;
const VISIBLE_SAMPLES = 60;

const fmt = (v: number | null | undefined, decimals = 0): string =>
  v == null ? '—' : v.toFixed(decimals);

const last = <T,>(arr: T[]): T | undefined => arr[arr.length - 1];

const GpuPanel: React.FC = () => {
  const [data, setData] = useState<GpuStatsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Newest sample timestamp we hold — sent as `since` so each poll returns
  // only NEW samples (typically one) instead of the whole ring (DEV-159).
  const lastTsRef = useRef<string | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      const res = await fetchGpuStats(lastTsRef.current);
      if (cancelled) return;
      if (res.error) { setError(res.error); return; }
      if (!res.data) return;
      const incoming = res.data;
      setError(null);
      setData(prev => {
        const merged = prev && lastTsRef.current
          ? [...prev.samples, ...incoming.samples].slice(-VISIBLE_SAMPLES)
          : incoming.samples;
        const newest = merged[merged.length - 1];
        if (newest) lastTsRef.current = newest.t;
        return { ...incoming, samples: merged };
      });
    };
    poll();
    const id = setInterval(poll, GPU_POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  if (error) return (
    <div className="card">
      <h2>GPU Compute</h2>
      <div className="api-error">{error}</div>
    </div>
  );
  if (!data) return (
    <div className="card">
      <h2>GPU Compute</h2>
      <p>Loading…</p>
    </div>
  );
  if (!data.available) return (
    <div className="card">
      <h2>GPU Compute</h2>
      <p>nvidia-smi not available on this host.</p>
    </div>
  );

  const samples: GpuSample[] = data.samples.slice(-VISIBLE_SAMPLES);
  const cur = last(samples);
  const vramTotal = data.vram_total_mib ?? cur?.vram_total_mib ?? 16303;
  const powerLimit = data.power_limit_w ?? cur?.power_limit_w ?? 380;

  const utilGpu = samples.map(s => s.util_gpu);
  const utilMem = samples.map(s => s.util_mem);
  const vram = samples.map(s => s.vram_used_mib);
  const power = samples.map(s => s.power_w);
  const temp = samples.map(s => s.temp_c);

  return (
    <div className="card">
      <h2>
        GPU Compute
        <span className="metric-window-hint"> live · 1 Hz · last {samples.length}s</span>
      </h2>

      <div className="metric-row">
        <div className="metric-row-head">
          <div>
            <span className="metric-label">GPU util</span>{' '}
            <span className="metric-label-secondary">/ memory util</span>
          </div>
          <div className="metric-value">
            <span style={{ color: '#0d6efd' }}>{fmt(cur?.util_gpu)}%</span>
            {' / '}
            <span style={{ color: '#fd7e14' }}>{fmt(cur?.util_mem)}%</span>
          </div>
        </div>
        <div className="metric-row-spark stacked">
          <Sparkline values={utilGpu} yMin={0} yMax={100} color="#0d6efd" fill />
          <Sparkline values={utilMem} yMin={0} yMax={100} color="#fd7e14" />
        </div>
        <div className="metric-row-hint">
          GPU util ≫ mem util → compute-bound · mem util ≈ GPU util → memory-BW bound (cpu_moe likely)
        </div>
      </div>

      <div className="metric-row">
        <div className="metric-row-head">
          <span className="metric-label">VRAM</span>
          <span className="metric-value">
            {fmt(cur?.vram_used_mib, 0)} / {fmt(vramTotal, 0)} MiB
            {cur?.vram_used_mib != null && vramTotal ? (
              <span className="metric-pct"> ({((cur.vram_used_mib / vramTotal) * 100).toFixed(0)}%)</span>
            ) : null}
          </span>
        </div>
        <div className="metric-row-spark">
          <Sparkline values={vram} yMin={0} yMax={vramTotal} color="#198754" fill />
        </div>
      </div>

      <div className="metric-row">
        <div className="metric-row-head">
          <span className="metric-label">Power</span>
          <span className="metric-value">
            {fmt(cur?.power_w, 1)} / {fmt(powerLimit, 0)} W
            {cur?.power_w != null && powerLimit ? (
              <span className="metric-pct"> ({((cur.power_w / powerLimit) * 100).toFixed(0)}%)</span>
            ) : null}
          </span>
        </div>
        <div className="metric-row-spark">
          <Sparkline values={power} yMin={0} yMax={powerLimit} color="#dc3545" fill />
        </div>
      </div>

      <div className="metric-row">
        <div className="metric-row-head">
          <span className="metric-label">Temperature</span>
          <span className="metric-value">{fmt(cur?.temp_c, 0)}°C</span>
        </div>
        <div className="metric-row-spark">
          <Sparkline values={temp} yMin={20} yMax={90} color="#6f42c1" />
        </div>
      </div>

      <div className="metric-tile-grid">
        <Tile label="GPU clock" value={`${fmt(cur?.clock_gr_mhz, 0)} MHz`} />
        <Tile label="Mem clock" value={`${fmt(cur?.clock_mem_mhz, 0)} MHz`} />
        <Tile label="P-state" value={cur?.pstate ?? '—'} />
        <Tile label="Power cap" value={`${fmt(powerLimit, 0)} W`} />
      </div>
    </div>
  );
};

const Tile: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <div className="metric-tile">
    <div className="metric-tile-label">{label}</div>
    <div className="metric-tile-value">{value}</div>
  </div>
);

export default GpuPanel;
