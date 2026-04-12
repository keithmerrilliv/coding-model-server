#!/usr/bin/env python3
"""qwen-orchestrator daemon — Phase 1b.

Long-running process that drives autonomous-mode specs through their state
machine.

State transitions handled here:

    PENDING_PLAN ──┬──> NEEDS_CLARIFICATION  (planner asked questions)
                   ├──> PLAN_REVIEW           (planner produced YAML)
                   └──> FAILED                (planner output unparseable)

    NEEDS_CLARIFICATION ──┬──> PENDING_PLAN   (clarification gate approved
                          │                    → re-run planner with answers)
                          └──> CANCELLED       (clarification gate rejected)

    PLAN_REVIEW ──┬──> EXECUTING               (plan approved — Phase 2 takes over)
                  └──> PENDING_PLAN            (plan rejected → planner re-runs
                                                with rejection notes as a
                                                clarification round)

    EXECUTING ──> [Phase 2]                    (no-op in 1b)

The daemon talks to the qwen_autonomous SQLite store directly (it shares
the file with qwen-server) and to the qwen-server inference HTTP API for
calling the planner agent. It must NOT serve HTTP itself.
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
from qwen_autonomous.planner import (
    PlannerClarify,
    PlannerError,
    PlannerYaml,
    call_planner,
)
from qwen_autonomous.jira_client import (
    AtlassianApiJiraClient,
    FakeJiraClient,
    JiraClient,
)
from qwen_autonomous.jira_sync import JiraSync

# ── Configuration ────────────────────────────────────────────────────────────

POLL_INTERVAL = float(os.getenv("ORCHESTRATOR_POLL_INTERVAL", "5"))
LOG_LEVEL = os.getenv("ORCHESTRATOR_LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - orchestrator - %(levelname)s - %(message)s",
)
logger = logging.getLogger("orchestrator")


# ── Gate prompt formatting ───────────────────────────────────────────────────

def _format_plan_gate_prompt(spec: Spec, yaml_text: str) -> str:
    return (
        f"## Plan ready for review: {spec.title}\n\n"
        f"Spec ID: `{spec.id}`\n\n"
        f"The planner has produced the following plan. Approve to begin "
        f"execution, or reject with notes to ask for changes (the planner "
        f"will re-run with your notes).\n\n"
        f"```yaml\n{yaml_text}\n```\n"
    )


def _format_clarification_gate_prompt(spec: Spec, questions: list[str]) -> str:
    """Markdown a human will see when reviewing a clarification gate.

    The numbered questions inside the fenced code block are the canonical
    form: ``_collect_clarification_rounds`` parses them back out on re-run.
    """
    qblock = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
    return (
        f"## Clarification needed: {spec.title}\n\n"
        f"Spec ID: `{spec.id}`\n\n"
        f"The planner needs more information before producing a plan. "
        f"Approve this gate with `--notes` containing your answers (numbered "
        f"or freeform — the planner will read them in context). Reject the "
        f"gate to cancel the spec.\n\n"
        f"### Questions\n\n"
        f"```\n{qblock}\n```\n"
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _collect_clarification_rounds(db: Database, spec_id: str) -> list[tuple[str, str]]:
    """Build the (questions, answers) round list for a planner re-run.

    Walks every clarification gate for this spec in chronological order and
    returns the ones that have a recorded answer. Pending gates are skipped
    — they're what we'd be waiting on, so they cannot be a "previous round."
    """
    rounds: list[tuple[str, str]] = []
    for gate in db.list_gates_for_spec(spec_id, GateType.CLARIFICATION):
        if gate.reviewer_notes is None or gate.status != GateStatus.APPROVED:
            continue
        rounds.append((gate.prompt_md, gate.reviewer_notes))
    return rounds


def _latest_gate_of_type(db: Database, spec_id: str,
                         gate_type: GateType) -> ReviewGate | None:
    gates = db.list_gates_for_spec(spec_id, gate_type)
    return gates[-1] if gates else None


# ── State machine handlers ───────────────────────────────────────────────────

def _process_pending_plan(db: Database, spec: Spec) -> None:
    """Run the planner agent against a spec in PENDING_PLAN state.

    Three possible outcomes:
      * PlannerYaml      → spec → PLAN_REVIEW + plan_approval gate
      * PlannerClarify   → spec → NEEDS_CLARIFICATION + clarification gate
      * PlannerError     → spec → FAILED with the parse error in the event log
    """
    spec_dir = db.spec_dir(spec.id)
    md_path = spec_dir / spec.source_md_path
    if not md_path.exists():
        logger.error("spec %s: source markdown missing at %s",
                     spec.id, md_path)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    markdown = md_path.read_text()
    rounds = _collect_clarification_rounds(db, spec.id)
    logger.info("spec %s: running planner (md=%d bytes, rounds=%d)",
                spec.id, len(markdown), len(rounds))

    try:
        result = call_planner(markdown, clarifications=rounds)
    except Exception as e:
        logger.exception("spec %s: planner call failed", spec.id)
        db.record_event(
            EventKind.PLANNER_RAN,
            spec_id=spec.id,
            payload={"error": f"{type(e).__name__}: {e}"},
        )
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    db.record_event(
        EventKind.PLANNER_RAN,
        spec_id=spec.id,
        payload={
            "result_kind": type(result).__name__,
            "rounds_provided": len(rounds),
        },
    )

    if isinstance(result, PlannerYaml):
        _accept_plan(db, spec, spec_dir, result)
    elif isinstance(result, PlannerClarify):
        _emit_clarification(db, spec, result)
    else:
        # PlannerError
        logger.error("spec %s: planner output unparseable: %s",
                     spec.id, result.reason)
        db.record_event(
            EventKind.PLANNER_RAN,
            spec_id=spec.id,
            payload={
                "error": result.reason,
                "raw_excerpt": result.raw_response[:500],
            },
        )
        db.update_spec_status(spec.id, SpecStatus.FAILED)


def _accept_plan(db: Database, spec: Spec, spec_dir, result: PlannerYaml) -> None:
    """Persist a YAML plan to disk and create the plan_approval gate."""
    yaml_text = result.yaml_text
    db.update_spec_status(
        spec.id,
        SpecStatus.PLAN_REVIEW,
        normalized_yaml=yaml_text,
    )
    yaml_path = spec_dir / "plan.yaml"
    yaml_path.write_text(yaml_text + "\n")
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
    logger.info("spec %s: plan accepted (%d bytes), plan_approval gate created",
                spec.id, len(yaml_text))


def _emit_clarification(db: Database, spec: Spec,
                        result: PlannerClarify) -> None:
    """Create a clarification gate and park the spec until the human responds."""
    db.update_spec_status(spec.id, SpecStatus.NEEDS_CLARIFICATION)
    db.create_gate(
        spec_id=spec.id,
        gate_type=GateType.CLARIFICATION,
        prompt_md=_format_clarification_gate_prompt(spec, result.questions),
    )
    logger.info("spec %s: planner needs clarification (%d questions)",
                spec.id, len(result.questions))


def _process_needs_clarification(db: Database, spec: Spec) -> None:
    """Check the latest clarification gate; if resolved, advance the spec.

    Approved → return to PENDING_PLAN so the planner re-runs with the
    answers in the next tick. Rejected → mark the spec CANCELLED.
    """
    gate = _latest_gate_of_type(db, spec.id, GateType.CLARIFICATION)
    if gate is None:
        logger.warning("spec %s: NEEDS_CLARIFICATION but no clarification "
                       "gate exists; marking failed", spec.id)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    if gate.status == GateStatus.PENDING:
        return  # waiting on the human

    if gate.status == GateStatus.APPROVED:
        if not gate.reviewer_notes:
            logger.warning("spec %s: clarification approved with no notes; "
                           "planner will re-run without new context", spec.id)
        logger.info("spec %s: clarification answered, returning to "
                    "PENDING_PLAN for replanning", spec.id)
        db.update_spec_status(spec.id, SpecStatus.PENDING_PLAN)
    elif gate.status == GateStatus.REJECTED:
        logger.info("spec %s: clarification rejected, cancelling spec",
                    spec.id)
        db.update_spec_status(spec.id, SpecStatus.CANCELLED)


def _process_plan_review(db: Database, spec: Spec) -> None:
    """Look for a resolved plan_approval gate and act on it."""
    gate = _latest_gate_of_type(db, spec.id, GateType.PLAN_APPROVAL)
    if gate is None:
        logger.warning("spec %s: PLAN_REVIEW without a plan_approval gate; "
                       "marking failed", spec.id)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    if gate.status == GateStatus.PENDING:
        return  # waiting on the human

    if gate.status == GateStatus.APPROVED:
        logger.info("spec %s: plan approved, transitioning to EXECUTING "
                    "(Phase 2 will take over)", spec.id)
        db.update_spec_status(spec.id, SpecStatus.EXECUTING)
    elif gate.status == GateStatus.REJECTED:
        # The reviewer's rejection notes become a synthetic clarification
        # round so the planner sees them on its next pass.
        logger.info("spec %s: plan rejected, replanning with notes=%r",
                    spec.id, gate.reviewer_notes)
        if gate.reviewer_notes:
            new_gate = db.create_gate(
                spec_id=spec.id,
                gate_type=GateType.CLARIFICATION,
                prompt_md=("## Plan rejection feedback\n\n"
                           "The reviewer rejected the previous plan with "
                           "the following notes — treat them as new "
                           "requirements and revise."),
            )
            db.respond_to_gate(new_gate.id, "approved",
                               notes=gate.reviewer_notes)
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
    """One pass over the spec table. Idempotent — safe to call any time.

    Order matters: clarification gates need to advance specs from
    NEEDS_CLARIFICATION → PENDING_PLAN before the planner pass picks them
    up in the same tick. Otherwise they'd wait an extra cycle for nothing.
    """
    needs_clar = db.list_specs(status=SpecStatus.NEEDS_CLARIFICATION)
    for spec in needs_clar:
        try:
            _process_needs_clarification(db, spec)
        except Exception:
            logger.exception("spec %s: error during clarification pass",
                             spec.id)

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


def _build_jira_client() -> JiraClient:
    """Construct the right Jira client based on environment configuration.

    If JIRA_URL / JIRA_EMAIL / JIRA_API_TOKEN are all set, use the real
    Atlassian client. Otherwise default to FakeJiraClient — the daemon
    still runs, the sync worker still runs, it just doesn't talk to a
    real Jira instance. This means we can develop and test against the
    fake without needing live credentials.
    """
    url = os.getenv("JIRA_URL", "").strip()
    email = os.getenv("JIRA_EMAIL", "").strip()
    token = os.getenv("JIRA_API_TOKEN", "").strip()
    project_key = os.getenv("JIRA_PROJECT_KEY", "AUTO").strip()

    if url and email and token:
        try:
            client: JiraClient = AtlassianApiJiraClient(
                url=url, email=email, api_token=token, project_key=project_key,
            )
            logger.info("Jira sync ENABLED (real client, project=%s)",
                        project_key)
            return client
        except Exception as e:
            logger.warning(
                "Jira credentials present but client init failed (%s); "
                "falling back to FakeJiraClient", e,
            )

    logger.info("Jira sync running with FakeJiraClient (no JIRA_URL/EMAIL/"
                "TOKEN configured) — events will not reach a real Jira "
                "instance, but the worker will run normally")
    return FakeJiraClient(project_key=project_key)


def main() -> int:
    logger.info("orchestrator daemon starting (poll=%.1fs)", POLL_INTERVAL)
    db = Database()
    logger.info("task store: %s", db.db_path)
    logger.info("workspace:  %s", db.workspace_root)

    # Spin up the Jira sync worker on its own thread. It shares the same
    # Database instance (SQLite WAL is thread-safe) and runs independently
    # of the main planner loop, so a Jira outage never blocks planning.
    jira_client = _build_jira_client()
    jira_sync = JiraSync(db, jira_client)
    jira_sync.start()

    flag = _ShutdownFlag()
    flag.install_handlers()

    last_heartbeat = 0.0
    HEARTBEAT_INTERVAL = 60.0

    try:
        while not flag.set:
            try:
                tick(db)
            except Exception:
                logger.exception("tick failed; continuing")

            # Periodic heartbeat into the events table — useful for liveness
            # checks and for the Jira sync worker to know the daemon is alive.
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
    finally:
        logger.info("stopping jira-sync worker...")
        jira_sync.stop()

    logger.info("orchestrator daemon stopped")
    db.close_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
