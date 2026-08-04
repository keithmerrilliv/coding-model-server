import React, { useEffect, useState } from 'react';
import { fetchRagStats } from '../api/client';
import type { RagStatsResponse, RagEvent, RagOutcome } from '../types/api';

// Retrieval is not high-frequency — one call per agent turn at most, and turns
// run for minutes. 5s keeps the panel current without polling a mostly-idle
// counter at 1 Hz the way the GPU panel has to.
const RAG_POLL_MS = 5000;

// MEMORY_RELEVANCE_THRESHOLD: max cosine DISTANCE, so lower is a better match
// and anything at-or-above this was rejected. Mirrored here only to colour the
// distances; the server is the authority.
const DISTANCE_CEILING = 0.6;

// Measured against the live collection (DEV-494): genuine matches landed at
// 0.28-0.45, while confident nonsense sat at 0.51-0.54 — under the ceiling and
// therefore injected. That band is the thing worth seeing at a glance.
const DISTANCE_GOOD = 0.45;

const OUTCOME_LABEL: Record<RagOutcome, string> = {
  injected: 'injected',
  empty: 'no match',
  skipped: 'skipped',
  timeout: 'timed out',
  error: 'error',
};

const OUTCOME_COLOR: Record<RagOutcome, string> = {
  injected: '#198754',
  empty: '#6c757d',
  skipped: '#adb5bd',
  timeout: '#fd7e14',
  error: '#dc3545',
};

const distanceColor = (d: number | null): string => {
  if (d == null) return 'inherit';
  if (d <= DISTANCE_GOOD) return '#198754';
  if (d < DISTANCE_CEILING) return '#fd7e14';
  return '#dc3545';
};

const shortTime = (iso: string): string => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleTimeString();
};

const RagPanel: React.FC = () => {
  const [data, setData] = useState<RagStatsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      const res = await fetchRagStats();
      if (cancelled) return;
      if (res.error) { setError(res.error); return; }
      if (!res.data) return;
      setError(null);
      setData(res.data);
    };
    poll();
    const id = setInterval(poll, RAG_POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  if (error) return (
    <div className="card">
      <h2>Memory Retrieval</h2>
      <p>{error}</p>
    </div>
  );
  if (!data) return (
    <div className="card">
      <h2>Memory Retrieval</h2>
      <p>Loading…</p>
    </div>
  );

  const counts = data.counts || {};
  const injected = data.injected ?? 0;
  const attempted = data.attempted ?? 0;
  const recent: RagEvent[] = data.recent || [];

  return (
    <div className="card">
      <h2>
        Memory Retrieval
        <span className="metric-window-hint"> live · RAG injections</span>
      </h2>

      <div className="metric-row">
        <div className="metric-row-head">
          <span className="metric-label">Injected / attempted</span>
          <span className="metric-value">
            <span style={{ color: OUTCOME_COLOR.injected }}>{injected}</span>
            {' / '}{attempted}
            {data.hit_rate != null ? (
              <span className="metric-pct"> ({(data.hit_rate * 100).toFixed(0)}%)</span>
            ) : null}
          </span>
        </div>
        <div className="metric-row-hint">
          {attempted === 0
            ? 'Nothing has attempted retrieval — a flag set to on does not mean it ran (DEV-488).'
            : 'Attempted excludes skip_memory callers. A low rate is fine when the corpus has nothing on topic.'}
        </div>
      </div>

      <div className="metric-row">
        <div className="metric-row-head">
          <span className="metric-label">Outcomes</span>
          <span className="metric-value">
            {(Object.keys(OUTCOME_LABEL) as RagOutcome[])
              .filter(k => (counts[k] ?? 0) > 0)
              .map((k, i, arr) => (
                <span key={k}>
                  <span style={{ color: OUTCOME_COLOR[k] }}>
                    {counts[k]} {OUTCOME_LABEL[k]}
                  </span>
                  {i < arr.length - 1 ? ' · ' : ''}
                </span>
              ))}
            {Object.values(counts).every(v => !v) ? '—' : null}
          </span>
        </div>
        <div className="metric-row-hint">
          &quot;no match&quot; means retrieval ran and everything scored above the {DISTANCE_CEILING} distance
          ceiling — the correct result when the corpus holds nothing relevant.
        </div>
      </div>

      <div className="metric-row">
        <div className="metric-row-head">
          <span className="metric-label">Recent queries</span>
        </div>
        {recent.length === 0 ? (
          <div className="metric-row-hint">No retrieval yet.</div>
        ) : (
          <table className="rag-recent">
            <thead>
              <tr>
                <th>time</th>
                <th>agent</th>
                <th>outcome</th>
                <th>hits</th>
                <th>best</th>
                <th>query</th>
              </tr>
            </thead>
            <tbody>
              {recent.map((e, i) => (
                <tr key={`${e.t}-${i}`}>
                  <td>{shortTime(e.t)}</td>
                  <td>{e.agent ?? '—'}</td>
                  <td style={{ color: OUTCOME_COLOR[e.outcome] }}>
                    {OUTCOME_LABEL[e.outcome] ?? e.outcome}
                  </td>
                  <td>{e.hits ?? '—'}</td>
                  <td style={{ color: distanceColor(e.best_distance) }}>
                    {e.best_distance != null ? e.best_distance.toFixed(3) : '—'}
                  </td>
                  <td className="rag-query" title={e.query ?? ''}>{e.query ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div className="metric-row-hint">
          Distance is cosine, so lower is better. Green ≤ {DISTANCE_GOOD} is a genuine match; amber up to
          the {DISTANCE_CEILING} ceiling is where DEV-494&apos;s wrong-but-confident hits sat, and is worth
          reading the query for.
        </div>
      </div>
    </div>
  );
};

export default RagPanel;
