"""Service metadata routes: root, health, model list."""
import time
from datetime import datetime

from fastapi import APIRouter

from coding_model_server.config import Config
from coding_model_server.runtime import get_autonomous_db, llama_server_manager
from coding_model_server.schemas import HealthResponse, ModelListResponse

router = APIRouter()


@router.get("/")
async def root():
    """Root endpoint with service information"""
    return {
        "service": "Coding Model Multi-Agent Server",
        "version": "2.0-fastapi",
        "endpoints": {
            "models": "/v1/models",
            "chat": "/v1/chat/completions",
            "memory": "/v1/memory",
            "search": "/v1/tools/search",
            "health": "/health",
            "docs": "/docs"
        }
    }


@router.get("/health", response_model=HealthResponse)
async def health():
    """Health check endpoint"""
    # DEV-430: surface work blocked on a human. Best-effort — a health check
    # must never fail because the autonomous store is unavailable, and None
    # reads as "unknown" rather than "none waiting".
    try:
        open_gates = len(get_autonomous_db().list_open_gates())
    except Exception:
        open_gates = None
    return {
        "status": "healthy",
        "model_loaded": llama_server_manager.is_running(),
        "agents": list(Config.AGENTS.keys()),
        "timestamp": datetime.now().isoformat(),
        "open_review_gates": open_gates,
    }


@router.get("/v1/models", response_model=ModelListResponse)
async def list_models():
    """List available agent models (OpenAI-compatible)"""
    models = []
    for agent_id, agent_config in Config.AGENTS.items():
        models.append({
            "id": agent_id,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "qwen-multi-agent",
            "description": agent_config['description']
        })

    return {"object": "list", "data": models}
