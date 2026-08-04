// Overview card: the execution DAG for whatever the pipeline is working
// on right now (DEV-428).
//
// Seeing the graph previously meant Specs → find the right spec → click
// through. The orchestrator only runs one spec at a time, so the
// Overview can just resolve "current" itself — see pickCurrentSpec.
//
// Two requests per poll (list, then detail) rather than a new server
// endpoint: the list is small and this keeps the change frontend-only.

import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { fetchSpecDetail, fetchSpecs } from "../api/client";
import { pickCurrentSpec, specStatusBadgeClass } from "./specStatus";
import type { Spec, SpecDetailResponse } from "../types/api";
import ExecutionDag from "./ExecutionDag";

// Matches HealthCard and SpecDetail.
const POLL_MS = 10000;

const CurrentExecutionCard: React.FC = () => {
  const [spec, setSpec] = useState<Spec | null>(null);
  const [detail, setDetail] = useState<SpecDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      const listRes = await fetchSpecs();
      if (cancelled) return;

      if (listRes.error) {
        setError(listRes.error);
        setLoaded(true);
        return;
      }

      const current = pickCurrentSpec(listRes.data || []);
      setSpec(current);

      if (!current) {
        // Everything is terminal — drop any stale graph.
        setDetail(null);
        setError(null);
        setLoaded(true);
        return;
      }

      // Clear the previous spec's graph so a handover never shows the
      // old DAG under the new spec's title.
      setDetail((prev) => (prev && prev.spec.id !== current.id ? null : prev));

      const detailRes = await fetchSpecDetail(current.id);
      if (cancelled) return;

      if (detailRes.error) setError(detailRes.error);
      else if (detailRes.data) {
        setDetail(detailRes.data);
        setError(null);
      }
      setLoaded(true);
    };

    poll();
    const interval = setInterval(poll, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  return (
    <div className="card">
      <div className="current-execution-header">
        <h2>Current Execution</h2>
        {spec && (
          <span className={`badge ${specStatusBadgeClass(spec.status)}`}>
            {spec.status}
          </span>
        )}
      </div>

      {error && <div className="api-error">{error}</div>}

      {spec && (
        <div className="current-execution-meta">
          <Link to={`/specs/${spec.id}`} className="current-execution-title">
            {spec.title}
          </Link>
          <span className="current-execution-sub">
            {spec.jira_epic_key ? `${spec.jira_epic_key} · ` : ""}
            updated {new Date(spec.updated_at).toLocaleString()}
          </span>
        </div>
      )}

      {detail ? (
        <ExecutionDag detail={detail} />
      ) : (
        !error &&
        (loaded ? (
          <p>{spec ? "Loading execution graph…" : "No spec is currently executing."}</p>
        ) : (
          <p>Loading current execution…</p>
        ))
      )}
    </div>
  );
};

export default CurrentExecutionCard;
