// Shared spec-status presentation helpers.
//
// The status → badge mapping used to be copy-pasted in SpecsTable and
// SpecDetail; CurrentExecutionCard made it three. Both copies also
// predated the `done`/`cancelled` statuses the orchestrator actually
// writes, so finished specs fell through to the yellow `badge-pending`.

import type { Spec } from "../types/api";

const STATUS_BADGE_CLASS: Record<string, string> = {
  pending_plan: "badge-pending",
  needs_clarification: "badge-needs-clarification",
  plan_review: "badge-plan-review",
  executing: "badge-executing",
  done: "badge-completed",
  completed: "badge-completed",
  failed: "badge-failed",
  cancelled: "badge-rejected",
  archived: "badge-pending",
};

export function specStatusBadgeClass(status: string): string {
  return STATUS_BADGE_CLASS[status] || "badge-pending";
}

// Statuses meaning the pipeline is finished with the spec. Everything
// else is still live. `completed` and `done` coexist because the server
// has written both over the life of the DB.
const TERMINAL_STATUSES = new Set([
  "done",
  "completed",
  "failed",
  "cancelled",
  "archived",
]);

export function isTerminalStatus(status: string): boolean {
  return TERMINAL_STATUSES.has(status);
}

// Newest-updated first. Timestamps are same-format ISO strings, so a
// lexicographic compare orders them correctly (same approach as dag.ts).
function byRecency(a: Spec, b: Spec): number {
  return b.updated_at.localeCompare(a.updated_at);
}

/**
 * Resolve the spec the dashboard should treat as "current".
 *
 * The orchestrator runs one spec at a time, so `executing` is the
 * answer whenever one exists. Between runs there is no executing spec
 * but there is usually one waiting on a human (plan_review,
 * needs_clarification), which is the thing worth showing next. Returns
 * null when every spec has reached a terminal status.
 */
export function pickCurrentSpec(specs: Spec[]): Spec | null {
  const executing = specs.filter((s) => s.status === "executing");
  if (executing.length > 0) return executing.sort(byRecency)[0];

  const live = specs.filter((s) => !isTerminalStatus(s.status));
  if (live.length > 0) return live.sort(byRecency)[0];

  return null;
}
