"""coding_model_autonomous — autonomous task orchestration shared between server and daemon.

This package is the source of truth for the SQLite-backed task store used by
the autonomous service mode. Both the FastAPI server (which exposes the
public /v1/autonomous endpoints) and the orchestrator daemon (which runs
agents and processes review gates) import from here.

Submodules: models (pydantic types) · db (SQLite store) · planner (spec → YAML)
· executor (agent calls + parsing + sandboxed test execution) · supervisor
(retry/replan decisions) · jira_client / jira_sync (bidirectional Atlassian
mirror).
"""
from coding_model_autonomous.models import (
    Spec,
    SpecStatus,
    Task,
    TaskStatus,
    Artifact,
    ArtifactKind,
    ReviewGate,
    GateType,
    GateStatus,
    Event,
    EventKind,
)
from coding_model_autonomous.db import (
    Database,
    GateAlreadyDecidedError,
    DEFAULT_DB_PATH,
    DEFAULT_WORKSPACE_ROOT,
)

__all__ = [
    "Spec",
    "SpecStatus",
    "Task",
    "TaskStatus",
    "Artifact",
    "ArtifactKind",
    "ReviewGate",
    "GateType",
    "GateStatus",
    "Event",
    "EventKind",
    "Database",
    "GateAlreadyDecidedError",
    "DEFAULT_DB_PATH",
    "DEFAULT_WORKSPACE_ROOT",
]
