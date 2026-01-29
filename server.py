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
from contextlib import asynccontextmanager

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


class IngestRequest(BaseModel):
    """Request to ingest a local file"""
    path: str


import subprocess
import shlex
from threading import Lock
from contextlib import asynccontextmanager

# ... (existing imports)

# ============================================================================ 
# Apple Deep Docs Service (MCP Integration)
# ============================================================================ 

class AppleDeepDocsService:
    """Service for interacting with the Apple Deep Docs MCP server on the Linux server"""
    
    def __init__(self, mcp_path: str):
        self.mcp_path = mcp_path
        self.process = None
        self.msg_id = 1
        self.lock = Lock()
        self.venv_python = os.path.join(mcp_path, "venv/bin/python")

    def start(self):
        """Start the MCP server process and perform handshake if not already running"""
        if self.process and self.process.poll() is None:
            return True
            
        try:
            main_py = os.path.join(self.mcp_path, "main.py")
            if not os.path.exists(self.venv_python):
                logger.error(f"Apple Deep Docs venv not found at {self.venv_python}")
                return False
                
            self.process = subprocess.Popen(
                [self.venv_python, main_py],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self.mcp_path
            )
            
            # Perform MCP Handshake
            logger.info("Performing MCP handshake with Apple Deep Docs...")
            
            # 1. Send initialize
            init_request = {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "qwen-server", "version": "2.0"}
                }
            }
            self.process.stdin.write(json.dumps(init_request) + "\n")
            self.process.stdin.flush()
            
            # 2. Wait for initialize response
            while True:
                line = self.process.stdout.readline()
                if not line:
                    logger.error("Failed to receive initialize response from MCP")
                    return False
                line = line.strip()
                if not line: continue
                try:
                    resp = json.loads(line)
                    if resp.get("id") == 0:
                        logger.info("MCP initialize successful")
                        break
                except:
                    continue
            
            # 3. Send initialized notification
            initialized_notif = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized"
            }
            self.process.stdin.write(json.dumps(initialized_notif) + "\n")
            self.process.stdin.flush()
            
            logger.info("Apple Deep Docs MCP server ready")
            return True
        except Exception as e:
            logger.error(f"Error starting Apple Deep Docs MCP: {e}")
            return False

    def stop(self):
        """Stop the MCP server process"""
        if self.process:
            self.process.terminate()
            self.process = None
            logger.info("Apple Deep Docs MCP server stopped")

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Call a specific tool on the MCP server and return the result as text"""
        if not self.start():
            return "Error: Apple Deep Docs MCP server failed to start."
            
        with self.lock:
            req_id = self.msg_id
            self.msg_id += 1
            
            request = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments
                }
            }
            
            try:
                # Write request
                payload = json.dumps(request)
                logger.info(f"Sending MCP Request: {payload[:200]}...")
                self.process.stdin.write(payload + "\n")
                self.process.stdin.flush()
                
                # Read response (blocking until line or process exit)
                # We loop to skip non-JSON lines (like logs or banners)
                while True:
                    line = self.process.stdout.readline()
                    if not line:
                        err = self.process.stderr.read() if self.process.poll() is not None else "No output"
                        logger.error(f"MCP Read Error: {err}")
                        return f"Error: No response from MCP server. {err[:200]}"
                    
                    line = line.strip()
                    logger.info(f"Received from MCP: {line[:500]}")
                    if not line:
                        continue
                        
                    try:
                        response = json.loads(line)
                        if response.get("id") == req_id:
                            result = response.get("result", {})
                            # Process content (usually a list of content items)
                            content = result.get("content", [])
                            text_parts = []
                            for item in content:
                                if item.get("type") == "text":
                                    text_parts.append(item.get("text", ""))
                            
                            if text_parts:
                                return "\n\n".join(text_parts)
                            
                            # If no text parts, return the whole result as string
                            return json.dumps(result, indent=2)
                        
                        logger.debug(f"Skipping MCP response with mismatching ID: {response.get('id')}")
                    except json.JSONDecodeError:
                        logger.debug(f"Skipping non-JSON MCP output: {line[:100]}...")
                        continue
                
            except Exception as e:
                logger.error(f"Communication error with Deep Docs MCP: {e}")
                return f"Error: Documentation fetch failed: {str(e)}"

# ============================================================================ 
# Configuration
# ============================================================================ 

class Config:
    PORT = int(os.getenv('PORT', 5000))
    HOST = os.getenv('HOST', '0.0.0.0')
    
    # Global defaults (can be overridden per model)
    DEFAULT_CONTEXT_SIZE = int(os.getenv('MODEL_CONTEXT_SIZE', 524288))
    # Increased default threads to 24 to match physical core count (8 P-cores + 16 E-cores)
    # This maximizes CPU utilization for the layers not offloaded to GPU
    DEFAULT_N_THREADS = int(os.getenv('MODEL_N_THREADS', 24))
    DEFAULT_N_BATCH = int(os.getenv('MODEL_N_BATCH', 2048))  # Increased to 2048 for better CPU saturation
    
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

7. APPLE DOCUMENTATION (Cupertino MCP):
   <code>
   final_answer("<<<CUPERTINO>>>\nAPI or Framework Name\n<<<CUPERTINO>>>")
   </code>
   Use this to search the local Apple Developer documentation on the user's macOS machine. This is the preferred source for Metal 4, Swift, and Apple platform APIs. Results are automatically indexed for RAG.

8. APPLE DEEP SEARCH (Server-side MCP):
   <code>
   final_answer("<<<APPLE_DEEP_DOCS>>>\n{\"tool\": \"TOOL_NAME\", \"arguments\": {\"arg\": \"val\"}}\n<<<APPLE_DEEP_DOCS>>>")
   </code>
   Use this for advanced Apple documentation searches on the server. Available tools:
   - fetch_apple_documentation: {"url": "https://developer.apple.com/..."}
   - search_apple_online: {"query": "term"}
   - search_swift_evolution: {"feature": "term"}
   - search_swift_repos: {"query": "term"}
   - search_wwdc_notes: {"query": "term"}
   - search_human_interface_guidelines: {"query": "term"}

# WORKFLOW FOR BUILDS/LONG TASKS
1. Start with REMOTE_EXEC_ASYNC.
2. Get Job ID.
3. Poll with REMOTE_CHECK_STATUS every few seconds.
4. When status is 'completed' or 'failed', use REMOTE_GET_OUTPUT to see results.

# STRATEGY FOR COMPLEX TASKS
If the task is complex or large:
1. PLAN FIRST: Create a step-by-step plan.
2. PHASED EXECUTION: Work on one phase at a time.
3. INTERMEDIATE SUMMARIES: Provide brief summaries after completing each phase to maintain context.

# CRITICAL RULES
- ALWAYS wrap your code actions in <code>...</code> blocks.
- NEVER try to use os.system() or subprocess locally for client tasks.
"""

    IMPLEMENTER_INSTRUCTION = """
# AGENT WORKFLOW: EXPLORE -> PLAN -> IMPLEMENT
You are an autonomous developer working on a Linux server. Your goal is to complete the user's task by any means necessary.

## PHASE 1: EXPLORATION (MANDATORY)
- You CANNOT edit files you haven't read.
- You CANNOT fix bugs you haven't located.
- **IMMEDIATELY** use shell tools to find relevant files:
  - `ls -R`: List files to understand structure.
  - `grep -r "term" .`: Search for code patterns.
  - `cat filename`: Read file content.

## PHASE 2: IMPLEMENTATION
- Once you have located the files, output the full, working code to fix the issue or add the feature.
- Always wrap code in triple backticks (e.g. ```python).
- If creating a new file, use `cat > filename << 'EOF'` or just provide the code block.

## RULES
1. **USE TOOLS:** Do not guess file paths. Check them first.
2. **BE CONCISE:** Do not waste tokens on long explanations. State your action ("Searching for X...") and then DO IT.
3. **TOOL SYNTAX:** To run commands, you MUST use the `final_answer("<<<REMOTE_EXEC>>>...")` format defined below.
"""

    AGENTS = {
        'implementer': {
            'description': 'Qwen3-Coder-30B-A3B (Smart - 48k Context)',
            'system_prompt': f'You are an autonomous developer. {IMPLEMENTER_INSTRUCTION}\n{REMOTE_EXEC_INSTRUCTION}',
            'model_config': {
                'path': '/home/keith-merrill/.lmstudio/models/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf',
                'n_gpu_layers': 33, # Increased to fill VRAM
                'n_ctx': 49152, # Increased to 48k
                'n_batch': 1024,
                'rope_scaling_type': 2,
                'rope_freq_scale': 1.0,
                'yarn_ext_factor': -1.0,
                'yarn_attn_factor': 1.0,
                'yarn_beta_fast': 32.0,
                'yarn_beta_slow': 1.0,
                'yarn_orig_ctx': 32768,
                'type_k': 8,
                'type_v': 8,
                'offload_kqv': True
            }
        },
        'architect': {
            'description': 'System architecture agent',
            'system_prompt': f'You are a system architect. Design scalable, maintainable solutions.\n{REMOTE_EXEC_INSTRUCTION}',
            'model_config': {
                'path': '/home/keith-merrill/.lmstudio/models/Qwen/Qwen3-32B-GGUF/Qwen3-32B-Q4_K_M.gguf',
                'n_gpu_layers': 33,
                'n_ctx': 43008,
                'n_batch': 2048,
                'rope_scaling_type': 2,
                'rope_freq_scale': 1.0,
                'yarn_ext_factor': -1.0,
                'yarn_attn_factor': 1.0,
                'yarn_beta_fast': 32.0,
                'yarn_beta_slow': 1.0,
                'yarn_orig_ctx': 32768,
                'type_k': 8,
                'type_v': 8,
                'offload_kqv': True
            }
        },
        'reviewer': {
            'description': 'Code review agent',
            'system_prompt': f'You are a code reviewer. Identify issues and suggest improvements.\n{REMOTE_EXEC_INSTRUCTION}',
            'model_config': {
                'path': '/home/keith-merrill/.lmstudio/models/unsloth/Qwen3-14B-GGUF/Qwen3-14B-Q6_K.gguf',
                'n_gpu_layers': 99,
                'n_ctx': 32768,
                'n_batch': 2048,
                'rope_scaling_type': 2,
                'rope_freq_scale': 1.0,
                'yarn_ext_factor': -1.0,
                'yarn_attn_factor': 1.0,
                'yarn_beta_fast': 32.0,
                'yarn_beta_slow': 1.0,
                'yarn_orig_ctx': 32768,
                'type_k': 8,
                'type_v': 8,
                'offload_kqv': True
            }
        },
        'debugger': {
            'description': 'Qwen3-Coder-30B-A3B (Smart - 48k Context)',
            'system_prompt': f'You are a master of debugging. {IMPLEMENTER_INSTRUCTION}\n{REMOTE_EXEC_INSTRUCTION}',
            'model_config': {
                'path': '/home/keith-merrill/.lmstudio/models/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf',
                'n_gpu_layers': 33,
                'n_ctx': 49152,
                'n_batch': 1024,
                'rope_scaling_type': 2,
                'rope_freq_scale': 1.0,
                'yarn_ext_factor': -1.0,
                'yarn_attn_factor': 1.0,
                'yarn_beta_fast': 32.0,
                'yarn_beta_slow': 1.0,
                'yarn_orig_ctx': 32768,
                'type_k': 8,
                'type_v': 8,
                'offload_kqv': True
            }
        },
        'metal_implementer': {
            'description': 'Qwen3-Coder-30B-A3B (Smart - 48k Context)',
            'system_prompt': f"""You are an autonomous Graphics Engineer specializing in Apple Metal 4.
{IMPLEMENTER_INSTRUCTION}

Your core expertise covers:
1. COMPUTE: High-performance kernels, SIMD-group operations, threadgroup memory.
2. GRAPHICS: Mesh Shaders, Ray Tracing, Render pipelines.
3. METAL 4: GPU Dynamic Indexing, Argument Buffers, Modern Binding.

{REMOTE_EXEC_INSTRUCTION}""",
            'model_config': {
                'path': '/home/keith-merrill/.lmstudio/models/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf',
                'n_gpu_layers': 33,
                'n_ctx': 49152,
                'n_batch': 1024,
                'rope_scaling_type': 2,
                'rope_freq_scale': 1.0,
                'yarn_ext_factor': -1.0,
                'yarn_attn_factor': 1.0,
                'yarn_beta_fast': 32.0,
                'yarn_beta_slow': 1.0,
                'yarn_orig_ctx': 32768,
                'type_k': 8,
                'type_v': 8,
                'offload_kqv': True
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
                try:
                    # Clear internal references
                    self.models.clear()
                    self.current_model_path = None
                    
                    # Force garbage collection
                    import gc
                    import time
                    gc.collect()
                    gc.collect() 
                    
                    # Clear CUDA cache if PyTorch is available (often used by other libs)
                    try:
                        import torch
                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                            torch.cuda.ipc_collect()
                    except ImportError:
                        pass
                    
                    time.sleep(2) # Allow async cleanup
                    logger.info("Models unloaded, memory freed")
                except Exception as e:
                    logger.error(f"Error during model unload: {e}")

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
                    n_threads_batch=Config.DEFAULT_N_THREADS,
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
# Prompt Format Helpers
# ============================================================================ 

CHATML_START = "<|im_start|>"
CHATML_END = "<|im_end|>"


def build_model_prompt(messages: List[ChatMessage], system_prompt: str, model_path: str) -> str:
    """Build a prompt using the appropriate format for the model"""
    
    # Detect DeepSeek-Coder (original) vs DeepSeek-R1-Distill-Qwen (ChatML)
    is_legacy_deepseek = "deepseek" in model_path.lower() and "qwen" not in model_path.lower()
    
    if is_legacy_deepseek:
        # DeepSeek-Coder-Instruct/Alpaca format
        parts = []
        if system_prompt:
            parts.append(f"### Instruction:\n{system_prompt}\n")
        
        for msg in messages:
            if msg.role == "user":
                parts.append(f"### Instruction:\n{msg.content}\n")
            elif msg.role == "assistant":
                parts.append(f"### Response:\n{msg.content}\n")
        
        parts.append("### Response:\n")
        return "".join(parts)
    
    else:
        # Standard ChatML format (Qwen, DeepSeek-R1-Distill-Qwen)
        parts = []
        if system_prompt:
            parts.append(f"{CHATML_START}system\n{system_prompt}{CHATML_END}\n")

        for msg in messages:
            parts.append(f"{CHATML_START}{msg.role}\n{msg.content}{CHATML_END}\n")

        parts.append(f"{CHATML_START}assistant\n")
        return "".join(parts)


def get_model_params(max_tokens: int, temperature: float, stream: bool = False) -> Dict[str, Any]:
    """Get common model inference parameters"""
    return {
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stop": [CHATML_END, CHATML_START, "<|EOT|>", "### Response:", "### Instruction:", "###"],
        "stream": stream,
        "repeat_penalty": 1.1,
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

model_manager = ModelManager()
memory_service = None
web_search_service = None
apple_deep_docs_service = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager for the FastAPI app"""
    global memory_service, web_search_service, apple_deep_docs_service
    
    # Startup
    logger.info("Server starting up...")
    
    # Initialize Memory Service
    try:
        logger.info("Initializing Memory Service...")
        memory_service = MemoryService()
        logger.info("Memory Service initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize memory service: {e}")
        memory_service = None

    # Initialize Web Search Service
    try:
        logger.info("Initializing Web Search Service...")
        web_search_service = WebSearchService()
        logger.info("Web Search Service initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize web search service: {e}")
        web_search_service = None

    # Initialize Apple Deep Docs Service
    try:
        logger.info("Initializing Apple Deep Docs Service...")
        mcp_path = os.path.join(os.getcwd(), "tools/appledeepdoc-mcp")
        apple_deep_docs_service = AppleDeepDocsService(mcp_path)
        if apple_deep_docs_service.start():
            logger.info("Apple Deep Docs Service initialized successfully")
        else:
            logger.error("Apple Deep Docs Service failed to start")
    except Exception as e:
        logger.error(f"Failed to initialize Apple Deep Docs Service: {e}")
        apple_deep_docs_service = None

    yield
    # Shutdown
    logger.info("Server shutting down - unloading models...")
    model_manager.unload_model()
    if apple_deep_docs_service:
        apple_deep_docs_service.stop()
    logger.info("Server shutdown complete.")

app = FastAPI(
    title="Qwen Multi-Agent Server",
    description="OpenAI-compatible API for Qwen LLM with multi-agent support",
    version="2.0",
    lifespan=lifespan
)

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


class DeepDocRequest(BaseModel):
    """Request for Apple Deep Docs"""
    tool: str
    arguments: Dict[str, Any]


@app.post("/v1/tools/apple_deep_docs")
async def apple_deep_docs(request: DeepDocRequest):
    """Perform an Apple Documentation search using the server-side MCP"""
    if not apple_deep_docs_service:
        raise HTTPException(status_code=503, detail="Apple Deep Docs service not initialized")
        
    result = apple_deep_docs_service.call_tool(request.tool, request.arguments)
    return {"result": result}


@app.post("/v1/memory/ingest")
async def ingest_memory(request: IngestRequest):
    """Ingest a local PDF file into long-term memory"""
    if not memory_service:
        raise HTTPException(status_code=503, detail="Memory service not initialized")
        
    result = memory_service.ingest_pdf(request.path)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
        
    return result


@app.post("/v1/admin/unload")
async def unload_models():
    """Manually unload all models to free VRAM"""
    model_manager.unload_model()
    return {"status": "success", "message": "All models unloaded"}


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

        prompt = build_model_prompt(request.messages, system_prompt, agent_config['model_config']['path'])

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
        reload=False,
        loop="asyncio" # Force standard asyncio loop for stability
    )