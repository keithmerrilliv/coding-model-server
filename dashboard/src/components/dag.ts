// Build a Mermaid flowchart definition from spec-detail data.
//
// The autonomous pipeline isn't strictly DAG (retries loop back), so we
// linearise it by chronological order and draw retry-loop edges as
// dashed back-edges. The output is "flowchart LR" so the result reads
// left-to-right in the rendered graph (DEV-439) — the pipeline is a long
// mostly-linear chain, which is a wide strip rather than a tall column.
//
// Note on data shape: Tasks correspond to architect / implementer /
// reviewer / supervisor steps. The planner is implicit (no Task row);
// we infer it from the spec's first plan_approval gate.

import type { Event, Gate, SpecDetailResponse, Task } from "../types/api";

type Entity =
  | { kind: "task"; t: Task; ts: string }
  | { kind: "gate"; g: Gate; ts: string };

const STATUS_ICON: Record<string, string> = {
  done: "✓",
  completed: "✓",
  failed: "✗",
  cancelled: "✗",
  running: "⟳",
  in_progress: "⟳",
  blocked_on_review: "⏸",
  pending: "•",
  approved: "✓",
  rejected: "✗",
};

const GATE_TYPE_LABEL: Record<string, string> = {
  clarification: "Clarification",
  plan_approval: "Plan Approval",
  design_approval: "Design Approval",
  code_review: "Code Review",
  release_approval: "Release Approval",
};

function fmtDuration(start: string | null, end: string | null): string | null {
  if (!start || !end) return null;
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (Number.isNaN(ms) || ms < 0) return null;
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return rem ? `${m}m${rem}s` : `${m}m`;
}

// Mermaid's parser is fussy about <br/> inside [] — escape any
// characters that would break label parsing. The label text goes inside
// double quotes, so we escape internal quotes.
function escapeLabel(s: string): string {
  return s.replace(/"/g, "&quot;");
}

function taskNode(t: Task): { id: string; def: string } {
  const id = `task_${t.id}`;
  const icon = STATUS_ICON[t.status] || "?";
  const dur = fmtDuration(t.started_at, t.completed_at);
  const retry = t.retry_count > 0 ? ` retry-${t.retry_count}` : "";
  const lines = [
    `${icon} ${t.role}${retry}`,
    `<small>${t.agent}</small>`,
    dur ? `<small>${dur}</small>` : null,
  ].filter(Boolean) as string[];
  const label = escapeLabel(lines.join("<br/>"));
  return { id, def: `${id}["${label}"]` };
}

function gateNode(g: Gate): { id: string; def: string } {
  const id = `gate_${g.id}`;
  const decisionIcon = g.reviewer_decision
    ? STATUS_ICON[g.reviewer_decision] || ""
    : g.status === "pending"
    ? "⏸"
    : "?";
  const label = escapeLabel(
    `${decisionIcon} ${GATE_TYPE_LABEL[g.gate_type] || g.gate_type}`,
  );
  // Diamond shape for decision points.
  return { id, def: `${id}{"${label}"}` };
}

function specEndNode(status: string): { id: string; def: string } {
  const icon = STATUS_ICON[status] || "?";
  // Stadium shape (parens) for terminal states.
  return { id: "spec_end", def: `spec_end(["${icon} ${status}"])` };
}

// Find the adversarial_test_writer agent_ran events. Each one is one
// provider's firing — in 'both' mode there may be 2 per spec.
function phaseBProviders(events: Event[]): { provider: string; model: string; passed: boolean | null }[] {
  const out: { provider: string; model: string; passed: boolean | null }[] = [];
  for (const e of events) {
    if (e.kind !== "agent_ran") continue;
    let p: Record<string, unknown>;
    try {
      p = JSON.parse(e.payload_json) as Record<string, unknown>;
    } catch {
      continue;
    }
    if (p.role !== "adversarial_test_writer") continue;
    out.push({
      provider: (p.provider as string) || "unknown",
      model: (p.model as string) || "?",
      passed: typeof p.passed === "boolean" ? p.passed : null,
    });
  }
  return out;
}

export function buildDagDefinition(data: SpecDetailResponse): string {
  const tasks = data.tasks || [];
  const gates = data.all_gates || [];
  const events = data.recent_events || [];

  if (tasks.length === 0 && gates.length === 0) {
    return "flowchart LR\n  empty[\"no tasks or gates yet\"]";
  }

  // Sort everything chronologically and render as a linear flow with
  // retry-loop back-edges where rejected gates exist.
  const entities: Entity[] = [
    ...tasks.map((t) => ({ kind: "task" as const, t, ts: t.created_at })),
    ...gates.map((g) => ({ kind: "gate" as const, g, ts: g.created_at })),
  ];
  entities.sort((a, b) => a.ts.localeCompare(b.ts));

  const lines: string[] = ["flowchart LR"];
  // Style classes for status colouring.
  lines.push(
    "  classDef done fill:#1e3a2e,stroke:#4ade80,color:#bbf7d0",
    "  classDef failed fill:#3a1e1e,stroke:#f87171,color:#fecaca",
    "  classDef running fill:#1e2e3a,stroke:#60a5fa,color:#bfdbfe",
    "  classDef pending fill:#2e2e2e,stroke:#a3a3a3,color:#d4d4d4",
    "  classDef phaseb fill:#3a2e1e,stroke:#fbbf24,color:#fef3c7",
  );

  // Synthetic spec_start node so the first edge has a source.
  lines.push(`  spec_start(["spec ${data.spec.id}"])`);
  let prevId = "spec_start";

  const nodeStatusClass: Record<string, string> = {};

  for (const ent of entities) {
    if (ent.kind === "task") {
      const { id, def } = taskNode(ent.t);
      lines.push(`  ${def}`);
      lines.push(`  ${prevId} --> ${id}`);
      prevId = id;
      const cls = ent.t.status === "done" || ent.t.status === "completed"
        ? "done"
        : ent.t.status === "failed" || ent.t.status === "cancelled"
        ? "failed"
        : ent.t.status === "running" || ent.t.status === "in_progress"
        ? "running"
        : "pending";
      nodeStatusClass[id] = cls;

      // Annotate Phase b under the reviewer task.
      if (ent.t.role === "reviewer") {
        for (const pb of phaseBProviders(events)) {
          const pbId = `phaseb_${id}_${pb.provider}`;
          const pbIcon = pb.passed === false ? "✗" : pb.passed === true ? "✓" : "•";
          const pbLabel = escapeLabel(
            `${pbIcon} adversarial<br/><small>${pb.provider}</small><br/><small>${pb.model}</small>`,
          );
          lines.push(`  ${pbId}["${pbLabel}"]`);
          lines.push(`  ${id} -.-> ${pbId}`);
          nodeStatusClass[pbId] = "phaseb";
        }
      }
    } else {
      const { id, def } = gateNode(ent.g);
      lines.push(`  ${def}`);
      const edgeLabel = ent.g.reviewer_decision
        ? `|${ent.g.reviewer_decision}|`
        : ent.g.status === "pending"
        ? "|pending|"
        : "";
      lines.push(`  ${prevId} -->${edgeLabel} ${id}`);
      prevId = id;
      const cls = ent.g.reviewer_decision === "approved"
        ? "done"
        : ent.g.reviewer_decision === "rejected"
        ? "failed"
        : "pending";
      nodeStatusClass[id] = cls;
    }
  }

  // Terminal node when the spec has reached a final status.
  if (["done", "failed", "cancelled"].includes(data.spec.status)) {
    const { id, def } = specEndNode(data.spec.status);
    lines.push(`  ${def}`);
    lines.push(`  ${prevId} --> ${id}`);
    nodeStatusClass[id] = data.spec.status === "done" ? "done" : "failed";
  }

  // Apply the class lines at the bottom so they don't bloat the middle.
  for (const [nodeId, cls] of Object.entries(nodeStatusClass)) {
    lines.push(`  class ${nodeId} ${cls}`);
  }

  return lines.join("\n");
}
