import React, { useState, useEffect } from 'react';
import { fetchHealth } from '../api/client';
import type { HealthResponse } from '../types/api';

const HealthCard: React.FC = () => {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      const res = await fetchHealth();
      if (cancelled) return;
      if (res.error) setError(res.error);
      else if (res.data) {
        setHealth(res.data);
        setError(null);
      }
    };
    poll();
    const interval = setInterval(poll, 10000);
    return () => { cancelled = true; clearInterval(interval); };
  }, []);

  const getStatusBadgeClass = (status: string) =>
    status.toLowerCase() === 'healthy' ? 'badge-healthy' : 'badge-unhealthy';

  const getRelativeTime = (isoString: string) => {
    try {
      const diff = Date.now() - new Date(isoString).getTime();
      // Clock skew between browser and server can make diff slightly negative
      const seconds = Math.max(0, Math.floor(diff / 1000));
      if (seconds < 60) return `${seconds}s ago`;
      const minutes = Math.floor(seconds / 60);
      return `${minutes}m ago`;
    } catch {
      return isoString;
    }
  };

  return (
    <div className="card">
      <h2>Server Health</h2>
      {error && <div className="api-error">{error}</div>}
      {health ? (
        <div style={{ marginTop: '12px' }}>
          <span className={`badge ${getStatusBadgeClass(health.status)}`}>{health.status}</span>
          <p style={{ marginTop: '8px' }}>Model Loaded: {health.model_loaded ? 'Yes' : 'No'}</p>
          <p>Timestamp: {getRelativeTime(health.timestamp)}</p>
          <p>Agents: {health.agents.join(', ') || 'None'}</p>
        </div>
      ) : (
        !error && <p>Loading health status...</p>
      )}
    </div>
  );
};

export default HealthCard;
