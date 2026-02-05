#!/usr/bin/env python3
"""
Qwen Multi-Agent Server (FastAPI)
Provides OpenAI-compatible API for remote command execution with multi-agent support
"""
import os
import sys
import json
import time
import uuid
import subprocess
import logging
import select
from datetime import datetime
from typing import Dict, List, Optional, Any, Literal, Iterator
from threading import Lock, Thread
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Header, Depends
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

    def _readline_with_timeout(self, timeout: float = 30) -> Optional[str]:
        """Read a line from the MCP subprocess stdout with a timeout using select.
        
        Returns the line string, or None on timeout / EOF.
        """
        if not self.process or not self.process.stdout:
            return None

        # Poll stdout for data
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        if ready:
            return self.process.stdout.readline()
        
        logger.error("MCP readline timed out after %.1f seconds", timeout)
        return None

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
                stderr=subprocess.DEVNULL,
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

            # 2. Wait for initialize response (with timeout)
            while True:
                line = self._readline_with_timeout(timeout=30)
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
                except Exception:
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

                # Read response with timeout to avoid blocking forever
                # We loop to skip non-JSON lines (like logs or banners)
                max_attempts = 50  # safety cap to prevent infinite loop
                for _ in range(max_attempts):
                    line = self._readline_with_timeout(timeout=60)
                    if not line:
                        if self.process.poll() is not None:
                            return "Error: MCP server process exited unexpectedly."
                        return "Error: No response from MCP server (timed out)."

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

                return "Error: MCP server sent too many non-matching responses."

            except Exception as e:
                logger.error(f"Communication error with Deep Docs MCP: {e}")
                return f"Error: Documentation fetch failed: {str(e)}"

# ============================================================================
# Helper Functions
# ============================================================================

def _create_model_config(path_env, path_default, n_gpu_layers, n_ctx=32768, n_batch=2048):
    """Helper function to create standardized model configurations"""
    return {
        'path': os.getenv(path_env, path_default),
        'n_gpu_layers': n_gpu_layers,
        'n_ctx': n_ctx,
        'n_batch': n_batch,
        'rope_scaling_type': 2,
        'rope_freq_scale': 1.0,
        'yarn_ext_factor': -1.0,
        'yarn_attn_factor': 1.0,
        'yarn_beta_fast': 32.0,
        'yarn_beta_slow': 1.0,
        'yarn_orig_ctx': 32768,
        'type_k': 8, 'type_v': 8, 'offload_kqv': True,
    }



def _create_agent_config(description, system_prompt, model_config, executor=False):
    """Helper function to create standardized agent configurations"""
    config = {
        'description': description,
        'system_prompt': system_prompt,
        'model_config': model_config,
    }
    if executor:
        config['executor'] = True
    return config


# ============================================================================
# Configuration
# ============================================================================

class Config:
    PORT = int(os.getenv('PORT', 5000))
    HOST = os.getenv('HOST', '0.0.0.0')
    ADMIN_API_KEY = os.getenv('ADMIN_API_KEY', '')
    INGEST_ALLOWED_DIR = os.getenv('INGEST_ALLOWED_DIR', '')
    
    # Global defaults (can be overridden per model)
    DEFAULT_CONTEXT_SIZE = int(os.getenv('MODEL_CONTEXT_SIZE', 524288))
    # Increased default threads to 24 to match physical core count (8 P-cores + 16 E-cores)
    # This maximizes CPU utilization for the layers not offloaded to GPU
    DEFAULT_N_THREADS = int(os.getenv('MODEL_N_THREADS', 24))
    DEFAULT_N_BATCH = int(os.getenv('MODEL_N_BATCH', 2048))  # Increased to 2048 for better CPU saturation
    
    # ── Unified tool reference ──
    BASE_TOOLS = [
        "<<<REMOTE_EXEC>>>command<<<REMOTE_EXEC>>>                         — run a shell command (Linux/macOS compatible)",
        "<<<REMOTE_EXEC_ASYNC>>>command<<<REMOTE_EXEC_ASYNC>>>             — run in background",
        "<<<REMOTE_CHECK_STATUS>>>JOB_ID<<<REMOTE_CHECK_STATUS>>>          — poll async job",
        "<<<REMOTE_GET_OUTPUT>>>JOB_ID<<<REMOTE_GET_OUTPUT>>>              — get finished job output",
        "<<<READ_FILE>>>path<<<READ_FILE>>>                                — read file content (safe, fast)",
        "<<<SAVE_MEMORY>>>fact<<<SAVE_MEMORY>>>                            — persist a fact",
        "<<<WEB_SEARCH>>>query<<<WEB_SEARCH>>>                             — web search",
        "<<<CUPERTINO>>>query<<<CUPERTINO>>>                               — Apple docs (local MCP)",
        '<<<APPLE_DEEP_DOCS>>>{"tool":"NAME","arguments":{}}<<<APPLE_DEEP_DOCS>>> — Apple docs (server MCP)',
        "<<<INSTALL_TOOL_HOMEBREW>>>tool_name<<<INSTALL_TOOL_HOMEBREW>>>   — install a tool using Homebrew"
    ]

    # ── Combined tools ──
    ALL_TOOLS = BASE_TOOLS

    TOOL_REFERENCE = "# TOOLS — emit these markers inline to execute commands.\n" + "\n".join(ALL_TOOLS)

    # ── Git-enhanced tool reference for reviewer ──
    GIT_TOOL_REFERENCE = (
        "# TOOLS — emit these markers inline and the client runs them automatically.\n" +
        "\n".join(ALL_TOOLS) +
        "\n\n# ESSENTIAL TOOLS FOR CODE REVIEW — Comprehensive toolkit for thorough code analysis:"
        "\n# Git commands for understanding code changes and history:" +
        "\n<<<REMOTE_EXEC>>>git status<<<REMOTE_EXEC>>>                      — check current repository state" +
        "\n<<<REMOTE_EXEC>>>git log --oneline -10<<<REMOTE_EXEC>>>           — view recent commit history" +
        "\n<<<REMOTE_EXEC>>>git diff<<<REMOTE_EXEC>>>                        — see current uncommitted changes" +
        "\n<<<REMOTE_EXEC>>>git diff --cached<<<REMOTE_EXEC>>>               — see staged changes" +
        "\n<<<REMOTE_EXEC>>>git diff HEAD~1<<<REMOTE_EXEC>>>                 — compare working directory to last commit" +
        "\n<<<REMOTE_EXEC>>>git show HEAD<<<REMOTE_EXEC>>>                   — show details of last commit" +
        "\n<<<REMOTE_EXEC>>>git blame filename<<<REMOTE_EXEC>>>              — see who made changes to each line" +
        "\n<<<REMOTE_EXEC>>>git log -p --follow filepath<<<REMOTE_EXEC>>>    — see history of changes to a specific file" +
        "\n<<<REMOTE_EXEC>>>git diff HEAD~3 HEAD<<<REMOTE_EXEC>>>            — compare changes between commits" +
        "\n<<<REMOTE_EXEC>>>git log --author=\"Author Name\" --since=\"2 weeks ago\"<<<REMOTE_EXEC>>> — find commits by author/time" +
        "\n\n# File system navigation and search:" +
        "\n<<<REMOTE_EXEC>>>find . -name \"*.py\" -type f<<<REMOTE_EXEC>>>     — find all Python files" +
        "\n<<<REMOTE_EXEC>>>find . -name \"*.js\" -o -name \"*.ts\"<<<REMOTE_EXEC>>> — find JavaScript/TypeScript files" +
        "\n<<<REMOTE_EXEC>>>find . -name \"*.java\" -o -name \"*.cpp\" -o -name \"*.h\"<<<REMOTE_EXEC>>> — find source files" +
        "\n<<<REMOTE_EXEC>>>find . -name \"*test*\" -o -name \"*spec*\"<<<REMOTE_EXEC>>> — find test files" +
        "\n<<<REMOTE_EXEC>>>find . -name \"*.md\" -o -name \"*.txt\"<<<REMOTE_EXEC>>> — find documentation files" +
        "\n<<<REMOTE_EXEC>>>find . -size +1M -name \"*.log\"<<<REMOTE_EXEC>>> — find large log files" +
        "\n<<<REMOTE_EXEC>>>grep -r \"TODO|FIXME|HACK\" .<<<REMOTE_EXEC>>>   — find code comments indicating work to do" +
        "\n<<<REMOTE_EXEC>>>grep -rn \"error\" .<<<REMOTE_EXEC>>>              — find error mentions in code" +
        "\n<<<REMOTE_EXEC>>>grep -rn \"DEBUG|debug|console.log\" .<<<REMOTE_EXEC>>> — find debug statements" +
        "\n\n# Code analysis and comparison:" +
        "\n<<<REMOTE_EXEC>>>diff file1 file2<<<REMOTE_EXEC>>>                — compare two files" +
        "\n<<<REMOTE_EXEC>>>diff -u old_file new_file<<<REMOTE_EXEC>>>       — unified diff format" +
        "\n<<<REMOTE_EXEC>>>wc -l filename<<<REMOTE_EXEC>>>                  — count lines in file" +
        "\n<<<REMOTE_EXEC>>>head -20 filename<<<REMOTE_EXEC>>>               — show first 20 lines" +
        "\n<<<REMOTE_EXEC>>>tail -20 filename<<<REMOTE_EXEC>>>               — show last 20 lines" +
        "\n<<<REMOTE_EXEC>>>sort filename<<<REMOTE_EXEC>>>                   — sort file contents" +
        "\n<<<REMOTE_EXEC>>>uniq -c filename<<<REMOTE_EXEC>>>                — count unique lines" +
        "\n<<<REMOTE_EXEC>>>stat filename<<<REMOTE_EXEC>>>                   — detailed file information"
    )

    # ── Token budget guidance (injected dynamically) ──
    TOKEN_BUDGET_GUIDANCE = """# OUTPUT BUDGET: ~{available_tokens} tokens available for your response.

CRITICAL: Plan your response to fit within this budget. If the task requires more output:

1. PARTITION LARGE TASKS: Break into logical, self-contained sections
   - Each section should be complete and usable on its own
   - For code: complete one file or one function fully before moving on
   - For explanations: complete one topic fully before the next

2. PRIORITIZE: Do the most important/requested work FIRST
   - Core functionality before edge cases
   - Critical files before auxiliary ones
   - Working code before optimizations

3. SIGNAL CONTINUATION: If you cannot finish everything, end with:
   <<<CONTINUE>>>
   REMAINING: [brief list of what still needs to be done]

   The client will automatically request continuation.

4. MAINTAIN ATOMIC INTEGRITY: When context limits prevent delivering a large file in one turn, DO NOT provide a partial rewrite. Instead, use incremental replace calls for specific blocks or write segments to temporary files and use shell tools (like cat) to assemble the complete final file. Always ensure the worktree remains syntactically valid at the end of each turn.

BUDGET GUIDELINES:
- ~100 tokens ≈ 75 words or ~4-5 lines of code
- A typical function: 50-200 tokens
- A typical file: 200-1000 tokens
- If budget < 1000: Keep response very concise
- If budget < 500: Single focused answer only"""

    # ── Behavioral instruction for action-oriented agents ──
    EXECUTOR_PROMPT = """You execute tasks by running shell commands.

WRONG: "You should run grep to find the file, then edit it."
RIGHT: "Finding the file.
<<<REMOTE_EXEC>>>
grep -r 'login' .
<<<REMOTE_EXEC>>>"

Rules:
- Every response MUST contain at least one <<<REMOTE_EXEC>>> block.
- Never ask for permission. You have full file access.
- Never claim you cannot run commands. You can.
- If unsure where something is, search for it.
- For code-related tasks, consider using Git commands to understand context and history.
"""

    # ── Shared model configs ──
    # Turbo: Optimized for speed and 80k context on RTX 5080 (Success Formula)
    _CODER_30B_TURBO = _create_model_config(
        'MODEL_PATH_30B_TURBO',
        '/home/keith-merrill/.lmstudio/models/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf',
        32, 81920, 2048
    )

    # HD: Optimized for high-precision code generation and review
    _CODER_30B_HD = _create_model_config(
        'MODEL_PATH_30B_HD',
        '/home/keith-merrill/.lmstudio/models/lmstudio-community/Qwen3-Coder-30B-A3B-Instruct-GGUF/Qwen3-Coder-30B-A3B-Instruct-Q8_0.gguf',
        36, 32768, 2048
    )

    # Lite: Faster reasoning on system RAM
    _QWEN_480B_LITE = _create_model_config(
        'MODEL_PATH_480B_LITE',
        '/home/keith-merrill/.lmstudio/models/unsloth/Qwen3-Coder-480B-A35B-Instruct-GGUF/Qwen3-Coder-480B-A35B-Instruct-UD-IQ1_M.gguf',
        4, 32768, 1024
    )

    # Ultra: Premium reasoning using Q2_K_XL on 192GB RAM
    _QWEN_480B_ULTRA = _create_model_config(
        'MODEL_PATH_480B_ULTRA',
        '/home/keith-merrill/.lmstudio/models/unsloth/Qwen3-Coder-480B-A35B-Instruct-GGUF/Qwen3-Coder-480B-A35B-Instruct-UD-Q2_K_XL-00001-of-00004.gguf',
        4, 49152, 1024
    )

    # ── Few-shot example injected for executor agents ──
    # The model sees this as a real prior exchange, so it copies the format.
    FEW_SHOT = [
        {"role": "user", "content": "List the Python files in this project."},
        {"role": "assistant", "content": "<<<REMOTE_EXEC>>>\nfind . -name '*.py' -type f\n<<<REMOTE_EXEC>>>"},
    ]

    # ── Agent definitions ──
    # 'executor': True means few-shot + fallback extraction are enabled.
    AGENTS = {
        'implementer': _create_agent_config(
            'Qwen3-Coder-30B-A3B HD',
            f'You are an implementer. {EXECUTOR_PROMPT}\n\nCOMPREHENSIVE IMPLEMENTATION: When implementing tasks, leverage multiple tools to understand the codebase thoroughly:\n\nEXECUTION ENVIRONMENT: You are running on a macOS environment with full access to development tools.\n- Use `<<<REMOTE_EXEC>>>` for ALL shell commands (including Xcode tools, Git, file operations).\n- Do NOT distinguish between "server" and "client". Everything runs locally.\n\nGIT AWARENESS: Use Git to understand code changes, history, and context:\n- Use `git log` to understand recent changes and history\n- Use `git diff` to see specific code differences\n- Use `git blame` to identify who made changes and why\n- Use `git show` to examine specific commits\n- Use `git status` to see current state of the repository\n\nFILE SYSTEM NAVIGATION: Use find/grep to locate and analyze relevant files:\n- Use `find` to locate specific file types or patterns\n- Use `grep` to search for specific terms, TODOs, FIXMEs, or error patterns\n- Use `grep -r` for recursive searches across the codebase\n\nAPPLE DEVELOPMENT:\n- For Xcode projects, use `xcodebuild`, `xcrun`, `xcodegen` directly via `<<<REMOTE_EXEC>>>`.\n- No special markers needed for client-side tools.\n\n{TOOL_REFERENCE}',
            _CODER_30B_HD,
            executor=True
        ),
        'architect': _create_agent_config(
            'System architecture agent (Ultra Reasoning)',
            f'You are a system architect. {EXECUTOR_PROMPT}\n\nDESIGN AND IMPLEMENTATION: You are expected to both design solutions and implement them using the tools available.\n\nEXECUTION ENVIRONMENT: You are running on a macOS environment with full access to development tools.\n- Use `<<<REMOTE_EXEC>>>` for ALL shell commands (including Xcode tools, Git, file operations).\n- Do NOT distinguish between "server" and "client". Everything runs locally.\n\nGIT AWARENESS: Use Git to understand code changes, history, and context:\n- Use `git log` to understand recent changes and history\n- Use `git diff` to see specific code differences\n- Use `git blame` to identify who made changes and why\n- Use `git show` to examine specific commits\n- Use `git status` to see current state of the repository\n\nFILE SYSTEM NAVIGATION: Use find/grep to locate and analyze relevant files:\n- Use `find` to locate specific file types or patterns\n- Use `grep` to search for specific terms, TODOs, FIXMEs, or error patterns\n- Use `grep -r` for recursive searches across the codebase\n\nXCODE DEVELOPMENT:\n- For Xcode projects, use `xcodebuild`, `xcrun`, `xcodegen` directly via `<<<REMOTE_EXEC>>>`.\n- Create and manage Xcode projects, schemes, targets, and build configurations.\n- Use `xcode-select` to manage Xcode installations.\n- Use `simctl` to manage iOS simulators.\n- Use `codesign` and `security` for code signing and certificates.\n\n{TOOL_REFERENCE}',
            _QWEN_480B_ULTRA,
            executor=True
        ),
        'reviewer': _create_agent_config(
            'Code review agent (High Precision)',
            f'You are a code reviewer. Identify issues and suggest improvements. You are encouraged to provide detailed advice and recommendations.\n\nCOMPREHENSIVE ANALYSIS: When performing code reviews, leverage multiple tools to understand the codebase thoroughly:\n\nGIT AWARENESS: Use Git to understand code changes, history, and context:\n- Use `git log` to understand recent changes and history\n- Use `git diff` to see specific code differences\n- Use `git blame` to identify who made changes and why\n- Use `git show` to examine specific commits\n- Use `git status` to see current state of the repository\n\nFILE SYSTEM NAVIGATION: Use find/grep to locate and analyze relevant files:\n- Use `find` to locate specific file types or patterns\n- Use `grep` to search for specific terms, TODOs, FIXMEs, or error patterns\n- Use `grep -r` for recursive searches across the codebase\n\nCODE COMPARISON: Use diff and other tools to analyze code changes:\n- Use `diff` to compare files and see changes\n- Use `wc`, `head`, `tail` to analyze file contents\n\nAlways use these tools to gather comprehensive context before providing your review. This helps you understand the evolution of code, locate related files, and provide more accurate feedback.\n{GIT_TOOL_REFERENCE}',
            _CODER_30B_HD
        ),
        'debugger': _create_agent_config(
            'Qwen3-Coder-30B-A3B HD',
            f'You are a debugger. {EXECUTOR_PROMPT}\n{TOOL_REFERENCE}',
            _CODER_30B_HD,
            executor=True
        ),
        'metal_implementer': _create_agent_config(
            'Qwen3-Coder-30B-A3B HD',
            f'You are a Metal 4 graphics engineer (compute kernels, mesh shaders, ray tracing, argument buffers). {EXECUTOR_PROMPT}\n\nEXECUTION ENVIRONMENT: You are running on a macOS environment with full access to Metal tools.\n- Use `<<<REMOTE_EXEC>>>` for ALL shell commands.\n- Do NOT distinguish between "server" and "client". Everything runs locally.\n\nMETAL DEVELOPMENT:\n- Metal shader compilation and validation → use `<<<REMOTE_EXEC>>>`\n- Metal framework integration → use `<<<REMOTE_EXEC>>>`\n- Metal performance profiling → use `<<<REMOTE_EXEC>>>`\n\n{TOOL_REFERENCE}',
            _CODER_30B_HD,
            executor=True
        ),
        'lite_architect': _create_agent_config(
            'System architecture agent (Lite Reasoning)',
            f'You are a system architect. {EXECUTOR_PROMPT}\n{TOOL_REFERENCE}',
            _QWEN_480B_LITE,
            executor=True
        ),
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
# Security Dependencies
# ============================================================================

async def verify_admin_key(x_admin_key: Optional[str] = Header(None)):
    """Verify admin API key if ADMIN_API_KEY is configured"""
    if Config.ADMIN_API_KEY:
        if not x_admin_key or x_admin_key != Config.ADMIN_API_KEY:
            raise HTTPException(status_code=401, detail="Invalid or missing admin API key")


# ============================================================================
# Model Manager
# ============================================================================

from collections import OrderedDict

class ModelManager:
    def __init__(self, max_cached_models=3):
        self.models: OrderedDict[str, Any] = OrderedDict()  # Use OrderedDict for LRU behavior
        self.lock = Lock()
        self.inference_lock = Lock()
        self.max_cached_models = max_cached_models  # Maximum number of models to keep cached

    def unload_model(self):
        """Unload all models and free VRAM"""
        with self.lock:
            if self.models:
                logger.info("Unloading models (Cleaning VRAM)...")
                try:
                    # Clear internal references
                    self.models.clear()

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

    def get_model(self, agent_name: str):
        """Get or load the model for the specific agent, using LRU caching"""
        if agent_name not in Config.AGENTS:
            raise ValueError(f"Unknown agent: {agent_name}")

        model_config = Config.AGENTS[agent_name]['model_config']
        model_path = model_config['path']

        with self.lock:
            # Check if model is already cached
            if model_path in self.models:
                # Move to end to mark as most recently used
                model = self.models.pop(model_path)
                self.models[model_path] = model
                logger.info(f"Reusing cached model for {agent_name}")
                return model

            # Evict oldest if cache is full
            while len(self.models) >= self.max_cached_models:
                oldest_path, oldest_model = self.models.popitem(last=False)
                logger.info(f"Evicting oldest model from cache: {oldest_path}")
                del oldest_model

            # Load the model (still inside the lock to prevent race conditions)
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

                # Add to cache
                self.models[model_path] = model
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
        "stop": [CHATML_END, CHATML_START, "<|EOT|>", "<|endoftext|>"],
        "stream": stream,
        "repeat_penalty": 1.15,
        "echo": False
    }


# ============================================================================ 
# Response Builders
# ============================================================================ 

def build_completion_response(model_id: str, text: str, usage: Dict[str, int],
                              finish_reason: str = "stop") -> Dict[str, Any]:
    """Build OpenAI-compatible completion response"""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": text
            },
            "finish_reason": finish_reason
        }],
        "usage": {
            "prompt_tokens": usage['prompt_tokens'],
            "completion_tokens": usage['completion_tokens'],
            "total_tokens": usage['total_tokens']
        }
    }


def build_stream_chunk(completion_id: str, model_id: str, content: Optional[str] = None,
                       finish: bool = False, finish_reason: Optional[str] = None) -> Dict[str, Any]:
    """Build OpenAI-compatible streaming chunk"""
    if finish and not finish_reason:
        finish_reason = "stop"
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model_id,
        "choices": [{
            "index": 0,
            "delta": {"content": content} if content else {},
            "finish_reason": finish_reason if finish else None
        }]
    }


# ============================================================================ 
# FastAPI Application
# ============================================================================ 

model_manager = ModelManager(max_cached_models=3)
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

_cors_origins = os.getenv("CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials="*" not in _cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
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
def save_memory(request: MemoryRequest):
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
def web_search(request: SearchRequest):
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
def apple_deep_docs(request: DeepDocRequest):
    """Perform an Apple Documentation search using the server-side MCP"""
    if not apple_deep_docs_service:
        raise HTTPException(status_code=503, detail="Apple Deep Docs service not initialized")
        
    result = apple_deep_docs_service.call_tool(request.tool, request.arguments)
    return {"result": result}


@app.post("/v1/memory/ingest")
def ingest_memory(request: IngestRequest):
    """Ingest a local PDF file into long-term memory"""
    if not memory_service:
        raise HTTPException(status_code=503, detail="Memory service not initialized")

    # Path security validation
    normalized = os.path.normpath(request.path)
    if '..' in normalized.split(os.sep):
        raise HTTPException(status_code=400, detail="Path traversal not allowed")
    if not os.path.isabs(normalized):
        raise HTTPException(status_code=400, detail="Only absolute paths are allowed")
    if Config.INGEST_ALLOWED_DIR:
        allowed = os.path.normpath(Config.INGEST_ALLOWED_DIR)
        if not normalized.startswith(allowed + os.sep) and normalized != allowed:
            raise HTTPException(status_code=403, detail=f"Path must be under {allowed}")

    result = memory_service.ingest_pdf(normalized)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@app.post("/v1/admin/unload", dependencies=[Depends(verify_admin_key)])
def unload_models():
    """Manually unload all models to free VRAM"""
    with model_manager.inference_lock:
        model_manager.unload_model()
    return {"status": "success", "message": "All models unloaded"}


@app.post("/v1/chat/completions")
def chat_completions(request: ChatCompletionRequest, raw_request: Request):
    """Handle chat completion requests (OpenAI-compatible)"""
    try:
        # Validate model exists
        if request.model not in Config.AGENTS:
            raise HTTPException(
                status_code=404,
                detail=f"Model '{request.model}' not found. Available models: {', '.join(Config.AGENTS.keys())}"
            )

        agent_config = Config.AGENTS[request.model]
        system_prompt = agent_config['system_prompt']

        # Few-shot: inject a fake exchange so executor agents see the correct format
        if agent_config.get('executor') and Config.FEW_SHOT:
            few_shot_msgs = [ChatMessage(role=m['role'], content=m['content']) for m in Config.FEW_SHOT]
            request.messages = few_shot_msgs + list(request.messages)

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

        # Pass components to completion functions - they will inject token budget
        model_path = agent_config['model_config']['path']

        if request.stream:
            return StreamingResponse(
                stream_completion(request.messages, system_prompt, model_path, request.model,
                                  request.max_tokens, request.temperature),
                media_type="text/event-stream"
            )
        else:
            return sync_completion(request.messages, system_prompt, model_path, request.model,
                                   request.max_tokens, request.temperature)

    except HTTPException:
        raise
    except FileNotFoundError as e:
        logger.error("Model file error: %s", e)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error("Error in chat_completions: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def sync_completion(messages: List[ChatMessage], system_prompt: str, model_path: str,
                    model_id: str, max_tokens: int, temperature: float) -> Dict[str, Any]:
    """Generate synchronous completion with token budget awareness"""
    with model_manager.inference_lock:
        try:
            model = model_manager.get_model(model_id)
            n_ctx = model.n_ctx()

            # Estimate the token count for the budget guidance string itself
            budget_guidance_template = Config.TOKEN_BUDGET_GUIDANCE.format(available_tokens=1000)  # Placeholder value
            budget_guidance_tokens = len(model.tokenize(budget_guidance_template.encode("utf-8")))

            # Build prompt without budget guidance to get the base token count
            preliminary_prompt = build_model_prompt(messages, system_prompt, model_path)
            preliminary_tokens = model.tokenize(preliminary_prompt.encode("utf-8"))
            n_preliminary = len(preliminary_tokens)

            # Calculate available tokens accounting for budget guidance overhead
            available = n_ctx - n_preliminary - budget_guidance_tokens
            if available < 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"Prompt ({n_preliminary} tokens) fills the entire context window ({n_ctx}). "
                           "Reduce conversation history and retry."
                )

            # Clamp to requested max_tokens
            clamped_max = min(max_tokens, available)

            # Now build the final prompt with actual budget guidance
            budget_guidance = Config.TOKEN_BUDGET_GUIDANCE.format(available_tokens=clamped_max)
            augmented_system_prompt = f"{system_prompt}\n{budget_guidance}"
            prompt = build_model_prompt(messages, augmented_system_prompt, model_path)

            # Final token count for logging
            final_tokens = model.tokenize(prompt.encode("utf-8"))
            n_prompt = len(final_tokens)
            final_available = n_ctx - n_prompt
            clamped_max = min(max_tokens, final_available)

            if clamped_max < max_tokens:
                logger.info(
                    "Clamped max_tokens %d -> %d for %s (prompt=%d, n_ctx=%d)",
                    max_tokens, clamped_max, model_id, n_prompt, n_ctx
                )

            logger.info(
                "Token budget injected for %s: budget=%d tokens communicated to model",
                model_id, clamped_max
            )

            params = get_model_params(clamped_max, temperature, stream=False)
            response = model(prompt, **params)

            text = response['choices'][0]['text'].strip()

            # Extract real finish_reason from llama-cpp response
            finish_reason = response['choices'][0].get('finish_reason', 'stop')
            if not finish_reason:
                finish_reason = 'stop'

            return build_completion_response(model_id, text, response['usage'],
                                             finish_reason=finish_reason)
        except Exception:
            raise


def stream_completion(messages: List[ChatMessage], system_prompt: str, model_path: str,
                      model_id: str, max_tokens: int, temperature: float) -> Iterator[str]:
    """Generate streaming completion with token budget awareness.

    The inference_lock is held for the full duration of streaming intentionally.
    llama-cpp-python is not thread-safe, so concurrent inference on the same
    model instance would cause undefined behavior or crashes.
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    finish_reason = "stop"

    try:
        with model_manager.inference_lock:
            model = model_manager.get_model(model_id)
            n_ctx = model.n_ctx()

            # Estimate the token count for the budget guidance string itself
            budget_guidance_template = Config.TOKEN_BUDGET_GUIDANCE.format(available_tokens=1000)  # Placeholder value
            budget_guidance_tokens = len(model.tokenize(budget_guidance_template.encode("utf-8")))

            # Build prompt without budget guidance to get the base token count
            preliminary_prompt = build_model_prompt(messages, system_prompt, model_path)
            preliminary_tokens = model.tokenize(preliminary_prompt.encode("utf-8"))
            n_preliminary = len(preliminary_tokens)

            # Calculate available tokens accounting for budget guidance overhead
            available = n_ctx - n_preliminary - budget_guidance_tokens
            if available < 1:
                error_chunk = {
                    "error": {
                        "message": f"Prompt ({n_preliminary} tokens) fills the entire context window "
                                   f"({n_ctx}). Reduce conversation history and retry.",
                        "type": "context_length_exceeded"
                    }
                }
                yield f"data: {json.dumps(error_chunk)}\n\n"
                return

            # Clamp to requested max_tokens
            clamped_max = min(max_tokens, available)

            # Now build the final prompt with actual budget guidance
            budget_guidance = Config.TOKEN_BUDGET_GUIDANCE.format(available_tokens=clamped_max)
            augmented_system_prompt = f"{system_prompt}\n{budget_guidance}"
            prompt = build_model_prompt(messages, augmented_system_prompt, model_path)

            # Final token count for logging
            final_tokens = model.tokenize(prompt.encode("utf-8"))
            n_prompt = len(final_tokens)
            final_available = n_ctx - n_prompt
            clamped_max = min(max_tokens, final_available)

            if clamped_max < max_tokens:
                logger.info(
                    "Clamped max_tokens %d -> %d for %s (prompt=%d, n_ctx=%d)",
                    max_tokens, clamped_max, model_id, n_prompt, n_ctx
                )

            logger.info(
                "Token budget injected for %s: budget=%d tokens communicated to model",
                model_id, clamped_max
            )

            params = get_model_params(clamped_max, temperature, stream=True)
            token_count = 0

            for output in model(prompt, **params):
                if 'choices' in output and len(output['choices']) > 0:
                    choice = output['choices'][0]
                    token = choice.get('text', '')
                    if token:
                        token_count += 1
                        chunk = build_stream_chunk(completion_id, model_id, content=token)
                        yield f"data: {json.dumps(chunk)}\n\n"
                    # Capture finish_reason from the last chunk llama-cpp emits
                    if choice.get('finish_reason'):
                        finish_reason = choice['finish_reason']

            # If llama-cpp didn't set a finish_reason but we hit the token limit,
            # infer "length" so the client knows the response was truncated
            if finish_reason == "stop" and token_count >= clamped_max:
                finish_reason = "length"

            # Always log completion stats for debugging truncation issues
            logger.info(
                "Completion stats for %s: prompt=%d, available=%d, clamped_max=%d, "
                "generated=%d, finish_reason=%s",
                model_id, n_prompt, final_available, clamped_max, token_count, finish_reason
            )

        final_chunk = build_stream_chunk(completion_id, model_id, finish=True,
                                         finish_reason=finish_reason)
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield "data: [DONE]\n\n"

    except Exception as e:
        logger.error("Error in stream_completion: %s", e, exc_info=True)
        error_chunk = {"error": {"message": str(e), "type": "server_error"}}
        yield f"data: {json.dumps(error_chunk)}\n\n"


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