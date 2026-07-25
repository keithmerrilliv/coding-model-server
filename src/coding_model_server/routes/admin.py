"""Admin/observability routes for the dashboard: metrics, GPU, active model."""
from fastapi import APIRouter, Depends

from coding_model_server.config import Config
from coding_model_server.metrics import gpu_sampler, request_metrics
from coding_model_server.runtime import chat_admission, llama_server_manager, verify_admin_key

router = APIRouter()


@router.get("/v1/admin/metrics", dependencies=[Depends(verify_admin_key)])
def admin_metrics(window_seconds: int = 60) -> dict:
    """Per-endpoint request KPIs over the last `window_seconds` seconds.

    Used by the dashboard's live charts. /v1/chat/completions is split by
    `agent_id` so each agent gets its own KPI row.
    """
    window = max(5, min(window_seconds, 600))
    return request_metrics.snapshot(window_seconds=window)


@router.get("/v1/admin/gpu_stats", dependencies=[Depends(verify_admin_key)])
def admin_gpu_stats(since: "str | None" = None, limit: int = 60) -> dict:
    """Rolling buffer of nvidia-smi samples for the dashboard's GPU panel.

    ``since`` makes polls incremental (only newer samples); ``limit``
    defaults to the 60 the panel actually renders (DEV-159).
    """
    return gpu_sampler.snapshot(since=since, limit=limit)


@router.get("/v1/admin/active_model", dependencies=[Depends(verify_admin_key)])
def admin_active_model() -> dict:
    """Snapshot of the currently loaded llama-server child + its config.

    Combines runtime state from LlamaServerManager (which model file is
    loaded, in-flight count, idle clock) with the agent_config description
    pulled from Config.AGENTS so the dashboard can render a single panel
    without having to cross-reference. The agent_id is None when nothing
    has been served yet (cold start before the first /v1/chat/completions).
    """
    snap = llama_server_manager.snapshot()
    agent_id = Config.resolve_agent(snap.get('agent_id'))
    if agent_id and agent_id in Config.AGENTS:
        agent_cfg = Config.AGENTS[agent_id]
        snap['agent_description'] = agent_cfg.get('description')
        snap['agent_executor'] = bool(agent_cfg.get('executor', False))
    else:
        snap['agent_description'] = None
        snap['agent_executor'] = None
    snap['available_agents'] = list(Config.AGENTS.keys())
    snap['chat_in_flight'] = chat_admission.in_flight
    snap['chat_max_inflight'] = chat_admission.max_inflight
    return snap
