import React, { useState } from 'react';
import type { Event } from '../types/api';

interface EventTimelineProps {
  events: Event[];
}

const parsePayload = (payloadJson: string): unknown | null => {
  try {
    return JSON.parse(payloadJson);
  } catch {
    return null;
  }
};

const EventTimeline: React.FC<EventTimelineProps> = ({ events }) => {
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());

  const toggleExpand = (id: number) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const sortedEvents = [...events].sort((a, b) => 
    new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
  );

  return (
    <div style={{ marginTop: '12px' }}>
      {sortedEvents.length === 0 && <p>No recent events.</p>}
      {sortedEvents.map((event) => {
        const isExpanded = expandedIds.has(event.id);
        const parsed = parsePayload(event.payload_json);
        
        let payloadDisplay: React.ReactNode;
        if (parsed === null) {
          payloadDisplay = <span className="parse-error">{event.payload_json}</span>;
        } else if (typeof parsed === 'object') {
          payloadDisplay = <pre className="event-payload">{JSON.stringify(parsed, null, 2)}</pre>;
        } else {
          payloadDisplay = <pre className="event-payload">{String(parsed)}</pre>;
        }

        return (
          <div key={event.id} className="event-item">
            <div className="event-meta">
              <strong>{event.kind}</strong> • {new Date(event.created_at).toLocaleString()}
              {event.task_id && <span> • Task: {event.task_id}</span>}
            </div>
            {isExpanded && payloadDisplay}
            <button 
              onClick={() => toggleExpand(event.id)} 
              style={{ marginTop: '4px', fontSize: '12px', cursor: 'pointer' }}
            >
              {isExpanded ? 'Hide Payload' : 'Show Payload'}
            </button>
          </div>
        );
      })}
    </div>
  );
};

export default EventTimeline;