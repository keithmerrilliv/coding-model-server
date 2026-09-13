"""Pydantic models mirroring the coding_model_autonomous SQLite schema.

Used by both the server endpoints and the orchestrator daemon. Keep these
in lockstep with schema.sql — every column has a field, and the enum values
match the strings stored in TEXT columns.
"""
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ── Enums ─────────────────────────────────────────────────────────────────────

class SpecStatus(str, Enum):
    PENDING_PLAN = "pending_plan"        # spec submitted, planner not yet run
    NEEDS_CLARIFICATION = "needs_clarification"  # planner asked questions
    PLAN_REVIEW = "plan_review"          # YAML produced, awaiting human approval
    EXECUTING = "executing"              # plan approved, agents running
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED_ON_REVIEW = "blocked_on_review"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class ArtifactKind(str, Enum):
    SPEC_MD = "spec_md"
    SPEC_YAML = "spec_yaml"
    DESIGN = "design"
    CODE = "code"
    TEST_REPORT = "test_report"
    REVIEW_REPORT = "review_report"


class GateType(str, Enum):
    CLARIFICATION = "clarification"      # planner needs more info
    PLAN_APPROVAL = "plan_approval"      # YAML plan ready, approve to execute
    DESIGN_APPROVAL = "design_approval"  # architect output, approve to implement
    CODE_REVIEW = "code_review"          # implementer output, approve to test
    RELEASE_APPROVAL = "release_approval"  # tests pass, approve to ship


class GateStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class EventKind(str, Enum):
    SPEC_SUBMITTED = "spec_submitted"
    SPEC_STATUS_CHANGED = "spec_status_changed"
    TASK_CREATED = "task_created"
    TASK_STATUS_CHANGED = "task_status_changed"
    ARTIFACT_CREATED = "artifact_created"
    GATE_CREATED = "gate_created"
    GATE_RESPONDED = "gate_responded"
    PLANNER_RAN = "planner_ran"
    # AGENT_RAN covers two things, and a per-agent query must not pool them
    # (DEV-528). A real model call carries `agent` plus `duration_ms` and,
    # when the server reported it, token counts — build the payload with
    # executor.agent_event_fields(meta) so the spelling matches everywhere.
    # The rest are anomaly and routing records that piggyback on this kind
    # without any model having run — a widened manifest, a duplicate-path
    # warning, a free retry. Those set `model_call: False`, which is what
    # a cost or quality query filters on. Events predating the field have
    # neither marker; `duration_ms IS NOT NULL` is the fallback discriminator.
    AGENT_RAN = "agent_ran"              # architect/implementer/reviewer completed
    OUTPUT_TRUNCATED = "output_truncated"  # an agent response hit max_tokens (finish_reason=length)
    TEST_RAN = "test_ran"                # subprocess test execution completed
    DAEMON_TICK = "daemon_tick"          # heartbeat for liveness checks
    SUPERVISOR_DECISION = "supervisor_decision"  # meta-orchestrator transition
    # DEV-629: one record per failed attempt, written by outcome.dispose —
    # the class (transport, truncation, parse failure, ...), the outcome
    # (no_verdict / verdict / terminal) and what was done about it. This is
    # the queryable failure taxonomy DEV-529 asked for.
    FAILURE_CLASSIFIED = "failure_classified"
    # DEV-631/DEV-530: one record per dispatch, written BEFORE the call —
    # the levers this attempt pulls (agent, prompt, feedback, temperature,
    # environment), what changed since the previous attempt, why, and the
    # difficulty proxy (what failure preceded it, how many diagnostics).
    ATTEMPT_PLANNED = "attempt_planned"
    # DEV-669: one record per context fetch (DEV-632) — what the runner
    # served at base_ref for this spec, what it could not, and which role
    # triggered the fetch. Used to be an AGENT_RAN row with role=context.
    CONTEXT_ASSEMBLED = "context_assembled"


# ── Payload schemas for the taxonomy events (DEV-669) ────────────────────────
#
# Event payloads are JSON in a text column, so nothing in the store enforces
# a shape. These three kinds are the ones queries are written against, so
# their shapes are fixed here and pinned by tests/test_event_schemas.py:
# every key a writer emits must be listed (required or optional), and every
# required key must be present. Add a key here BEFORE emitting it.
EVENT_PAYLOAD_SCHEMAS: dict = {
    EventKind.FAILURE_CLASSIFIED: {
        "required": {
            "role": "the task role that failed (architect | implementer | reviewer | daemon)",
            "outcome": "no_verdict | verdict | terminal (outcome.Outcome)",
            "cls": "the failure class (outcome.FailureClass value)",
            "source": "model_call | parse | apply | build_check | tests | gate | runner | daemon",
            "detail": "first 600 chars of the failure text; its first line is the signature",
            "signature": "cls + first 140 chars of the first detail line — precise, for diagnostics",
            "coarse_key": "cls | phase | first path named — the identity invariance is judged on (DEV-631)",
            "retry": "task.retry_count when recorded (the attempt this failure belongs to)",
            "consecutive": "no-verdicts in a row on this attempt, 0 for verdicts",
            "cap": "the consecutive no-verdict cap for this class, null for shutdown",
            "disposition": "requeue | rotate | charge | synthesize | park | terminal | supervisor | handled",
            "rotate": "no-verdict only: the next dispatch should reach a different agent",
            "exc_type": "the exception type for exception-class failures, else empty",
            "phase": "free text naming where it happened (build_check, existing_fetch, ...)",
            "diagnostics": "sorted location-stripped attributed diagnostic messages, at most 40 (DEV-631)",
            "diagnostic_classes": "sorted closed-set classes over the diagnostics (DEV-529)",
            "cited_files": "files the diagnostics name, repository-relative where possible (DEV-529)",
            "symbols": "quoted identifiers the diagnostics name, minus builtin noise (DEV-529)",
        },
        "optional": {
            "agent": "the agent that produced the attempt (resolved from AGENT_RAN when not given)",
            "disposition_detail": "why this disposition, when it is not the default for the class",
            # Failure.extra — per-class particulars a writer attaches:
            "status": "HTTP status for http_refusal",
            "module": "the module the sandbox could not import (sandbox_provisioning)",
            "missing": "planned outputs the attempt did not produce (DEV-645)",
            "blocks": "unappliable edit blocks (DEV-581)",
            "warnings": "blocking compiler warnings (DEV-547)",
            "action": "the supervisor's action when it handled the failure",
            "gate_carries_notes": "the human gate already holds the notes the retry reads",
            "needed_tokens": "prompt_too_large: what the prompt needs (DEV-633)",
            "allowed_tokens": "prompt_too_large: what the largest window allows",
            "max_tokens": "prompt_too_large: the completion reserve that was budgeted",
        },
    },
    EventKind.ATTEMPT_PLANNED: {
        "required": {
            "role": "the role being dispatched",
            "retry": "task.retry_count for this dispatch",
            "agent": "the agent the dispatch goes to",
            "prompt_digest": "digest of everything prompt-shaping (design, clarifications, feedback)",
            "feedback_digest": "digest of the feedback alone, empty when there is none",
            "temperature": "sampling temperature of the call",
            "env_digest": "digest of the test_strategy the attempt is judged in",
            "assignment": "recommended | rotation | random | injected | fixed — how the agent was chosen (DEV-530)",
            "prior_cls": "class of the failure that caused this attempt, null on the first",
            "prior_coarse_key": "its coarse key",
            "prior_agent": "the agent that produced it",
            "prior_outcome": "its outcome",
            "diagnostics": "attributed diagnostic count in the feedback (difficulty proxy)",
            "feedback_chars": "length of the feedback (difficulty proxy)",
            "changed": "levers that differ from the previous attempt's plan",
            "identical_to": "retry index of an earlier attempt this one repeats on every lever, else null",
            "rationale": "the plan in words",
        },
        "optional": {},
    },
    EventKind.CONTEXT_ASSEMBLED: {
        "required": {
            "trigger": "the role whose need triggered the fetch (plan probe, architect, implementer, ...)",
            "repo": "the registered repository read from",
            "base_ref": "the ref the files were read at",
            "fetched_by": "the role recorded on the persisted context.json",
            "fetches": "how many runner fetches this spec has made",
            "editable": "paths served as editable (the declared modification set)",
            "protected": "paths served as read-only references",
            "omitted": "'path (section): reason' for every requested path not served",
            "unknown": "requested paths whose absence the runner could not confirm (DEV-630)",
            "editable_chars": "total chars of the editable section",
            "protected_chars": "total chars of the protected section",
        },
        "optional": {},
    },
}


def check_event_payload(kind: "EventKind", payload: dict) -> list:
    """Problems with *payload* against EVENT_PAYLOAD_SCHEMAS — missing
    required keys and keys the schema does not know. Empty means it fits."""
    schema = EVENT_PAYLOAD_SCHEMAS.get(kind)
    if schema is None:
        return []
    known = set(schema["required"]) | set(schema["optional"])
    problems = [f"missing required key {k!r}" for k in schema["required"] if k not in payload]
    problems += [f"undocumented key {k!r}" for k in payload if k not in known]
    return problems


# ── Records ──────────────────────────────────────────────────────────────────

class _Base(BaseModel):
    model_config = ConfigDict(use_enum_values=False, from_attributes=True)


class Spec(_Base):
    id: str
    title: str
    source_md_path: str
    normalized_yaml: Optional[str] = None
    status: SpecStatus
    jira_epic_key: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class Task(_Base):
    id: str
    spec_id: str
    parent_id: Optional[str] = None
    agent: str
    role: str
    title: str
    description: Optional[str] = None
    status: TaskStatus
    execution_target: Optional[str] = None
    jira_issue_key: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    retry_count: int = 0
    created_at: datetime
    updated_at: datetime


class Artifact(_Base):
    id: str
    spec_id: str
    task_id: Optional[str] = None
    kind: ArtifactKind
    path: str
    sha256: Optional[str] = None
    role: Optional[str] = None        # DEV-642: the role that wrote it
    created_at: datetime


class ReviewGate(_Base):
    id: str
    spec_id: str
    task_id: Optional[str] = None
    gate_type: GateType
    prompt_md: str
    status: GateStatus
    reviewer_decision: Optional[str] = None
    reviewer_notes: Optional[str] = None
    jira_issue_key: Optional[str] = None
    created_at: datetime
    responded_at: Optional[datetime] = None


class Event(_Base):
    id: Optional[int] = None              # autoincrement, NULL until insert
    spec_id: Optional[str] = None
    task_id: Optional[str] = None
    gate_id: Optional[str] = None
    kind: EventKind
    payload_json: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def payload(self) -> Optional[dict]:
        """payload_json parsed to a dict; None when absent or unparseable.

        The read-side twin of record_event(payload=...). The daemon's requeue
        counters had read `e.payload` since DEV-538 with no accessor behind
        it — the second unreachable-runner requeue on one spec would have
        died on AttributeError mid-tick (DEV-622).
        """
        if not self.payload_json:
            return None
        try:
            data = json.loads(self.payload_json)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None


# ── Request/response DTOs (used by the public HTTP API) ──────────────────────

class SubmitSpecRequest(BaseModel):
    """POST /v1/autonomous/specs body."""
    title: Optional[str] = None
    markdown: str
    # If omitted, the title is extracted from the first H1 in the markdown,
    # or falls back to "untitled spec <id>".


class SubmitSpecResponse(BaseModel):
    spec_id: str
    title: str
    status: SpecStatus


class GateRespondRequest(BaseModel):
    """POST /v1/autonomous/gates/{id}/respond body."""
    decision: str  # 'approved' or 'rejected'
    notes: Optional[str] = None


class SpecSummary(BaseModel):
    """Compact view of a spec for list/status endpoints."""
    spec: Spec
    open_gates: list[ReviewGate]
    task_count: int
    recent_events: list[Event]
    # All tasks for the spec (each retry of each role is a separate Task row).
    # Used by the dashboard to render the execution DAG. Older clients that
    # ignore this field continue to work; the field is always present.
    tasks: list[Task] = []
    # All gates including closed ones. The DAG renderer needs gate decisions
    # (approved / rejected) to draw retry-loop edges. open_gates above is a
    # subset. When tasks is empty (legacy callers), this is also empty.
    all_gates: list[ReviewGate] = []
