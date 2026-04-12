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
from qwen_autonomous.executor import (
    ArchitectResult,
    ImplementerResult,
    MAX_RETRIES,
    ParseError,
    ReviewerResult,
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
    if not tasks:
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
    plan = _yaml.safe_load(spec.normalized_yaml)
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
            agent=__import__("qwen_autonomous.executor",
                             fromlist=["role_to_agent"]).role_to_agent(role),
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

    messages = build_implementer_message(spec_md, design_md,
                                         rejection_notes=rejection_notes)
    raw = call_agent("implementer", messages)
    result = parse_implementer_response(raw)

    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": "implementer",
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

    messages = build_reviewer_message(spec_md, design_md, code_files,
                                       test_framework=framework)
    raw = call_agent("reviewer", messages)
    result = parse_reviewer_response(raw)

    db.record_event(EventKind.AGENT_RAN, spec_id=spec.id, task_id=task.id,
                    payload={"role": "reviewer",
                             "result_kind": type(result).__name__})

    if isinstance(result, ParseError):
        logger.error("spec %s: reviewer response unparseable: %s",
                     spec.id, result.reason)
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
        tests_passed, test_output = run_tests(spec_dir, framework=framework)
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
        # Tests failed or reviewer said FAIL — attempt retry
        failure_detail = (
            f"Reviewer verdict: {result.verdict}\n\n"
            f"Test output:\n```\n{test_output[:3000]}\n```\n\n"
            f"Review:\n{result.review_md}\n"
        )
        _write_artifact(spec_dir, "failure_report.md", failure_detail)
        _attempt_retry(db, spec, task, failure_detail)


def _attempt_retry(db: Database, spec: Spec, task, failure_detail: str) -> None:
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
