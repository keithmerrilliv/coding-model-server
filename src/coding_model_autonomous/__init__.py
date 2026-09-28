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
  agents     settings · prompts · _http (call_agent) · parsers · messages ·
             normalize · executor (a re-exporting façade) · planner ·
             architect_tools · plan_paths · apply_edits ·
             thinking (strip reasoning from a response)
  languages  languages (detection, the pack interface) · languages.swift ·
             .python · .javascript · .c_family
  guards     design_testability · citations (cited diagnostics in repair)
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
