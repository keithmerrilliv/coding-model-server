"""Autonomous-mode HTTP routes: spec ingest, task store, review gates.

State lives in tasks_db/tasks.sqlite via qwen_autonomous.Database. The
orchestrator daemon (qwen-orchestrator.service) reads/writes the same database
to drive planning and gate processing — these endpoints are the public HTTP face
of that store. The Database singleton is obtained from runtime.get_autonomous_db.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from qwen_server.runtime import get_autonomous_db, verify_admin_key
from qwen_autonomous.models import (
    GateRespondRequest,
    SpecSummary,
    SubmitSpecRequest,
    SubmitSpecResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _extract_title_from_md(markdown: str) -> str:
    """Pull the first H1 ('# Title') out of a markdown document."""
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or "untitled spec"
    return "untitled spec"


@router.post("/v1/autonomous/specs", dependencies=[Depends(verify_admin_key)])
def submit_spec(request: SubmitSpecRequest) -> SubmitSpecResponse:
    """Accept a markdown spec, persist it on disk, and create a Spec record.

    The orchestrator daemon polls for new specs and runs the planner agent
    against them. This endpoint returns immediately — execution is async.
    """
    if not request.markdown.strip():
        raise HTTPException(status_code=400, detail="markdown is empty")

    MAX_SPEC_BYTES = 256 * 1024  # 256 KB cap on a single spec
    if len(request.markdown.encode("utf-8")) > MAX_SPEC_BYTES:
        raise HTTPException(status_code=413, detail="spec exceeds 256 KB")

    title = (request.title.strip() if request.title
             else _extract_title_from_md(request.markdown))

    db = get_autonomous_db()
    spec = db.create_spec(title=title, source_md_path="spec.md")

    # Write the spec markdown into the spec's workspace directory. The
    # source_md_path stored on the row is relative to that directory so
    # other components don't need to know the workspace root.
    spec_dir = db.spec_dir(spec.id)
    (spec_dir / "spec.md").write_text(request.markdown)

    logger.info("Autonomous spec submitted: %s (%s, %d bytes)",
                spec.id, title, len(request.markdown))
    return SubmitSpecResponse(spec_id=spec.id, title=spec.title, status=spec.status)


@router.get("/v1/autonomous/specs", dependencies=[Depends(verify_admin_key)])
def list_autonomous_specs(limit: int = 50) -> list[dict]:
    """List recent specs (newest first), no events or gates."""
    db = get_autonomous_db()
    specs = db.list_specs(limit=limit)
    return [s.model_dump(mode="json") for s in specs]


@router.get("/v1/autonomous/specs/{spec_id}", dependencies=[Depends(verify_admin_key)])
def get_autonomous_spec(spec_id: str) -> SpecSummary:
    """Full status for a single spec including open gates and recent events."""
    db = get_autonomous_db()
    spec = db.get_spec(spec_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"spec {spec_id} not found")
    return SpecSummary(
        spec=spec,
        open_gates=db.list_open_gates(spec_id=spec_id),
        task_count=db.count_tasks_for_spec(spec_id),
        recent_events=db.list_recent_events(spec_id=spec_id, limit=20),
        tasks=db.list_tasks_for_spec(spec_id),
        all_gates=db.list_gates_for_spec(spec_id),
    )


@router.get("/v1/autonomous/gates", dependencies=[Depends(verify_admin_key)])
def list_open_gates(spec_id: Optional[str] = None) -> list[dict]:
    """List all open review gates, optionally filtered by spec."""
    db = get_autonomous_db()
    gates = db.list_open_gates(spec_id=spec_id)
    return [g.model_dump(mode="json") for g in gates]


@router.get("/v1/autonomous/gates/{gate_id}", dependencies=[Depends(verify_admin_key)])
def get_gate(gate_id: str) -> dict:
    db = get_autonomous_db()
    gate = db.get_gate(gate_id)
    if gate is None:
        raise HTTPException(status_code=404, detail=f"gate {gate_id} not found")
    return gate.model_dump(mode="json")


@router.post("/v1/autonomous/gates/{gate_id}/respond",
             dependencies=[Depends(verify_admin_key)])
def respond_to_gate(gate_id: str, request: GateRespondRequest) -> dict:
    """Approve or reject a review gate. The orchestrator daemon picks up the
    state change on its next tick and proceeds (or rolls back).
    """
    if request.decision not in ("approved", "rejected"):
        raise HTTPException(
            status_code=400,
            detail="decision must be 'approved' or 'rejected'",
        )
    db = get_autonomous_db()
    if db.get_gate(gate_id) is None:
        raise HTTPException(status_code=404, detail=f"gate {gate_id} not found")
    gate = db.respond_to_gate(gate_id, request.decision, notes=request.notes)
    logger.info("Gate %s %s by reviewer", gate_id, request.decision)
    return gate.model_dump(mode="json")


@router.get("/v1/autonomous/specs/{spec_id}/events",
            dependencies=[Depends(verify_admin_key)])
def get_spec_events(spec_id: str, limit: int = 100) -> list[dict]:
    db = get_autonomous_db()
    if db.get_spec(spec_id) is None:
        raise HTTPException(status_code=404, detail=f"spec {spec_id} not found")
    events = db.list_recent_events(spec_id=spec_id, limit=limit)
    return [e.model_dump(mode="json") for e in events]
