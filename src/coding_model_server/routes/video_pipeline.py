"""Video (GoPro consolidation) pipeline status for the dashboard (DEV-401).

The pipeline is a separate program (~/Dev/video-pipeline) that writes one
atomic JSON snapshot; this route only reads it. Coupling stops at that file's
shape, so the pipeline can run, crash or be absent without touching the
inference server.
"""
import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends

from coding_model_server.runtime import verify_admin_key

router = APIRouter()


def state_path() -> Path:
    base = os.getenv("VIDEO_PIPELINE_STATE_DIR") or Path.home() / ".local/state/video-pipeline"
    return Path(base) / "state.json"


def _alive(pid: object) -> bool:
    """Whether `pid` is still a running video-pipeline process.

    The snapshot is only rewritten while a run progresses, so a run killed
    with SIGKILL leaves `finished_at: null` forever. Checking the process,
    not the file, is what stops the dashboard from showing it as running.
    """
    if not isinstance(pid, int):
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    argv = [a.decode(errors="replace") for a in cmdline.split(b"\0") if a]
    # `python -m video_pipeline ...` or the installed `video-pipeline` script.
    # A substring match on the whole cmdline would also accept an editor or
    # grep that merely mentions the name.
    if any(a == "-m" and b == "video_pipeline" for a, b in zip(argv, argv[1:])):
        return True
    return any(Path(a).name == "video-pipeline" for a in argv[:2])


@router.get("/v1/admin/video_pipeline", dependencies=[Depends(verify_admin_key)])
def admin_video_pipeline() -> dict:
    """Latest pipeline run, or ``{"available": false}`` if none has ever run.

    ``active`` is derived here from the process table. ``stale_seconds`` is the
    age of the snapshot, so the panel can say "last updated 3 days ago" instead
    of implying a finished run is live.
    """
    path = state_path()
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return {"available": False, "reason": f"no state file at {path}"}
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "reason": f"state file unreadable: {exc}"}
    doc["available"] = True
    doc["active"] = doc.get("finished_at") is None and _alive(doc.get("pid"))
    doc["stale_seconds"] = max(0.0, time.time() - float(doc.get("updated_at", 0)))
    return doc
