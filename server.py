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
import gc
import hmac
import re
import asyncio
from datetime import datetime
from typing import Dict, List, Optional, Any, Literal, Iterator
from threading import Lock, Thread
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Header, Depends
from fastapi.responses import JSONResponse, StreamingResponse
import signal
import requests as http_requests
import llama_cpp
from llama_cpp import Llama
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from memory_service import MemoryService
from web_search_service import WebSearchService
from server_manager import AppleDeepDocsService

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
    text: str = Field(..., max_length=200_000)  # ~100KB, matches client file-size cap
    source: Optional[str] = None  # file path hint for language detection


class SearchRequest(BaseModel):
    """Request to search the web"""
    query: str


class IngestRequest(BaseModel):
    """Request to ingest a local file"""
    path: str


# ============================================================================
# Helper Functions
# ============================================================================

def _create_model_config(path_env, path_default, n_gpu_layers, n_ctx=32768, n_batch=2048, backend='llama_cpp',
                         server_extra_args=None, logit_bias=None, yarn=False, type_k=8, type_v=8,
                         repeat_penalty=1.15, repeat_last_n=256, cpu_moe=False, n_ubatch=512):
    """Helper function to create standardized model configurations.

    Args:
        yarn: If True, include YaRN RoPE scaling parameters. Only needed for models
              using extended context via YaRN (e.g. 480B Ultra with 2x scaling).
        type_k: GGML type for KV cache keys (8=Q8_0, 2=Q4_0). Default Q8_0.
        type_v: GGML type for KV cache values (8=Q8_0, 2=Q4_0). Default Q8_0.
        repeat_penalty: Penalizes repeated tokens (1.0=off). Lower values help code generation.
        repeat_last_n: Window of recent tokens to apply repeat penalty to (256=windowed, -1=full context).
        cpu_moe: Keep MoE expert weights on CPU (llama_server only). Allows more attention layers on GPU.
        n_ubatch: Physical micro-batch size for prompt processing (default 512).
    """
    config = {
        'path': os.getenv(path_env, path_default),
        'n_gpu_layers': n_gpu_layers,
        'n_ctx': n_ctx,
        'n_batch': n_batch,
        'n_ubatch': n_ubatch,
        'type_k': type_k, 'type_v': type_v, 'offload_kqv': True,
        'backend': backend,
        'repeat_penalty': repeat_penalty,
        'repeat_last_n': repeat_last_n,
        'cpu_moe': cpu_moe,
    }
    if yarn:
        config.update({
            'rope_scaling_type': 2,
            'rope_freq_scale': 1.0,
            'yarn_ext_factor': -1.0,
            'yarn_attn_factor': 1.0,
            'yarn_beta_fast': 32.0,
            'yarn_beta_slow': 1.0,
            'yarn_orig_ctx': 32768,
        })
    if server_extra_args is not None:
        config['server_extra_args'] = server_extra_args
    if logit_bias is not None:
        config['logit_bias'] = logit_bias
    return config



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
    # 24 = physical core count (8 P-cores + 16 E-cores); hyperthreads hurt decode
    DEFAULT_N_THREADS = int(os.getenv('MODEL_N_THREADS', 24))
    # Prefill (batch) benefits from hyperthreads — use all 32 threads
    DEFAULT_N_THREADS_BATCH = int(os.getenv('MODEL_N_THREADS_BATCH', 32))
    DEFAULT_N_BATCH = int(os.getenv('MODEL_N_BATCH', 2048))  # Reverted to 2048 to prevent OOM
    
    # ── Unified tool reference ──
    BASE_TOOLS = [
        "<<<REMOTE_EXEC>>>command                          — run a shell command (Linux/macOS compatible)",
        "<<<READ_FILE>>>path                               — read file content (safe, fast)",
        "<<<WRITE_FILE>>>path\\ncontent                     — write content to file (first line = path, rest = content)",
        "<<<EDIT_FILE>>>path\\n<<<OLD>>>\\nold text\\n<<<NEW>>>\\nnew text  — surgical edit: find and replace text in file",
        "<<<LIST_DIR>>>path                                — list directory contents with sizes and dates",
        "<<<GLOB>>>pattern                                 — find files matching pattern (e.g., **/*.swift, src/*.py)",
        "<<<GREP>>>pattern|path|options                    — search file contents (options: i=ignore case)",
        "<<<SAVE_MEMORY>>>fact                             — persist a fact",
        "<<<WEB_SEARCH>>>query                             — web search",
        "<<<CUPERTINO>>>query                              — Apple docs (local MCP)",
        '<<<APPLE_DEEP_DOCS>>>{"tool":"NAME","arguments":{}}  — Apple docs (server MCP)',
        "<<<INGEST_PDF>>>path                              — ingest a PDF file into memory (supports local: prefix for client files)",
        "<<<SCRATCHPAD>>>                                  — update your working memory (FACTS, OPEN_QUESTIONS, DEAD_ENDS)",
        "<<<PLAN>>>                                        — create/update your retrieval plan (GOAL, STEPS with [x]/[ ], CURRENT)",
        "<<<CONFIDENCE>>>N                                 — report confidence 0-100 in your current information",
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
        "\n<<<REMOTE_EXEC>>>git status                      — check current repository state" +
        "\n<<<REMOTE_EXEC>>>git log --oneline -10           — view recent commit history" +
        "\n<<<REMOTE_EXEC>>>git diff                        — see current uncommitted changes" +
        "\n<<<REMOTE_EXEC>>>git diff --cached               — see staged changes" +
        "\n<<<REMOTE_EXEC>>>git diff HEAD~1                 — compare working directory to last commit" +
        "\n<<<REMOTE_EXEC>>>git show HEAD                   — show details of last commit" +
        "\n<<<REMOTE_EXEC>>>git blame filename              — see who made changes to each line" +
        "\n<<<REMOTE_EXEC>>>git log -p --follow filepath    — see history of changes to a specific file" +
        "\n<<<REMOTE_EXEC>>>git diff HEAD~3 HEAD            — compare changes between commits" +
        "\n<<<REMOTE_EXEC>>>git log --author=\"Author Name\" --since=\"2 weeks ago\"  — find commits by author/time" +
        "\n\n# File system navigation and search:" +
        "\n<<<REMOTE_EXEC>>>find . -name \"*.py\" -type f     — find all Python files" +
        "\n<<<REMOTE_EXEC>>>find . -name \"*.js\" -o -name \"*.ts\"  — find JavaScript/TypeScript files" +
        "\n<<<REMOTE_EXEC>>>find . -name \"*.java\" -o -name \"*.cpp\" -o -name \"*.h\"  — find source files" +
        "\n<<<REMOTE_EXEC>>>find . -name \"*test*\" -o -name \"*spec*\"  — find test files" +
        "\n<<<REMOTE_EXEC>>>find . -name \"*.md\" -o -name \"*.txt\"  — find documentation files" +
        "\n<<<REMOTE_EXEC>>>find . -size +1M -name \"*.log\"  — find large log files" +
        "\n<<<REMOTE_EXEC>>>grep -r \"TODO|FIXME|HACK\" .    — find code comments indicating work to do" +
        "\n<<<REMOTE_EXEC>>>grep -rn \"error\" .              — find error mentions in code" +
        "\n<<<REMOTE_EXEC>>>grep -rn \"DEBUG|debug|console.log\" .  — find debug statements" +
        "\n\n# Code analysis and comparison:" +
        "\n<<<REMOTE_EXEC>>>diff file1 file2                — compare two files" +
        "\n<<<REMOTE_EXEC>>>diff -u old_file new_file       — unified diff format" +
        "\n<<<REMOTE_EXEC>>>wc -l filename                  — count lines in file" +
        "\n<<<REMOTE_EXEC>>>head -20 filename               — show first 20 lines" +
        "\n<<<REMOTE_EXEC>>>tail -20 filename               — show last 20 lines" +
        "\n<<<REMOTE_EXEC>>>sort filename                   — sort file contents" +
        "\n<<<REMOTE_EXEC>>>uniq -c filename                — count unique lines" +
        "\n<<<REMOTE_EXEC>>>stat filename                   — detailed file information"
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

    # ── macOS development toolkit (injected into EXECUTOR_PROMPT) ──
    MACOS_TOOLKIT = """
MACOS DEVELOPMENT TOOLKIT — Available via `<<<REMOTE_EXEC>>>`:
You are running on macOS with FULL local access. You CAN and SHOULD write and execute scripts.

SCRIPTING RUNTIMES — Write scripts to files and execute them for complex tasks:
- Python 3: `python3 script.py` — data processing, API calls, complex logic, automation
- Node.js: `node script.js` or `npx <package>` — JS scripting, ad-hoc npm packages
- Swift: `swift script.swift` — Apple-native scripting, no Xcode project needed
- Ruby: `ruby script.rb` — quick scripting
- Perl: `perl -e '...'` — powerful one-liners, regex processing

DATA PROCESSING — Transform, query, and convert data:
- jq: `jq '.key' file.json` — parse/transform JSON (use this for ALL JSON manipulation)
- xmllint: `xmllint --xpath '//tag' file.xml` — parse/query XML and HTML
- awk: `awk '{print $2}' file` — columnar data extraction, field processing
- sqlite3: `sqlite3 db.sqlite 'SELECT ...'` — query any SQLite database directly
- plutil: `plutil -convert json file.plist -o -` — convert plists to/from JSON/XML
- textutil: `textutil -convert txt file.docx` — convert between doc formats (html, rtf, txt, docx)

macOS-SPECIFIC POWER TOOLS:
- mdfind: `mdfind -name "file"` or `mdfind "content"` — Spotlight search (extremely fast)
- mdls: `mdls file` — rich file metadata (dimensions, author, dates, etc.)
- sips: `sips -z 100 100 img.png` — resize/convert/rotate images (no ImageMagick needed)
- pbcopy/pbpaste: pipe to/from clipboard
- osascript: `osascript -e 'tell app "Finder" to ...'` — automate any macOS app
- open: `open file.pdf` or `open -a Safari url` — open files/URLs in apps
- defaults: `defaults read com.apple.finder` — read/write macOS preferences

MEDIA & PDF (Homebrew):
- ffmpeg: audio/video processing, conversion, extraction
- pdftotext/pdfinfo (poppler): extract text from PDFs, get PDF metadata

BUILD & COMPILATION:
- make / cmake: build automation
- clang / clang++: C/C++ compilation
- swiftc: Swift compilation
- xcodebuild / xcrun: full Xcode CLI toolchain
- gh: GitHub CLI (issues, PRs, releases, API)

NETWORKING:
- curl: HTTP requests, API calls, downloads
- wget: file downloads with resume support

BINARY INSPECTION:
- otool -L: list linked libraries (like ldd)
- nm: list symbols in object files
- lipo -info: inspect universal binary architectures
- file: identify file types

IMPORTANT: Do NOT hesitate to write a Python/Node/Swift script when the task is complex.
A 20-line Python script is often better than a long chain of shell commands."""

    # ── Behavioral instruction for action-oriented agents ──
    EXECUTOR_PROMPT = """You execute tasks by running commands and writing files.

TOOL SYNTAX: Each tool is a single opening tag. Content runs until the next tool tag.
No closing tags. Just open the next tool (or end your response) to terminate the previous block.

NAVIGATION & SEARCH:
<<<LIST_DIR>>>path
<<<GLOB>>>**/*.swift
<<<GREP>>>pattern|path
<<<READ_FILE>>>path

FILE MODIFICATION:
<<<WRITE_FILE>>>path
content

<<<EDIT_FILE>>>path
<<<OLD>>>
existing code to find
<<<NEW>>>
replacement code

Rules:
- Every response MUST contain at least one tool block
- For NEW files: use <<<WRITE_FILE>>>
- For EXISTING files: prefer <<<EDIT_FILE>>> for targeted changes (safer, shows intent clearly)
- Use <<<WRITE_FILE>>> for existing files only when rewriting most of the file
- NEVER just output code in markdown blocks - that does NOT save anything!
- NEVER use <<<REMOTE_EXEC>>> with Python/sed/awk to modify files. ALWAYS use <<<EDIT_FILE>>> or <<<WRITE_FILE>>> instead. Shell-based file edits bypass safety checks, produce no diff preview, and break checkpoint/undo.
- Use <<<GLOB>>> and <<<GREP>>> to find files instead of shell find/grep (faster, cleaner output)
- After writing/editing files, use <<<REMOTE_EXEC>>> to compile/build and verify changes work
- Reserve <<<REMOTE_EXEC>>> for: builds, tests, git commands, and read-only inspection. NOT for file modification.
- Never ask for permission. You have full file access.
- Never claim you cannot run commands or write files. You can.

CONTEXT MANAGEMENT — your context window is limited. Work efficiently:
- Work FILE-BY-FILE: read a file, modify it, verify it, then move to the next.
  Do NOT read all files before starting work.
- After reading a file, save key findings with <<<SAVE_MEMORY>>> before moving on.
  This lets you drop the raw content from context while retaining what matters.
- Prefer <<<GREP>>> over <<<READ_FILE>>> when you only need to find specific content.

WORKING MEMORY — track your progress across tool calls:
<<<SCRATCHPAD>>>
FACTS:
- list key findings here
OPEN_QUESTIONS:
- what you still need to find
DEAD_ENDS:
- approaches that didn't work

<<<PLAN>>>
GOAL: What you're trying to accomplish
STEPS:
1. [ ] First step
2. [ ] Second step
CURRENT: 1

<<<CONFIDENCE>>>N
Report your confidence (0-100) that you have enough information to answer.
Update these after each retrieval step. They help you stay organized and efficient.
""" + MACOS_TOOLKIT

    # ── Shared model configs ──
    # Turbo: Speed-optimized implementer on RTX 5080
    # Q4_0 KV cache halves cache VRAM vs Q8_0. Bumped ctx 82K→131K (native max).
    # ngl=30 at 131K Q4_0: 1,475 MiB free (measured 2026-03-30)
    _CODER_30B_TURBO = _create_model_config(
        'MODEL_PATH_30B_TURBO',
        '/home/keith-merrill/.lmstudio/models/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf',
        30, 131072, 2048, type_k=2, type_v=2
    )

    # FAST: Lightweight Q4_K_M for quick implementation tasks (256k native context, moderate GPU)
    # Alternative to the 80B Next model when speed matters more than quality
    # Q4_0 KV cache is REQUIRED — Q8_0 OOMs at ngl≥22 with 262K context
    # ngl=26: 883 MiB free (measured 2026-03-30) | ngl=27: tight | ngl=28: OOM
    _CODER_30B_FAST = _create_model_config(
        'MODEL_PATH_30B_FAST',
        '/home/keith-merrill/.lmstudio/models/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf',
        26, 262144, 1024, type_k=2, type_v=2
    )

    # NEXT: Qwen3-Coder-Next-Q8_0 (80B MoE with 3B active params)
    # Very smart but runs mostly on system RAM (slow). Native 256k context enabled.
    # ngl=48 (--cpu-moe): 8,304 MiB free. All 48 attention layers on GPU.
    # --swa-full enables prompt cache reuse (avoids full re-prefill each turn).
    # n_batch/n_ubatch=4096 for faster prefill (8 GB headroom supports large batches).
    _CODER_NEXT_Q8 = _create_model_config(
        'MODEL_PATH_NEXT_Q8',
        '/home/keith-merrill/.lmstudio/models/unsloth/Qwen3-Coder-Next-GGUF/Q8_0/Qwen3-Coder-Next-Q8_0-00001-of-00003.gguf',
        48, 262144, 4096, backend='llama_server',
        server_extra_args=['--chat-template', 'chatml', '--swa-full'],
        logit_bias=[[151657, -100.0], [151658, -100.0]],
        cpu_moe=True, n_ubatch=4096,
    )

    # HD: High-precision Q8_0 weights with Q4_0 KV cache for reviews
    # Q8_0 weights are ~2x Q4_K_M per layer — ngl=21 + 82K ctx OOMs.
    # Reduced to 65K ctx to fit. Q4_0 KV cache keeps VRAM manageable.
    _CODER_30B_HD = _create_model_config(
        'MODEL_PATH_30B_HD',
        '/home/keith-merrill/.lmstudio/models/lmstudio-community/Qwen3-Coder-30B-A3B-Instruct-GGUF/Qwen3-Coder-30B-A3B-Instruct-Q8_0.gguf',
        21, 65536, 2048, type_k=2, type_v=2
    )

    # Lite: Faster reasoning on system RAM (32k native context, no YaRN — IQ1_M too
    # aggressively quantized for reliable extended-context output)
    _QWEN_480B_LITE = _create_model_config(
        'MODEL_PATH_480B_LITE',
        '/home/keith-merrill/.lmstudio/models/unsloth/Qwen3-Coder-480B-A35B-Instruct-GGUF/Qwen3-Coder-480B-A35B-Instruct-UD-IQ1_M.gguf',
        4, 32768, 1024
    )

    # Ultra: Premium reasoning using Q2_K_XL on 192GB RAM (64k context via YaRN 2x scaling)
    _QWEN_480B_ULTRA = _create_model_config(
        'MODEL_PATH_480B_ULTRA',
        '/home/keith-merrill/.lmstudio/models/unsloth/Qwen3-Coder-480B-A35B-Instruct-GGUF/Qwen3-Coder-480B-A35B-Instruct-UD-Q2_K_XL-00001-of-00004.gguf',
        4, 65536, 1024, yarn=True
    )

    # MINIMAX: MiniMax M2.5 (230B MoE, 10B active params, 62 layers, ~1,760 MiB/layer)
    # Uses llama-server subprocess backend with native Jinja template
    # ngl=4 at 32K Q8_0: 6,207 MiB free (measured 2026-03-30)
    # ngl=6 at 65K Q4_0: testing (est. ~2,500 MiB free)
    # ngl=6 (no --cpu-moe): 6,207 MiB free | ngl=6 (--cpu-moe): 12,665 MiB free
    # 62 attention layers total. With --cpu-moe, targeting ngl=62 (all layers).
    # KV at 65K Q4_0: 4,392 MiB (62 GPU layers). 7,188 MiB free at ngl=62.
    # Bumping to 98K Q4_0: ~6,588 MiB KV → ~1 GB free. Tight but fits.
    # Q5_0 cache OOM at ngl=62 (10 GB compute buffer). Staying at Q4_0.
    # MiniMax has less headroom (4.8 GB) — use 2048 ubatch (conservative)
    _MINIMAX_M25 = _create_model_config(
        'MODEL_PATH_MINIMAX_M25',
        '/home/keith-merrill/.lmstudio/models/unsloth/MiniMax-M2.5-GGUF/Q4_K_M/MiniMax-M2.5-Q4_K_M-00001-of-00004.gguf',
        62, 118784, 4096, backend='llama_server', n_ubatch=4096,
        server_extra_args=['--jinja', '--reasoning-format', 'none'],
        logit_bias=[[200052, -100.0], [200053, -100.0]],
        type_k=2, type_v=2,
        cpu_moe=True,
    )

    # ── Qwen3.5 family ──

    # Qwen3.5-35B-A3B Q4_K_M — successor to Coder-30B, same 3B active MoE
    # 22 GB model. Qwen3.5 arch supported since llama-cpp-python 0.3.17 (in-process).
    # 262K native context with Q4_0 KV cache. Reduced ngl from 24 to 22 for 262K headroom.
    # ngl=24 at 131K: 1,619 MiB free | ngl=22 at 262K: testing (measured 2026-04-01)
    _QWEN35_35B = _create_model_config(
        'MODEL_PATH_QWEN35_35B',
        '/home/keith-merrill/.lmstudio/models/unsloth/Qwen3.5-35B-A3B-GGUF/Qwen3.5-35B-A3B-Q4_K_M.gguf',
        22, 262144, 1024,
        type_k=2, type_v=2,
        repeat_penalty=1.05,  # Lower penalty for code generation — 1.15 caused premature EOS on large files
    )

    # Qwen3.5-122B-A10B Q4_K_M — mid-tier MoE (10B active, 76.5 GB, 3 shards)
    # Strong agentic/function-calling (72.2 BFCL-V4). Mostly CPU, limited GPU layers.
    # Qwen3.5 arch supported since llama-cpp-python 0.3.17 (in-process).
    # 131K native context, using 65K to leave headroom.
    # ngl=9 at 65K: 1,209 MiB free (measured 2026-03-30, ~1,507 MiB/layer)
    _QWEN35_122B = _create_model_config(
        'MODEL_PATH_QWEN35_122B',
        '/home/keith-merrill/.lmstudio/models/unsloth/Qwen3.5-122B-A10B-GGUF/Q4_K_M/Qwen3.5-122B-A10B-Q4_K_M-00001-of-00003.gguf',
        9, 65536, 1024,
    )

    # Qwen3.5-397B-A17B IQ1_M — flagship (17B active, ~100 GB, 4 shards, 60 layers)
    # Successor to 480B Coder as premium architect — DESIGN ROLE, not implementation.
    # Moved to llama_server with --cpu-moe: expert weights on CPU, attention on GPU.
    # ngl=8 in-process: ~5 min/turn (prefill-bound). With --cpu-moe, targeting ngl=60 (all layers).
    _QWEN35_397B = _create_model_config(
        'MODEL_PATH_QWEN35_397B',
        '/home/keith-merrill/.lmstudio/models/unsloth/Qwen3.5-397B-A17B-GGUF/UD-IQ1_M/Qwen3.5-397B-A17B-UD-IQ1_M-00001-of-00004.gguf',
        61, 98304, 4096, backend='llama_server',
        server_extra_args=['--jinja', '--reasoning-format', 'none', '-fa', 'on'],
        type_k=8, type_v=2,
        cpu_moe=True, n_ubatch=4096,
    )

    # ── Non-Qwen models ──

    # Nemotron-3-Nano-30B-A3B Q4_K_M — NVIDIA hybrid Mamba-Transformer MoE
    # 3.5B active, 24.6 GB. Needs llama_server (nemotron_h_moe arch not in llama-cpp-python).
    # 32K native context. ~3.3x throughput vs Qwen3-30B on same hardware.
    # ngl=28 (no --cpu-moe): 1,964 MiB free | ngl=28 (--cpu-moe): 12,840 MiB free
    # 52 attention layers total. With --cpu-moe, targeting ngl=52 (all layers).
    # Mamba-hybrid: only 6/52 layers use KV cache (rest are recurrent — no KV needed).
    # KV at 1M Q8_0: ~3,264 MiB → ~8.6 GB free. Full 1M native context fits easily.
    _NEMOTRON_NANO = _create_model_config(
        'MODEL_PATH_NEMOTRON_NANO',
        '/home/keith-merrill/.lmstudio/models/unsloth/Nemotron-3-Nano-30B-A3B-GGUF/Nemotron-3-Nano-30B-A3B-Q4_K_M.gguf',
        52, 1048576, 1024, backend='llama_server',
        server_extra_args=['--jinja', '--reasoning-format', 'none'],
        cpu_moe=True, n_ubatch=1024,
    )

    # GLM-4.7-Flash Q4_K_M — Zhipu AI 30B-A3B MoE, 18.3 GB
    # Uses llama_server for proper glm4 template handling. 128K native context.
    # Q4_0 cache at 82K ctx. Smallest model — can push most GPU layers.
    # ngl=34 (no --cpu-moe): 884 MiB free | ngl=34 (--cpu-moe): 12,652 MiB free
    # 47 attention layers total. With --cpu-moe, targeting ngl=47 (all layers).
    # KV at 82K Q4_0: 1,164 MiB (47 GPU layers). 12,228 MiB free at ngl=47.
    # Bumping to 262K Q4_0: ~3,713 MiB KV → ~8.5 GB free. Fits easily.
    # KV cache upgraded Q4_0→Q8_0 (9 GB free at Q4_0 — plenty for 2x cache size)
    _GLM47_FLASH = _create_model_config(
        'MODEL_PATH_GLM47_FLASH',
        '/home/keith-merrill/.lmstudio/models/unsloth/GLM-4.7-Flash-GGUF/GLM-4.7-Flash-Q4_K_M.gguf',
        47, 262144, 2048, backend='llama_server',
        server_extra_args=['--jinja', '--reasoning-format', 'none'],
        n_ubatch=2048,
        cpu_moe=True,
    )

    # ── Few-shot example injected for executor agents ──
    # The model sees this as a real prior exchange, so it copies the format.
    FEW_SHOT = [
        {"role": "user", "content": "List the Python files in this project."},
        {"role": "assistant", "content": "<<<GLOB>>>**/*.py"},
        {"role": "user", "content": "Find where the login function is defined."},
        {"role": "assistant", "content": "<<<GREP>>>def login|."},
        {"role": "user", "content": "Create a hello world Swift file."},
        {"role": "assistant", "content": "<<<WRITE_FILE>>>/tmp/hello.swift\nimport Foundation\n\nprint(\"Hello, World!\")\n\nNow let me verify it compiles:\n<<<REMOTE_EXEC>>>swiftc /tmp/hello.swift -o /tmp/hello && /tmp/hello"},
        {"role": "user", "content": "Change the greeting from Hello to Hi in that file."},
        {"role": "assistant", "content": "<<<EDIT_FILE>>>/tmp/hello.swift\n<<<OLD>>>\nprint(\"Hello, World!\")\n<<<NEW>>>\nprint(\"Hi, World!\")"},
        {"role": "user", "content": "Parse the API response in /tmp/response.json and extract all user emails."},
        {"role": "assistant", "content": "<<<REMOTE_EXEC>>>jq '[.users[].email]' /tmp/response.json"},
        {"role": "user", "content": "Write a script to find all Swift files over 500 lines and report their line counts."},
        {"role": "assistant", "content": "<<<WRITE_FILE>>>/tmp/long_files.py\nimport subprocess, os\n\nresult = subprocess.run(['find', '.', '-name', '*.swift', '-type', 'f'], capture_output=True, text=True)\nfor path in sorted(result.stdout.strip().split('\\n')):\n    if path:\n        with open(path) as f:\n            count = sum(1 for _ in f)\n        if count > 500:\n            print(f'{count:>6} {path}')\n\n<<<REMOTE_EXEC>>>python3 /tmp/long_files.py"},
        {"role": "user", "content": "Resize all PNG images in assets/ to 512x512."},
        {"role": "assistant", "content": "Let me find the PNGs first:\n<<<GLOB>>>assets/**/*.png\n\nNow I'll resize them using macOS sips:\n<<<REMOTE_EXEC>>>for f in assets/*.png; do sips -z 512 512 \"$f\"; done"},
        {"role": "user", "content": "Find and fix the bug causing 500 errors on the /api/chat endpoint."},
        {"role": "assistant", "content": "<<<PLAN>>>\nGOAL: Find and fix 500 error on /api/chat\nSTEPS:\n1. [ ] Find the endpoint handler\n2. [ ] Trace the error path\n3. [ ] Identify the root cause\n4. [ ] Apply fix and verify\nCURRENT: 1\n\n<<<CONFIDENCE>>>10\n\nLet me start by finding the endpoint:\n<<<GREP>>>/api/chat|."},
    ]

    # ── Shared agent prompts ──
    _IMPLEMENTER_SYSTEM_PROMPT = (
        f'You are an implementer. {EXECUTOR_PROMPT}\n\nCOMPREHENSIVE IMPLEMENTATION: When implementing tasks, leverage multiple tools to understand the codebase thoroughly:\n\nEXECUTION ENVIRONMENT: You are running on a macOS environment with full access to development tools.\n- Use `<<<REMOTE_EXEC>>>` for ALL shell commands (including Xcode tools, Git, file operations).\n- Do NOT distinguish between "server" and "client". Everything runs locally.\n\nFILE OPERATIONS:\n- Use `<<<GLOB>>>` to find files: `<<<GLOB>>>**/*.swift`\n- Use `<<<GREP>>>` to search code: `<<<GREP>>>TODO|src/`\n- Use `<<<LIST_DIR>>>` to explore directories\n- Use `<<<READ_FILE>>>` to read file contents\n- Use `<<<WRITE_FILE>>>` for new files or complete rewrites\n- Use `<<<EDIT_FILE>>>` for targeted changes to existing files (PREFERRED)\n\nGIT AWARENESS: Use Git via `<<<REMOTE_EXEC>>>` to understand code context:\n- `git log`, `git diff`, `git blame`, `git show`, `git status`\n\nAPPLE DEVELOPMENT via `<<<REMOTE_EXEC>>>`:\n- Compile Swift: `swiftc file.swift -o output`\n- Compile Metal: `xcrun -sdk macosx metal -c shader.metal -o shader.air`\n- Build Xcode: `xcodebuild -project Foo.xcodeproj -scheme Foo build`\n\n{TOOL_REFERENCE}'
    )

    _ARCHITECT_SYSTEM_PROMPT = (
        f'You are a system architect. {EXECUTOR_PROMPT}\n\n'
        'ROLE: You DESIGN systems and PLAN implementations. You do NOT write large amounts '
        'of code yourself. Your job is to:\n'
        '1. Understand the codebase by reading files, searching, and exploring\n'
        '2. Design the architecture, interfaces, and file structure\n'
        '3. Write a clear, actionable implementation plan\n'
        '4. Create small scaffolding files (configs, interfaces, stubs) if helpful\n'
        '5. Delegate the bulk implementation to an implementer agent\n\n'
        'WHAT YOU SHOULD DO:\n'
        '- Use <<<GLOB>>>, <<<GREP>>>, <<<READ_FILE>>>, <<<LIST_DIR>>> extensively to understand the codebase\n'
        '- Use <<<REMOTE_EXEC>>> to run git log, git diff, git blame for context\n'
        '- Use <<<SAVE_MEMORY>>> to record key findings and design decisions\n'
        '- Write short config files, interface definitions, or type stubs with <<<WRITE_FILE>>>\n'
        '- Write documentation (README, ARCHITECTURE.md, ADRs) with <<<WRITE_FILE>>>\n'
        '- Output a structured implementation plan as your final deliverable\n\n'
        'WHAT YOU SHOULD NOT DO:\n'
        '- Do NOT write large source files (>50 lines). Delegate to an implementer.\n'
        '- Do NOT enter build/test/fix loops. That is implementer work.\n'
        '- Do NOT rewrite files repeatedly. Write once or delegate.\n'
        '- Do NOT run xcodebuild, compilers, or test suites. Leave verification to implementers.\n\n'
        'EDIT_FILE FORMAT (for small targeted edits only):\n'
        '<<<EDIT_FILE>>>path\n<<<OLD>>>\nexact text to find\n<<<NEW>>>\nreplacement text\n\n'
        'WARNING: Do NOT use git-style markers like <<<<<<< SEARCH or ======= or >>>>>>> REPLACE. '
        'Use <<<OLD>>> and <<<NEW>>> only.\n\n'
        f'{TOOL_REFERENCE}'
    )

    # MiniMax M2.5 generates garbled Unicode (triple-encoded U+FFFD from training
    # data corruption). Force ASCII-only diagrams to prevent hallucinated mojibake.
    _MINIMAX_UNICODE_GUARD = (
        '\n\nCRITICAL — FORMATTING RULE: NEVER use Unicode box-drawing characters '
        '(├, └, │, ─, etc.) or Unicode symbols in your output. They WILL render as '
        'garbled text. Use ONLY plain ASCII for diagrams and trees:\n'
        '  +-- for branches\n'
        '  |   for vertical lines\n'
        '  `-- for last items\n'
        'For architecture diagrams, prefer Mermaid syntax in code blocks.\n'
    )

    # ── Agent definitions ──
    # 'executor': True means few-shot + fallback extraction are enabled.
    AGENTS = {
        'implementer': _create_agent_config(
            'Implementer — Qwen3.5-35B Q4_K_M (3B/35B MoE, 262K ctx, ngl=22, default)',
            _IMPLEMENTER_SYSTEM_PROMPT,
            _QWEN35_35B,
            executor=True
        ),
        'deep_implementer': _create_agent_config(
            'Implementer — Coder-Next Q8_0 (3B/80B MoE, 256K ctx, ngl=48, deep reasoning)',
            _IMPLEMENTER_SYSTEM_PROMPT,
            _CODER_NEXT_Q8,
            executor=True
        ),
        'fast_implementer': _create_agent_config(
            'Implementer — Coder-30B Q4_K_M (3B/30B MoE, 256K ctx, ngl=26, fast)',
            _IMPLEMENTER_SYSTEM_PROMPT,
            _CODER_30B_FAST,
            executor=True
        ),
        'architect': _create_agent_config(
            'Architect — Coder-480B Q2_K_XL (35B/480B MoE, 64K ctx, ngl=4, ultra reasoning)',
            _ARCHITECT_SYSTEM_PROMPT,
            _QWEN_480B_ULTRA,
            executor=True
        ),
        'reviewer': _create_agent_config(
            'Reviewer — Coder-30B Q8_0 (3B/30B MoE, 65K ctx, ngl=21, high precision)',
            f'You are a code reviewer. {EXECUTOR_PROMPT}\n\nIdentify issues and suggest improvements. You are encouraged to provide detailed advice and recommendations.\n\nCOMPREHENSIVE ANALYSIS: When performing code reviews, leverage multiple tools to understand the codebase thoroughly:\n\nGIT AWARENESS: Use Git via `<<<REMOTE_EXEC>>>` to understand code context:\n- `git log`, `git diff`, `git blame`, `git show`, `git status`\n\nFILE NAVIGATION: Use `<<<GLOB>>>` and `<<<GREP>>>` to find and search files.\n\nDOCUMENTATION - You can and should write/update documentation:\n- Use `<<<WRITE_FILE>>>` for NEW documentation files\n- Use `<<<EDIT_FILE>>>` for targeted updates to EXISTING docs (PREFERRED)\n\nEDIT_FILE FORMAT (use EXACTLY this format):\n<<<EDIT_FILE>>>/path/to/file\n<<<OLD>>>\nexact text to find\n<<<NEW>>>\nreplacement text\n\nWARNING: Do NOT use git-style markers like <<<<<<< SEARCH or ======= or >>>>>>> REPLACE. Use <<<OLD>>> and <<<NEW>>> only.\n\nAlways gather comprehensive context before providing your review.\n{GIT_TOOL_REFERENCE}',
            _CODER_30B_HD,
            executor=True
        ),
        'debugger': _create_agent_config(
            'Debugger — Coder-30B Q4_K_M (3B/30B MoE, 131K ctx, ngl=30, turbo)',
            f'You are a debugger. {EXECUTOR_PROMPT}\n\nDEBUGGING WORKFLOW:\n- Use `<<<READ_FILE>>>` to examine source code\n- Use `<<<REMOTE_EXEC>>>` to run tests, check logs, execute debuggers\n- Use `<<<WRITE_FILE>>>` to apply fixes to source files\n- After fixing, use `<<<REMOTE_EXEC>>>` to verify the fix works (compile, run tests)\n\n{TOOL_REFERENCE}',
            _CODER_30B_TURBO,
            executor=True
        ),
        'lite_architect': _create_agent_config(
            'Architect — Coder-480B IQ1_M (35B/480B MoE, 32K ctx, ngl=4, lite reasoning)',
            f'You are a system architect. {EXECUTOR_PROMPT}\n\nFILE WRITING: Use `<<<WRITE_FILE>>>` to create or update source files. After writing, use `<<<REMOTE_EXEC>>>` to compile and verify.\n\n{TOOL_REFERENCE}',
            _QWEN_480B_LITE,
            executor=True
        ),
        'm25_implementer': _create_agent_config(
            'Implementer — MiniMax M2.5 Q4_K_M (10B/230B MoE, 116K ctx, ngl=62)',
            _IMPLEMENTER_SYSTEM_PROMPT + _MINIMAX_UNICODE_GUARD,
            _MINIMAX_M25,
            executor=True
        ),
        'm25_architect': _create_agent_config(
            'Architect — MiniMax M2.5 Q4_K_M (10B/230B MoE, 116K ctx, ngl=62)',
            _ARCHITECT_SYSTEM_PROMPT + _MINIMAX_UNICODE_GUARD,
            _MINIMAX_M25,
            executor=True
        ),
        # ── Qwen3.5 agents ──
        'q35_architect': _create_agent_config(
            'Architect — Qwen3.5-122B Q4_K_M (10B/122B MoE, 65K ctx, ngl=9, mid-tier)',
            _ARCHITECT_SYSTEM_PROMPT,
            _QWEN35_122B,
            executor=True
        ),
        'q35_ultra': _create_agent_config(
            'Architect — Qwen3.5-397B IQ1_M (17B/397B MoE, 96K ctx, ngl=60, flagship)',
            _ARCHITECT_SYSTEM_PROMPT,
            _QWEN35_397B,
            executor=True
        ),
        # ── Non-Qwen agents ──
        'nemotron': _create_agent_config(
            'Brainstorm — Nemotron-3-Nano Q4_K_M (3.5B/30B Mamba-MoE, 1M ctx, ngl=52, fastest)',
            'You are a fast brainstorming assistant. Help the user think through ideas, '
            'explore approaches, outline plans, and draft designs. You are great at rapid '
            'iteration and generating options quickly.\n\n'
            'IMPORTANT: You do NOT have access to tools, files, or shell commands. '
            'Do NOT output <<<WRITE_FILE>>>, <<<REMOTE_EXEC>>>, or any tool markers. '
            'Do NOT fabricate file contents or command outputs. If the user asks you to '
            'read, write, or execute something, tell them to switch to the implementer agent.\n\n'
            'Focus on: brainstorming, planning, outlining, comparing approaches, drafting '
            'pseudocode, explaining concepts, and reviewing ideas.',
            _NEMOTRON_NANO,
        ),
        'glm': _create_agent_config(
            'Implementer — GLM-4.7-Flash Q4_K_M (3B/30B MoE, 262K ctx, ngl=47, Zhipu AI)',
            _IMPLEMENTER_SYSTEM_PROMPT,
            _GLM47_FLASH,
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
    """Verify admin API key if ADMIN_API_KEY is configured.

    Uses hmac.compare_digest for timing-safe comparison to prevent
    key extraction via timing side-channel attacks.
    """
    if Config.ADMIN_API_KEY:
        if not x_admin_key or not hmac.compare_digest(x_admin_key, Config.ADMIN_API_KEY):
            raise HTTPException(status_code=401, detail="Invalid or missing admin API key")


# ============================================================================
# Model Manager
# ============================================================================

class ModelManager:
    def __init__(self):
        self.lock = Lock()
        self.inference_lock = Lock()
        self._cached_model = None
        self._cached_agent: Optional[str] = None

    def unload_model(self):
        """Release cached model and free VRAM"""
        with self.lock:
            if self._cached_model is not None:
                logger.info("Unloading cached model for %s...", self._cached_agent)
                del self._cached_model
                self._cached_model = None
                self._cached_agent = None

            gc.collect()
            gc.collect()

            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    torch.cuda.ipc_collect()
            except ImportError:
                pass

            time.sleep(0.5)
            logger.info("Memory cleanup complete")

    def get_model(self, agent_name: str):
        """Load the model for the specific agent (LRU-1 cache: reuse if same agent).

        The entire load-or-reuse sequence is serialized under self.lock to prevent
        two concurrent requests from loading different models simultaneously (OOM risk).
        Health checks use is_loaded() which doesn't acquire the lock.
        """
        if agent_name not in Config.AGENTS:
            raise ValueError(f"Unknown agent: {agent_name}")

        model_config = Config.AGENTS[agent_name]['model_config']
        model_path = model_config['path']

        # Free VRAM from llama-server subprocess before loading a llama_cpp model
        llama_server_manager.shutdown()

        with self.lock:
            # Cache hit — same agent as last time
            if self._cached_model is not None and self._cached_agent == agent_name:
                logger.info("Reusing cached model for %s", agent_name)
                return self._cached_model

            # Cache miss — unload previous model first
            if self._cached_model is not None:
                logger.info("Agent switch: unloading %s to load %s", self._cached_agent, agent_name)
                del self._cached_model
                self._cached_model = None
                self._cached_agent = None
                gc.collect(); gc.collect()

            # Load fresh — held under lock to prevent concurrent OOM
            logger.info("Loading model for %s: %s", agent_name, model_path)
            try:
                model = Llama(
                    model_path=model_path,
                    n_ctx=model_config.get('n_ctx', Config.DEFAULT_CONTEXT_SIZE),
                    n_gpu_layers=model_config.get('n_gpu_layers', 0),
                    n_threads=Config.DEFAULT_N_THREADS,
                    n_threads_batch=Config.DEFAULT_N_THREADS_BATCH,
                    n_batch=model_config.get('n_batch', Config.DEFAULT_N_BATCH),
                    flash_attn=True,
                    type_k=model_config.get('type_k'),
                    type_v=model_config.get('type_v'),
                    use_mmap=True,
                    use_mlock=False,
                    offload_kqv=model_config.get('offload_kqv', True),
                    rope_scaling_type=model_config.get('rope_scaling_type', -1),
                    rope_freq_base=model_config.get('rope_freq_base', 0.0),
                    rope_freq_scale=model_config.get('rope_freq_scale', 0.0),
                    yarn_ext_factor=model_config.get('yarn_ext_factor', -1.0),
                    yarn_attn_factor=model_config.get('yarn_attn_factor', 1.0),
                    yarn_beta_fast=model_config.get('yarn_beta_fast', 32.0),
                    yarn_beta_slow=model_config.get('yarn_beta_slow', 1.0),
                    yarn_orig_ctx=model_config.get('yarn_orig_ctx', 0),
                    verbose=False
                )

                logger.info("Model loaded successfully: %s", model_path)
                self._cached_model = model
                self._cached_agent = agent_name
                return model
            except Exception as e:
                logger.error("Failed to load model %s: %s", model_path, e)
                raise

    def is_loaded(self) -> bool:
        """Check if a model is currently cached and ready"""
        return self._cached_model is not None


# ============================================================================
# Llama-Server Subprocess Manager
# ============================================================================

class LlamaServerManager:
    """Manages a llama-server subprocess for models that require it (e.g. qwen3next arch)."""

    LLAMA_SERVER_PORT = 8081
    IDLE_TIMEOUT = 600  # 10 minutes
    HEALTH_POLL_INTERVAL = 0.5
    HEALTH_TIMEOUT = 120  # seconds to wait for /health

    # GGML type enum → llama-server --cache-type string
    _CACHE_TYPE_NAMES = {
        0: 'f32', 1: 'f16', 2: 'q4_0', 3: 'q4_1',
        6: 'q5_0', 7: 'q5_1', 8: 'q8_0', 9: 'q8_1',
    }

    def __init__(self):
        self.lock = Lock()
        self.process: Optional[subprocess.Popen] = None
        self.current_model_path: Optional[str] = None
        self.last_request_time: float = 0
        self._watchdog_thread: Optional[Thread] = None
        self._watchdog_running = False

    def start(self, model_config: dict):
        """Spawn llama-server with the given model config, wait for /health."""
        tools_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools')
        binary = os.path.join(tools_dir, 'llama-server')

        if not os.path.isfile(binary):
            raise FileNotFoundError(f"llama-server binary not found: {binary}")

        model_path = model_config['path']
        # Map GGML type_k/type_v integers to llama-server flag strings
        cache_k = self._CACHE_TYPE_NAMES.get(model_config.get('type_k', 8), 'q8_0')
        cache_v = self._CACHE_TYPE_NAMES.get(model_config.get('type_v', 8), 'q8_0')

        cmd = [
            binary,
            '-m', model_path,
            '-ngl', str(model_config.get('n_gpu_layers', 0)),
            '-c', str(model_config.get('n_ctx', 32768)),
            '-b', str(model_config.get('n_batch', 2048)),
            '-ub', str(model_config.get('n_ubatch', 512)),
            '-t', str(Config.DEFAULT_N_THREADS),
            '-tb', str(Config.DEFAULT_N_THREADS_BATCH),
            '-fa', 'auto',
            '--mmap',
            '--cache-type-k', cache_k,
            '--cache-type-v', cache_v,
            '--host', '127.0.0.1',
            '--port', str(self.LLAMA_SERVER_PORT),
            '-np', '1',
            '--lookup-cache-dynamic', '/tmp/llama-lookup-cache.bin',
        ]

        # MoE models: keep expert weights on CPU, put more attention layers on GPU
        if model_config.get('cpu_moe'):
            cmd.append('--cpu-moe')

        # Add model-specific server args (chat template, jinja, etc.)
        extra_args = model_config.get('server_extra_args', ['--chat-template', 'chatml'])
        cmd.extend(extra_args)

        env = os.environ.copy()
        env['LD_LIBRARY_PATH'] = tools_dir + ':' + env.get('LD_LIBRARY_PATH', '')

        logger.info("Starting llama-server: %s", ' '.join(cmd))
        self.process = subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        self.current_model_path = model_path

        # Background thread to drain stdout so the pipe doesn't block
        def _drain_output(proc):
            try:
                for line in iter(proc.stdout.readline, b''):
                    logger.info("[llama-server] %s", line.decode('utf-8', errors='replace').rstrip())
            except (ValueError, OSError):
                pass  # Process closed
        drain_thread = Thread(target=_drain_output, args=(self.process,), daemon=True)
        drain_thread.start()

        # Poll /health until ready
        health_url = f"http://127.0.0.1:{self.LLAMA_SERVER_PORT}/health"
        deadline = time.time() + self.HEALTH_TIMEOUT
        while time.time() < deadline:
            # Check if process died
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"llama-server exited with code {self.process.returncode} during startup"
                )
            try:
                resp = http_requests.get(health_url, timeout=2)
                if resp.status_code == 200:
                    logger.info("llama-server healthy after %.1fs", time.time() - (deadline - self.HEALTH_TIMEOUT))
                    self.last_request_time = time.time()
                    self._start_watchdog()
                    return
            except http_requests.ConnectionError:
                pass
            time.sleep(self.HEALTH_POLL_INTERVAL)

        # Timeout — kill the process
        self._shutdown_unlocked()
        raise TimeoutError(f"llama-server did not become healthy within {self.HEALTH_TIMEOUT}s")

    def _shutdown_unlocked(self):
        """Internal: stop the subprocess. Caller must hold self.lock or ensure exclusivity."""
        if self.process is None:
            return

        self._watchdog_running = False
        pid = self.process.pid
        logger.info("Shutting down llama-server (PID %d)...", pid)
        try:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
                logger.info("llama-server (PID %d) terminated gracefully", pid)
            except subprocess.TimeoutExpired:
                logger.warning("llama-server (PID %d) didn't exit, sending SIGKILL", pid)
                self.process.kill()
                self.process.wait(timeout=5)
        except Exception as e:
            logger.error("Error shutting down llama-server: %s", e)
        finally:
            self.process = None
            self.current_model_path = None

    def shutdown(self):
        """Stop the subprocess gracefully, then force-kill if needed. Thread-safe."""
        with self.lock:
            self._shutdown_unlocked()

    def ensure_running(self, model_config: dict):
        """Ensure llama-server is running with the correct model. Handles model swaps."""
        with self.lock:
            model_path = model_config['path']
            if self.process is not None and self.process.poll() is None:
                if self.current_model_path == model_path:
                    # Already running with the right model
                    self.last_request_time = time.time()
                    return
                # Different model — shut down first
                logger.info("Model swap: shutting down llama-server for new model")
                self._shutdown_unlocked()

            # Free VRAM from any llama_cpp model before starting
            model_manager.unload_model()
            try:
                self.start(model_config)
            except Exception as e:
                logger.error("Failed to start llama-server for %s: %s", model_path, e)
                self.current_model_path = None
                raise

    def _start_watchdog(self):
        """Start the idle watchdog thread."""
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._watchdog_running = True
        self._watchdog_thread = Thread(target=self._idle_watchdog, daemon=True)
        self._watchdog_thread.start()

    def _idle_watchdog(self):
        """Background thread: shut down subprocess if idle for IDLE_TIMEOUT seconds."""
        while self._watchdog_running:
            time.sleep(30)  # Check every 30s
            if not self._watchdog_running:
                break
            with self.lock:
                if self.process is None:
                    break
                idle = time.time() - self.last_request_time
                if idle >= self.IDLE_TIMEOUT:
                    logger.info("llama-server idle for %.0fs, shutting down to free resources", idle)
                    self._shutdown_unlocked()
                    break

    def proxy_stream(self, messages: List[dict], system_prompt: str,
                     model_id: str, max_tokens: int, temperature: float,
                     model_config: dict = None,
                     est_prompt_tokens: int = 0) -> Iterator[str]:
        """Stream a chat completion via the llama-server subprocess."""
        self.last_request_time = time.time()

        # Emit progress event so client can display prompt size during prefill
        n_ctx = (model_config or {}).get('n_ctx', 32768)
        progress_event = {"type": "progress", "stage": "prefill",
                          "prompt_tokens": est_prompt_tokens, "n_ctx": n_ctx}
        yield f"data: {json.dumps(progress_event)}\n\n"

        openai_messages = self._build_openai_messages(messages, system_prompt)
        url = f"http://127.0.0.1:{self.LLAMA_SERVER_PORT}/v1/chat/completions"
        payload = {
            "messages": openai_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            # No explicit stop sequences — llama-server's chat template handles
            # end-of-turn tokens (im_end, EOT, etc.) natively. Passing them here
            # causes premature stopping via double-matching.
            "repeat_penalty": (model_config or {}).get('repeat_penalty', 1.15),
            "repeat_last_n": (model_config or {}).get('repeat_last_n', 256),
            # Ban model-specific native tool tokens to prevent format corruption.
            # Each model config specifies which tokens to ban via logit_bias.
            "logit_bias": (model_config or {}).get('logit_bias', []),
        }

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        finish_reason = None
        accumulated_text = []  # Diagnostic: capture full response
        think_stripper = ThinkingStripper()

        try:
            with http_requests.post(url, json=payload, stream=True, timeout=600) as resp:
                if resp.status_code != 200:
                    error_body = resp.text
                    logger.error("llama-server returned %d: %s", resp.status_code, error_body)
                    error_chunk = {"error": {"message": f"llama-server error: {error_body}", "type": "server_error"}}
                    yield f"data: {json.dumps(error_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                resp.encoding = 'utf-8'  # llama-server sends UTF-8; override requests' ISO-8859-1 default
                for line in resp.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        choices = chunk.get("choices", [])
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {})
                        content = delta.get("content")
                        fr = choices[0].get("finish_reason")
                        if fr:
                            finish_reason = fr

                        # Also check for tool_calls that might not be in content
                        if delta.get("tool_calls"):
                            logger.warning("llama-server returned tool_calls in delta (not proxied): %s",
                                           json.dumps(delta["tool_calls"]))

                        if content:
                            accumulated_text.append(content)
                            # Strip <think>...</think> blocks from streaming output
                            filtered = think_stripper.feed(content)
                            if filtered:
                                self.last_request_time = time.time()  # Keep watchdog at bay during long streams
                                out_chunk = build_stream_chunk(completion_id, model_id, content=filtered)
                                yield f"data: {json.dumps(out_chunk)}\n\n"
                    except json.JSONDecodeError:
                        continue

        except Exception as e:
            logger.error("Error proxying stream from llama-server: %s", e, exc_info=True)
            error_chunk = {"error": {"message": str(e), "type": "server_error"}}
            yield f"data: {json.dumps(error_chunk)}\n\n"
            yield "data: [DONE]\n\n"
            return

        # Flush any remaining buffered content from the thinking stripper
        remaining = think_stripper.flush()
        if remaining:
            out_chunk = build_stream_chunk(completion_id, model_id, content=remaining)
            yield f"data: {json.dumps(out_chunk)}\n\n"

        # Log the full response for diagnostics (repr escapes newlines for single-line journald)
        full_text = ''.join(accumulated_text)
        logger.info("llama-server proxy response (%d chars): %s",
                     len(full_text), repr(full_text[:2000]))

        final_chunk = build_stream_chunk(completion_id, model_id, finish=True,
                                         finish_reason=finish_reason or "stop")
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield "data: [DONE]\n\n"
        self.last_request_time = time.time()

    def proxy_sync(self, messages: List[dict], system_prompt: str,
                   model_id: str, max_tokens: int, temperature: float,
                   model_config: dict = None) -> dict:
        """Synchronous chat completion via the llama-server subprocess."""
        self.last_request_time = time.time()

        openai_messages = self._build_openai_messages(messages, system_prompt)
        url = f"http://127.0.0.1:{self.LLAMA_SERVER_PORT}/v1/chat/completions"
        payload = {
            "messages": openai_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
            "repeat_penalty": (model_config or {}).get('repeat_penalty', 1.15),
            "repeat_last_n": (model_config or {}).get('repeat_last_n', 256),
            "logit_bias": (model_config or {}).get('logit_bias', []),
        }

        resp = http_requests.post(url, json=payload, timeout=600)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"llama-server error: {resp.text}")

        result = resp.json()
        text = result["choices"][0]["message"]["content"]
        text = strip_thinking(text)
        finish_reason = result["choices"][0].get("finish_reason", "stop")
        usage = result.get("usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})

        self.last_request_time = time.time()
        return build_completion_response(model_id, text, usage, finish_reason=finish_reason)

    @staticmethod
    def _build_openai_messages(messages: List, system_prompt: str) -> List[dict]:
        """Build OpenAI-format messages array from ChatMessage list + system prompt."""
        openai_msgs = []
        if system_prompt:
            openai_msgs.append({"role": "system", "content": system_prompt})
        for msg in messages:
            if isinstance(msg, dict):
                openai_msgs.append({"role": msg["role"], "content": msg["content"]})
            else:
                openai_msgs.append({"role": msg.role, "content": msg.content})
        return openai_msgs


# ============================================================================
# Thinking Tag Stripping
# ============================================================================

# Models with --reasoning-format none still emit thinking content in raw text.
# Three patterns observed:
#   1. Full block:    <think>reasoning...</think>actual response
#   2. Orphan close:  reasoning...</think>actual response  (Jinja consumed <think>)
#   3. Unclosed open: <think>reasoning...  (truncated by max_tokens, no </think>)
_THINK_FULL_RE = re.compile(r'<think>.*?</think>\s*', re.DOTALL)
_THINK_ORPHAN_RE = re.compile(r'^.*?</think>\s*', re.DOTALL)
_THINK_UNCLOSED_RE = re.compile(r'<think>(?:(?!</think>).)*$', re.DOTALL)
_REACT_RE = re.compile(r'<REACT>.*?</REACT>\s*', re.DOTALL)


def strip_thinking(text: str) -> str:
    """Remove thinking/reasoning content from completed text."""
    text = _THINK_FULL_RE.sub('', text)
    text = _THINK_ORPHAN_RE.sub('', text)
    text = _THINK_UNCLOSED_RE.sub('', text)  # Truncated thinking (hit max_tokens)
    text = _REACT_RE.sub('', text)  # Qwen3.5 reasoning blocks
    return text


class ThinkingStripper:
    """Streaming state machine that suppresses thinking content.

    Handles two patterns:
        1. <think>reasoning...</think>response  (full block)
        2. reasoning...</think>response          (orphan — Jinja consumed <think>)

    Buffers all tokens until </think> is found, then emits everything after it.
    Thinking content can itself contain tool markers (<<<WRITE_FILE>>> etc.)
    as the model reasons about tools, so we cannot use those as shortcuts.

    If no </think> appears within MAX_BUFFER chars, assumes no thinking block
    and flushes the buffer as real content.
    """
    BUFFERING = 0
    PASSTHROUGH = 1

    MAX_BUFFER = 32768  # chars — Nemotron generates 8K+ thinking blocks

    def __init__(self):
        self.state = self.BUFFERING
        self.buffer = ''

    def feed(self, token: str) -> str:
        """Feed a token, return text to emit (empty string = suppress)."""
        if self.state == self.PASSTHROUGH:
            return token

        self.buffer += token

        # Check for </think> — everything before it (inclusive) is thinking
        close_idx = self.buffer.find('</think>')
        if close_idx != -1:
            after = self.buffer[close_idx + len('</think>'):]
            self.state = self.PASSTHROUGH
            self.buffer = ''
            return after.lstrip()

        # Safety valve: if buffer grows too large without </think>,
        # this response has no thinking block — flush as real content
        if len(self.buffer) >= self.MAX_BUFFER:
            self.state = self.PASSTHROUGH
            result = self.buffer
            self.buffer = ''
            return result

        return ''

    def flush(self) -> str:
        """Flush remaining buffer at end of stream.

        If still in BUFFERING state, no </think> was ever seen.  This means
        either: (a) the model doesn't use thinking tags (common — e.g. chatml
        models), so the buffer is the entire valid response, or (b) an orphaned
        <think> block was never closed (rare).  We return the buffer in both
        cases — discarding it would drop valid tool calls for non-thinking models.
        """
        if self.buffer:
            result = self.buffer
            self.buffer = ''
            self.state = self.PASSTHROUGH
            return result
        return ''


# ============================================================================
# Prompt Format Helpers
# ============================================================================

CHATML_START = "<|im_start|>"
CHATML_END = "<|im_end|>"


def build_model_prompt(messages: List[ChatMessage], system_prompt: str, model_path: str) -> str:
    """Build a ChatML-formatted prompt for Qwen models"""
    parts = []
    if system_prompt:
        parts.append(f"{CHATML_START}system\n{system_prompt}{CHATML_END}\n")

    for msg in messages:
        parts.append(f"{CHATML_START}{msg.role}\n{msg.content}{CHATML_END}\n")

    parts.append(f"{CHATML_START}assistant\n")
    return "".join(parts)


def get_model_params(max_tokens: int, temperature: float, stream: bool = False,
                     model_config: dict = None) -> Dict[str, Any]:
    """Get common model inference parameters.

    Note: repeat_last_n is NOT supported by llama-cpp-python's __call__ API,
    so it's only passed in the llama_server proxy payloads (proxy_stream/proxy_sync).
    """
    return {
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stop": [CHATML_END, CHATML_START, "<|EOT|>", "<|endoftext|>"],
        "stream": stream,
        "repeat_penalty": (model_config or {}).get('repeat_penalty', 1.15),
        "echo": False
    }


def calculate_token_budget(model, messages: List, system_prompt: str, model_path: str,
                           max_tokens: int, model_id: str) -> tuple:
    """Calculate token budget and build final prompt with budget guidance.

    Uses 2 tokenizations instead of 3: tokenize the budget guidance template once
    to estimate its overhead, then tokenize the final prompt once.

    Returns:
        tuple: (prompt, clamped_max_tokens, n_prompt, error_message)
               error_message is None on success, or a string describing the error
    """
    n_ctx = model.n_ctx()

    # Tokenize budget guidance template once to estimate its overhead
    budget_guidance_template = Config.TOKEN_BUDGET_GUIDANCE.format(available_tokens=99999)
    budget_guidance_tokens = len(model.tokenize(budget_guidance_template.encode("utf-8")))

    # Build the final prompt with a placeholder budget
    preliminary_prompt = build_model_prompt(messages, system_prompt, model_path)

    # Fast path: use char-based estimate (~3.5 chars/token) when clearly within budget.
    # Only pay for full tokenization when the prompt is close to the context limit.
    approx_tokens = len(preliminary_prompt) // 3
    if approx_tokens < n_ctx * 0.5:
        n_preliminary = approx_tokens
    else:
        n_preliminary = len(model.tokenize(preliminary_prompt.encode("utf-8")))

    # Calculate available tokens accounting for budget guidance overhead
    available = n_ctx - n_preliminary - budget_guidance_tokens
    if available < 1:
        error_msg = (f"Prompt ({n_preliminary} tokens) fills the entire context window ({n_ctx}). "
                     "Reduce conversation history and retry.")
        return None, 0, n_preliminary, error_msg

    clamped_max = min(max_tokens, available)

    # Build the real prompt with actual budget number.
    # The token count difference from changing "99999" to the real number is negligible
    # (a few tokens at most), so we use the estimated n_prompt without re-tokenizing.
    budget_guidance = Config.TOKEN_BUDGET_GUIDANCE.format(available_tokens=clamped_max)
    augmented_system_prompt = f"{system_prompt}\n{budget_guidance}"
    prompt = build_model_prompt(messages, augmented_system_prompt, model_path)
    n_prompt = n_preliminary + budget_guidance_tokens

    if clamped_max < max_tokens:
        logger.info(
            "Clamped max_tokens %d -> %d for %s (prompt~%d, n_ctx=%d)",
            max_tokens, clamped_max, model_id, n_prompt, n_ctx
        )

    logger.info(
        "Token budget injected for %s: budget=%d tokens communicated to model",
        model_id, clamped_max
    )

    return prompt, clamped_max, n_prompt, None


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

model_manager = ModelManager()
llama_server_manager = LlamaServerManager()
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
        logger.error("Failed to initialize memory service: %s", e)
        memory_service = None

    # Initialize Web Search Service
    try:
        logger.info("Initializing Web Search Service...")
        web_search_service = WebSearchService()
        logger.info("Web Search Service initialized successfully")
    except Exception as e:
        logger.error("Failed to initialize web search service: %s", e)
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
        logger.error("Failed to initialize Apple Deep Docs Service: %s", e)
        apple_deep_docs_service = None

    yield
    # Shutdown
    logger.info("Server shutting down - unloading models...")
    llama_server_manager.shutdown()
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

_cors_origins_raw = os.getenv("CORS_ORIGINS", "localhost,127.0.0.1").split(",")
_cors_origins = []
for origin in _cors_origins_raw:
    origin = origin.strip()
    if not origin:
        continue
    if origin == "*":
        _cors_origins = ["*"]
        break
    # Ensure origins have a scheme — bare hostnames don't match in CORS
    if not origin.startswith("http"):
        _cors_origins.append(f"http://{origin}")
    else:
        _cors_origins.append(origin)

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


@app.post("/v1/memory", dependencies=[Depends(verify_admin_key)])
def save_memory_endpoint(request: MemoryRequest):
    """Save a memory/fact to the long-term storage.

    If *source* is provided (e.g. a file path like "main.swift"), the text is
    parsed into language-aware chunks using tree-sitter before storage.
    Without *source*, the text is stored as a single document (backward compatible).
    """
    if not memory_service:
        raise HTTPException(status_code=503, detail="Memory service not initialized")

    try:
        if request.source:
            result = memory_service.add_memory_chunked(request.text, source=request.source)
        else:
            result = memory_service.add_memory(request.text)

        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])

        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error saving memory: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/memory/search", dependencies=[Depends(verify_admin_key)])
def search_memory_endpoint(request: SearchRequest):
    """Search for relevant memories in the long-term storage"""
    if not memory_service:
        raise HTTPException(status_code=503, detail="Memory service not initialized")
        
    try:
        results = memory_service.search_memory(request.query)
        return {"results": results}
    except Exception as e:
        logger.error("Error searching memory: %s", e)
        raise HTTPException(status_code=500, detail=str(e))



@app.post("/v1/tools/search", dependencies=[Depends(verify_admin_key)])
def web_search_endpoint(request: SearchRequest):
    """Perform a web search using DuckDuckGo"""
    if not web_search_service:
        raise HTTPException(status_code=503, detail="Web search service not initialized")
        
    result = web_search_service.search(request.query)
    return {"result": result}


class DeepDocRequest(BaseModel):
    """Request for Apple Deep Docs"""
    tool: str
    arguments: Dict[str, Any]


@app.post("/v1/tools/apple_deep_docs", dependencies=[Depends(verify_admin_key)])
def apple_deep_docs_endpoint(request: DeepDocRequest):
    """Perform an Apple Documentation search using the server-side MCP"""
    if not apple_deep_docs_service:
        raise HTTPException(status_code=503, detail="Apple Deep Docs service not initialized")
        
    result = apple_deep_docs_service.call_tool(request.tool, request.arguments)
    return {"result": result}


@app.post("/v1/memory/ingest", dependencies=[Depends(verify_admin_key)])
def ingest_memory_endpoint(request: IngestRequest):
    """Ingest a local PDF file into long-term memory"""
    if not memory_service:
        raise HTTPException(status_code=503, detail="Memory service not initialized")

    # Path security validation
    normalized = os.path.normpath(request.path)
    if '..' in normalized.split(os.sep):
        raise HTTPException(status_code=400, detail="Path traversal not allowed")
    if not os.path.isabs(normalized):
        raise HTTPException(status_code=400, detail="Only absolute paths are allowed")
    
    # Allow ingestion from system temp directory (for uploads)
    import tempfile
    temp_dir = os.path.normpath(tempfile.gettempdir())
    is_in_temp = normalized.startswith(temp_dir + os.sep) or normalized == temp_dir

    if Config.INGEST_ALLOWED_DIR:
        allowed = os.path.normpath(Config.INGEST_ALLOWED_DIR)
        is_in_allowed = normalized.startswith(allowed + os.sep) or normalized == allowed
        
        if not is_in_allowed and not is_in_temp:
            raise HTTPException(status_code=403, detail=f"Path must be under {allowed} or {temp_dir}")
    elif not is_in_temp:
        raise HTTPException(status_code=403, detail=f"Path must be under {temp_dir} (set INGEST_ALLOWED_DIR to allow other paths)")

    result = memory_service.ingest_pdf(normalized)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return result


class FileUploadRequest(BaseModel):
    """Request to upload a file to the server"""
    filename: str
    content: str  # Base64 encoded content


@app.post("/v1/files/upload", dependencies=[Depends(verify_admin_key)])
def upload_file_endpoint(request: FileUploadRequest):
    """Upload a file to the server's temporary directory"""
    try:
        import base64
        import tempfile

        # Reject oversized uploads before decoding (100 MB base64 ≈ 75 MB file)
        MAX_UPLOAD_B64_LEN = 100 * 1024 * 1024
        if len(request.content) > MAX_UPLOAD_B64_LEN:
            raise HTTPException(
                status_code=413,
                detail=f"Upload too large ({len(request.content) // (1024*1024)} MB base64). Max: 100 MB"
            )

        # Decode the base64 content
        try:
            file_content = base64.b64decode(request.content)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid base64 content: {str(e)}")
        
        # Sanitize filename: strip directory components to prevent path traversal
        safe_filename = os.path.basename(request.filename)
        if not safe_filename:
            raise HTTPException(status_code=400, detail="Invalid filename")

        temp_dir = tempfile.gettempdir()
        # Use mkstemp for atomic creation (avoids race conditions and overwrites)
        suffix = os.path.splitext(safe_filename)[1]
        fd, temp_path = tempfile.mkstemp(suffix=suffix, dir=temp_dir)
        try:
            os.write(fd, file_content)
        finally:
            os.close(fd)

        logger.info("File uploaded successfully: %s", temp_path)
        return {"status": "success", "path": temp_path, "size": len(file_content)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error uploading file: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Autonomous Mode Endpoints (Phase 1a)
#
# Spec ingest, task store, and review gates for the autonomous service mode.
# State lives in qwen_tasks_db/tasks.sqlite via qwen_autonomous.Database.
# The orchestrator daemon (qwen-orchestrator.service) reads/writes the same
# database to drive planning and gate processing — these endpoints are the
# public HTTP face of that store.
# ============================================================================

from qwen_autonomous import Database as _AutonomousDatabase
from qwen_autonomous.models import (
    SubmitSpecRequest,
    SubmitSpecResponse,
    GateRespondRequest,
    SpecSummary,
)

# Single shared Database instance — internally thread-safe via WAL.
_autonomous_db: Optional[_AutonomousDatabase] = None


def get_autonomous_db() -> _AutonomousDatabase:
    global _autonomous_db
    if _autonomous_db is None:
        _autonomous_db = _AutonomousDatabase()
        logger.info("Autonomous task store initialized at %s",
                    _autonomous_db.db_path)
    return _autonomous_db


def _extract_title_from_md(markdown: str) -> str:
    """Pull the first H1 ('# Title') out of a markdown document."""
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or "untitled spec"
    return "untitled spec"


@app.post("/v1/autonomous/specs", dependencies=[Depends(verify_admin_key)])
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


@app.get("/v1/autonomous/specs", dependencies=[Depends(verify_admin_key)])
def list_autonomous_specs(limit: int = 50) -> list[dict]:
    """List recent specs (newest first), no events or gates."""
    db = get_autonomous_db()
    specs = db.list_specs(limit=limit)
    return [s.model_dump(mode="json") for s in specs]


@app.get("/v1/autonomous/specs/{spec_id}", dependencies=[Depends(verify_admin_key)])
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
    )


@app.get("/v1/autonomous/gates", dependencies=[Depends(verify_admin_key)])
def list_open_gates(spec_id: Optional[str] = None) -> list[dict]:
    """List all open review gates, optionally filtered by spec."""
    db = get_autonomous_db()
    gates = db.list_open_gates(spec_id=spec_id)
    return [g.model_dump(mode="json") for g in gates]


@app.get("/v1/autonomous/gates/{gate_id}", dependencies=[Depends(verify_admin_key)])
def get_gate(gate_id: str) -> dict:
    db = get_autonomous_db()
    gate = db.get_gate(gate_id)
    if gate is None:
        raise HTTPException(status_code=404, detail=f"gate {gate_id} not found")
    return gate.model_dump(mode="json")


@app.post("/v1/autonomous/gates/{gate_id}/respond",
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


@app.get("/v1/autonomous/specs/{spec_id}/events",
         dependencies=[Depends(verify_admin_key)])
def get_spec_events(spec_id: str, limit: int = 100) -> list[dict]:
    db = get_autonomous_db()
    if db.get_spec(spec_id) is None:
        raise HTTPException(status_code=404, detail=f"spec {spec_id} not found")
    events = db.list_recent_events(spec_id=spec_id, limit=limit)
    return [e.model_dump(mode="json") for e in events]


@app.post("/v1/chat/completions", dependencies=[Depends(verify_admin_key)])
async def chat_completions(request: ChatCompletionRequest, raw_request: Request):
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

        # Few-shot: inject format examples only for short conversations
        # (once the model has seen enough real exchanges, the examples waste tokens)
        if agent_config.get('executor') and Config.FEW_SHOT and len(request.messages) <= 4:
            few_shot_msgs = [ChatMessage(role=m['role'], content=m['content']) for m in Config.FEW_SHOT]
            request.messages = few_shot_msgs + list(request.messages)

        # RAG: Retrieve relevant memories
        if memory_service and request.messages:
            # Find the last user message to use as query
            last_user_msg = next((m.content for m in reversed(request.messages) if m.role == 'user'), None)

            if last_user_msg:
                try:
                    context = await asyncio.wait_for(
                        asyncio.to_thread(memory_service.get_context_string, last_user_msg),
                        timeout=2.0
                    )
                    if context:
                        logger.info("Injecting memory context for query: %s...", last_user_msg[:50])
                        system_prompt = f"{system_prompt}\n\n{context}"
                except asyncio.TimeoutError:
                    logger.warning("Memory retrieval timed out (>2s), skipping RAG context")
                except Exception as e:
                    logger.error("Memory retrieval failed: %s", e)

        model_config = agent_config['model_config']
        model_path = model_config['path']

        # Route by backend
        if model_config.get('backend') == 'llama_server':
            # llama-server subprocess backend
            llama_server_manager.ensure_running(model_config)

            # Estimate token budget (no local model to tokenize with)
            prompt_text = system_prompt + ''.join(m.content for m in request.messages)
            est_prompt_tokens = int(len(prompt_text) / 3.5)
            n_ctx = model_config.get('n_ctx', 32768)
            available = max(n_ctx - est_prompt_tokens, 1)
            clamped_max = min(request.max_tokens, available)

            # Inject budget guidance into system prompt
            budget_guidance = Config.TOKEN_BUDGET_GUIDANCE.format(available_tokens=clamped_max)
            augmented_system = f"{system_prompt}\n{budget_guidance}"

            logger.info(
                "llama-server request for %s: est_prompt=%d, budget=%d, n_ctx=%d",
                request.model, est_prompt_tokens, clamped_max, n_ctx
            )

            if request.stream:
                return StreamingResponse(
                    llama_server_manager.proxy_stream(
                        request.messages, augmented_system, request.model,
                        clamped_max, request.temperature, model_config=model_config,
                        est_prompt_tokens=est_prompt_tokens),
                    media_type="text/event-stream"
                )
            else:
                # Run blocking sync inference in thread pool to keep event loop responsive
                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(
                    None,
                    lambda: llama_server_manager.proxy_sync(
                        request.messages, augmented_system, request.model,
                        clamped_max, request.temperature, model_config=model_config)
                )
        else:
            # Standard llama_cpp path
            if request.stream:
                return StreamingResponse(
                    stream_completion(request.messages, system_prompt, model_path, request.model,
                                      request.max_tokens, request.temperature, model_config=model_config),
                    media_type="text/event-stream"
                )
            else:
                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(
                    None,
                    lambda: sync_completion(request.messages, system_prompt, model_path, request.model,
                                            request.max_tokens, request.temperature, model_config=model_config)
                )

    except HTTPException:
        raise
    except FileNotFoundError as e:
        logger.error("Model file error: %s", e)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error("Error in chat_completions: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def sync_completion(messages: List[ChatMessage], system_prompt: str, model_path: str,
                    model_id: str, max_tokens: int, temperature: float,
                    model_config: dict = None) -> Dict[str, Any]:
    """Generate synchronous completion with token budget awareness"""
    with model_manager.inference_lock:
        model = model_manager.get_model(model_id)

        prompt, clamped_max, n_prompt, error_msg = calculate_token_budget(
            model, messages, system_prompt, model_path, max_tokens, model_id
        )

        if error_msg:
            raise HTTPException(status_code=400, detail=error_msg)

        params = get_model_params(clamped_max, temperature, stream=False, model_config=model_config)
        response = model(prompt, **params)

        text = strip_thinking(response['choices'][0]['text'].strip())

        finish_reason = response['choices'][0].get('finish_reason', 'stop')
        if not finish_reason:
            finish_reason = 'stop'

        return build_completion_response(model_id, text, response['usage'],
                                         finish_reason=finish_reason)


STREAM_TTFT_TIMEOUT = 600  # seconds — abort if no token generated within 10 minutes


def stream_completion(messages: List[ChatMessage], system_prompt: str, model_path: str,
                      model_id: str, max_tokens: int, temperature: float,
                      model_config: dict = None) -> Iterator[str]:
    """Generate streaming completion with token budget awareness.

    The inference_lock is held for the full duration of streaming intentionally.
    llama-cpp-python is not thread-safe, so concurrent inference on the same
    model instance would cause undefined behavior or crashes.
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    finish_reason = "stop"
    start_time = time.time()

    try:
        with model_manager.inference_lock:
                model = model_manager.get_model(model_id)

                prompt, clamped_max, n_prompt, error_msg = calculate_token_budget(
                    model, messages, system_prompt, model_path, max_tokens, model_id
                )

                budget_elapsed = time.time() - start_time
                if budget_elapsed > STREAM_TTFT_TIMEOUT:
                    logger.error(
                        "Token budget calculation took %.1fs for %s (prompt=%d tokens) — aborting",
                        budget_elapsed, model_id, n_prompt
                    )
                    error_chunk = {
                        "error": {
                            "message": f"Server timeout: prompt tokenization took {budget_elapsed:.0f}s. "
                                       "Reduce conversation history and retry.",
                            "type": "context_length_exceeded"
                        }
                    }
                    yield f"data: {json.dumps(error_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                if error_msg:
                    error_chunk = {
                        "error": {
                            "message": error_msg,
                            "type": "context_length_exceeded"
                        }
                    }
                    yield f"data: {json.dumps(error_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                # Emit progress event so client can display prompt size during prefill
                n_ctx = model.n_ctx()
                progress_event = {"type": "progress", "stage": "prefill",
                                  "prompt_tokens": n_prompt, "n_ctx": n_ctx}
                yield f"data: {json.dumps(progress_event)}\n\n"

                params = get_model_params(clamped_max, temperature, stream=True, model_config=model_config)
                token_count = 0
                think_stripper = ThinkingStripper()

                for output in model(prompt, **params):
                    # TTFT timeout: abort if stuck in prefill
                    if token_count == 0 and (time.time() - start_time) > STREAM_TTFT_TIMEOUT:
                        logger.error(
                            "TTFT timeout (%.0fs) for %s — prompt=%d tokens, aborting",
                            time.time() - start_time, model_id, n_prompt
                        )
                        error_chunk = {
                            "error": {
                                "message": f"Server timeout: no tokens generated within {STREAM_TTFT_TIMEOUT}s. "
                                           "Reduce conversation history and retry.",
                                "type": "context_length_exceeded"
                            }
                        }
                        yield f"data: {json.dumps(error_chunk)}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                    if 'choices' in output and len(output['choices']) > 0:
                        choice = output['choices'][0]
                        token = choice.get('text', '')
                        if token:
                            filtered = think_stripper.feed(token)
                            if filtered:
                                token_count += 1
                                chunk = build_stream_chunk(completion_id, model_id, content=filtered)
                                yield f"data: {json.dumps(chunk)}\n\n"
                        # Capture finish_reason from the last chunk llama-cpp emits
                        if choice.get('finish_reason'):
                            finish_reason = choice['finish_reason']

                # Flush any remaining buffered content from the thinking stripper
                remaining = think_stripper.flush()
                if remaining:
                    token_count += 1
                    chunk = build_stream_chunk(completion_id, model_id, content=remaining)
                    yield f"data: {json.dumps(chunk)}\n\n"

                # If llama-cpp didn't set a finish_reason but we hit the token limit,
                # infer "length" so the client knows the response was truncated
                if finish_reason == "stop" and token_count >= clamped_max:
                    finish_reason = "length"

                # Always log completion stats for debugging truncation issues
                logger.info(
                    "Completion stats for %s: prompt=%d, clamped_max=%d, "
                    "generated=%d, finish_reason=%s",
                    model_id, n_prompt, clamped_max, token_count, finish_reason
                )

        final_chunk = build_stream_chunk(completion_id, model_id, finish=True,
                                         finish_reason=finish_reason)
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield "data: [DONE]\n\n"

    except Exception as e:
        logger.error("Error in stream_completion: %s", e, exc_info=True)
        error_chunk = {"error": {"message": str(e), "type": "server_error"}}
        yield f"data: {json.dumps(error_chunk)}\n\n"
        yield "data: [DONE]\n\n"


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
        loop="asyncio"  # Force standard asyncio loop for stability
    )