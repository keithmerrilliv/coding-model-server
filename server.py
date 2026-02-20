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
    text: str
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
                         server_extra_args=None, logit_bias=None):
    """Helper function to create standardized model configurations"""
    config = {
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
        'backend': backend,
    }
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
    # 24 = physical core count (8 P-cores + 16 E-cores); hyperthreads hurt llama.cpp
    DEFAULT_N_THREADS = int(os.getenv('MODEL_N_THREADS', 24))
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
        "<<<INGEST_PDF>>>path                              — ingest a PDF file into memory (supports local: prefix for client files)"
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
- Use <<<GLOB>>> and <<<GREP>>> to find files instead of shell find/grep (faster, cleaner output)
- After writing/editing files, use <<<REMOTE_EXEC>>> to compile/build and verify changes work
- Never ask for permission. You have full file access.
- Never claim you cannot run commands or write files. You can.

CONTEXT MANAGEMENT — your context window is limited. Work efficiently:
- Work FILE-BY-FILE: read a file, modify it, verify it, then move to the next.
  Do NOT read all files before starting work.
- After reading a file, save key findings with <<<SAVE_MEMORY>>> before moving on.
  This lets you drop the raw content from context while retaining what matters.
- Prefer <<<GREP>>> over <<<READ_FILE>>> when you only need to find specific content.
""" + MACOS_TOOLKIT

    # ── Shared model configs ──
    # Turbo: Optimized for speed and 80k context on RTX 5080 (Success Formula)
    _CODER_30B_TURBO = _create_model_config(
        'MODEL_PATH_30B_TURBO',
        '/home/keith-merrill/.lmstudio/models/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf',
        32, 81920, 2048
    )

    # FAST: Lightweight Q4_K_M for quick implementation tasks (256k native context, moderate GPU)
    # Alternative to the 80B Next model when speed matters more than quality
    _CODER_30B_FAST = _create_model_config(
        'MODEL_PATH_30B_FAST',
        '/home/keith-merrill/.lmstudio/models/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf',
        22, 262144, 1024
    )

    # NEXT: Qwen3-Coder-Next-Q8_0 (80B MoE with 3B active params)
    # Very smart but runs mostly on system RAM (slow). Native 256k context enabled.
    # Uses llama-server subprocess backend (qwen3next arch not supported by llama-cpp-python 0.3.16)
    _CODER_NEXT_Q8 = _create_model_config(
        'MODEL_PATH_NEXT_Q8',
        '/home/keith-merrill/.lmstudio/models/unsloth/Qwen3-Coder-Next-GGUF/Q8_0/Qwen3-Coder-Next-Q8_0-00001-of-00003.gguf',
        8, 262144, 1024, backend='llama_server',
        server_extra_args=['--chat-template', 'chatml'],
        logit_bias=[[151657, -100.0], [151658, -100.0]],
    )

    # HD: High-precision Q8_0 with expanded context (49k) for review and Metal work
    # Reduced GPU layers (16) to free VRAM for larger KV cache
    _CODER_30B_HD = _create_model_config(
        'MODEL_PATH_30B_HD',
        '/home/keith-merrill/.lmstudio/models/lmstudio-community/Qwen3-Coder-30B-A3B-Instruct-GGUF/Qwen3-Coder-30B-A3B-Instruct-Q8_0.gguf',
        21, 49152, 2048
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
        4, 65536, 1024
    )

    # MINIMAX: MiniMax M2.5 (230B MoE, 10B active params)
    # Uses llama-server subprocess backend with native Jinja template
    _MINIMAX_M25 = _create_model_config(
        'MODEL_PATH_MINIMAX_M25',
        '/home/keith-merrill/.lmstudio/models/unsloth/MiniMax-M2.5-GGUF/MiniMax-M2.5-Q4_K_M-00001-of-00005.gguf',
        4, 32768, 4096, backend='llama_server',
        server_extra_args=['--jinja', '--reasoning-format', 'none'],
        logit_bias=[[200052, -100.0], [200053, -100.0]],
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
    ]

    # ── Shared agent prompts ──
    _IMPLEMENTER_SYSTEM_PROMPT = (
        f'You are an implementer. {EXECUTOR_PROMPT}\n\nCOMPREHENSIVE IMPLEMENTATION: When implementing tasks, leverage multiple tools to understand the codebase thoroughly:\n\nEXECUTION ENVIRONMENT: You are running on a macOS environment with full access to development tools.\n- Use `<<<REMOTE_EXEC>>>` for ALL shell commands (including Xcode tools, Git, file operations).\n- Do NOT distinguish between "server" and "client". Everything runs locally.\n\nFILE OPERATIONS:\n- Use `<<<GLOB>>>` to find files: `<<<GLOB>>>**/*.swift`\n- Use `<<<GREP>>>` to search code: `<<<GREP>>>TODO|src/`\n- Use `<<<LIST_DIR>>>` to explore directories\n- Use `<<<READ_FILE>>>` to read file contents\n- Use `<<<WRITE_FILE>>>` for new files or complete rewrites\n- Use `<<<EDIT_FILE>>>` for targeted changes to existing files (PREFERRED)\n\nGIT AWARENESS: Use Git via `<<<REMOTE_EXEC>>>` to understand code context:\n- `git log`, `git diff`, `git blame`, `git show`, `git status`\n\nAPPLE DEVELOPMENT via `<<<REMOTE_EXEC>>>`:\n- Compile Swift: `swiftc file.swift -o output`\n- Compile Metal: `xcrun -sdk macosx metal -c shader.metal -o shader.air`\n- Build Xcode: `xcodebuild -project Foo.xcodeproj -scheme Foo build`\n\n{TOOL_REFERENCE}'
    )

    _ARCHITECT_SYSTEM_PROMPT = (
        f'You are a system architect. {EXECUTOR_PROMPT}\n\nDESIGN AND IMPLEMENTATION: You are expected to both design solutions and implement them using the tools available.\n\nEXECUTION ENVIRONMENT: You are running on a macOS environment with full access to development tools.\n- Use `<<<REMOTE_EXEC>>>` for ALL shell commands (including Xcode tools, Git, file operations).\n- Do NOT distinguish between "server" and "client". Everything runs locally.\n\nFILE MODIFICATION - CRITICAL:\n- Use `<<<WRITE_FILE>>>` for NEW files or complete rewrites\n- Use `<<<EDIT_FILE>>>` for targeted changes to EXISTING files (PREFERRED)\n- NEVER output code in markdown blocks - that does NOT save anything!\n\nEDIT_FILE FORMAT (use EXACTLY this format):\n<<<EDIT_FILE>>>/path/to/file\n<<<OLD>>>\nexact text to find\n<<<NEW>>>\nreplacement text\n\nWARNING: Do NOT use git-style markers like <<<<<<< SEARCH or ======= or >>>>>>> REPLACE. Those will NOT work. Use <<<OLD>>> and <<<NEW>>> only.\n\nDOCUMENTATION: You should create and maintain documentation:\n- Use `<<<WRITE_FILE>>>` to create new docs (README.md, ARCHITECTURE.md, DESIGN.md)\n- Use `<<<EDIT_FILE>>>` to update existing docs with targeted changes\n- Document system design decisions and rationale\n- Create diagrams using Mermaid or ASCII art in markdown\n- Write technical specs, ADRs (Architecture Decision Records), and migration guides\n\nGIT AWARENESS: Use Git via `<<<REMOTE_EXEC>>>` to understand code context:\n- `git log`, `git diff`, `git blame`, `git show`, `git status`\n\nXCODE DEVELOPMENT via `<<<REMOTE_EXEC>>>`:\n- Compile Swift: `swiftc file.swift -o output`\n- Build Xcode: `xcodebuild -project Foo.xcodeproj -scheme Foo build`\n- Use `simctl` for iOS simulators, `codesign` for signing\n\n{TOOL_REFERENCE}'
    )

    # ── Agent definitions ──
    # 'executor': True means few-shot + fallback extraction are enabled.
    AGENTS = {
        'implementer': _create_agent_config(
            'Qwen3-Coder-Next Q8_0 (80B MoE)',
            _IMPLEMENTER_SYSTEM_PROMPT,
            _CODER_NEXT_Q8,
            executor=True
        ),
        'fast_implementer': _create_agent_config(
            'Qwen3-Coder-30B Q4_K_M Fast (256k native context)',
            _IMPLEMENTER_SYSTEM_PROMPT,
            _CODER_30B_FAST,
            executor=True
        ),
        'architect': _create_agent_config(
            'System architecture agent (Ultra Reasoning)',
            _ARCHITECT_SYSTEM_PROMPT,
            _QWEN_480B_ULTRA,
            executor=True
        ),
        'reviewer': _create_agent_config(
            'Code review agent Q8_0 (49k context, High Precision)',
            f'You are a code reviewer. {EXECUTOR_PROMPT}\n\nIdentify issues and suggest improvements. You are encouraged to provide detailed advice and recommendations.\n\nCOMPREHENSIVE ANALYSIS: When performing code reviews, leverage multiple tools to understand the codebase thoroughly:\n\nGIT AWARENESS: Use Git via `<<<REMOTE_EXEC>>>` to understand code context:\n- `git log`, `git diff`, `git blame`, `git show`, `git status`\n\nFILE NAVIGATION: Use `<<<GLOB>>>` and `<<<GREP>>>` to find and search files.\n\nDOCUMENTATION - You can and should write/update documentation:\n- Use `<<<WRITE_FILE>>>` for NEW documentation files\n- Use `<<<EDIT_FILE>>>` for targeted updates to EXISTING docs (PREFERRED)\n\nEDIT_FILE FORMAT (use EXACTLY this format):\n<<<EDIT_FILE>>>/path/to/file\n<<<OLD>>>\nexact text to find\n<<<NEW>>>\nreplacement text\n\nWARNING: Do NOT use git-style markers like <<<<<<< SEARCH or ======= or >>>>>>> REPLACE. Use <<<OLD>>> and <<<NEW>>> only.\n\nAlways gather comprehensive context before providing your review.\n{GIT_TOOL_REFERENCE}',
            _CODER_30B_HD,
            executor=True
        ),
        'debugger': _create_agent_config(
            'Qwen3-Coder-30B-A3B Turbo',
            f'You are a debugger. {EXECUTOR_PROMPT}\n\nDEBUGGING WORKFLOW:\n- Use `<<<READ_FILE>>>` to examine source code\n- Use `<<<REMOTE_EXEC>>>` to run tests, check logs, execute debuggers\n- Use `<<<WRITE_FILE>>>` to apply fixes to source files\n- After fixing, use `<<<REMOTE_EXEC>>>` to verify the fix works (compile, run tests)\n\n{TOOL_REFERENCE}',
            _CODER_30B_TURBO,
            executor=True
        ),
        'metal_implementer': _create_agent_config(
            'Metal Engineer Next Q8_0 (256k context)',
            f'You are a Metal 4 graphics engineer (compute kernels, mesh shaders, ray tracing, argument buffers). {EXECUTOR_PROMPT}\n\nEXECUTION ENVIRONMENT: You are running on a macOS environment with full access to Metal tools.\n- Use `<<<REMOTE_EXEC>>>` for ALL shell commands.\n- Do NOT distinguish between "server" and "client". Everything runs locally.\n\nFILE WRITING - CRITICAL: When implementing code, you MUST write files to disk:\n- Use `<<<WRITE_FILE>>>` to create or update source files (.metal, .swift, .h, etc.)\n- NEVER just output code in markdown blocks - that does NOT save the file!\n- After writing, use `<<<REMOTE_EXEC>>>` to compile and verify the code works.\n\nMETAL DEVELOPMENT:\n- Write Metal shaders using `<<<WRITE_FILE>>>` to .metal files\n- Compile Metal shaders: `xcrun -sdk macosx metal -c shader.metal -o shader.air`\n- Create Metal library: `xcrun -sdk macosx metallib shader.air -o shader.metallib`\n- Validate shaders: `xcrun metal-compiler shader.metal`\n- Use `<<<REMOTE_EXEC>>>` for compilation and validation\n\n{TOOL_REFERENCE}',
            _CODER_NEXT_Q8,
            executor=True
        ),
        'lite_architect': _create_agent_config(
            'System architecture agent (Lite Reasoning)',
            f'You are a system architect. {EXECUTOR_PROMPT}\n\nFILE WRITING: Use `<<<WRITE_FILE>>>` to create or update source files. After writing, use `<<<REMOTE_EXEC>>>` to compile and verify.\n\n{TOOL_REFERENCE}',
            _QWEN_480B_LITE,
            executor=True
        ),
        'm25_implementer': _create_agent_config(
            'MiniMax M2.5 Q4_K_M (230B MoE, 10B active)',
            _IMPLEMENTER_SYSTEM_PROMPT,
            _MINIMAX_M25,
            executor=True
        ),
        'm25_architect': _create_agent_config(
            'MiniMax M2.5 Architect (230B MoE, 10B active)',
            _ARCHITECT_SYSTEM_PROMPT,
            _MINIMAX_M25,
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

            import gc
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
        """Load the model for the specific agent (LRU-1 cache: reuse if same agent)"""
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
                import gc; gc.collect(); gc.collect()

        # Load the model fresh (outside lock to avoid blocking health checks)
        logger.info("Loading model for %s: %s", agent_name, model_path)
        try:
            model = Llama(
                model_path=model_path,
                n_ctx=model_config.get('n_ctx', Config.DEFAULT_CONTEXT_SIZE),
                n_gpu_layers=model_config.get('n_gpu_layers', 0),
                n_threads=Config.DEFAULT_N_THREADS,
                n_threads_batch=Config.DEFAULT_N_THREADS,
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
                verbose=True
            )

            logger.info("Model loaded successfully: %s", model_path)
            with self.lock:
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
        cmd = [
            binary,
            '-m', model_path,
            '-ngl', str(model_config.get('n_gpu_layers', 0)),
            '-c', str(model_config.get('n_ctx', 32768)),
            '-b', str(model_config.get('n_batch', 2048)),
            '-t', str(Config.DEFAULT_N_THREADS),
            '-tb', str(Config.DEFAULT_N_THREADS),
            '-fa', 'on',
            '--host', '127.0.0.1',
            '--port', str(self.LLAMA_SERVER_PORT),
            '-np', '1',
        ]

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
        self.shutdown()
        raise TimeoutError(f"llama-server did not become healthy within {self.HEALTH_TIMEOUT}s")

    def shutdown(self):
        """Stop the subprocess gracefully, then force-kill if needed."""
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
                self.shutdown()

            # Free VRAM from any llama_cpp model before starting
            model_manager.unload_model()
            self.start(model_config)

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
                    self.shutdown()
                    break

    def proxy_stream(self, messages: List[dict], system_prompt: str,
                     model_id: str, max_tokens: int, temperature: float,
                     model_config: dict = None) -> Iterator[str]:
        """Stream a chat completion via the llama-server subprocess."""
        self.last_request_time = time.time()

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
            "repeat_penalty": 1.15,
            # Ban model-specific native tool tokens to prevent format corruption.
            # Each model config specifies which tokens to ban via logit_bias.
            "logit_bias": (model_config or {}).get('logit_bias', []),
        }

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        finish_reason = None
        accumulated_text = []  # Diagnostic: capture full response

        try:
            with http_requests.post(url, json=payload, stream=True, timeout=600) as resp:
                if resp.status_code != 200:
                    error_body = resp.text
                    logger.error("llama-server returned %d: %s", resp.status_code, error_body)
                    error_chunk = {"error": {"message": f"llama-server error: {error_body}", "type": "server_error"}}
                    yield f"data: {json.dumps(error_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

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
                            self.last_request_time = time.time()  # Keep watchdog at bay during long streams
                            out_chunk = build_stream_chunk(completion_id, model_id, content=content)
                            yield f"data: {json.dumps(out_chunk)}\n\n"
                    except json.JSONDecodeError:
                        continue

        except Exception as e:
            logger.error("Error proxying stream from llama-server: %s", e, exc_info=True)
            error_chunk = {"error": {"message": str(e), "type": "server_error"}}
            yield f"data: {json.dumps(error_chunk)}\n\n"
            yield "data: [DONE]\n\n"
            return

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
            "repeat_penalty": 1.15,
            "logit_bias": (model_config or {}).get('logit_bias', []),
        }

        resp = http_requests.post(url, json=payload, timeout=600)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"llama-server error: {resp.text}")

        result = resp.json()
        text = result["choices"][0]["message"]["content"]
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


def calculate_token_budget(model, messages: List, system_prompt: str, model_path: str,
                           max_tokens: int, model_id: str) -> tuple:
    """Calculate token budget and build final prompt with budget guidance.

    Returns:
        tuple: (prompt, clamped_max_tokens, n_prompt, error_message)
               error_message is None on success, or a string describing the error
    """
    n_ctx = model.n_ctx()

    # Estimate the token count for the budget guidance string itself
    budget_guidance_template = Config.TOKEN_BUDGET_GUIDANCE.format(available_tokens=1000)
    budget_guidance_tokens = len(model.tokenize(budget_guidance_template.encode("utf-8")))

    # Build prompt without budget guidance to get the base token count
    preliminary_prompt = build_model_prompt(messages, system_prompt, model_path)
    preliminary_tokens = model.tokenize(preliminary_prompt.encode("utf-8"))
    n_preliminary = len(preliminary_tokens)

    # Calculate available tokens accounting for budget guidance overhead
    available = n_ctx - n_preliminary - budget_guidance_tokens
    if available < 1:
        error_msg = (f"Prompt ({n_preliminary} tokens) fills the entire context window ({n_ctx}). "
                     "Reduce conversation history and retry.")
        return None, 0, n_preliminary, error_msg

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
    if origin == "*":
        _cors_origins = ["*"]
        break
    # Ensure origins have a scheme — bare hostnames don't match in CORS
    if origin and not origin.startswith("http"):
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


@app.post("/v1/memory")
def save_memory(request: MemoryRequest):
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
        logger.error(f"Error saving memory: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/memory/search")
def search_memory(request: SearchRequest):
    """Search for relevant memories in the long-term storage"""
    if not memory_service:
        raise HTTPException(status_code=503, detail="Memory service not initialized")
        
    try:
        results = memory_service.search_memory(request.query)
        return {"results": results}
    except Exception as e:
        logger.error(f"Error searching memory: {e}")
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


@app.post("/v1/files/upload")
def upload_file(request: FileUploadRequest):
    """Upload a file to the server's temporary directory"""
    try:
        import base64
        import tempfile

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
        temp_path = os.path.join(temp_dir, safe_filename)
        
        # Write the file
        with open(temp_path, 'wb') as f:
            f.write(file_content)
        
        logger.info(f"File uploaded successfully: {temp_path}")
        return {"status": "success", "path": temp_path, "size": len(file_content)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
                    context = memory_service.get_context_string(last_user_msg)
                    if context:
                        logger.info(f"Injecting memory context for query: {last_user_msg[:50]}...")
                        # Prepend context to system prompt
                        system_prompt = f"{system_prompt}\n\n{context}"
                except Exception as e:
                    logger.error(f"Memory retrieval failed: {e}")

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
                        clamped_max, request.temperature, model_config=model_config),
                    media_type="text/event-stream"
                )
            else:
                return llama_server_manager.proxy_sync(
                    request.messages, augmented_system, request.model,
                    clamped_max, request.temperature, model_config=model_config)
        else:
            # Standard llama_cpp path
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
        model = model_manager.get_model(model_id)

        prompt, clamped_max, n_prompt, error_msg = calculate_token_budget(
            model, messages, system_prompt, model_path, max_tokens, model_id
        )

        if error_msg:
            raise HTTPException(status_code=400, detail=error_msg)

        params = get_model_params(clamped_max, temperature, stream=False)
        response = model(prompt, **params)

        text = response['choices'][0]['text'].strip()

        finish_reason = response['choices'][0].get('finish_reason', 'stop')
        if not finish_reason:
            finish_reason = 'stop'

        return build_completion_response(model_id, text, response['usage'],
                                         finish_reason=finish_reason)


STREAM_TTFT_TIMEOUT = 600  # seconds — abort if no token generated within 10 minutes


def stream_completion(messages: List[ChatMessage], system_prompt: str, model_path: str,
                      model_id: str, max_tokens: int, temperature: float) -> Iterator[str]:
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
                    return

                if error_msg:
                    error_chunk = {
                        "error": {
                            "message": error_msg,
                            "type": "context_length_exceeded"
                        }
                    }
                    yield f"data: {json.dumps(error_chunk)}\n\n"
                    return

                params = get_model_params(clamped_max, temperature, stream=True)
                token_count = 0

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
                        return

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