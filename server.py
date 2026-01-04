#!/usr/bin/env python3
"""
Qwen Multi-Agent Server (FastAPI)
Provides OpenAI-compatible API for remote command execution with multi-agent support
"""
import os
import sys
import json
import time
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any, Literal, Iterator
from threading import Lock

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================================
# Pydantic Models for Request/Response Validation
# ============================================================================

class ChatMessage(BaseModel):
    """A single message in the chat conversation"""
    role: Literal["system", "user", "assistant"]
    content: str

    @field_validator('content')
    @classmethod
    def content_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError('content cannot be empty')
        return v


class ChatCompletionRequest(BaseModel):
    """Request body for chat completions endpoint"""
    model: str = "implementer"
    messages: List[ChatMessage]
    stream: bool = False
    max_tokens: int = Field(default=2048, ge=1, le=32768)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)

    @field_validator('messages')
    @classmethod
    def messages_not_empty(cls, v: List[ChatMessage]) -> List[ChatMessage]:
        if not v:
            raise ValueError('messages array cannot be empty')
        return v


class ChatMessageResponse(BaseModel):
    """Response message format"""
    role: str
    content: str


class ChatChoice(BaseModel):
    """A single choice in chat completion response"""
    index: int
    message: ChatMessageResponse
    finish_reason: str


class UsageStats(BaseModel):
    """Token usage statistics"""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    """Full chat completion response"""
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatChoice]
    usage: UsageStats


class ModelInfo(BaseModel):
    """Model information for list endpoint"""
    id: str
    object: str = "model"
    created: int
    owned_by: str
    description: str


class ModelListResponse(BaseModel):
    """Response for list models endpoint"""
    object: str = "list"
    data: List[ModelInfo]


class HealthResponse(BaseModel):
    """Health check response"""
    status: str
    model_loaded: bool
    agents: List[str]
    timestamp: str


class ErrorDetail(BaseModel):
    """Error response detail"""
    message: str
    type: str
    code: int


class ErrorResponse(BaseModel):
    """Error response wrapper"""
    error: ErrorDetail


# ============================================================================
# Configuration
# ============================================================================

class Config:
    PORT = int(os.getenv('PORT', 5000))
    HOST = os.getenv('HOST', '0.0.0.0')
    MODEL_PATH = os.path.expanduser(os.getenv('MODEL_PATH', '')) if os.getenv('MODEL_PATH') else None
    MODEL_CONTEXT_SIZE = int(os.getenv('MODEL_CONTEXT_SIZE', 32768))
    MODEL_GPU_LAYERS = int(os.getenv('MODEL_GPU_LAYERS', 99))
    MODEL_N_THREADS = int(os.getenv('MODEL_N_THREADS', 24))
    MODEL_N_BATCH = int(os.getenv('MODEL_N_BATCH', 2048))
    MODEL_FLASH_ATTENTION = os.getenv('MODEL_FLASH_ATTENTION', 'true').lower() == 'true'
    MODEL_USE_MMAP = os.getenv('MODEL_USE_MMAP', 'true').lower() == 'true'
    MODEL_USE_MLOCK = os.getenv('MODEL_USE_MLOCK', 'true').lower() == 'true'
    MODEL_N_CTX_BATCH = int(os.getenv('MODEL_N_CTX_BATCH', 2048))

    @classmethod
    def validate(cls) -> List[str]:
        """Validate configuration before starting server"""
        errors = []

        if not cls.MODEL_PATH:
            errors.append("MODEL_PATH is not set. Please set it in .env file.")
        elif not os.path.exists(cls.MODEL_PATH):
            errors.append(f"Model file not found: {cls.MODEL_PATH}")
        elif not os.path.isfile(cls.MODEL_PATH):
            errors.append(f"MODEL_PATH is not a file: {cls.MODEL_PATH}")
        elif not os.access(cls.MODEL_PATH, os.R_OK):
            errors.append(f"Model file is not readable: {cls.MODEL_PATH}")
        else:
            file_size = os.path.getsize(cls.MODEL_PATH)
            if file_size < 1024 * 1024:
                errors.append(f"Model file seems too small ({file_size} bytes): {cls.MODEL_PATH}")

        if not 1 <= cls.PORT <= 65535:
            errors.append(f"PORT must be between 1 and 65535, got: {cls.PORT}")

        if cls.MODEL_CONTEXT_SIZE < 512:
            errors.append(f"MODEL_CONTEXT_SIZE seems too small: {cls.MODEL_CONTEXT_SIZE}")

        if cls.MODEL_GPU_LAYERS < 0:
            errors.append(f"MODEL_GPU_LAYERS cannot be negative: {cls.MODEL_GPU_LAYERS}")

        if cls.MODEL_N_THREADS < 0:
            errors.append(f"MODEL_N_THREADS cannot be negative: {cls.MODEL_N_THREADS}")

        if cls.MODEL_N_BATCH < 1:
            errors.append(f"MODEL_N_BATCH must be at least 1: {cls.MODEL_N_BATCH}")

        if cls.MODEL_N_CTX_BATCH < 1:
            errors.append(f"MODEL_N_CTX_BATCH must be at least 1: {cls.MODEL_N_CTX_BATCH}")

        return errors

    REMOTE_EXEC_INSTRUCTION = """
# REMOTE CLIENT EXECUTION PROTOCOL
You are running on a remote Linux server, but the user is on a macOS client.
To run commands on the client, you MUST use this specific protocol:

1. SYNC EXECUTION (for quick commands < 30s like 'ls', 'mkdir'):
   <code>
   final_answer("<<<REMOTE_EXEC>>>\nCOMMAND_TO_RUN\n<<<REMOTE_EXEC>>>")
   </code>

2. ASYNC EXECUTION (for long tasks like builds, downloads):
   <code>
   final_answer("<<<REMOTE_EXEC_ASYNC>>>\nCOMMAND_TO_RUN\n<<<REMOTE_EXEC_ASYNC>>>")
   </code>
   Returns a Job ID immediately.

3. CHECK STATUS (monitor async jobs):
   <code>
   final_answer("<<<REMOTE_CHECK_STATUS>>>\nJOB_ID\n<<<REMOTE_CHECK_STATUS>>>")
   </code>

4. GET OUTPUT (when job is completed):
   <code>
   final_answer("<<<REMOTE_GET_OUTPUT>>>\nJOB_ID\n<<<REMOTE_GET_OUTPUT>>>")
   </code>

# WORKFLOW FOR BUILDS/LONG TASKS
1. Start with REMOTE_EXEC_ASYNC.
2. Get Job ID.
3. Poll with REMOTE_CHECK_STATUS every few seconds.
4. When status is 'completed' or 'failed', use REMOTE_GET_OUTPUT to see results.

# CRITICAL RULES
- ALWAYS wrap your code actions in <code>...</code> blocks.
- NEVER try to use os.system() or subprocess locally for client tasks.
"""

    AGENTS = {
        'implementer': {
            'description': 'Code implementation agent',
            'system_prompt': f'You are an expert software engineer. Provide clear, working code implementations.\n{REMOTE_EXEC_INSTRUCTION}'
        },
        'architect': {
            'description': 'System architecture agent',
            'system_prompt': f'You are a system architect. Design scalable, maintainable solutions.\n{REMOTE_EXEC_INSTRUCTION}'
        },
        'reviewer': {
            'description': 'Code review agent',
            'system_prompt': f'You are a code reviewer. Identify issues and suggest improvements.\n{REMOTE_EXEC_INSTRUCTION}'
        },
        'debugger': {
            'description': 'Debugging agent',
            'system_prompt': f'You are a debugging expert. Analyze errors and suggest fixes.\n{REMOTE_EXEC_INSTRUCTION}'
        }
    }


# ============================================================================
# Model Manager
# ============================================================================

class ModelManager:
    def __init__(self):
        self.model = None
        self.lock = Lock()
        self.inference_lock = Lock()

    def get_model(self):
        """Get or load the model"""
        with self.lock:
            if self.model is None:
                if not os.path.exists(Config.MODEL_PATH):
                    error_msg = f"Model file not found: {Config.MODEL_PATH}"
                    logger.error(error_msg)
                    raise FileNotFoundError(error_msg)

                logger.info("Loading model from: %s", Config.MODEL_PATH)
                logger.info("Performance settings - threads: %d, batch: %d, flash attention: %s, mmap: %s, mlock: %s, ctx_batch: %d, gpu_layers: %d",
                           Config.MODEL_N_THREADS, Config.MODEL_N_BATCH, Config.MODEL_FLASH_ATTENTION,
                           Config.MODEL_USE_MMAP, Config.MODEL_USE_MLOCK, Config.MODEL_N_CTX_BATCH, Config.MODEL_GPU_LAYERS)

                try:
                    from llama_cpp import Llama
                    self.model = Llama(
                        model_path=Config.MODEL_PATH,
                        n_ctx=Config.MODEL_CONTEXT_SIZE,
                        n_gpu_layers=Config.MODEL_GPU_LAYERS,
                        n_threads=Config.MODEL_N_THREADS,
                        n_batch=Config.MODEL_N_BATCH,
                        flash_attn=Config.MODEL_FLASH_ATTENTION,
                        use_mmap=Config.MODEL_USE_MMAP,
                        use_mlock=Config.MODEL_USE_MLOCK,
                        n_ctx_batch=Config.MODEL_N_CTX_BATCH,
                        verbose=True
                    )
                    logger.info("Model loaded successfully with performance optimizations")
                except Exception as e:
                    logger.error("Failed to load model: %s", e)
                    raise

            return self.model

    def is_loaded(self) -> bool:
        """Check if model is loaded"""
        return self.model is not None


# ============================================================================
# ChatML Format Helpers
# ============================================================================

CHATML_START = "<|im_start|>"
CHATML_END = "<|im_end|>"


def format_chatml_message(role: str, content: str) -> str:
    """Format a single message in ChatML format"""
    return f"{CHATML_START}{role}\n{content}{CHATML_END}\n"


def build_chatml_prompt(messages: List[ChatMessage], system_prompt: str) -> str:
    """Build a prompt using ChatML format"""
    parts = []

    if system_prompt:
        parts.append(format_chatml_message("system", system_prompt))

    for msg in messages:
        parts.append(format_chatml_message(msg.role, msg.content))

    parts.append(f"{CHATML_START}assistant\n")

    return "".join(parts)


def get_model_params(max_tokens: int, temperature: float, stream: bool = False) -> Dict[str, Any]:
    """Get common model inference parameters"""
    return {
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stop": [CHATML_END, CHATML_START],
        "stream": stream,
        "echo": False
    }


# ============================================================================
# Response Builders
# ============================================================================

def build_completion_response(model_id: str, text: str, usage: Dict[str, int]) -> Dict[str, Any]:
    """Build OpenAI-compatible completion response"""
    return {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": text
            },
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": usage['prompt_tokens'],
            "completion_tokens": usage['completion_tokens'],
            "total_tokens": usage['total_tokens']
        }
    }


def build_stream_chunk(completion_id: str, model_id: str, content: Optional[str] = None, finish: bool = False) -> Dict[str, Any]:
    """Build OpenAI-compatible streaming chunk"""
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model_id,
        "choices": [{
            "index": 0,
            "delta": {"content": content} if content else {},
            "finish_reason": "stop" if finish else None
        }]
    }


# ============================================================================
# FastAPI Application
# ============================================================================

app = FastAPI(
    title="Qwen Multi-Agent Server",
    description="OpenAI-compatible API for Qwen LLM with multi-agent support",
    version="2.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

model_manager = ModelManager()


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Custom HTTP exception handler for OpenAI-compatible error format"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.detail,
                "type": "invalid_request_error" if exc.status_code < 500 else "server_error",
                "code": exc.status_code
            }
        }
    )


@app.get("/")
async def root():
    """Root endpoint with service information"""
    return {
        "service": "Qwen Multi-Agent Server",
        "version": "2.0-fastapi",
        "endpoints": {
            "models": "/v1/models",
            "chat": "/v1/chat/completions",
            "health": "/health",
            "docs": "/docs"
        }
    }


@app.get("/health", response_model=HealthResponse)
async def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "model_loaded": model_manager.is_loaded(),
        "agents": list(Config.AGENTS.keys()),
        "timestamp": datetime.now().isoformat()
    }


@app.get("/v1/models", response_model=ModelListResponse)
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


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    """Handle chat completion requests (OpenAI-compatible)"""
    try:
        # Validate model exists
        if request.model not in Config.AGENTS:
            raise HTTPException(
                status_code=404,
                detail=f"Model '{request.model}' not found. Available models: {', '.join(Config.AGENTS.keys())}"
            )

        # Validate max_tokens against context size
        if request.max_tokens > Config.MODEL_CONTEXT_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"'max_tokens' must be between 1 and {Config.MODEL_CONTEXT_SIZE}"
            )

        agent_config = Config.AGENTS[request.model]
        prompt = build_chatml_prompt(request.messages, agent_config['system_prompt'])

        if request.stream:
            return StreamingResponse(
                stream_completion(prompt, request.model, request.max_tokens, request.temperature),
                media_type="text/event-stream"
            )
        else:
            return sync_completion(prompt, request.model, request.max_tokens, request.temperature)

    except HTTPException:
        raise
    except FileNotFoundError as e:
        logger.error("Model file error: %s", e)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error("Error in chat_completions: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def sync_completion(prompt: str, model_id: str, max_tokens: int, temperature: float) -> Dict[str, Any]:
    """Generate synchronous completion"""
    model = model_manager.get_model()
    params = get_model_params(max_tokens, temperature, stream=False)

    with model_manager.inference_lock:
        response = model(prompt, **params)
    text = response['choices'][0]['text'].strip()

    return build_completion_response(model_id, text, response['usage'])


def stream_completion(prompt: str, model_id: str, max_tokens: int, temperature: float) -> Iterator[str]:
    """Generate streaming completion"""
    try:
        model = model_manager.get_model()
        params = get_model_params(max_tokens, temperature, stream=True)
        completion_id = f"chatcmpl-{int(time.time())}"

        with model_manager.inference_lock:
            for output in model(prompt, **params):
                if 'choices' in output and len(output['choices']) > 0:
                    token = output['choices'][0].get('text', '')
                    if token:
                        chunk = build_stream_chunk(completion_id, model_id, content=token)
                        yield f"data: {json.dumps(chunk)}\n\n"

        final_chunk = build_stream_chunk(completion_id, model_id, finish=True)
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield "data: [DONE]\n\n"

    except Exception as e:
        logger.error("Error in stream_completion: %s", e, exc_info=True)
        error_chunk = {"error": {"message": str(e), "type": "server_error"}}
        yield f"data: {json.dumps(error_chunk)}\n\n"


# ============================================================================
# Main
# ============================================================================

if __name__ == '__main__':
    logger.info("=" * 60)
    logger.info("Qwen Multi-Agent Server (FastAPI)")
    logger.info("=" * 60)

    logger.info("Validating configuration...")
    config_errors = Config.validate()
    if config_errors:
        logger.error("Configuration validation failed:")
        for error in config_errors:
            logger.error("  - %s", error)
        logger.error("=" * 60)
        logger.error("Server startup aborted due to configuration errors")
        logger.error("=" * 60)
        sys.exit(1)

    logger.info("Configuration valid")
    logger.info("Port: %d", Config.PORT)
    logger.info("Model: %s", Config.MODEL_PATH)
    logger.info("Model size: %.2f GB", os.path.getsize(Config.MODEL_PATH) / (1024**3))
    logger.info("Agents: %s", list(Config.AGENTS.keys()))
    logger.info("=" * 60)
    logger.info("Starting server...")
    logger.info("API docs available at: http://%s:%d/docs", Config.HOST, Config.PORT)

    import uvicorn
    uvicorn.run(
        app,
        host=Config.HOST,
        port=Config.PORT,
        log_level="info"
    )
