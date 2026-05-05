import React, { useEffect, useState } from 'react';
import { fetchActiveModel } from '../api/client';
import type { ActiveModelResponse } from '../types/api';

const POLL_MS = 2000;

const fmtCtx = (n: number | null | undefined): string => {
  if (n == null) return '—';
  if (n >= 1024) return `${(n / 1024).toFixed(0)}K`;
  return String(n);
};

const fmtUptime = (s: number | null | undefined): string => {
  if (s == null) return '—';
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
};

const fmtAgo = (s: number | null | undefined): string => {
  if (s == null) return 'never';
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
};

const ActiveModelCard: React.FC = () => {
  const [data, setData] = useState<ActiveModelResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      const res = await fetchActiveModel();
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
      <h2>Active Model</h2>
      <div className="api-error">{error}</div>
    </div>
  );
  if (!data) return (
    <div className="card">
      <h2>Active Model</h2>
      <p>Loading…</p>
    </div>
  );

  if (!data.running) {
    // Show transition states distinctly from "fully idle" — during a
    // swap the user wants to know the system is busy, not that it's
    // doing nothing.
    const stateLabel = data.state === 'starting' ? 'starting up'
                     : data.state === 'stopping' ? 'shutting down'
                     : 'no model loaded';
    const stateBadge = data.state === 'starting' || data.state === 'stopping'
                     ? 'badge-executing' : 'badge-pending';
    return (
      <div className="card">
        <h2>Active Model</h2>
        <div className="active-model-empty">
          <span className={`badge ${stateBadge}`}>{stateLabel}</span>
          <p style={{ marginTop: 8, color: '#6c757d', fontSize: 13 }}>
            {data.state === 'starting' ? 'Loading model into VRAM…'
             : data.state === 'stopping' ? 'Killing the previous llama-server child to make room for a new one.'
             : 'llama-server is idle. The next chat-completion request will spawn a child process and load a model.'}
          </p>
          <p style={{ marginTop: 4, color: '#6c757d', fontSize: 12 }}>
            Idle timeout: {data.idle_timeout_s}s · {data.available_agents.length} agents configured
            {data.chat_in_flight != null && ` · ${data.chat_in_flight}/${data.chat_max_inflight} chat slots`}
          </p>
        </div>
      </div>
    );
  }

  const cfg = data.config;
  const inFlightBadge = data.active_requests > 0 ? 'badge-executing' : 'badge-completed';

  return (
    <div className="card">
      <h2>
        Active Model
        <span className="metric-window-hint">
          {' live'}
          {data.pid != null ? ` · PID ${data.pid}` : ''}
          {data.uptime_seconds != null ? ` · up ${fmtUptime(data.uptime_seconds)}` : ''}
        </span>
      </h2>

      <div className="active-model-head">
        <div className="active-model-id">
          <span className="active-model-agent">{data.agent_id ?? <em>unknown agent</em>}</span>
          {data.agent_executor === true && (
            <span className="badge badge-executing" style={{ marginLeft: 8 }}>executor</span>
          )}
          <span className={`badge ${inFlightBadge}`} style={{ marginLeft: 8 }}>
            {data.active_requests} in-flight
          </span>
        </div>
        {data.agent_description && (
          <div className="active-model-desc">{data.agent_description}</div>
        )}
        {data.model_basename && (
          <div className="active-model-file" title={data.model_path ?? undefined}>
            <span className="active-model-file-label">model:</span>
            <code>{data.model_basename}</code>
          </div>
        )}
      </div>

      {cfg && (
        <div className="metric-tile-grid active-model-tiles">
          <Tile label="ctx" value={fmtCtx(cfg.n_ctx)} />
          <Tile label="ngl" value={cfg.n_gpu_layers != null ? String(cfg.n_gpu_layers) : '—'} />
          <Tile label="ubatch" value={cfg.n_ubatch != null ? String(cfg.n_ubatch) : '—'} />
          <Tile label="batch" value={cfg.n_batch != null ? String(cfg.n_batch) : '—'} />
          <Tile label="kv k/v" value={`${cfg.cache_type_k ?? '—'} / ${cfg.cache_type_v ?? '—'}`} />
          <Tile label="cpu_moe" value={cfg.cpu_moe ? 'on' : 'off'} />
          <Tile label="rep penalty" value={cfg.repeat_penalty != null ? cfg.repeat_penalty.toFixed(2) : '—'} />
          <Tile label="rep last n" value={cfg.repeat_last_n != null ? String(cfg.repeat_last_n) : '—'} />
        </div>
      )}

      {cfg?.draft && (
        <div className="active-model-draft">
          <span className="metric-label">Speculative draft:</span>
          <code>{cfg.draft.basename ?? '—'}</code>
          {' · ngl '}{cfg.draft.n_gpu_layers ?? '—'}
          {' · ctx '}{fmtCtx(cfg.draft.n_ctx)}
          {cfg.draft.cpu_moe && ' · cpu_moe'}
        </div>
      )}

      <div className="active-model-foot">
        last request {fmtAgo(data.last_request_seconds_ago)}
        {' · idle timeout '}{data.idle_timeout_s}s
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

export default ActiveModelCard;
