import React, { useEffect, useState } from 'react';
import { fetchVideoPipeline } from '../api/client';
import type { VideoJob, VideoJobStatus, VideoPipelineResponse } from '../types/api';
import GpuPanel from './GpuPanel';

// ffmpeg reports progress several times a second, but a poll per second is
// already smoother than a human can read a percentage.
const POLL_MS = 2000;

type Snapshot = Extract<VideoPipelineResponse, { available: true }>;

const gb = (bytes: number | null | undefined): string =>
  bytes == null ? '—' : `${(bytes / 1e9).toFixed(1)} GB`;

const clock = (seconds: number): string => {
  if (!isFinite(seconds) || seconds < 0) return '—';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m ${Math.floor(seconds % 60)}s`;
};

const STATUS_BADGE: Record<VideoJobStatus, string> = {
  pending: 'badge-pending',
  running: 'badge-executing',
  done: 'badge-completed',
  failed: 'badge-failed',
  skipped: 'badge-needs-clarification',
  passthrough: 'badge-plan-review',
};

// Share of a job's wall time spent merging when a transcode follows. Measured
// on a 4.4 GB recording: ~1 min stream-copy, ~5 min NVENC re-encode.
const MERGE_SHARE_WITH_TRANSCODE = 0.15;

// A job's progress resets to 0 at the start of each stage, so the transcode
// starting would read as the job going backwards. Fold the stages into one
// 0..1 figure.
export function overallProgress(job: VideoJob, transcoding: boolean): number {
  if (job.status === 'done') return 1;
  if (job.status !== 'running') return 0;
  if (!transcoding) return job.progress;
  const merge = MERGE_SHARE_WITH_TRANSCODE;
  if (job.stage === 'transcode') return merge + job.progress * (1 - merge);
  return job.stage === 'verify' ? merge : job.progress * merge;
}

// Remaining time from input bytes already processed, not from job counts: the
// jobs range from 0.1 GB to 20 GB, so a count would swing wildly. A running
// job contributes its fraction so the estimate moves between completions.
export function estimateRemainingSeconds(snap: Snapshot, nowSec: number): number | null {
  const work = snap.jobs.filter(j => j.status !== 'passthrough' && j.status !== 'skipped');
  const doneBytes = work.reduce(
    (sum, j) => sum + j.input_bytes * overallProgress(j, snap.transcode != null), 0);
  const totalBytes = work.reduce((sum, j) => sum + j.input_bytes, 0);
  const elapsed = nowSec - snap.started_at;
  if (doneBytes <= 0 || elapsed <= 0) return null;
  return ((totalBytes - doneBytes) / doneBytes) * elapsed;
}

const VideoPipeline: React.FC = () => {
  const [snap, setSnap] = useState<VideoPipelineResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showPassthrough, setShowPassthrough] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      const res = await fetchVideoPipeline();
      if (cancelled) return;
      if (res.error) { setError(res.error); return; }
      setError(null);
      if (res.data) setSnap(res.data);
    };
    poll();
    const id = setInterval(poll, POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  if (error) return (
    <div className="card"><h2>Video pipeline</h2><div className="api-error">{error}</div></div>
  );
  if (!snap) return <div className="card"><h2>Video pipeline</h2>Loading…</div>;
  if (!snap.available) return (
    <>
      <div className="card">
        <h2>Video pipeline</h2>
        <p className="host-note">No run recorded yet. {snap.reason}</p>
        <p className="host-note">Start one with <code>python -m video_pipeline &lt;dir&gt; --dry-run</code>.</p>
      </div>
      <GpuPanel />
    </>
  );

  const nowSec = Date.now() / 1000;
  const running = snap.jobs.find(j => j.status === 'running');
  const eta = snap.active ? estimateRemainingSeconds(snap, nowSec) : null;
  const counts = snap.summary.counts;
  const visible = snap.jobs.filter(j => showPassthrough || j.status !== 'passthrough');
  const state = snap.active ? 'running'
    : snap.dry_run ? 'dry run'
    : snap.finished_at ? 'finished' : 'stopped';

  return (
    <>
      <div className="card">
        <h2>
          Video pipeline
          <span className={`badge ${snap.active ? 'badge-executing' : 'badge-plan-review'}`}
            style={{ marginLeft: 10 }}>{state}</span>
          <span className="metric-window-hint">
            run {snap.run_id} · {snap.root} → {snap.out_root}
            {snap.transcode ? ` · transcode ${snap.transcode}` : ''}
            {!snap.active && ` · updated ${clock(snap.stale_seconds)} ago`}
          </span>
        </h2>
        {state === 'stopped' && (
          <div className="api-error">
            The last run never recorded a finish and its process is gone — it was
            killed or crashed. Progress below is where it stopped.
          </div>
        )}
        <div className="endpoint-charts">
          <Stat label="to merge" value={String((counts.pending ?? 0) + (counts.running ?? 0))} />
          <Stat label="done" value={String(counts.done ?? 0)} />
          <Stat label="failed" value={String(counts.failed ?? 0)} danger={(counts.failed ?? 0) > 0} />
          <Stat label="est. remaining" value={eta != null ? clock(eta) : '—'} />
          <Stat label="skipped / single" value={`${counts.skipped ?? 0} / ${counts.passthrough ?? 0}`} />
          <Stat label="input" value={gb(snap.summary.input_bytes)} />
          <Stat label="output" value={gb(snap.summary.output_bytes)} />
        </div>
        {running && <Running job={running} />}
      </div>

      <GpuPanel />

      <div className="card">
        <h2>
          Recordings
          <label className="metric-window-hint">
            <input type="checkbox" checked={showPassthrough}
              onChange={e => setShowPassthrough(e.target.checked)} /> show single-chapter
          </label>
        </h2>
        <table>
          <thead>
            <tr><th>Recording</th><th>Chapters</th><th>Input</th><th>Status</th><th>Output</th><th>Note</th></tr>
          </thead>
          <tbody>
            {visible.map(j => (
              <tr key={j.key}>
                <td>{j.source_dir}/{j.recording}</td>
                <td>{j.chapters}</td>
                <td>{gb(j.input_bytes)}</td>
                <td><span className={`badge ${STATUS_BADGE[j.status]}`}>{j.status}</span></td>
                <td>{gb(j.output_bytes)}</td>
                <td>{j.note}</td>
              </tr>
            ))}
            {visible.length === 0 && <tr><td colSpan={6}>Nothing to show.</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h2>Events</h2>
        <ul className="video-events">
          {[...snap.events].reverse().slice(0, 30).map((e, i) => (
            <li key={i} className={e.level === 'error' ? 'video-event-error' : ''}>
              <time>{new Date(e.t * 1000).toLocaleTimeString()}</time> {e.message}
            </li>
          ))}
        </ul>
      </div>
    </>
  );
};

const Stat: React.FC<{ label: string; value: string; danger?: boolean }> = ({ label, value, danger }) => (
  <div className="chart-col">
    <div className="chart-col-head">
      <span className="chart-col-label">{label}</span>
    </div>
    <span className="video-stat" style={danger ? { color: 'var(--color-danger)' } : undefined}>{value}</span>
  </div>
);

const Running: React.FC<{ job: VideoJob }> = ({ job }) => (
  <div className="video-running">
    <div>
      <strong>{job.source_dir}/{job.recording}</strong> · {job.stage || 'starting'}
      {job.speed != null && ` · ${job.speed.toFixed(1)}× realtime`}
      <span style={{ float: 'right' }}>{(job.progress * 100).toFixed(0)}%</span>
    </div>
    <div className="video-bar"><div className="video-bar-fill" style={{ width: `${job.progress * 100}%` }} /></div>
  </div>
);

export default VideoPipeline;
