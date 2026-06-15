"""Jira sync worker for autonomous mode.

Two responsibilities, both running on a background thread inside the
orchestrator daemon:

  Forward (SQLite → Jira)
      Walk the events table from the last processed id and translate each
      event into a Jira API call (create epic, create issue, transition,
      add comment). Persist the new high-water mark in ``sync_state`` so
      a daemon restart resumes where it left off rather than re-mirroring
      everything.

  Reverse (Jira → SQLite)
      Poll every gate that has been mirrored to a Jira issue. If the user
      has moved the issue to a "done" or "rejected" status in the Jira UI
      since we last looked, write a ``gate_responded`` back to SQLite so
      the daemon's main state machine can advance the spec on its next
      tick.

Design rules:

- Idempotent. Re-running the worker against the same events must not
  duplicate Jira issues. We rely on three things for this:
    1. The bookkeeping ``sync_state`` row tracks the last event id.
    2. SPEC_SUBMITTED handlers check ``spec.jira_epic_key`` first; if
       set, the epic already exists and we skip the create call.
    3. GATE_CREATED handlers check ``gate.jira_issue_key`` first.
- Failure-tolerant. Any individual API call can fail; we log and move on
  rather than crashing the whole worker. The high-water mark only
  advances after the event is fully processed, so retried events will
  see the idempotency guards above and become no-ops.
- The worker never blocks the orchestrator's main loop. It runs on its
  own thread with its own poll interval, sharing the SQLite Database
  instance (which is thread-safe via WAL).
- The SQLite store remains the source of truth. Jira is a projection.
  If Jira goes down, the daemon keeps working; the projection just falls
  behind and catches up when Jira comes back.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from coding_model_autonomous import (
    Database,
    Event,
    EventKind,
    GateStatus,
    GateType,
    SpecStatus,
)
from coding_model_autonomous.jira_client import (
    ISSUE_TYPE_EPIC,
    ISSUE_TYPE_TASK,
    JiraClient,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_IN_REVIEW,
    STATUS_REJECTED,
    STATUS_TODO,
)

logger = logging.getLogger("orchestrator.jira_sync")


# ── Configuration ────────────────────────────────────────────────────────────

SYNC_POLL_INTERVAL = float(os.getenv("JIRA_SYNC_POLL_INTERVAL", "10"))
SYNC_BATCH_SIZE = int(os.getenv("JIRA_SYNC_BATCH_SIZE", "50"))

# Bookkeeping keys in the sync_state table.
LAST_EVENT_KEY = "jira_last_event_id"


# ── Spec status → Jira epic status mapping ───────────────────────────────────

SPEC_STATUS_TO_JIRA: dict[SpecStatus, str] = {
    SpecStatus.PENDING_PLAN: STATUS_TODO,
    SpecStatus.NEEDS_CLARIFICATION: STATUS_IN_REVIEW,
    SpecStatus.PLAN_REVIEW: STATUS_IN_REVIEW,
    SpecStatus.EXECUTING: STATUS_IN_PROGRESS,
    SpecStatus.DONE: STATUS_DONE,
    SpecStatus.FAILED: STATUS_CANCELLED,
    SpecStatus.CANCELLED: STATUS_CANCELLED,
}


def _spec_status_to_jira(status: SpecStatus) -> str:
    return SPEC_STATUS_TO_JIRA.get(status, STATUS_TODO)


# ── Sync worker ──────────────────────────────────────────────────────────────

class JiraSync:
    """Background worker that mirrors SQLite events into Jira and back."""

    def __init__(
        self,
        db: Database,
        client: JiraClient,
        *,
        poll_interval: float = SYNC_POLL_INTERVAL,
    ):
        self.db = db
        self.client = client
        self.poll_interval = poll_interval
        self._shutdown = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Maps event_id → consecutive-failure count; cleared on success.
        self._event_retries: dict[int, int] = {}

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("JiraSync already started")
        self._thread = threading.Thread(
            target=self._run, name="jira-sync", daemon=True,
        )
        self._thread.start()
        logger.info("jira-sync thread started (poll=%.1fs, client=%s)",
                    self.poll_interval, type(self.client).__name__)

    def stop(self, timeout: float = 5.0) -> None:
        self._shutdown.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        while not self._shutdown.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("jira-sync tick failed; continuing")
            # Sleep responsively so SIGTERM is fast.
            self._shutdown.wait(timeout=self.poll_interval)
        logger.info("jira-sync thread stopped")

    # ── tick ─────────────────────────────────────────────────────────────────

    def tick(self) -> None:
        """One sync pass.

        We run forward → reverse → forward so the cycle bottoms out
        within a single tick: any ``gate_responded`` events the reverse
        sync writes to SQLite get mirrored back to Jira immediately,
        rather than sitting unprocessed until the next tick (which
        would also let a fresh sync worker against a fresh Jira instance
        try to update issues that don't exist there).
        """
        self._forward_sync()
        self._reverse_sync()
        self._forward_sync()

    # ── forward: SQLite → Jira ───────────────────────────────────────────────

    # In-memory retry counter for events that fail to sync. Keyed by event id.
    # An event is retried up to _MAX_EVENT_RETRIES times before we advance the
    # watermark past it (logging a CRITICAL warning so the operator can
    # reconcile the SQLite event log against Jira manually).
    _MAX_EVENT_RETRIES = 3

    def _forward_sync(self) -> None:
        last_seen = int(self.db.get_sync_state(LAST_EVENT_KEY, default="0") or "0")
        events = self.db.events_after(last_seen, limit=SYNC_BATCH_SIZE)
        if not events:
            return
        logger.debug("jira-sync: processing %d events from id>%d",
                     len(events), last_seen)
        for event in events:
            if event.id is None:
                continue  # safety: rows always have an id, but skip if not
            try:
                self._handle_event(event)
                self._event_retries.pop(event.id, None)
                self.db.set_sync_state(LAST_EVENT_KEY, str(event.id))
            except Exception:
                count = self._event_retries.get(event.id, 0) + 1
                self._event_retries[event.id] = count
                if count >= self._MAX_EVENT_RETRIES:
                    logger.critical(
                        "jira-sync: event id=%s kind=%s failed %d times — "
                        "advancing watermark; reconcile SQLite events vs Jira manually",
                        event.id, event.kind.value, count,
                    )
                    self._event_retries.pop(event.id, None)
                    self.db.set_sync_state(LAST_EVENT_KEY, str(event.id))
                else:
                    logger.warning(
                        "jira-sync: event id=%s kind=%s failed (attempt %d/%d); "
                        "watermark held, will retry next tick",
                        event.id, event.kind.value, count, self._MAX_EVENT_RETRIES,
                    )
                    # Stop processing this batch — the failed event blocks
                    # subsequent ones until it succeeds or hits the cap.
                    break

    def _handle_event(self, event: Event) -> None:
        kind = event.kind

        if kind == EventKind.SPEC_SUBMITTED:
            self._handle_spec_submitted(event)
        elif kind == EventKind.SPEC_STATUS_CHANGED:
            self._handle_spec_status_changed(event)
        elif kind == EventKind.GATE_CREATED:
            self._handle_gate_created(event)
        elif kind == EventKind.GATE_RESPONDED:
            self._handle_gate_responded(event)
        elif kind == EventKind.PLANNER_RAN:
            self._handle_planner_ran(event)
        elif kind == EventKind.AGENT_RAN:
            self._handle_agent_ran(event)
        elif kind == EventKind.TEST_RAN:
            self._handle_test_ran(event)
        # Other event kinds (TASK_*, ARTIFACT_CREATED, DAEMON_TICK) are
        # intentionally ignored in 1c — Phase 2 wires them up when tasks
        # actually start executing.

    def _handle_spec_submitted(self, event: Event) -> None:
        if event.spec_id is None:
            return
        spec = self.db.get_spec(event.spec_id)
        if spec is None:
            return
        if spec.jira_epic_key:
            return  # already mirrored — idempotent
        # Read the spec markdown for the description so the epic is useful
        # to read in the Jira UI without context-switching.
        try:
            md = (self.db.spec_dir(spec.id) / spec.source_md_path).read_text()
        except OSError:
            md = "(spec markdown unavailable)"
        body = (
            f"Autonomous spec `{spec.id}`\n\n"
            f"Submitted: {spec.created_at.isoformat()}\n\n"
            f"---\n\n{md}"
        )
        epic_key = self.client.create_epic(spec.title, body)
        self.db.set_spec_jira_epic(spec.id, epic_key)
        logger.info("jira-sync: mirrored spec %s → %s", spec.id, epic_key)

    def _handle_spec_status_changed(self, event: Event) -> None:
        if event.spec_id is None:
            return
        spec = self.db.get_spec(event.spec_id)
        if spec is None or not spec.jira_epic_key:
            return
        target = _spec_status_to_jira(spec.status)
        try:
            self.client.transition_issue(spec.jira_epic_key, target)
            logger.info("jira-sync: spec %s → epic %s status=%s",
                        spec.id, spec.jira_epic_key, target)
        except Exception as e:
            logger.warning("jira-sync: failed to transition epic %s: %s",
                           spec.jira_epic_key, e)

    def _handle_gate_created(self, event: Event) -> None:
        if event.gate_id is None:
            return
        gate = self.db.get_gate(event.gate_id)
        if gate is None:
            return
        if gate.jira_issue_key:
            return  # already mirrored
        spec = self.db.get_spec(gate.spec_id)
        parent_epic_key = spec.jira_epic_key if spec else None
        # Title format: "<gate_type>: <spec.title>" so the Jira board reads
        # naturally. The body is the gate's prompt_md, which already
        # contains everything the human reviewer needs.
        title = f"{gate.gate_type.value}: {spec.title if spec else gate.spec_id}"
        issue_key = self.client.create_issue(
            summary=title,
            description=gate.prompt_md,
            issue_type=ISSUE_TYPE_TASK,
            parent_epic_key=parent_epic_key,
        )
        self.db.set_gate_jira_issue(gate.id, issue_key)
        # Move the issue into "In Review" so the human knows it's waiting
        # on them. Some workflows don't have In Review — fall back to To Do.
        try:
            self.client.transition_issue(issue_key, STATUS_IN_REVIEW)
        except Exception:
            logger.debug("jira-sync: gate issue %s does not support "
                         "In Review transition; leaving as To Do", issue_key)
        logger.info("jira-sync: mirrored gate %s → %s", gate.id, issue_key)

    def _handle_gate_responded(self, event: Event) -> None:
        if event.gate_id is None:
            return
        gate = self.db.get_gate(event.gate_id)
        if gate is None or not gate.jira_issue_key:
            return
        target = STATUS_DONE if gate.status == GateStatus.APPROVED else STATUS_REJECTED
        try:
            if gate.reviewer_notes:
                self.client.add_comment(
                    gate.jira_issue_key,
                    f"**Reviewer notes:**\n\n{gate.reviewer_notes}",
                )
            self.client.transition_issue(gate.jira_issue_key, target)
            logger.info("jira-sync: gate %s → issue %s status=%s",
                        gate.id, gate.jira_issue_key, target)
        except Exception as e:
            logger.warning("jira-sync: failed to update gate issue %s: %s",
                           gate.jira_issue_key, e)

    def _handle_planner_ran(self, event: Event) -> None:
        """Append a comment to the spec's epic with the planner outcome."""
        if event.spec_id is None:
            return
        spec = self.db.get_spec(event.spec_id)
        if spec is None or not spec.jira_epic_key:
            return
        try:
            import json
            payload = json.loads(event.payload_json) if event.payload_json else {}
        except Exception:
            payload = {}
        body = "**Planner ran**\n\n```json\n" + str(payload) + "\n```"
        try:
            self.client.add_comment(spec.jira_epic_key, body)
        except Exception as e:
            logger.warning("jira-sync: failed to comment on epic %s: %s",
                           spec.jira_epic_key, e)

    def _handle_agent_ran(self, event: Event) -> None:
        """Comment on the spec epic when an execution agent completes."""
        self._comment_on_epic(event, "Agent ran")

    def _handle_test_ran(self, event: Event) -> None:
        """Comment on the spec epic with test run results."""
        self._comment_on_epic(event, "Tests ran")

    def _comment_on_epic(self, event: Event, label: str) -> None:
        if event.spec_id is None:
            return
        spec = self.db.get_spec(event.spec_id)
        if spec is None or not spec.jira_epic_key:
            return
        try:
            import json
            payload = json.loads(event.payload_json) if event.payload_json else {}
        except Exception:
            payload = {}
        body = f"**{label}**\n\n```json\n{payload}\n```"
        try:
            self.client.add_comment(spec.jira_epic_key, body)
        except Exception as e:
            logger.warning("jira-sync: failed to comment on epic %s: %s",
                           spec.jira_epic_key, e)

    # ── reverse: Jira → SQLite ───────────────────────────────────────────────

    def _reverse_sync(self) -> None:
        """Detect human-driven status changes on tracked gate issues.

        For every still-pending gate that has a Jira issue key, fetch the
        current state from Jira. If the human has moved it to Done or
        Rejected (i.e., approved or rejected the review), write the
        decision back to SQLite. The orchestrator's main loop picks up
        the change on its next tick.
        """
        pending_gates = self.db.list_open_gates()
        for gate in pending_gates:
            if not gate.jira_issue_key:
                continue
            try:
                issue = self.client.get_issue(gate.jira_issue_key)
            except Exception:
                logger.exception("jira-sync: get_issue failed for %s",
                                 gate.jira_issue_key)
                continue
            if issue is None:
                continue
            if issue.status == STATUS_DONE:
                self._respond_from_jira(gate.id, "approved", issue)
            elif issue.status == STATUS_REJECTED:
                self._respond_from_jira(gate.id, "rejected", issue)
            # Any other status (To Do, In Progress, In Review) means the
            # human hasn't acted yet — leave the gate pending.

    def _respond_from_jira(self, gate_id: str, decision: str, issue) -> None:
        # Pull the latest comment as the reviewer notes if present.
        notes = issue.comments[-1] if issue.comments else None
        try:
            self.db.respond_to_gate(gate_id, decision, notes=notes)
            logger.info("jira-sync: reverse-synced gate %s as %s "
                        "(from issue %s)", gate_id, decision, issue.key)
        except Exception:
            logger.exception("jira-sync: failed to apply reverse-sync "
                             "decision for gate %s", gate_id)
