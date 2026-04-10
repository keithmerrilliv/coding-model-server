"""qwen_autonomous — autonomous task orchestration shared between server and daemon.

This package is the source of truth for the SQLite-backed task store used by
the autonomous service mode. Both the FastAPI server (which exposes the
public /v1/autonomous endpoints) and the orchestrator daemon (which runs
agents and processes review gates) import from here.

Phase 1a scope: schema, models, db helpers, event log. No agent execution.
"""
from qwen_autonomous.models import (
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
from qwen_autonomous.db import (
    Database,
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
    "DEFAULT_DB_PATH",
    "DEFAULT_WORKSPACE_ROOT",
]
