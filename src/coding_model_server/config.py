"""Server configuration: the settings the inference server reads, and the
``Config`` interface over the agent roster (roster.py) and the interactive
agents' prompt texts (agent_prompts.py).

Imports are kept narrow so this module can be loaded without pulling in
FastAPI/HTTP machinery.
"""
import os
from typing import List

from . import agent_prompts as _agent_prompts
from . import roster as _roster
# Re-exported: tests build agent configs with these.
from .roster import _MODELS_ROOT, _create_agent_config, _create_model_config  # noqa: F401


class Config:
    PORT = int(os.getenv('PORT', 5000))
    HOST = os.getenv('HOST', '127.0.0.1')
    ADMIN_API_KEY = os.getenv('ADMIN_API_KEY', '')
    INGEST_ALLOWED_DIR = os.getenv('INGEST_ALLOWED_DIR', '')
    # Per-file byte cap for ingestion. Documented since forever but read by
    # nothing (DEV-164), so an operator who set it got no protection: the
    # only limit was the 100MB base64 upload cap, and PDF ingest had none.
    # 0 disables the cap. Default 100MB matches the upload ceiling rather
    # than the old .env.example's 100000 (100KB), which would have started
    # rejecting PDFs that ingest fine today.
    INGEST_MAX_FILE_SIZE = int(os.getenv('INGEST_MAX_FILE_SIZE', 100 * 1024 * 1024))
    
    # llama-server thread split. 24 = physical core count (8 P-cores + 16
    # E-cores); hyperthreads hurt decode. Prefill (batch) benefits from
    # hyperthreads, use all 32 threads.
    DEFAULT_N_THREADS = int(os.getenv('MODEL_N_THREADS', 24))
    DEFAULT_N_THREADS_BATCH = int(os.getenv('MODEL_N_THREADS_BATCH', 32))

    # Upper bound on a single llama-server inference call. Must be >= the
    # longest autonomous role timeout (coding_model_autonomous/settings.py
    # defaults ARCHITECT/REVIEWER to 2700s) so that the inner request doesn't fail
    # before the outer orchestrator's deadline. Override via env if you need
    # patience for slower hardware or longer max_tokens.
    LLAMA_SERVER_REQUEST_TIMEOUT = float(os.getenv('LLAMA_SERVER_REQUEST_TIMEOUT', 2700))

    # The roster (roster.py) and the prompt texts (agent_prompts.py) are
    # defined in their own modules and bound here, because Config is the
    # one interface callers read and tests patch.
    BASE_TOOLS = _agent_prompts.BASE_TOOLS
    ALL_TOOLS = _agent_prompts.ALL_TOOLS
    TOOL_REFERENCE = _agent_prompts.TOOL_REFERENCE
    GIT_TOOL_REFERENCE = _agent_prompts.GIT_TOOL_REFERENCE
    _BUDGET_HEADER = _agent_prompts._BUDGET_HEADER
    _BUDGET_GUIDELINES = _agent_prompts._BUDGET_GUIDELINES
    TOKEN_BUDGET_GUIDANCE_CORE = _agent_prompts.TOKEN_BUDGET_GUIDANCE_CORE
    TOKEN_BUDGET_GUIDANCE = _agent_prompts.TOKEN_BUDGET_GUIDANCE
    MACOS_TOOLKIT = _agent_prompts.MACOS_TOOLKIT
    EXECUTOR_PROMPT = _agent_prompts.EXECUTOR_PROMPT
    FEW_SHOT = _agent_prompts.FEW_SHOT
    _IMPLEMENTER_SYSTEM_PROMPT = _agent_prompts._IMPLEMENTER_SYSTEM_PROMPT
    _ARCHITECT_SYSTEM_PROMPT = _agent_prompts._ARCHITECT_SYSTEM_PROMPT
    _REVIEWER_SYSTEM_PROMPT = _agent_prompts._REVIEWER_SYSTEM_PROMPT
    _UNICODE_GUARD = _agent_prompts._UNICODE_GUARD
    _MOE_30B_FAST = _roster._MOE_30B_FAST
    _MOE_80B_Q8 = _roster._MOE_80B_Q8
    _MOE_230B = _roster._MOE_230B
    _MOE_122B = _roster._MOE_122B
    _MOE_35B = _roster._MOE_35B
    _DENSE_27B = _roster._DENSE_27B
    _MUSE_GLIMMER_30B = _roster._MUSE_GLIMMER_30B
    AGENTS = _roster.AGENTS
    AGENT_ALIASES = _roster.AGENT_ALIASES

    @classmethod
    def resolve_agent(cls, name):
        """Map a possibly-legacy agent name to its canonical key.

        Returns the canonical name when `name` is a known alias, otherwise
        returns `name` unchanged (callers still do their own existence check).
        """
        return cls.AGENT_ALIASES.get(name, name)

    @classmethod
    def validate(cls) -> List[str]:
        """Validate configuration before starting server"""
        errors = []
        if not 1 <= cls.PORT <= 65535:
            errors.append(f"PORT must be between 1 and 65535, got: {cls.PORT}")
            
        for agent, config in cls.AGENTS.items():
            path = config['model_config']['path']
            if not os.path.exists(path):
                errors.append(f"Model for {agent} not found: {path}")
                
        return errors
