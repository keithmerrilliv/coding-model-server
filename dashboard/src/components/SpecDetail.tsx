import React, { useState, useEffect, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { fetchSpecDetail } from '../api/client';
import type { SpecDetailResponse } from '../types/api';
import EventTimeline from './EventTimeline';
import GateActions from './GateActions';

const SpecDetail: React.FC = () => {
  const { id } = useParams<{ id: string }>()!;
  const navigate = useNavigate();
  const [detail, setDetail] = useState<SpecDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refetch = useCallback(async () => {
    if (!id) return;
    const res = await fetchSpecDetail(id);
    if (res.error) setError(res.error);
    else if (res.data) setDetail(res.data);
  }, [id]);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      if (cancelled) return;
      await refetch();
    };
    poll();
    const interval = setInterval(poll, 10000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [refetch]);

  if (!detail) {
    return <div className="card">{error ? <div className="api-error">{error}</div> : 'Loading spec details...'}</div>;
  }

  const getStatusClass = (status: string) => {
    const map: Record<string, string> = {
      pending_plan: 'badge-pending',
      needs_clarification: 'badge-needs-clarification',
      plan_review: 'badge-plan-review',
      executing: 'badge-executing',
      completed: 'badge-completed',
      failed: 'badge-failed',
      archived: 'badge-pending',
    };
    return map[status] || 'badge-pending';
  };

  return (
    <div>
      <button onClick={() => navigate('/specs')} style={{ marginBottom: '16px', cursor: 'pointer' }}>← Back to Specs</button>

      <div className="card">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h2>{detail.spec.title}</h2>
          <span className={`badge ${getStatusClass(detail.spec.status)}`}>{detail.spec.status}</span>
        </div>
        <p style={{ marginTop: '8px', color: '#6c757d' }}>Task Count: {detail.task_count}</p>
        <p style={{ fontSize: '12px', color: '#868e96' }}>ID: {detail.spec.id} | Created: {new Date(detail.spec.created_at).toLocaleString()}</p>
      </div>

      <div className="grid grid-2">
        <div className="card">
          <h3>Open Gates ({detail.open_gates.length})</h3>
          {detail.open_gates.map((gate) => (
            <GateActions key={gate.id} gate={gate} onAction={refetch} />
          ))}
          {detail.open_gates.length === 0 && <p>No open gates.</p>}
        </div>

        <div className="card">
          <h3>Recent Events ({detail.recent_events.length})</h3>
          <EventTimeline events={detail.recent_events} />
        </div>
      </div>
    </div>
  );
};

export default SpecDetail;
