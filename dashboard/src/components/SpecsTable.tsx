import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { fetchSpecs } from '../api/client';
import type { Spec } from '../types/api';

const SpecsTable: React.FC = () => {
  const [specs, setSpecs] = useState<Spec[]>([]);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      const res = await fetchSpecs(50);
      if (!cancelled) {
        if (res.error) setError(res.error);
        else if (res.data) setSpecs(res.data);
      }
    };

    poll();
    const interval = setInterval(poll, 10000);
    return () => { cancelled = true; clearInterval(interval); };
  }, []);

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
    <div className="card">
      <h2>Specifications</h2>
      {error && <div className="api-error">{error}</div>}
      <table style={{ marginTop: '12px' }}>
        <thead>
          <tr>
            <th>Title</th>
            <th>Status</th>
            <th>Created</th>
          </tr>
        </thead>
        <tbody>
          {specs.map((spec) => (
            <tr key={spec.id} onClick={() => navigate(`/specs/${spec.id}`)}>
              <td>{spec.title}</td>
              <td><span className={`badge ${getStatusClass(spec.status)}`}>{spec.status}</span></td>
              <td>{new Date(spec.created_at).toLocaleString()}</td>
            </tr>
          ))}
          {specs.length === 0 && !error && <tr><td colSpan={3}>Loading specs...</td></tr>}
        </tbody>
      </table>
    </div>
  );
};

export default SpecsTable;