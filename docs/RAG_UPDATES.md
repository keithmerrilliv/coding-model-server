# RAG Database & Query Strategy Updates

This document describes the recent overhaul of the RAG (Retrieval-Augmented Generation) system powering the multi-agent server. The changes span database quality, ingestion strategy, and the client-side agentic query layer.

**Related docs:** [TUTORIAL.md](TUTORIAL.md) (Section 3.5: RAG Memory System), [README.md](../README.md) (API section)

---

## 1. Database Cleanup: 842K to 85K Documents

The single highest-impact change was purging 757,000 low-quality entries from ChromaDB.

### What was removed

An early bulk ingestion script (`ingest_local_dev.py`) had walked the entire project tree and ingested every source file using naive character-count chunking (fixed 1000-char windows with 200-char overlap). This produced chunks that:

- Split mid-function, mid-comment, or mid-string literal
- Contained no semantic boundaries (a chunk could start halfway through a `for` loop)
- Had no structural metadata (no node type, no parent context)
- Flooded retrieval results with irrelevant code fragments

### What remains (~85K documents)

| Source | Count | Description |
|--------|-------|-------------|
| Agent memories | ~75K | Facts and decisions saved via `<<<SAVE_MEMORY>>>` during agent sessions |
| Markdown docs | ~9K | Ingested documentation (Metal specs, API references, project docs) |
| PDFs | ~815 | Technical PDFs ingested via `/ingest` (feature specs, shading language docs) |

### Impact on retrieval

With the bulk code removed, retrieval precision improved significantly. Queries that previously returned 3-5 irrelevant code fragments now return actual design decisions, documented facts, and relevant API descriptions. The relevance threshold (cosine distance <= 0.35) remained unchanged but became effective again because the noise floor dropped.

Database size shrank from 3.8 GB to 2.2 GB after a `VACUUM`.

---

## 2. AST-Aware Code Chunking (CodeChunker)

The old bulk ingestion is replaced by on-demand, structure-aware ingestion via the `/ingest-code` client command.

### How it works

`CodeChunker` (`src/coding_model_server/code_chunker.py`) uses tree-sitter to parse source files into ASTs and extract chunks at semantically meaningful boundaries:

```
Source file  -->  tree-sitter AST  -->  Walk nodes  -->  Match chunk types  -->  Emit chunks
```

Each chunk corresponds to a complete syntactic unit (a function, class, struct, protocol, etc.) rather than an arbitrary character window.

### Supported languages (25)

| Category | Languages |
|----------|-----------|
| Apple / Systems | Swift*, Metal (as C++), C, C++, Objective-C |
| Python | Python |
| JS / TS | JavaScript, TypeScript, TSX |
| Shell | Bash |
| Web | HTML, CSS |
| Data / Config | JSON, YAML, TOML, Markdown |
| Other | Go, Rust, Java, Kotlin, C#, Ruby, Perl, Scala, PHP, Lua, SQL |
| Build | Make, Dockerfile |

\* Swift falls back to sliding-window chunking (tree_sitter_languages does not include Swift).

### Chunk type extraction

Each language has defined AST node types that map to "chunk boundaries." For example:

- **Python:** `class_definition`, `function_definition`
- **C++/Metal:** `class_specifier`, `function_definition`, `struct_specifier`, `namespace_definition`, `template_declaration`
- **Swift:** `class_declaration`, `function_declaration`, `struct_declaration`, `enum_declaration`, `protocol_declaration`, `extension_declaration`
- **Go:** `function_declaration`, `method_declaration`, `type_declaration`
- **Rust:** `function_item`, `impl_item`, `struct_item`, `enum_item`, `trait_item`, `mod_item`

### Metadata preserved per chunk

Each chunk stored in ChromaDB carries:

| Field | Example | Purpose |
|-------|---------|---------|
| `source` | `Renderer.swift` | File origin |
| `node_type` | `function_definition` | AST node type |
| `context` | `> class_specifier > function_definition` | Parent chain for nested definitions |
| `chunk_index` | `3` | Position within the file |

### Fallback behavior

When tree-sitter cannot parse a file (unsupported language, parse error), `CodeChunker` falls back to sliding-window chunking (3000 chars, 300 overlap) — the same algorithm, but with larger windows and overlap than the old bulk script.

---

## 3. Agentic Query Layer

The client now runs a five-component agentic context system (`coding_model_client/agentic/`) that wraps every completion request. This layer operates entirely client-side with zero additional model calls for classification.

### 3.1 Query Classification

A regex-based classifier (`classifier.py`) categorizes each user message into a query type that determines the agent's tool-use budget:

| Type | Triggers | Budget |
|------|----------|--------|
| **LOCATE** | "where is", "find the", "which file", "path to" | 8 iterations |
| **EXPLAIN** | "how does", "explain", "what is", "architecture" | 20 iterations |
| **DEBUG** | "error", "crash", "bug", "fix", "broken", "not working" | 30 iterations |
| **IMPLEMENT** | "build", "create", "implement", "add feature", "write a function" | 80 iterations |
| **REFACTOR** | "refactor", "rename", "move to", "reorganize", "migrate" | 40 iterations |
| **GENERAL** | (fallback) | 25 iterations |

Pattern matching is priority-ordered (first match wins). No model inference is needed.

### 3.2 Retrieval Budget

`RetrievalBudget` (`budget.py`) counts tool iterations per task and enforces limits:

- **At 75% budget:** A warning is injected into the next prompt telling the model to focus on synthesizing from gathered information
- **At 100% budget:** The orchestrator forces synthesis — no further tool calls are allowed (soft limit for IMPLEMENT tasks)

This prevents runaway retrieval loops where the model keeps searching without converging.

### 3.3 Scratchpad (Working Memory)

`Scratchpad` (`scratchpad.py`) gives the model a persistent working memory within a task. The model writes to it via `<<<SCRATCHPAD>>>` markers with three sections:

- **FACTS:** Confirmed information gathered so far
- **OPEN_QUESTIONS:** What still needs to be resolved
- **DEAD_ENDS:** Approaches that were tried and failed

The scratchpad state is injected into every subsequent completion, letting the model build on prior iterations rather than re-deriving context.

### 3.4 Retrieval Plan

`RetrievalPlan` (`planner.py`) lets the model outline a multi-step retrieval strategy via `<<<PLAN>>>` markers:

```
GOAL: Understand how the render pipeline handles transparency
STEPS:
[x] 1. Find the main render pass file
[x] 2. Read the transparency sorting logic
[ ] 3. Check how blend states are configured
CURRENT: 3
```

The plan persists across iterations and is re-injected each turn, keeping the model on track during complex investigations.

### 3.5 Confidence Gate

`ConfidenceGate` (`confidence.py`) allows the model to self-report confidence (0-100) via `<<<CONFIDENCE>>>N`:

| Score | Interpretation | Action |
|-------|---------------|--------|
| < 50 | Insufficient information | Prompt to continue retrieval |
| 50-69 | Neutral | No injection |
| >= 70 | Sufficient to answer | Prompt to synthesize |

The confidence gate interacts with budget exhaustion — if the budget runs out but confidence is low, the model is still forced to synthesize with what it has.

### Combined injection

All five components are aggregated by `AgenticContext` (`context.py`) into a single injection block appended to the conversation before each completion:

```
## Query Classification: IMPLEMENT

## WORKING MEMORY (Scratchpad)
FACTS:
- The render pipeline uses a two-pass approach...
OPEN_QUESTIONS:
- How are blend states configured?

## RETRIEVAL PLAN
GOAL: ...
STEPS: ...

BUDGET WARNING: You have used 60/80 retrieval steps. Only 20 steps remain.
```

---

## 4. Server-Side RAG Integration

The server (`src/coding_model_server/server.py`) automatically injects RAG context into every completion request:

1. Extract the last user message from the conversation
2. Embed it via `SentenceTransformer('all-MiniLM-L6-v2')` (384-dim, CPU-bound)
3. Query ChromaDB with cosine similarity, top-5 results
4. Filter by relevance threshold (cosine distance <= 0.35)
5. Format as numbered list under `## RELEVANT MEMORIES (FACTS & DECISIONS):`
6. Append to the system prompt

This runs async with a **2-second hard timeout** to prevent stalls from large databases or slow CPU embedding. If the timeout fires, the completion proceeds without RAG context.

### Authentication

All client requests (completions, memory, search, ingestion, model listing) include an `X-Admin-Key` header when `ADMIN_API_KEY` is configured. This is handled centrally by `Config.auth_headers` (`coding_model_client/config.py`).

### Deduplication

`add_memory()` computes an MD5 content hash before storing. If an identical document already exists in ChromaDB (matched via `content_hash` metadata), the duplicate is skipped and the existing ID is returned. This prevents the same fact or code chunk from accumulating multiple entries during re-ingestion.

### Input validation

The `POST /v1/memory` endpoint enforces a `max_length=200_000` character limit on the `text` field (~100 KB), matching the client-side file-size cap for `/ingest-code`.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_RELEVANCE_THRESHOLD` | 0.35 | Max cosine distance for inclusion |
| `PDF_CHUNK_SIZE` | 1000 | Character chunk size for PDF ingestion |
| `PDF_CHUNK_OVERLAP` | 200 | Overlap between PDF chunks |
| `ADMIN_API_KEY` | (empty) | API key for `X-Admin-Key` authentication |

---

## 5. Ingestion Paths

| Method | Entry Point | Chunking | Use Case |
|--------|-------------|----------|----------|
| `<<<SAVE_MEMORY>>>` | Agent response marker | Single document (no chunking) | Saving facts, decisions, findings during a session |
| `/ingest <path>` | Client command | Character-window (1000 chars) | PDF technical documents |
| `/ingest-code <dir>` | Client command | AST-aware via CodeChunker | On-demand codebase indexing |
| `POST /v1/memory` | HTTP API | Chunked if `source` provided | Programmatic ingestion |

---

## 6. Maintenance

### Inspecting the database

```bash
source venv/bin/activate
python3 scripts/rag_utils.py count          # Total document count
python3 scripts/rag_utils.py recent 10      # Last 10 entries
python3 scripts/rag_utils.py inspect <id>   # Full metadata for an entry
```

### Cleaning junk entries

`scripts/cleanup_memory.py` scans for and removes:
- PDF table-of-contents noise (dotted leader lines)
- Leaked model thinking tokens — flagged when >50% of lines match thinking patterns (e.g., `<think>`, `Maybe user expects`, `Steps:`)
- Empty or near-empty documents

### Reclaiming disk space

```bash
# Stop the server first
sqlite3 var/memory_db/chroma.sqlite3 "VACUUM;"
```

### Purging by source pattern

`scripts/purge_bulk_code.py` removes all documents whose `source` metadata matches a given pattern. This was used to remove the 757K bulk-ingested code entries.
