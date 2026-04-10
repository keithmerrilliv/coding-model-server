"""Pydantic models mirroring the qwen_autonomous SQLite schema.

Used by both the server endpoints and the orchestrator daemon. Keep these
in lockstep with schema.sql — every column has a field, and the enum values
match the strings stored in TEXT columns.
"""
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
    DAEMON_TICK = "daemon_tick"          # heartbeat for liveness checks


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
