#!/usr/bin/env python3
"""qwen-orchestrator daemon — Phase 1a skeleton.

Long-running process that drives autonomous-mode specs through their state
machine. Phase 1a behavior:

  1. Poll the qwen_autonomous task store every POLL_INTERVAL seconds.
  2. For each spec in PENDING_PLAN status, run the trivial "echo planner":
     wraps the markdown in a fake YAML and creates a plan_approval gate.
     The real planner agent (calling the LLM via /v1/chat/completions)
     lands in Phase 1b.
  3. For each spec in PLAN_REVIEW status with an approved plan_approval
     gate, transition to EXECUTING and stop (Phase 2 picks up here).
  4. For each spec in PLAN_REVIEW with a rejected plan_approval gate,
     transition back to PENDING_PLAN so the planner reruns. Reviewer notes
     are surfaced in the next planner pass once 1b is wired up.

This daemon must NOT block on the qwen-server FastAPI process — it talks to
the same SQLite file directly. The HTTP API is purely for clients; the
daemon is the executor. Do not mix the two.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

from qwen_autonomous import (
    ArtifactKind,
    Database,
    EventKind,
    GateStatus,
    GateType,
    SpecStatus,
)
from qwen_autonomous.models import ReviewGate, Spec

# ── Configuration ────────────────────────────────────────────────────────────

POLL_INTERVAL = float(os.getenv("ORCHESTRATOR_POLL_INTERVAL", "5"))
LOG_LEVEL = os.getenv("ORCHESTRATOR_LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - orchestrator - %(levelname)s - %(message)s",
)
logger = logging.getLogger("orchestrator")


# ── Trivial echo planner (Phase 1a placeholder) ──────────────────────────────

def _echo_plan_yaml(spec: Spec, markdown: str) -> str:
    """Return a fake YAML plan that echoes the spec back.

    Phase 1b replaces this with a real planner agent call. The shape of the
    YAML returned here intentionally matches what the real planner will emit
    so downstream code can be developed in parallel.
    """
    return (
        f"# Auto-generated placeholder plan for {spec.id}\n"
        f"# (real planner agent lands in Phase 1b)\n"
        f"spec_id: {spec.id}\n"
        f"title: {spec.title!r}\n"
        f"phases:\n"
        f"  - name: design\n"
        f"    role: architect\n"
        f"    description: Produce architecture from the spec.\n"
        f"  - name: implement\n"
        f"    role: implementer\n"
        f"    description: Build the design.\n"
        f"  - name: test\n"
        f"    role: reviewer\n"
        f"    description: Validate via tests.\n"
        f"source_markdown_bytes: {len(markdown)}\n"
    )


def _format_plan_gate_prompt(spec: Spec, yaml_text: str) -> str:
    return (
        f"## Plan ready for review: {spec.title}\n\n"
        f"Spec ID: `{spec.id}`\n\n"
        f"The planner has produced the following plan. Approve to begin "
        f"execution, or reject with notes to ask for changes.\n\n"
        f"```yaml\n{yaml_text}```\n"
    )


# ── State machine handlers ───────────────────────────────────────────────────

def _process_pending_plan(db: Database, spec: Spec) -> None:
    """Run the (fake) planner against a freshly submitted spec."""
    spec_dir = db.spec_dir(spec.id)
    md_path = spec_dir / spec.source_md_path
    if not md_path.exists():
        logger.error("spec %s: source markdown missing at %s",
                     spec.id, md_path)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    markdown = md_path.read_text()
    logger.info("spec %s: running placeholder planner (%d bytes)",
                spec.id, len(markdown))

    yaml_text = _echo_plan_yaml(spec, markdown)
    db.update_spec_status(
        spec.id,
        SpecStatus.PLAN_REVIEW,
        normalized_yaml=yaml_text,
    )

    # Persist the YAML on disk too so artifacts/code paths line up later.
    yaml_path = spec_dir / "plan.yaml"
    yaml_path.write_text(yaml_text)
    db.create_artifact(
        spec_id=spec.id,
        kind=ArtifactKind.SPEC_YAML,
        path="plan.yaml",
    )

    db.create_gate(
        spec_id=spec.id,
        gate_type=GateType.PLAN_APPROVAL,
        prompt_md=_format_plan_gate_prompt(spec, yaml_text),
    )
    db.record_event(
        EventKind.PLANNER_RAN,
        spec_id=spec.id,
        payload={"planner": "echo_placeholder", "yaml_bytes": len(yaml_text)},
    )


def _process_plan_review(db: Database, spec: Spec) -> None:
    """Look for a resolved plan_approval gate and act on it."""
    open_gates = db.list_open_gates(spec_id=spec.id)
    plan_gates_open = [g for g in open_gates
                       if g.gate_type == GateType.PLAN_APPROVAL]
    if plan_gates_open:
        return  # still waiting on the human

    # Find the most recent plan_approval gate (open or closed). The simplest
    # path is to scan recent events for the latest gate_responded matching
    # this spec — Phase 1b will replace this with a proper query helper.
    events = db.list_recent_events(spec_id=spec.id, limit=20)
    latest_response: ReviewGate | None = None
    for event in events:
        if event.kind != EventKind.GATE_RESPONDED or event.gate_id is None:
            continue
        gate = db.get_gate(event.gate_id)
        if gate and gate.gate_type == GateType.PLAN_APPROVAL:
            latest_response = gate
            break

    if latest_response is None:
        logger.warning("spec %s: in PLAN_REVIEW with no open or resolved "
                       "plan_approval gate; not advancing", spec.id)
        return

    if latest_response.status == GateStatus.APPROVED:
        logger.info("spec %s: plan approved by reviewer, transitioning to "
                    "EXECUTING (Phase 2 will take over)", spec.id)
        db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    elif latest_response.status == GateStatus.REJECTED:
        logger.info("spec %s: plan rejected, returning to PENDING_PLAN "
                    "for replanning. notes=%r",
                    spec.id, latest_response.reviewer_notes)
        db.update_spec_status(spec.id, SpecStatus.PENDING_PLAN)


# ── Main loop ────────────────────────────────────────────────────────────────

class _ShutdownFlag:
    """Shared flag flipped by SIGTERM/SIGINT handlers."""
    def __init__(self) -> None:
        self.set = False

    def install_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._handle)
        signal.signal(signal.SIGTERM, self._handle)

    def _handle(self, signum, frame):
        logger.info("received signal %d, shutting down after this tick", signum)
        self.set = True


def tick(db: Database) -> None:
    """One pass over the spec table. Idempotent — safe to call any time."""
    pending = db.list_specs(status=SpecStatus.PENDING_PLAN)
    for spec in pending:
        try:
            _process_pending_plan(db, spec)
        except Exception:
            logger.exception("spec %s: error during planner pass", spec.id)
            db.update_spec_status(spec.id, SpecStatus.FAILED)

    in_review = db.list_specs(status=SpecStatus.PLAN_REVIEW)
    for spec in in_review:
        try:
            _process_plan_review(db, spec)
        except Exception:
            logger.exception("spec %s: error during plan-review pass", spec.id)


def main() -> int:
    logger.info("orchestrator daemon starting (poll=%.1fs)", POLL_INTERVAL)
    db = Database()
    logger.info("task store: %s", db.db_path)
    logger.info("workspace:  %s", db.workspace_root)

    flag = _ShutdownFlag()
    flag.install_handlers()

    last_heartbeat = 0.0
    HEARTBEAT_INTERVAL = 60.0

    while not flag.set:
        try:
            tick(db)
        except Exception:
            logger.exception("tick failed; continuing")

        # Periodic heartbeat into the events table — useful for liveness checks
        # and for sync workers (Phase 1c) to know the daemon is alive.
        now = time.time()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            try:
                db.record_event(EventKind.DAEMON_TICK,
                                payload={"poll_interval": POLL_INTERVAL})
            except Exception:
                logger.exception("heartbeat write failed")
            last_heartbeat = now

        # Sleep in small chunks so SIGTERM is responsive.
        slept = 0.0
        while slept < POLL_INTERVAL and not flag.set:
            time.sleep(min(0.5, POLL_INTERVAL - slept))
            slept += 0.5

    logger.info("orchestrator daemon stopped")
    db.close_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
