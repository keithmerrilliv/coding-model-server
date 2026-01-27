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
from memory_service import MemoryService
from web_search_service import WebSearchService

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
    max_tokens: int = Field(default=16384, ge=1, le=524288)
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


class MemoryRequest(BaseModel):
    """Request to save a memory"""
    text: str


class SearchRequest(BaseModel):
    """Request to search the web"""
    query: str


# ============================================================================ 
# Configuration
# ============================================================================ 

class Config:
    PORT = int(os.getenv('PORT', 5000))
    HOST = os.getenv('HOST', '0.0.0.0')
    
    # Global defaults (can be overridden per model)
    DEFAULT_CONTEXT_SIZE = int(os.getenv('MODEL_CONTEXT_SIZE', 262144))
    DEFAULT_N_THREADS = int(os.getenv('MODEL_N_THREADS', 8))
    DEFAULT_N_BATCH = int(os.getenv('MODEL_N_BATCH', 1024))  # Increased to 1024 for better prompt evaluation speed
    
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

5. SAVE KNOWLEDGE (Long-term memory):
   <code>
   final_answer("<<<SAVE_MEMORY>>>\nFact to remember\n<<<SAVE_MEMORY>>>")
   </code>
   Use this to save architectural decisions, user preferences, or important facts for future reference.

6. WEB SEARCH (DuckDuckGo):
   <code>
   final_answer("<<<WEB_SEARCH>>>\nSearch Query\n<<<WEB_SEARCH>>>")
   </code>
   Use this to look up up-to-date information, documentation, or solve errors you don't know about.

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
            'system_prompt': f'You are an expert software engineer. Provide clear, working code implementations.\n{REMOTE_EXEC_INSTRUCTION}',
            'model_config': {
                'path': '/home/keith-merrill/.lmstudio/models/tp7030/Qwen3-Coder-30B-A3B-Instruct-FP8-Q6_K-GGUF/qwen3-coder-30b-a3b-instruct-fp8-q6_k.gguf',
                'n_gpu_layers': 25,  # Balanced for 512k context buffers
                'n_ctx': 524288,     # 512k context
                'n_batch': 1024,
                'rope_scaling_type': 2, # Yarn
                'rope_freq_scale': 1.0,
                'yarn_ext_factor': -1.0,
                'yarn_attn_factor': 1.0,
                'yarn_beta_fast': 32.0,
                'yarn_beta_slow': 1.0,
                'yarn_orig_ctx': 32768,
                'type_k': 8, # GGML_TYPE_Q8_0
                'type_v': 8, # GGML_TYPE_Q8_0
                'offload_kqv': False # Force KV cache to RAM
            }
        },
        'architect': {
            'description': 'System architecture agent',
            'system_prompt': f'You are a system architect. Design scalable, maintainable solutions.\n{REMOTE_EXEC_INSTRUCTION}',
            'model_config': {
                'path': '/home/keith-merrill/.lmstudio/models/unsloth/Qwen3-Coder-480B-A35B-Instruct-GGUF/Qwen3-Coder-480B-A35B-Instruct-UD-IQ1_M.gguf',
                'n_gpu_layers': 4,   # Balanced for 256k context with KV in RAM
                'n_ctx': 262144,     # 256k context
                'n_batch': 1024,
                'rope_scaling_type': 2,
                'rope_freq_scale': 1.0,
                'yarn_ext_factor': -1.0,
                'yarn_attn_factor': 1.0,
                'yarn_beta_fast': 32.0,
                'yarn_beta_slow': 1.0,
                'yarn_orig_ctx': 32768,
                'type_k': 8, # GGML_TYPE_Q8_0
                'type_v': 8, # GGML_TYPE_Q8_0
                'offload_kqv': False # Force KV cache to RAM
            }
        },
        'reviewer': {
            'description': 'Code review agent',
            'system_prompt': f'You are a code reviewer. Identify issues and suggest improvements.\n{REMOTE_EXEC_INSTRUCTION}',
            'model_config': {
                'path': '/home/keith-merrill/.lmstudio/models/unsloth/Qwen3-Coder-480B-A35B-Instruct-GGUF/Qwen3-Coder-480B-A35B-Instruct-UD-IQ1_M.gguf',
                'n_gpu_layers': 4,   # Balanced for 256k context with KV in RAM
                'n_ctx': 262144,     # 256k context
                'n_batch': 1024,
                'rope_scaling_type': 2,
                'rope_freq_scale': 1.0,
                'yarn_ext_factor': -1.0,
                'yarn_attn_factor': 1.0,
                'yarn_beta_fast': 32.0,
                'yarn_beta_slow': 1.0,
                'yarn_orig_ctx': 32768,
                'type_k': 8, # GGML_TYPE_Q8_0
                'type_v': 8, # GGML_TYPE_Q8_0
                'offload_kqv': False # Force KV cache to RAM
            }
        },
        'debugger': {
            'description': 'Debugging agent',
            'system_prompt': f'You are a debugging expert. Analyze errors and suggest fixes.\n{REMOTE_EXEC_INSTRUCTION}',
            'model_config': {
                'path': '/home/keith-merrill/.lmstudio/models/n00b001/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M-GGUF/qwen3-coder-30b-a3b-instruct-q4_k_m.gguf',
                'n_gpu_layers': 25,  # Balanced for 512k context buffers
                'n_ctx': 524288,     # 512k context
                'n_batch': 1024,
                'rope_scaling_type': 2,
                'rope_freq_scale': 1.0,
                'yarn_ext_factor': -1.0,
                'yarn_attn_factor': 1.0,
                'yarn_beta_fast': 32.0,
                'yarn_beta_slow': 1.0,
                'yarn_orig_ctx': 32768,
                'type_k': 8, # GGML_TYPE_Q8_0
                'type_v': 8, # GGML_TYPE_Q8_0
                'offload_kqv': False # Force KV cache to RAM
            }
        }
    }

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


# ============================================================================ 
# Model Manager
# ============================================================================ 

class ModelManager:
    def __init__(self):
        self.models: Dict[str, Any] = {}
        self.lock = Lock()
        self.inference_lock = Lock()
        self.current_model_path = None

    def unload_model(self):
        """Unload all models and free VRAM"""
        with self.lock:
            if self.models:
                logger.info("Unloading models (Cleaning VRAM)...")
                self.models.clear()
                self.current_model_path = None

                # Force garbage collection to ensure VRAM is released
                import gc
                import time
                gc.collect()
                gc.collect()  # Call twice to catch circular references
                time.sleep(2)  # Give CUDA time to actually release memory

                logger.info("Models unloaded, memory freed")

    def get_model(self, agent_name: str, force_reload: bool = True):
        """Get or load the model for the specific agent, unloading others if needed"""
        if agent_name not in Config.AGENTS:
            raise ValueError(f"Unknown agent: {agent_name}")

        model_config = Config.AGENTS[agent_name]['model_config']
        model_path = model_config['path']

        # Conditional Reload Policy:
        # If force_reload is False, we try to reuse the existing model to speed up
        # agent loops (e.g. tool use).
        # If force_reload is True (default), we aggressively unload/reload to clear VRAM.
        
        if not force_reload and self.current_model_path == model_path and model_path in self.models:
            logger.info(f"Reusing loaded model for {agent_name} (Fast Path)")
            return self.models[model_path]

        # Otherwise, perform full unload/reload cycle
        self.unload_model()

        with self.lock:
            logger.info("Loading model for %s: %s", agent_name, model_path)
            try:
                import llama_cpp
                from llama_cpp import Llama
                
                model = Llama(
                    model_path=model_path,
                    n_ctx=model_config.get('n_ctx', Config.DEFAULT_CONTEXT_SIZE),
                    n_gpu_layers=model_config.get('n_gpu_layers', 0),
                    n_threads=Config.DEFAULT_N_THREADS,
                    n_batch=model_config.get('n_batch', Config.DEFAULT_N_BATCH),
                    flash_attn=True,   # Enabled for better performance
                    type_k=model_config.get('type_k'), # None = Model default (usually F16)
                    type_v=model_config.get('type_v'), # None = Model default (usually F16)
                    use_mmap=True,
                    use_mlock=True,
                    offload_kqv=model_config.get('offload_kqv', True), # True = Offload to GPU, False = RAM
                    # RoPE / YaRN Scaling for extended context
                    rope_scaling_type=model_config.get('rope_scaling_type', -1), # -1 = Unspecified
                    rope_freq_base=model_config.get('rope_freq_base', 0.0),      # 0.0 = Model default
                    rope_freq_scale=model_config.get('rope_freq_scale', 0.0),    # 0.0 = Model default
                    yarn_ext_factor=model_config.get('yarn_ext_factor', -1.0),   # -1.0 = Unspecified
                    yarn_attn_factor=model_config.get('yarn_attn_factor', 1.0),
                    yarn_beta_fast=model_config.get('yarn_beta_fast', 32.0),
                    yarn_beta_slow=model_config.get('yarn_beta_slow', 1.0),
                    yarn_orig_ctx=model_config.get('yarn_orig_ctx', 0),          # 0 = Model default
                    verbose=True
                )
                self.models[model_path] = model
                self.current_model_path = model_path
                logger.info("Model loaded successfully: %s", model_path)
                return model
            except Exception as e:
                logger.error("Failed to load model %s: %s", model_path, e)
                raise

    def is_loaded(self) -> bool:
        """Check if any model is loaded"""
        return len(self.models) > 0


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
# Initialize Memory Service
try:
    memory_service = MemoryService()
except Exception as e:
    logger.error(f"Failed to initialize memory service: {e}")
    memory_service = None

# Initialize Web Search Service
try:
    web_search_service = WebSearchService()
except Exception as e:
    logger.error(f"Failed to initialize web search service: {e}")
    web_search_service = None


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
            "memory": "/v1/memory",
            "search": "/v1/tools/search",
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


@app.post("/v1/memory")
async def save_memory(request: MemoryRequest):
    """Save a memory/fact to the long-term storage"""
    if not memory_service:
        raise HTTPException(status_code=503, detail="Memory service not initialized")
        
    try:
        mem_id = memory_service.add_memory(request.text)
        if not mem_id:
            raise HTTPException(status_code=500, detail="Failed to save memory")
            
        return {"status": "success", "id": mem_id}
    except Exception as e:
        logger.error(f"Error saving memory: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/tools/search")
async def web_search(request: SearchRequest):
    """Perform a web search using DuckDuckGo"""
    if not web_search_service:
        raise HTTPException(status_code=503, detail="Web search service not initialized")
        
    result = web_search_service.search(request.query)
    return {"result": result}


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest, raw_request: Request):
    """Handle chat completion requests (OpenAI-compatible)"""
    try:
        # Validate model exists
        if request.model not in Config.AGENTS:
            raise HTTPException(
                status_code=404,
                detail=f"Model '{request.model}' not found. Available models: {', '.join(Config.AGENTS.keys())}"
            )

        # Check for reload flag (default to True for safety)
        force_reload = raw_request.headers.get("X-Qwen-Force-Reload", "true").lower() == "true"

        agent_config = Config.AGENTS[request.model]
        system_prompt = agent_config['system_prompt']
        
        # RAG: Retrieve relevant memories
        if memory_service and request.messages:
            # Find the last user message to use as query
            last_user_msg = next((m.content for m in reversed(request.messages) if m.role == 'user'), None)
            
            if last_user_msg:
                try:
                    context = memory_service.get_context_string(last_user_msg)
                    if context:
                        logger.info(f"Injecting memory context for query: {last_user_msg[:50]}...")
                        # Prepend context to system prompt
                        system_prompt = f"{system_prompt}\n\n{context}"
                except Exception as e:
                    logger.error(f"Memory retrieval failed: {e}")

        prompt = build_chatml_prompt(request.messages, system_prompt)

        if request.stream:
            return StreamingResponse(
                stream_completion(prompt, request.model, request.max_tokens, request.temperature, force_reload),
                media_type="text/event-stream"
            )
        else:
            return sync_completion(prompt, request.model, request.max_tokens, request.temperature, force_reload)

    except HTTPException:
        raise
    except FileNotFoundError as e:
        logger.error("Model file error: %s", e)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error("Error in chat_completions: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def sync_completion(prompt: str, model_id: str, max_tokens: int, temperature: float, force_reload: bool) -> Dict[str, Any]:
    """Generate synchronous completion"""
    params = get_model_params(max_tokens, temperature, stream=False)

    try:
        with model_manager.inference_lock:
            model = model_manager.get_model(model_id, force_reload=force_reload)
            response = model(prompt, **params)
        
        text = response['choices'][0]['text'].strip()
        return build_completion_response(model_id, text, response['usage'])
    finally:
        if force_reload:
            model_manager.unload_model()

def stream_completion(prompt: str, model_id: str, max_tokens: int, temperature: float, force_reload: bool) -> Iterator[str]:
    """Generate streaming completion"""
    try:
        completion_id = f"chatcmpl-{int(time.time())}"
        
        with model_manager.inference_lock:
            model = model_manager.get_model(model_id, force_reload=force_reload)
            params = get_model_params(max_tokens, temperature, stream=True)

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
    finally:
        if force_reload:
            model_manager.unload_model()


if __name__ == "__main__":
    import uvicorn
    
    # Validate configuration
    errors = Config.validate()
    if errors:
        for error in errors:
            logger.error(error)
        # We don't exit here to allow the server to start even if some models are missing
        # The endpoints will just fail for those specific models
        logger.warning("Starting server with configuration errors...")

    uvicorn.run(
        "server:app",
        host=Config.HOST,
        port=Config.PORT,
        log_level="info",
        reload=False
    )