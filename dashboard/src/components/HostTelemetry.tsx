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
      <h2>Host · CPU</h2>
      <div className="api-error">{error}</div>
    </div>
  );

  const last = samples.length ? samples[samples.length - 1] : null;
  const util = samples.map(s => s.util_cpu);
  const power = samples.map(s => s.power_w);
  const meanPower = mean(power);
  const powerOff = meta ? !meta.cpu_power_available : false;

  return (
    <div className="card">
      <h2>
        Host · CPU
        <span className="metric-window-hint">
          live · 1 Hz · last {samples.length}s
          {meta?.cpu_count ? ` · ${meta.cpu_count} threads` : ''}
        </span>
      </h2>

      <div className="endpoint-charts">
        <ChartCol
          label="utilization"
          value={last?.util_cpu != null ? `${last.util_cpu.toFixed(1)}%` : '—'}
          color="#0d6efd"
        >
          <Sparkline values={util} color="#0d6efd" fill height={36} yMin={0} yMax={100} />
        </ChartCol>

        <ChartCol
          label="package power"
          // An unreadable sensor must never render as 0 W. That exact
          // substitution put two months of fictitious zeroes into the stats
          // log (DEV-725), and a panel is a louder place to repeat it.
          value={powerOff ? 'unavailable'
            : last?.power_w != null ? `${last.power_w.toFixed(1)} W` : '—'}
          color={powerOff ? '#6c757d' : '#d63384'}
        >
          {!powerOff && <Sparkline values={power} color="#d63384" fill height={36} yMin={0} />}
        </ChartCol>

        <ChartCol
          label={`mean power ${samples.length}s`}
          value={meanPower != null ? `${meanPower.toFixed(1)} W` : '—'}
          color="#6f42c1"
        >
          <span className="chart-col-note">package only, not wall draw</span>
        </ChartCol>

        <ChartCol
          label="load 1m"
          value={last?.load1 != null ? last.load1.toFixed(2) : '—'}
          color="#198754"
        >
          <span className="chart-col-note">
            {last?.load5 != null && last?.load15 != null
              ? `5m ${last.load5.toFixed(2)} · 15m ${last.load15.toFixed(2)}`
              : ''}
          </span>
        </ChartCol>
      </div>

      {powerOff && (
        <p className="host-note">
          CPU power is <strong>not being measured</strong>. The RAPL energy
          counter is root-only by default — that restriction is the mitigation
          for PLATYPUS (CVE-2020-8694). Utilization and load above need no
          privilege and are live. To enable power, run{' '}
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

const ChartCol: React.FC<{
  label: string;
  value: string;
  color: string;
  children: React.ReactNode;
}> = ({ label, value, color, children }) => (
  <div className="chart-col">
    <div className="chart-col-head">
      <span className="chart-col-label">{label}</span>
      <span className="chart-col-value" style={{ color }}>{value}</span>
    </div>
    {children}
  </div>
);

export default HostTelemetry;
