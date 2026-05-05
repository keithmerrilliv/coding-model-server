import React, { useState, useEffect } from 'react';
import { fetchModels } from '../api/client';
import type { ModelInfo } from '../types/api';

const AgentsGrid: React.FC = () => {
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const res = await fetchModels();
      if (!cancelled) {
        if (res.error) setError(res.error);
        else if (res.data) setModels(res.data.data);
      }
    };
    load();
    return () => { cancelled = true; };
  }, []);

  return (
    <div className="card">
      <h2>Agent Grid</h2>
      {error && <div className="api-error">{error}</div>}
      <div className="grid grid-3" style={{ marginTop: '12px' }}>
        {models.map((m) => (
          <div key={m.id} style={{ padding: '12px', border: '1px solid var(--color-border)', borderRadius: 'var(--radius)' }}>
            <strong>{m.id}</strong>
            <p style={{ fontSize: '12px', color: '#6c757d', marginTop: '4px', lineHeight: '1.4' }}>
              {m.description || m.owned_by || 'N/A'}
            </p>
          </div>
        ))}
        {models.length === 0 && !error && <p>Loading agents...</p>}
      </div>
    </div>
  );
};

export default AgentsGrid;