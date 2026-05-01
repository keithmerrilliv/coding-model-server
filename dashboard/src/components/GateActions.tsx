import React, { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { respondToGate } from '../api/client';
import type { Gate } from '../types/api';

interface GateActionsProps {
  gate: Gate;
  onAction: () => void;
}

const GateActions: React.FC<GateActionsProps> = ({ gate, onAction }) => {
  const [showNotes, setShowNotes] = useState(false);
  const [notes, setNotes] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleRespond = async (decision: 'approved' | 'rejected') => {
    setLoading(true);
    setError(null);
    try {
      const res = await respondToGate(gate.id, { decision, notes: notes.trim() || undefined });
      if (res.error) throw new Error(res.error);
      setShowNotes(false);
      setNotes('');
      onAction();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to respond');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ padding: '12px', border: '1px solid var(--color-border)', borderRadius: 'var(--radius)', marginBottom: '8px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
        <strong>{gate.gate_type}</strong>
        <span className={`badge ${gate.status === 'pending' ? 'badge-pending' : gate.status === 'approved' ? 'badge-approved' : 'badge-rejected'}`}>
          {gate.status}
        </span>
      </div>
      
      <div className="gate-prompt">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{gate.prompt_md}</ReactMarkdown>
      </div>

      {error && <div className="api-error" style={{ marginTop: '8px', fontSize: '12px' }}>{error}</div>}

      {gate.status === 'pending' && (
        <div className="gate-actions">
          <button 
            className="btn-approve" 
            onClick={() => handleRespond('approved')}
            disabled={loading}
          >
            Approve
          </button>
          {!showNotes ? (
            <button className="btn-reject" onClick={() => setShowNotes(true)} disabled={loading}>
              Reject
            </button>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', width: '100%' }}>
              <textarea
                className="notes-input"
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
                placeholder="Add rejection notes..."
                rows={3}
              />
              <div style={{ display: 'flex', gap: '8px' }}>
                <button 
                  className="btn-reject" 
                  onClick={() => handleRespond('rejected')}
                  disabled={loading}
                >
                  Confirm Reject
                </button>
                <button 
                  onClick={() => { setShowNotes(false); setNotes(''); }}
                  disabled={loading}
                  style={{ padding: '4px 8px', fontSize: '12px', borderRadius: 'var(--radius)', border: '1px solid var(--color-border)', cursor: 'pointer' }}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default GateActions;