"""coding_model_autonomous — the autonomous pipeline.

The FastAPI server (which exposes /v1/autonomous) and the orchestrator daemon
(which drives specs through the pipeline) both import from here. This package
imports nothing from coding_model_server: the server depends on the pipeline,
never the reverse (DEV-837).

Submodules, by role:
  store      models (pydantic types) · db (SQLite store)
  kernel     workspace (artifact ledger) · outcome (failure classification and
             disposition) · context (one read, one prompt budget) ·
             retry_policy (what a retry keeps, and who retries)
  agents     planner · executor (prompts, parsers, call_agent) ·
             architect_tools · plan_paths · apply_edits · supervisor · _http ·
             thinking (strip reasoning from a response)
  guards     design_testability · swift_prechecks · swift_rules
  testing    test_runner (sandboxed dispatch, Mac runner transport) ·
             seccomp_filter · gate_output · delivery
  jira       jira_client · jira_sync
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
