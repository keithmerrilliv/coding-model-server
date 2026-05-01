#!/usr/bin/env python3
"""qwen-orchestrator daemon.

Long-running process that drives autonomous-mode specs through their state
machine. The full pipeline (plan → architect → implementer → reviewer →
supervisor retry/replan) lives here.

State transitions:

    PENDING_PLAN ──┬──> NEEDS_CLARIFICATION  (planner asked questions)
                   ├──> PLAN_REVIEW           (planner produced YAML)
                   └──> FAILED                (planner output unparseable)

    NEEDS_CLARIFICATION ──┬──> PENDING_PLAN   (clarification gate approved
                          │                    → re-run planner with answers)
                          └──> CANCELLED       (clarification gate rejected)

    PLAN_REVIEW ──┬──> EXECUTING               (plan approved)
                  └──> PENDING_PLAN            (plan rejected → planner re-runs
                                                with rejection notes as a
                                                clarification round)

    EXECUTING ──> [architect → review-gate → implementer → review-gate →
                   reviewer → review-gate → COMPLETED, with supervisor-
                   driven retries on review rejection or test failure]

The daemon talks to the qwen_autonomous SQLite store directly (it shares
the file with qwen-server) and to the qwen-server inference HTTP API for
calling each agent. It must NOT serve HTTP itself.
"""
from __future__ import annotations

import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from qwen_autonomous import (
    ArtifactKind,
    Database,
    EventKind,
    GateStatus,
    GateType,
    SpecStatus,
    TaskStatus,
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
from qwen_autonomous import executor
from qwen_autonomous.executor import (
    ALLOWED_IMPLEMENTER_AGENTS,
    ArchitectResult,
    ImplementerResult,
    MAX_RETRIES,
    ParseError,
    ReviewerResult,
    TIER_TO_IMPLEMENTER,
    _write_artifact,
    build_architect_message,
    build_implementer_message,
    build_reviewer_message,
    call_agent,
    parse_architect_response,
    parse_implementer_response,
    parse_reviewer_response,
    run_tests,
)
from qwen_autonomous import supervisor as _supervisor

# ── Configuration ────────────────────────────────────────────────────────────

POLL_INTERVAL = float(os.getenv("ORCHESTRATOR_POLL_INTERVAL", "5"))
LOG_LEVEL = os.getenv("ORCHESTRATOR_LOG_LEVEL", "INFO").upper()
SUPERVISOR_ENABLED = os.getenv("AUTONOMOUS_SUPERVISOR", "0") == "1"

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
        logger.info("spec %s: plan approved, transitioning to EXECUTING", spec.id)
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

    executing = db.list_specs(status=SpecStatus.EXECUTING)
    for spec in executing:
        try:
            _process_executing(db, spec)
        except Exception:
            logger.exception("spec %s: error during execution pass", spec.id)


# ── Execution state machine (Phase 2) ───────────────────────────────────────

# Maps task role → which gate type to create after the agent finishes.
_ROLE_TO_GATE_TYPE = {
    "architect": GateType.DESIGN_APPROVAL,
    "implementer": GateType.CODE_REVIEW,
    "reviewer": GateType.RELEASE_APPROVAL,
}


def _process_executing(db: Database, spec: Spec) -> None:
    """Drive a spec through its task DAG while it's in EXECUTING state.

    On each tick we:
      1. Bootstrap tasks from the YAML if they don't exist yet.
      2. Find the first non-finished task.
      3. Either start it, check its review gate, or handle crash recovery.
    """
    tasks = db.list_tasks_for_spec(spec.id)
    # Bootstrap when there are no tasks (fresh spec) OR every task is SKIPPED
    # (post-replan: supervisor invalidated the prior task DAG and we re-entered
    # EXECUTING with a new plan). Without this, a replan that completes leaves
    # only DONE+SKIPPED tasks, _find_current_task returns None, and the spec
    # gets falsely marked DONE without ever re-running the new plan.
    if not tasks or all(t.status == TaskStatus.SKIPPED for t in tasks):
        _bootstrap_tasks(db, spec)
        return

    current = _find_current_task(tasks)
    if current is None:
        # All tasks done — mark spec done.
        logger.info("spec %s: all tasks completed, marking DONE", spec.id)
        db.update_spec_status(spec.id, SpecStatus.DONE)
        return

    if current.status == TaskStatus.PENDING:
        _start_task(db, spec, current)
    elif current.status == TaskStatus.RUNNING:
        # Shouldn't happen in normal operation since agent calls block the
        # tick. If we see RUNNING, the daemon crashed mid-call — reset.
        logger.warning("spec %s: task %s stuck in RUNNING (crash recovery?), "
                       "resetting to PENDING", spec.id, current.id)
        db.update_task_status(current.id, TaskStatus.PENDING)
    elif current.status == TaskStatus.BLOCKED_ON_REVIEW:
        _check_execution_gate(db, spec, current)


def _bootstrap_tasks(db: Database, spec: Spec) -> None:
    """Parse the planner's YAML into Task rows."""
    import yaml as _yaml
    if not spec.normalized_yaml:
        # State machine reached EXECUTING without a plan (supervisor replan
        # path or hand-edited DB row). Without this guard, safe_load(None)
        # returns None and plan.get("phases") AttributeErrors with a stack
        # trace that's hard to read in the daemon log.
        logger.error("spec %s: EXECUTING with no normalized_yaml — marking failed",
                     spec.id)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return
    plan = _yaml.safe_load(spec.normalized_yaml)
    if not isinstance(plan, dict):
        logger.error("spec %s: normalized_yaml is not a dict (got %s) — marking failed",
                     spec.id, type(plan).__name__)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return
    phases = plan.get("phases", [])
    if not phases:
        logger.error("spec %s: plan YAML has no phases — marking failed",
                     spec.id)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return
    for phase in phases:
        role = phase.get("role", "implementer")
        db.create_task(
            spec_id=spec.id,
            agent=executor.role_to_agent(role),
            role=role,
            title=phase.get("name", role),
            description=str(phase.get("success", "")),
            execution_target=phase.get("execution_target", "server"),
        )
    logger.info("spec %s: bootstrapped %d tasks from plan YAML",
                spec.id, len(phases))


def _find_current_task(tasks: list) -> "Task | None":
    """Return the first task that isn't DONE or SKIPPED, or None."""
    for t in tasks:
        if t.status not in (TaskStatus.DONE, TaskStatus.SKIPPED):
            return t
    return None


def _start_task(db: Database, spec: Spec, task) -> None:
    """Call the appropriate agent, parse the response, create artifacts
    and a review gate. This is synchronous — it blocks the tick thread
    for the entire duration of the inference call.
    """
    db.update_task_status(task.id, TaskStatus.RUNNING)
    spec_dir = db.spec_dir(spec.id)

    try:
        if task.role == "architect":
            _run_architect(db, spec, task, spec_dir)
        elif task.role == "implementer":
            _run_implementer(db, spec, task, spec_dir)
        elif task.role == "reviewer":
            _run_reviewer(db, spec, task, spec_dir)
        else:
            logger.error("spec %s: unknown role %r for task %s",
                         spec.id, task.role, task.id)
            db.update_task_status(task.id, TaskStatus.FAILED)
    except Exception:
        logger.exception("spec %s: task %s (%s) failed with exception",
                         spec.id, task.id, task.role)
        db.update_task_status(task.id, TaskStatus.FAILED)
        db.update_spec_status(spec.id, SpecStatus.FAILED)


def _run_architect(db: Database, spec: Spec, task, spec_dir) -> None:
    spec_md = (spec_dir / spec.source_md_path).read_text()
    messages = build_architect_message(spec_md)

    raw = call_agent("architect", messages)
    result = parse_architect_response(raw)

    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": "architect",
                             "result_kind": type(result).__name__})

    if isinstance(result, ParseError):
        logger.error("spec %s: architect response unparseable: %s",
                     spec.id, result.reason)
        db.update_task_status(task.id, TaskStatus.FAILED)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    # Write design.md
    _write_artifact(spec_dir, "design.md", result.design_md)
    db.create_artifact(spec_id=spec.id, task_id=task.id,
                       kind=ArtifactKind.DESIGN, path="design.md")

    # Persist complexity assessment as a workspace artifact. None when the
    # architect skipped or malformed the COMPLEXITY block — _run_implementer
    # falls back to the env-default agent in that case.
    if result.complexity:
        import json as _json
        _write_artifact(spec_dir, "complexity.json",
                        _json.dumps(result.complexity, indent=2) + "\n")
        logger.info("spec %s: architect complexity assessment: tier=%r agent=%r",
                    spec.id, result.complexity.get("tier"),
                    result.complexity.get("recommended_agent"))

    # Create design_approval gate
    db.update_task_status(task.id, TaskStatus.BLOCKED_ON_REVIEW)
    db.create_gate(
        spec_id=spec.id,
        task_id=task.id,
        gate_type=GateType.DESIGN_APPROVAL,
        prompt_md=(
            f"## Design ready for review: {spec.title}\n\n"
            f"Spec ID: `{spec.id}`\n\n"
            f"The architect has produced the following design. Approve to "
            f"begin implementation, or reject with notes.\n\n---\n\n"
            f"{result.design_md}\n"
        ),
    )
    logger.info("spec %s: architect done, design_approval gate created",
                spec.id)


def _run_implementer(db: Database, spec: Spec, task, spec_dir) -> None:
    spec_md = (spec_dir / spec.source_md_path).read_text()
    design_path = spec_dir / "design.md"
    design_md = design_path.read_text() if design_path.exists() else ""

    # On retry, include rejection notes from the most recent code_review gate.
    rejection_notes = None
    if task.retry_count > 0:
        prev_gates = db.list_gates_for_spec(spec.id, GateType.CODE_REVIEW)
        for g in reversed(prev_gates):
            if g.status == GateStatus.REJECTED and g.reviewer_notes:
                rejection_notes = g.reviewer_notes
                break

    # Pick the implementer. Retry 0 honors the architect's complexity-based
    # recommendation; later retries walk the rotation chain so each attempt
    # uses a different model family — see project_implementer_rotation.md.
    # Falling back to task.agent (env-default) when complexity.json is absent
    # so retries still rotate from a sensible anchor.
    initial_agent = _select_implementer_agent(spec_dir) or task.agent
    chosen_agent = _rotation_pick(initial_agent, task.retry_count)
    if chosen_agent and chosen_agent != task.agent:
        if task.retry_count == 0:
            logger.info("spec %s: architect recommendation overrides implementer agent: %r → %r",
                        spec.id, task.agent, chosen_agent)
        else:
            logger.info("spec %s: rotation pick (retry=%d): %r → %r",
                        spec.id, task.retry_count, task.agent, chosen_agent)
        db.update_task_agent(task.id, chosen_agent)

    messages = build_implementer_message(spec_md, design_md,
                                         rejection_notes=rejection_notes)
    raw = call_agent("implementer", messages, agent=chosen_agent)
    result = parse_implementer_response(raw)

    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": "implementer",
                             "agent": chosen_agent or task.agent,
                             "result_kind": type(result).__name__,
                             "retry": task.retry_count})

    if isinstance(result, ParseError):
        logger.error("spec %s: implementer response unparseable: %s",
                     spec.id, result.reason)
        db.update_task_status(task.id, TaskStatus.FAILED)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    # Write all files
    for rel_path, content in result.files:
        _write_artifact(spec_dir, rel_path, content)
        db.create_artifact(spec_id=spec.id, task_id=task.id,
                           kind=ArtifactKind.CODE, path=rel_path)

    # Create code_review gate
    file_list = "\n".join(f"- `{p}`" for p, _ in result.files)
    db.update_task_status(task.id, TaskStatus.BLOCKED_ON_REVIEW)
    db.create_gate(
        spec_id=spec.id,
        task_id=task.id,
        gate_type=GateType.CODE_REVIEW,
        prompt_md=(
            f"## Code review: {spec.title}\n\n"
            f"Spec ID: `{spec.id}`\n"
            f"Retry: {task.retry_count}\n\n"
            f"The implementer produced the following files:\n\n{file_list}\n\n"
            f"Approve to proceed to testing, or reject with notes.\n"
        ),
    )
    logger.info("spec %s: implementer done (%d files, retry=%d), "
                "code_review gate created",
                spec.id, len(result.files), task.retry_count)


# Pytest summary line: e.g. "1 passed in 0.01s", "2 failed, 3 passed in 0.5s",
# "5 errors in 1.0s". We only care that *some* outcome count is reported.
_PYTEST_SUMMARY_RE = re.compile(
    r"\b\d+\s+(passed|failed|error|errors|skipped|xfailed|xpassed|deselected)\b",
    re.IGNORECASE,
)
# Jest summary: "Tests: N passed, M total"
_JEST_SUMMARY_RE = re.compile(r"Tests?:\s+\d+\s+\w+", re.IGNORECASE)


def _extract_actionable_test_output(output: str, framework: str, max_chars: int = 8000) -> str:
    """Trim test output to the actionable parts for implementer retry feedback.

    Verbose pytest output is mostly noise (collection, plugin versions, progress
    markers) and the actionable bits (assertion tracebacks + the short summary)
    are at the bottom. The previous 3000-char truncation often cut OFF the
    failures and left the implementer with only the preamble — leading to
    repeated retries that converged on the same wrong implementation. Drop the
    preamble; keep the failures and the final summary.

    For unrecognized frameworks, fall back to a tail-biased truncation.
    """
    if not output:
        return output
    fw = framework.lower()
    if fw in ("pytest", "python"):
        # Pytest's failure section starts with `=========== FAILURES ===========`.
        # Anything before that is collection/progress noise.
        marker = output.find("FAILURES")
        if marker != -1:
            # Step back to the start of the line to keep the banner intact.
            line_start = output.rfind("\n", 0, marker) + 1
            extracted = output[line_start:]
            if len(extracted) <= max_chars:
                return extracted
            # Keep the head (first failures) + the tail (summary) — the middle
            # is just more failures of the same kind in most cases.
            head = extracted[: max_chars - 1200]
            tail = extracted[-1200:]
            return head + "\n\n[... output truncated ...]\n\n" + tail
    # Default: tail-biased — the summary is at the end and matters most.
    if len(output) <= max_chars:
        return output
    return "[... output truncated ...]\n\n" + output[-max_chars:]


def _validate_test_output_structure(test_output: str, framework: str) -> tuple[bool, str]:
    """Confirm the test output has the structural shape of a real test run.

    Catches the failure mode where a sandbox error or collection failure exits
    cleanly without any tests actually running, leaving the orchestrator with
    no evidence either way. Returns (ok, reason). On (False, reason) the
    caller should force tests_passed=False — the runner can't be trusted.

    Frameworks we don't recognize (Swift via mac-runner, custom) pass through.
    """
    if not test_output or not test_output.strip():
        return False, "test_output is empty"
    fw = framework.lower()
    if fw in ("pytest", "python"):
        if not _PYTEST_SUMMARY_RE.search(test_output):
            return False, "no pytest summary line ('N passed/failed/error') detected"
    elif fw == "jest":
        if not _JEST_SUMMARY_RE.search(test_output):
            return False, "no jest summary line ('Tests: ...') detected"
    return True, ""


def _run_reviewer(db: Database, spec: Spec, task, spec_dir) -> None:
    import yaml as _yaml

    spec_md = (spec_dir / spec.source_md_path).read_text()
    design_path = spec_dir / "design.md"
    design_md = design_path.read_text() if design_path.exists() else ""

    # Gather all code files from implementer artifacts
    code_artifacts = [a for a in _list_code_artifacts(db, spec.id)]
    code_files = []
    for art in code_artifacts:
        fpath = spec_dir / art.path
        if fpath.exists():
            code_files.append((art.path, fpath.read_text()))

    # Detect test framework from the plan
    plan = _yaml.safe_load(spec.normalized_yaml) if spec.normalized_yaml else {}
    test_strategy = plan.get("test_strategy", {})
    framework = test_strategy.get("framework", "pytest")
    tests_required = test_strategy.get("required", True)

    # Pull supervisor feedback if this is a reviewer retry. Without this the
    # reviewer reruns blind, regenerating the same broken tests — observed in
    # spec_d8ac6b36 where a missing `import pytest` survived 3 retries.
    rejection_notes = _latest_supervisor_feedback(db, spec.id, target_role="reviewer") \
        if task.retry_count > 0 else None

    messages = build_reviewer_message(spec_md, design_md, code_files,
                                       test_framework=framework,
                                       rejection_notes=rejection_notes)
    raw = call_agent("reviewer", messages)
    result = parse_reviewer_response(raw)

    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": "reviewer",
                             "result_kind": type(result).__name__})

    if isinstance(result, ParseError):
        logger.error("spec %s: reviewer response unparseable: %s",
                     spec.id, result.reason)
        # Persist the raw response so the operator can post-mortem the
        # parse failure. Without this, ~10 minutes of reviewer compute
        # is opaque after the fact.
        try:
            (spec_dir / "reviewer_failed_response.txt").write_text(
                f"# parse error: {result.reason}\n\n{result.text}"
            )
        except OSError as e:
            logger.warning("spec %s: could not persist failed reviewer "
                           "response: %s", spec.id, e)
        db.update_task_status(task.id, TaskStatus.FAILED)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    # Write test files
    for rel_path, content in result.test_files:
        _write_artifact(spec_dir, rel_path, content)
        db.create_artifact(spec_id=spec.id, task_id=task.id,
                           kind=ArtifactKind.TEST_REPORT, path=rel_path)

    # Write review report
    _write_artifact(spec_dir, "review_report.md", result.review_md)
    db.create_artifact(spec_id=spec.id, task_id=task.id,
                       kind=ArtifactKind.REVIEW_REPORT, path="review_report.md")

    # Run tests if required
    tests_passed = True
    test_output = ""
    if tests_required and result.test_files:
        # Pass through framework-specific options from the planner's test_strategy
        # (repo, scheme, destination, etc. for Swift/Xcode via the mac-runner).
        framework_opts = {
            k: v for k, v in test_strategy.items()
            if k not in ("framework", "required")
        }
        tests_passed, test_output = run_tests(
            spec_dir, framework=framework, **framework_opts,
        )
        # Layer 1 (anti-hallucination guard): a test runner that exits 0 with
        # no parseable summary line means the runner short-circuited (sandbox
        # error, no tests collected, output truncation). Trusting the
        # subprocess returncode alone let a "Reviewer verdict: PASS"
        # propagate while pytest never executed (the bwrap/AppArmor case).
        # If the output doesn't match the framework's summary shape, force
        # FAIL — even when subprocess.returncode was 0.
        if tests_passed:
            ok, reason = _validate_test_output_structure(test_output, framework)
            if not ok:
                logger.warning(
                    "spec %s: test_output failed structural validation (%s); "
                    "forcing tests_passed=False to block hallucinated PASS",
                    spec.id, reason,
                )
                tests_passed = False
                test_output = (
                    f"[orchestrator guard] {reason}\n\n"
                    f"Original test runner output:\n{test_output}"
                )
        _write_artifact(spec_dir, "test_output.txt", test_output)
        db.record_event(EventKind.TEST_RAN, spec_id=spec.id, task_id=task.id,
                        payload={"passed": tests_passed,
                                 "output_chars": len(test_output)})

    if tests_passed and result.verdict == "PASS":
        # Everything looks good — create release_approval gate
        db.update_task_status(task.id, TaskStatus.BLOCKED_ON_REVIEW)
        db.create_gate(
            spec_id=spec.id,
            task_id=task.id,
            gate_type=GateType.RELEASE_APPROVAL,
            prompt_md=(
                f"## Release approval: {spec.title}\n\n"
                f"Spec ID: `{spec.id}`\n\n"
                f"Tests **PASSED**. Reviewer verdict: **PASS**.\n\n"
                f"### Review Report\n\n{result.review_md}\n\n"
                f"### Test Output\n\n```\n{test_output[:3000]}\n```\n\n"
                f"Approve to mark this spec as DONE, or reject to send "
                f"back to the implementer.\n"
            ),
        )
        logger.info("spec %s: reviewer done, tests passed, "
                    "release_approval gate created", spec.id)
    else:
        # Tests failed or reviewer said FAIL — attempt retry. Send the
        # actionable slice of test output (failures + summary) rather than
        # the verbose head, so the implementer's retry sees the real
        # AssertionError lines instead of pytest's collection preamble.
        actionable = _extract_actionable_test_output(test_output, framework)
        failure_detail = (
            f"Reviewer verdict: {result.verdict}\n\n"
            f"Test output (failures + summary, full output in test_output.txt):\n"
            f"```\n{actionable}\n```\n\n"
            f"Review:\n{result.review_md}\n"
        )
        _write_artifact(spec_dir, "failure_report.md", failure_detail)
        _attempt_retry(db, spec, task, failure_detail)


# ── Supervisor-driven transition layer (Phase 2.5) ──────────────────────────
#
# When SUPERVISOR_ENABLED, _attempt_retry and _handle_gate_rejection consult
# the supervisor agent (qwen_autonomous.supervisor) to decide the next
# transition instead of routing on hardcoded if/elif by role.
#
# Limitations of the prototype:
#   - request_clarification halts the spec on a CLARIFICATION gate; the human
#     must respond manually. There is no auto-resume that re-invokes the
#     supervisor with the response — that's a follow-up.
#   - retry target_role=architect creates an approved CLARIFICATION gate with
#     the feedback as notes, but the existing build_architect_message() does
#     NOT consume clarification rounds. The audit trail is captured but the
#     architect re-runs without seeing the feedback. The supervisor's system
#     prompt steers it toward `replan` for design-level defects, which is
#     the path that actually plumbs the feedback (via _process_pending_plan).
#   - retry target_role=reviewer just resets reviewer to PENDING; no feedback
#     channel exists for the reviewer.

def _list_artifact_summaries(db: Database, spec_id: str) -> list[dict]:
    """Compact summary of all artifacts on disk — for the supervisor context."""
    rows = db._conn().execute(
        "SELECT kind, path FROM artifacts WHERE spec_id = ? ORDER BY created_at",
        (spec_id,),
    ).fetchall()
    spec_dir = db.spec_dir(spec_id)
    out = []
    for r in rows:
        path = r["path"]
        full = spec_dir / path
        size = full.stat().st_size if full.exists() else None
        item = {"kind": r["kind"], "path": path}
        if size is not None:
            item["bytes"] = size
        out.append(item)
    return out


def _build_supervisor_context(db: Database, spec: Spec, task, outcome: str,
                              *, reviewer_notes: str | None = None,
                              test_output_excerpt: str | None = None,
                              agent_error_excerpt: str | None = None,
                              ) -> "_supervisor.SupervisorContext":
    """Assemble the structured snapshot the supervisor reasons over."""
    transitions_used = db.count_events(
        spec_id=spec.id, kind=EventKind.SUPERVISOR_DECISION,
    )
    return {
        "spec_id": spec.id,
        "spec_title": spec.title,
        "phase": task.role,
        "role": task.role,
        "outcome": outcome,
        "retry_count": task.retry_count,
        "transitions_used": transitions_used,
        "plan_yaml": spec.normalized_yaml,
        "reviewer_notes": reviewer_notes,
        "test_output_excerpt": test_output_excerpt,
        "agent_error_excerpt": agent_error_excerpt,
        "artifacts": _list_artifact_summaries(db, spec.id),
        "prior_decisions": _load_prior_decisions(db, spec.id),
    }


def _select_implementer_agent(spec_dir) -> "str | None":
    """Read complexity.json and pick the implementer agent.

    Precedence: architect's specific `recommended_agent` (if it's in the
    whitelist) → tier default (if tier is recognized) → None (caller uses the
    env-default IMPLEMENTER_AGENT). Returns None silently on any error so a
    malformed or absent complexity.json never blocks the pipeline.
    """
    import json as _json
    cpath = spec_dir / "complexity.json"
    if not cpath.exists():
        return None
    try:
        c = _json.loads(cpath.read_text())
    except (OSError, _json.JSONDecodeError):
        return None
    rec = (c.get("recommended_agent") or "").strip()
    if rec in ALLOWED_IMPLEMENTER_AGENTS:
        return rec
    tier = (c.get("tier") or "").strip().lower()
    return TIER_TO_IMPLEMENTER.get(tier)


# Rotation chain for implementer retries. Ordered for vendor/family diversity:
# Qwen3.6 → Qwen3-Coder-Next → GLM (Zhipu) → MiniMax → Qwen3-Coder. With
# MAX_RETRIES=5 each retry slot 0..4 maps to a distinct model. See
# project_implementer_rotation.md for the rationale and the ECS abstraction
# tie-in (this hardcoded list becomes a capability query later).
_IMPLEMENTER_ROTATION = [
    "implementer", "deep_implementer", "glm",
    "m25_implementer", "fast_implementer",
]


def _rotation_pick(initial_agent: "str | None", retry_count: int) -> "str | None":
    """Advance to the next implementer in the rotation chain on retry.

    Retry 0 returns ``initial_agent`` unchanged — the architect's complexity
    recommendation wins on first attempt. From retry 1 onward, walks the
    rotation chain so each retry uses a different model and we get out of
    any single-model fragility pattern (see
    project_implementer_revision_fragility.md).

    The chain starts with ``initial_agent`` so retry index N maps to the
    Nth agent in a stable order; consecutive retries never repeat the same
    model.
    """
    if retry_count == 0 or not initial_agent:
        return initial_agent
    if initial_agent in _IMPLEMENTER_ROTATION:
        chain = [initial_agent] + [a for a in _IMPLEMENTER_ROTATION if a != initial_agent]
    else:
        chain = _IMPLEMENTER_ROTATION
    return chain[retry_count % len(chain)]


def _latest_supervisor_feedback(db: Database, spec_id: str,
                                *, target_role: str) -> str | None:
    """Most recent supervisor `feedback_to_inject` for *target_role* on this spec.

    Used by `_run_reviewer` (and any role whose retry path can't carry feedback
    through a synthetic gate) to read the supervisor's directive on retry.
    Returns None if no matching decision is found.
    """
    import json
    rows = db.list_events_by_kind(
        spec_id=spec_id, kind=EventKind.SUPERVISOR_DECISION, limit=20,
    )
    for r in rows:  # most-recent first
        if not r.payload_json:
            continue
        try:
            payload = json.loads(r.payload_json)
        except json.JSONDecodeError:
            continue
        if (payload.get("action") == "retry"
                and payload.get("target_role") == target_role
                and payload.get("feedback_to_inject")):
            return payload["feedback_to_inject"]
    return None


def _load_prior_decisions(db: Database, spec_id: str) -> list[dict]:
    """Most-recent N supervisor decisions for *spec_id*, oldest-first.

    Decoded from the events table's payload_json, capped to the supervisor
    transition budget so we never render more than the model could have made.
    """
    import json
    rows = db.list_events_by_kind(
        spec_id=spec_id,
        kind=EventKind.SUPERVISOR_DECISION,
        limit=_supervisor.MAX_SUPERVISOR_TRANSITIONS,
    )
    out = []
    for r in reversed(rows):  # oldest-first for natural reading order
        if not r.payload_json:
            continue
        try:
            payload = json.loads(r.payload_json)
        except json.JSONDecodeError:
            continue
        out.append({
            "action": payload.get("action", "?"),
            "target_role": payload.get("target_role"),
            "reason": payload.get("reason", ""),
        })
    return out


def _retry_role_with_feedback(db: Database, spec: Spec, target_role: str,
                              feedback: str, *, current_task) -> None:
    """Apply a supervisor-issued retry to *target_role*.

    For implementer/reviewer-driven retries that originate from the reviewer
    task (test failure or release rejection), we also reset the current_task
    (typically the reviewer) to PENDING so it re-runs after the implementer.
    """
    role_tasks = db.list_tasks_for_spec_by_role(spec.id, target_role)
    target = role_tasks[0] if role_tasks else None
    if target is None:
        logger.error("spec %s: supervisor said retry %s but no such task; aborting",
                     spec.id, target_role)
        db.update_task_status(current_task.id, TaskStatus.FAILED)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    if target.retry_count >= MAX_RETRIES:
        logger.error("spec %s: supervisor said retry %s but retry budget exhausted (%d/%d); aborting",
                     spec.id, target_role, target.retry_count, MAX_RETRIES)
        db.update_task_status(current_task.id, TaskStatus.FAILED)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    if target_role == "implementer":
        synth = db.create_gate(
            spec_id=spec.id, task_id=target.id,
            gate_type=GateType.CODE_REVIEW,
            prompt_md="## Supervisor-issued retry",
        )
        db.respond_to_gate(synth.id, "rejected", notes=feedback)
    elif target_role == "architect":
        synth = db.create_gate(
            spec_id=spec.id,
            gate_type=GateType.CLARIFICATION,
            prompt_md="## Supervisor-issued architect retry",
        )
        db.respond_to_gate(synth.id, "approved", notes=feedback)
    # reviewer: no feedback channel; just rerun

    db.increment_task_retry(target.id)
    db.update_task_status(target.id, TaskStatus.PENDING)
    if current_task.id != target.id:
        # Reset downstream task too so it re-runs after target completes
        db.update_task_status(current_task.id, TaskStatus.PENDING)
    logger.info("spec %s: supervisor retry %s (attempt %d/%d)",
                spec.id, target_role, target.retry_count + 1, MAX_RETRIES)


def _apply_supervisor_decision(db: Database, spec: Spec, task,
                               decision: "_supervisor.SupervisorDecision",
                               *, legacy_feedback: str | None) -> None:
    """Translate a SupervisorDecision into DB state changes.

    `legacy_feedback` is the failure_detail / reviewer_notes the legacy code
    would have used — passed through when the supervisor declines to provide
    its own feedback (defensive default for retry/replan).
    """
    db.record_event(
        EventKind.SUPERVISOR_DECISION,
        spec_id=spec.id, task_id=task.id,
        payload={
            "action": decision.action,
            "target_role": decision.target_role,
            "reason": decision.reason,
            "feedback_to_inject": decision.feedback_to_inject,
        },
    )

    if decision.action == "advance":
        logger.info("spec %s: supervisor → advance: %s", spec.id, decision.reason)
        db.update_task_status(task.id, TaskStatus.DONE)
        return

    if decision.action == "abort":
        logger.warning("spec %s: supervisor → abort: %s", spec.id, decision.reason)
        db.update_task_status(task.id, TaskStatus.FAILED)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    if decision.action == "retry":
        feedback = decision.feedback_to_inject or legacy_feedback or ""
        logger.info("spec %s: supervisor → retry %s: %s",
                    spec.id, decision.target_role, decision.reason)
        _retry_role_with_feedback(db, spec, decision.target_role, feedback,
                                  current_task=task)
        return

    if decision.action == "replan":
        logger.info("spec %s: supervisor → replan: %s", spec.id, decision.reason)
        feedback = decision.feedback_to_inject or legacy_feedback
        if feedback:
            synth = db.create_gate(
                spec_id=spec.id,
                gate_type=GateType.CLARIFICATION,
                prompt_md="## Supervisor-requested replan",
            )
            db.respond_to_gate(synth.id, "approved", notes=feedback)
        # Mark ALL existing tasks SKIPPED (including DONE ones) so the new
        # plan produces a fresh task DAG. Leaving DONE tasks in place causes
        # _process_executing to skip the bootstrap branch and falsely mark
        # the spec DONE without running the new plan.
        for t in db.list_tasks_for_spec(spec.id):
            if t.status != TaskStatus.SKIPPED:
                db.update_task_status(t.id, TaskStatus.SKIPPED)
        db.update_spec_status(spec.id, SpecStatus.PENDING_PLAN)
        return

    if decision.action == "request_clarification":
        logger.info("spec %s: supervisor → request_clarification: %s",
                    spec.id, decision.reason)
        db.update_task_status(task.id, TaskStatus.BLOCKED_ON_REVIEW)
        db.create_gate(
            spec_id=spec.id, task_id=task.id,
            gate_type=GateType.CLARIFICATION,
            prompt_md=(
                "## Supervisor needs clarification\n\n"
                f"{decision.feedback_to_inject}\n\n"
                "Approve with notes to provide an answer, or reject to abort."
            ),
        )
        return

    # Defensive: schema validation in supervisor.py should make this unreachable
    logger.error("spec %s: unknown supervisor action %r; aborting",
                 spec.id, decision.action)
    db.update_task_status(task.id, TaskStatus.FAILED)
    db.update_spec_status(spec.id, SpecStatus.FAILED)


def _attempt_retry(db: Database, spec: Spec, task, failure_detail: str) -> None:
    """Decide what to do after a test-failure / reviewer-FAIL outcome.

    With SUPERVISOR_ENABLED, asks the supervisor agent; on SupervisorError
    (transport, parse, schema violation), falls back to the legacy retry
    path so a flaky meta-call can't take down the spec.
    """
    if not SUPERVISOR_ENABLED:
        _legacy_attempt_retry(db, spec, task, failure_detail)
        return

    ctx = _build_supervisor_context(
        db, spec, task,
        outcome="test_fail",
        test_output_excerpt=failure_detail,
    )
    try:
        decision = _supervisor.decide(ctx)
    except _supervisor.SupervisorError as e:
        logger.warning("spec %s: supervisor failed (%s); falling back to legacy retry",
                       spec.id, e)
        _legacy_attempt_retry(db, spec, task, failure_detail)
        return
    _apply_supervisor_decision(db, spec, task, decision,
                               legacy_feedback=failure_detail)


def _legacy_attempt_retry(db: Database, spec: Spec, task, failure_detail: str) -> None:
    """Send the spec back to the implementer for another attempt, or fail."""
    impl_tasks = db.list_tasks_for_spec_by_role(spec.id, "implementer")
    impl_task = impl_tasks[0] if impl_tasks else None

    if impl_task is None:
        logger.error("spec %s: no implementer task to retry", spec.id)
        db.update_task_status(task.id, TaskStatus.FAILED)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    if impl_task.retry_count >= MAX_RETRIES:
        logger.error("spec %s: max retries (%d) exhausted, failing",
                     spec.id, MAX_RETRIES)
        db.update_task_status(task.id, TaskStatus.FAILED)
        db.update_spec_status(spec.id, SpecStatus.FAILED)
        return

    # Create a synthetic rejected code_review gate with the failure details
    # so _run_implementer picks it up as rejection_notes on its next run.
    synth_gate = db.create_gate(
        spec_id=spec.id,
        task_id=impl_task.id,
        gate_type=GateType.CODE_REVIEW,
        prompt_md="## Automated test failure — retry",
    )
    db.respond_to_gate(synth_gate.id, "rejected", notes=failure_detail)

    db.increment_task_retry(impl_task.id)
    db.update_task_status(impl_task.id, TaskStatus.PENDING)
    db.update_task_status(task.id, TaskStatus.PENDING)  # reviewer re-runs too
    logger.info("spec %s: tests failed, retrying implementer (attempt %d/%d)",
                spec.id, impl_task.retry_count + 1, MAX_RETRIES)


def _check_execution_gate(db: Database, spec: Spec, task) -> None:
    """Check the review gate for a task in BLOCKED_ON_REVIEW."""
    gate_type = _ROLE_TO_GATE_TYPE.get(task.role)
    if gate_type is None:
        logger.error("spec %s: no gate type for role %r", spec.id, task.role)
        db.update_task_status(task.id, TaskStatus.FAILED)
        return

    gate = _latest_gate_of_type(db, spec.id, gate_type)
    if gate is None or gate.status == GateStatus.PENDING:
        return  # waiting on the human

    if gate.status == GateStatus.APPROVED:
        logger.info("spec %s: task %s (%s) approved, marking done",
                    spec.id, task.id, task.role)
        db.update_task_status(task.id, TaskStatus.DONE)
    elif gate.status == GateStatus.REJECTED:
        _handle_gate_rejection(db, spec, task, gate)


def _handle_gate_rejection(db: Database, spec: Spec, task, gate) -> None:
    """Decide what to do after a human-rejected review gate.

    With SUPERVISOR_ENABLED, asks the supervisor agent; on SupervisorError,
    falls back to the legacy role-keyed branching.
    """
    if not SUPERVISOR_ENABLED:
        _legacy_handle_gate_rejection(db, spec, task, gate)
        return

    ctx = _build_supervisor_context(
        db, spec, task,
        outcome="review_reject",
        reviewer_notes=gate.reviewer_notes,
    )
    try:
        decision = _supervisor.decide(ctx)
    except _supervisor.SupervisorError as e:
        logger.warning("spec %s: supervisor failed (%s); falling back to legacy gate handler",
                       spec.id, e)
        _legacy_handle_gate_rejection(db, spec, task, gate)
        return
    _apply_supervisor_decision(db, spec, task, decision,
                               legacy_feedback=gate.reviewer_notes)


def _legacy_handle_gate_rejection(db: Database, spec: Spec, task, gate) -> None:
    """Handle a rejected review gate for a task."""
    if task.role == "architect":
        # Re-run the architect with rejection notes as a clarification.
        if gate.reviewer_notes:
            synth = db.create_gate(
                spec_id=spec.id,
                gate_type=GateType.CLARIFICATION,
                prompt_md="## Design rejection feedback",
            )
            db.respond_to_gate(synth.id, "approved", notes=gate.reviewer_notes)
        db.update_task_status(task.id, TaskStatus.PENDING)
        logger.info("spec %s: architect design rejected, re-running", spec.id)

    elif task.role == "implementer":
        impl_task = task
        if impl_task.retry_count < MAX_RETRIES:
            db.increment_task_retry(impl_task.id)
            db.update_task_status(impl_task.id, TaskStatus.PENDING)
            logger.info("spec %s: code rejected by human, retry %d/%d",
                        spec.id, impl_task.retry_count + 1, MAX_RETRIES)
        else:
            db.update_task_status(impl_task.id, TaskStatus.FAILED)
            db.update_spec_status(spec.id, SpecStatus.FAILED)
            logger.error("spec %s: code rejected, max retries exhausted",
                         spec.id)

    elif task.role == "reviewer":
        # Release rejected — send back to implementer.
        impl_tasks = db.list_tasks_for_spec_by_role(spec.id, "implementer")
        impl_task = impl_tasks[0] if impl_tasks else None
        if impl_task and impl_task.retry_count < MAX_RETRIES:
            if gate.reviewer_notes:
                synth = db.create_gate(
                    spec_id=spec.id,
                    task_id=impl_task.id,
                    gate_type=GateType.CODE_REVIEW,
                    prompt_md="## Release rejection — retry",
                )
                db.respond_to_gate(synth.id, "rejected",
                                   notes=gate.reviewer_notes)
            db.increment_task_retry(impl_task.id)
            db.update_task_status(impl_task.id, TaskStatus.PENDING)
            db.update_task_status(task.id, TaskStatus.PENDING)
            logger.info("spec %s: release rejected, retrying implementer",
                        spec.id)
        else:
            db.update_task_status(task.id, TaskStatus.FAILED)
            db.update_spec_status(spec.id, SpecStatus.FAILED)
            logger.error("spec %s: release rejected, max retries exhausted",
                         spec.id)


def _list_code_artifacts(db: Database, spec_id: str):
    """Return all CODE artifacts for a spec (for feeding to the reviewer)."""
    # We need to query artifacts by kind — add inline since we don't
    # have a dedicated db method yet.
    rows = db._conn().execute(
        "SELECT * FROM artifacts WHERE spec_id = ? AND kind = ? "
        "ORDER BY created_at",
        (spec_id, ArtifactKind.CODE.value),
    ).fetchall()
    from qwen_autonomous.models import Artifact
    from qwen_autonomous.db import _parse_iso
    return [
        Artifact(
            id=r["id"], spec_id=r["spec_id"], task_id=r["task_id"],
            kind=ArtifactKind(r["kind"]), path=r["path"],
            sha256=r["sha256"], created_at=_parse_iso(r["created_at"]),
        )
        for r in rows
    ]


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
