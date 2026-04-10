# Work Summary: Qwen Multi-Agent Client & Server

## 1. Project Overview
This project is a local LLM inference server with a multi-agent CLI client. The server provides an OpenAI-compatible API backed by llama.cpp (both in-process and subprocess backends), with automatic VRAM management on an NVIDIA RTX 5080. The client is an agentic coding assistant with tool execution, RAG memory, session management, and a five-component agentic context system. The system runs 13 agent configurations spanning models from 30B to 480B parameters.

## 2. Key Features
*   **Multi-Agent Architecture:** 13 agents with specialized roles and model configurations:
    *   `implementer` (Qwen3.5-35B, default), `deep_implementer` (Coder-Next 80B), `fast_implementer` (Coder-30B)
    *   `debugger` (Coder-30B), `reviewer` (Coder-30B Q8_0)
    *   `architect` (Coder-480B Q2_K_XL), `lite_architect` (Coder-480B IQ1_M), `q35_architect` (Qwen3.5-122B), `q35_ultra` (Qwen3.5-397B)
    *   `m25_implementer` / `m25_architect` (MiniMax M2.5 230B)
    *   `nemotron` (Nemotron-3-Nano, 1M native context), `glm` (GLM-4.7-Flash)
*   **Dual Backend:** Models run either in-process via llama-cpp-python or as a llama-server subprocess. The subprocess backend enables `--cpu-moe` (MoE expert weights on CPU), native Jinja templates, and architectures not yet in llama-cpp-python.
*   **Agentic RAG:** Client-side query classification, retrieval budget, scratchpad working memory, retrieval planning, and confidence gating — all operating without extra model calls. Server-side ChromaDB vector search with automatic system prompt injection.
*   **Tool System:** Agents emit structured markers (`<<<REMOTE_EXEC>>>`, `<<<READ_FILE>>>`, `<<<WRITE_FILE>>>`, `<<<EDIT_FILE>>>`, `<<<GLOB>>>`, `<<<GREP>>>`, `<<<SAVE_MEMORY>>>`, `<<<WEB_SEARCH>>>`) processed by the client with three permission tiers and safety guards.
*   **Context Management:** Two-tier automatic compaction (model-generated summary at 120K chars, hard trim at 150K) with send-time compression preserving KV-cache stability. Manual `/compact` available.
*   **Session Management:** Named sessions with persistent history, context tracking, and cross-session switching.
*   **Modular Client:** Refactored from a monolithic script (`qwen_remote.py`) into a package (`qwen_client/`) with separate modules for orchestration, completion, compaction, commands, history, config, and the agentic layer.

## 3. Architecture & Technical Configuration
*   **Server (`server.py`):**
    *   FastAPI with OpenAI-compatible endpoints (`/v1/chat/completions`, `/v1/models`, `/v1/memory`).
    *   LRU-1 model cache: only one model loaded at a time, mutual exclusion between backends.
    *   Per-model configs: `n_gpu_layers`, `n_ctx`, `n_batch`, `type_k`/`type_v` (KV cache quantization), `repeat_penalty`, `cpu_moe`, `backend` selection.
    *   Subprocess backend manages a `llama-server` child process with health polling, watchdog timeout (10 min idle), and SSE stream proxying.
    *   Token budget calculation injected into system prompt so models know remaining response space.
    *   RAG context retrieval: SentenceTransformer embedding + ChromaDB cosine search, 2-second async timeout.
*   **Client (`qwen_client/`):**
    *   Interactive CLI with readline, persistent command history, and agent themes (colored prompts per agent).
    *   Orchestrator loop: send completion → parse response → extract tool markers → execute tools → inject results → repeat until agent signals done or budget exhausted.
    *   Agentic context system: classifier, budget, scratchpad, planner, confidence gate aggregated by `AgenticContext`.
    *   Thinking-tag stripper for models that emit `<think>` blocks (Qwen3.5, etc.).
    *   Few-shot example injection for short conversations to bootstrap tool marker syntax.

## 4. Development Timeline

### Phase 1: Foundation & Optimization (Jan 3 – Jan 5, 2026)
*   Established FastAPI server and basic client connectivity.
*   VRAM optimization: tuned `n_gpu_layers`, `n_batch` to fit models within 16 GB RTX 5080.
*   Introduced agent specialization (smaller fast model for implementation, 480B for architecture).
*   Context expansion up to 512K with YaRN scaling.

### Phase 2: Client Enhancements & Stability (Jan 24 – Jan 27)
*   Persistent chat history and command history with readline.
*   Service hardening via systemd (`StartLimitIntervalSec`, `StartLimitBurst`).
*   `@agent` syntax for inline agent switching, persistent agent state across sessions.

### Phase 3: Robustness & Capability Expansion (Jan 28 – Jan 29)
*   Architect upgraded to Qwen3-Coder-480B (IQ1_M) loaded in RAM.
*   Task queue (`PENDING_TASKS`) and `/resume` for interrupted multi-agent workflows.
*   Sequential tool call support: agents perform multi-step actions (search → read → plan) in a single turn.
*   `<<<READ_FILE>>>` tool for safe read-only code access without shell risks.

### Phase 4: Metal 4 Integration & Hardware Tuning (Jan 30 – Feb 1)
*   Ingested 871 chunks of Metal 4 documentation (Feature Sets, Shading Language Spec).
*   Deployed `metal_implementer` agent for graphics programming.
*   Standardized Implementer/Debugger on Coder-30B with 80K context and 32 GPU layers (~94% VRAM).

### Phase 5: Client Modularization & Tool Refinement (Feb 1 – Feb 8)
*   Apple documentation scraping pipeline (`/scrape` command) with auto-ingestion into ChromaDB.
*   Apple Deep Docs MCP integration for live documentation queries.
*   PDF ingestion endpoint (`/v1/memory/ingest`) and client-side `/ingest` command with file upload.
*   Tree-sitter AST-aware code chunking (`CodeChunker`) supporting 25 languages, replacing naive character-split ingestion.
*   Server-side chunked memory ingestion via `add_memory_chunked()`.
*   Forced CPU for SentenceTransformer embeddings to avoid RTX 5080 sm_120 CUDA conflicts.
*   Load-on-demand architecture: eliminated model caching, explicit load/unload per request.
*   Token budget awareness: clamped `max_tokens` to remaining context budget to prevent truncation.

### Phase 6: Subprocess Backend & New Models (Feb 6 – Feb 20)
*   Integrated `llama-server` as a subprocess backend for models needing native Jinja templates or unsupported architectures.
*   Added `deep_implementer` (Coder-Next Q8_0, 256K native context) and `fast_implementer` (Coder-30B Q4_K_M).
*   MiniMax M2.5 230B support (`m25_implementer`, `m25_architect`) with per-model llama-server config.
*   Split `requirements.txt` into server and client files.
*   Fixed memory service 503 errors and continuation tag corruption.

### Phase 7: Security Hardening & Major Refactor (Mar 9 – Mar 27)
*   Comprehensive security audit: protected paths, dangerous command detection, deny rules, write-loop detection, response-level loop detection.
*   Modularized client from monolithic `qwen_remote.py` into `qwen_client/` package (orchestrator, completion, compaction, commands, history, config, models).
*   Added agentic RAG layer: query classifier, retrieval budget, scratchpad, planner, confidence gate.
*   5 new model configs: Qwen3.5-35B/122B/397B, Nemotron-3-Nano, GLM-4.7-Flash.
*   Q4_0 KV cache quantization option for VRAM-constrained models.
*   10 Claude Code-inspired UX features for the client.

### Phase 8: Performance, Stability & Flagship Models (Mar 28 – Apr 1)
*   `--cpu-moe` optimization: MoE expert weights on CPU, enabling near-max GPU layer offload for attention layers. Unlocked 1M native context for Nemotron (Mamba-hybrid, only 6/52 layers need KV cache).
*   Swapped default implementer to Qwen3.5-35B (262K context, in-process backend).
*   Upgraded llama-cpp-python to 0.3.19, moved Qwen3.5 to in-process backend.
*   Prefill optimization: `-ub 4096` + `--swa-full` for 4.6x faster Coder-Next prefill.
*   q35_ultra moved to llama_server with `--cpu-moe`: 5 min → 26 sec per turn, then ubatch 4096 for higher prefill throughput. Run `benchmark_prefill.py` to measure current speeds.
*   Two-tier context compaction system (model-generated summary, hard trim) with send-time compression for KV-cache stability.
*   Session management: named sessions, `/sessions`, `/session`, `/rename`, `/context`.
*   ThinkingStripper fixes for Qwen3.5 `<think>` tags, non-thinking models, and unclosed tag leaks.
*   Content sanitizer for all file writes (strips conflict markers, thinking tags).
*   Comprehensive documentation rewrite: README, CONFIGURATION, and new 10-stage TUTORIAL.

### Phase 9: RAG Database Overhaul & Hardening (Apr 2 – Apr 8)
*   Purged 757K low-quality bulk-ingested code entries from ChromaDB (842K → 85K documents, 3.8 GB → 2.2 GB).
*   Added `cleanup_memory.py` to remove junk entries (PDF TOC noise, leaked thinking tokens with >50% line-match threshold).
*   Added client-side `/ingest-code` command with AST-aware chunking via CodeChunker, replacing the old bulk ingestion script.
*   Remaining corpus: ~75K agent memories, ~9K markdown docs, ~815 PDF chunks.
*   Hardened RAG memory system: MD5 content-hash deduplication in `add_memory()`, `max_length=200_000` input validation on the memory endpoint, and `X-Admin-Key` authentication across all client API calls.
*   See [RAG_UPDATES.md](RAG_UPDATES.md) for full technical details.

## 5. Current Status
As of April 2026, the project runs 13 agent configurations across two backends on an RTX 5080. The default implementer is Qwen3.5-35B with 262K context. The flagship reasoning agent (q35_ultra, Qwen3.5-397B) achieves 26-second turn times via `--cpu-moe`. The RAG database holds ~85K high-quality documents after a major cleanup, with AST-aware ingestion available on demand. The client is a fully modular package with agentic context (query classification, budget enforcement, working memory, retrieval planning, confidence gating), three-tier context compaction, session management, and comprehensive safety guards.
