import React, { useEffect, useState } from 'react';
import { fetchMetrics } from '../api/client';
import Sparkline from './Sparkline';
import type { MetricsResponse, EndpointMetric } from '../types/api';

const POLL_MS = 1000;
const WINDOW_S = 60;

const fmt = (v: number | null | undefined, decimals = 0): string =>
  v == null ? '—' : v.toFixed(decimals);

const lastNonNull = (arr: (number | null)[]): number | null => {
  for (let i = arr.length - 1; i >= 0; i--) {
    if (arr[i] != null) return arr[i];
  }
  return null;
};

const EndpointMetrics: React.FC = () => {
  const [data, setData] = useState<MetricsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      const res = await fetchMetrics(WINDOW_S);
      if (cancelled) return;
      if (res.error) setError(res.error);
      else if (res.data) { setData(res.data); setError(null); }
    };
    poll();
    const id = setInterval(poll, POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  if (error) return (
    <div className="card">
      <h2>Endpoint KPIs</h2>
      <div className="api-error">{error}</div>
    </div>
  );
  if (!data) return (
    <div className="card">
      <h2>Endpoint KPIs</h2>
      <p>Loading…</p>
    </div>
  );

  return (
    <div className="card">
      <h2>
        Endpoint KPIs
        <span className="metric-window-hint"> live · 1 Hz · last {data.window_seconds}s in 1s buckets</span>
      </h2>
      {data.endpoints.length === 0 ? (
        <p style={{ color: '#6c757d' }}>No traffic yet — fire some requests at the server.</p>
      ) : (
        <div className="endpoint-list">
          {data.endpoints.map(ep => (
            <EndpointRow
              key={`${ep.method}|${ep.path}|${ep.subkey ?? ''}`}
              ep={ep}
            />
          ))}
        </div>
      )}
    </div>
  );
};

const EndpointRow: React.FC<{ ep: EndpointMetric }> = ({ ep }) => {
  const counts = ep.buckets.map(b => b.count);
  const p50s = ep.buckets.map(b => b.p50_ms);
  const p95s = ep.buckets.map(b => b.p95_ms);
  const errors = ep.buckets.map(b => b.errors);

  // `errors` here counts every status >= 400 (server-side metrics.py:123).
  // Labelled "non-2xx" rather than "err" because 4xx auth-fails from a
  // forgotten LAN tab would otherwise read as endpoint health problems.
  const totalNon2xx = errors.reduce((a, b) => a + b, 0);
  const non2xxPct = ep.window_count
    ? ((totalNon2xx / ep.window_count) * 100).toFixed(1)
    : '0.0';
  const lastP50 = lastNonNull(p50s);
  const lastP95 = lastNonNull(p95s);

  // counts within a 60s window are a direct proxy for requests/min
  const qpm = ep.window_count;

  // Aggregate cumulative breakdown to one tooltip-style line. Empty
  // when no errors have happened yet — this is intentionally cumulative
  // (not windowed) so flaky-categories don't disappear when the bucket
  // window rolls over.
  const breakdownEntries = Object.entries(ep.error_breakdown ?? {})
    .filter(([, n]) => n > 0)
    .sort(([, a], [, b]) => b - a);

  return (
    <div className="endpoint-row">
      <div className="endpoint-title">
        <span className={`http-method http-${ep.method.toLowerCase()}`}>{ep.method}</span>
        <code className="endpoint-path">{ep.path}</code>
        {ep.subkey && <span className="endpoint-subkey">[{ep.subkey}]</span>}
        <span className="endpoint-meta">
          {ep.total_count.toLocaleString()} total · last {ep.last_seen_seconds_ago ?? '—'}s ago
          {breakdownEntries.length > 0 && (
            <span className="endpoint-breakdown" title="cumulative non-2xx breakdown">
              {' · '}
              {breakdownEntries.map(([cat, n]) => `${cat}=${n}`).join(' ')}
            </span>
          )}
        </span>
      </div>
      <div className="endpoint-charts">
        <ChartCol label="req/min" value={String(qpm)} color="#0d6efd">
          <Sparkline values={counts} color="#0d6efd" fill height={36} />
        </ChartCol>
        <ChartCol label="p50" value={lastP50 != null ? `${fmt(lastP50, 0)}ms` : '—'} color="#198754">
          <Sparkline values={p50s} color="#198754" height={36} />
        </ChartCol>
        <ChartCol label="p95" value={lastP95 != null ? `${fmt(lastP95, 0)}ms` : '—'} color="#fd7e14">
          <Sparkline values={p95s} color="#fd7e14" height={36} />
        </ChartCol>
        <ChartCol label="non-2xx" value={`${non2xxPct}%`} color="#dc3545">
          <Sparkline values={errors} color="#dc3545" fill height={36} />
        </ChartCol>
      </div>
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

export default EndpointMetrics;
