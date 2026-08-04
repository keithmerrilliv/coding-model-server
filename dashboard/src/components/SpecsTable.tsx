import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { fetchSpecs } from '../api/client';
import type { Spec } from '../types/api';
import { specStatusBadgeClass } from './specStatus';

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
            <tr
              key={spec.id}
              onClick={() => navigate(`/specs/${spec.id}`)}
              tabIndex={0}
              role="button"
              aria-label={`Open ${spec.title}`}
            >
              <td>{spec.title}</td>
              <td><span className={`badge ${specStatusBadgeClass(spec.status)}`}>{spec.status}</span></td>
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