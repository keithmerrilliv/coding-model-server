# Qwen Multi-Agent Server

A local LLM inference server with a multi-agent CLI client. The server provides an OpenAI-compatible API backed by llama.cpp, supporting multiple model configurations with automatic VRAM management. The client is an agentic coding assistant that can execute shell commands, read/write files, search codebases, and manage long-running tasks.

## Architecture

```
Client (macOS/Linux)                    Server (Linux + GPU)
┌──────────────────┐                   ┌──────────────────────────────────┐
│ src/qwen_client/ │  HTTP/SSE         │ qwen_server.server (FastAPI :5000)│
│  main.py         │◄─────────────────►│  ├─ llama_server backend          │
│  orchestrator.py │  /v1/chat/        │  │   (llama-server subprocess)    │
│  completion.py   │  completions      │  ├─ tool_handlers (server-side)   │
│  compaction.py   │                   │  ├─ memory_service                │
│  commands.py     │                   │  │   (ChromaDB + RAG)             │
│  history.py      │                   │  └─ web_search_service            │
└──────────────────┘                   └──────────────────────────────────┘
```

**Single backend**: Every agent runs on a `llama-server` subprocess, almost
all with `--cpu-moe` — attention sublayers on GPU, MoE expert weights on CPU,
so `ngl` can push nearly all attention layers onto a 16 GB card. The old
in-process `llama_cpp` backend was retired in April 2026; `server.py` now
routes solely through `LlamaServerManager` (`llama_server.py`).

**VRAM coordination**: Only one model is loaded at a time. A model swap waits
for any in-flight request to drain (and for the GPU to actually release VRAM)
before launching the next subprocess, so concurrent agent switches don't OOM.

**On-demand loading**: Models load when first requested and unload when
idle (30-min watchdog; in-flight requests block the kill).

## Quick Start

### Server (Linux with NVIDIA GPU)

```bash
git clone <repo-url> && cd qwen-server
./setup.sh                  # Creates venv, installs dependencies
pip install -e .            # Install qwen_server / qwen_client / qwen_autonomous
cp .env.example .env        # Configure IP, ports, model paths
./start.sh                  # Starts on port 5000
```

The `pip install -e .` step is required after `setup.sh` — it makes the
three packages under `src/` importable and installs the `qwen-client` /
`qwen-autonomous` console scripts into the venv.

Or as a systemd service:
```bash
sudo systemctl enable qwen-server
sudo systemctl start qwen-server
journalctl -u qwen-server -f   # View logs
```

### Client (macOS or Linux)

```bash
./start-client.sh                        # Default agent (implementer)
./start-client.sh --model architect      # Specific agent
./start-client.sh --name my-project      # Named session
```

## Agents

Each agent maps to a model configuration and system prompt. Switch with `/agent <name>` or `@agent_name message`.

| Agent | Role | Model | Active/Total | Context | KV | ngl | Backend |
|-------|------|-------|--------------|---------|-----|-----|---------|
| `implementer` | Default implementation | Qwen3.6-35B-A3B UD-Q4_K_M | 3B/35B | 262K | Q8_0 | 48 cpu_moe | subprocess |
| `deep_implementer` | Deep reasoning | Coder-Next Q8_0 | 3B/80B | 256K | Q8_0 | 48 cpu_moe | subprocess |
| `fast_implementer` | Fast implementation | Coder-30B Q4_K_M | 3B/30B | 256K | Q4_0 | 26 | subprocess |
| `debugger` | Debugging | Coder-30B Q4_K_M | 3B/30B | 131K | Q4_0 | 30 | subprocess |
| `reviewer` | Code review | Coder-30B Q8_0 | 3B/30B | 196K | Q8_0 | 49 cpu_moe | subprocess |
| `deep_reviewer` | Deep judgment | Qwen3.5-122B-A10B Q4_K_M | 10B/122B | **256K** | Q8_0 | 49 cpu_moe | subprocess |
| `architect` | System design | Coder-480B Q2_K_XL | 35B/480B | 32K | Q8_0 | 63 cpu_moe | subprocess |
| `lite_architect` | Lite design | Coder-480B IQ1_M | 35B/480B | 32K | Q8_0 | 63 cpu_moe | subprocess |
| `q36_architect` / `supervisor` | Flagship design | Qwen3.6-27B Q4_K_M (dense) | 27B dense | 131K | Q4_0 | 40 | subprocess |
| `m25_implementer` / `m25_architect` | Implementation / architecture | MiniMax M2.5 Q4_K_M | 10B/230B | 116K | Q4_0 | 62 cpu_moe | subprocess |
| `nemotron` | Fastest brainstorm | Nemotron-3-Nano Q4_K_M | 3.5B/30B | **1M** | Q8_0 | 52 cpu_moe | subprocess |
| `glm` | Implementation | GLM-4.7-Flash Q4_K_M | 3B/30B | 262K | Q8_0 | 47 cpu_moe | subprocess |

Subprocess models use `--cpu-moe` to keep MoE expert weights on CPU, enabling near-max GPU layer offload for attention. Nemotron's Mamba-hybrid architecture (only 6/52 layers need KV cache) allows a full 1M native context on RTX 5080. KV-cache preference is Q8_0 wherever it fits — see `~/.claude/.../memory/feedback_kv_quant_preference.md` for the rationale (KV-quant noise produces diffuse quality degradation harder to manage than smaller context).

## Client Commands

### General
| Command | Description |
|---------|-------------|
| `/help` | Show all commands and available agents |
| `/exit`, `/quit` | Exit the CLI |
| `/agent <name>` | Switch agent |
| `/clear` | Clear conversation history |
| `/resume` | Resume interrupted multi-agent tasks |
| `@agent msg` | Switch agent and send message in one go |

### Session Management
| Command | Description |
|---------|-------------|
| `/sessions` | List all saved sessions with metadata |
| `/session <name>` | Switch to an existing session |
| `/session new <name>` | Create and switch to a new session |
| `/rename <name>` | Rename current session (migrates file) |
| `/context` | Show context window usage |
| `/compact` | Model-generated conversation summary |

### Security
| Command | Description |
|---------|-------------|
| `/permissions` | Cycle permission mode (default/acceptEdits/yolo) |
| `/undo` | Revert the last file modification |

### Tools
| Command | Description |
|---------|-------------|
| `/ingest <path>` | Ingest a PDF into RAG memory |
| `/cupertino <query>` | Search Apple docs (macOS) |
| `/apple <tool> <args>` | Apple Deep Docs MCP |

## Tool System

Agents execute tools by emitting markers in their responses:

| Marker | Purpose |
|--------|---------|
| `<<<REMOTE_EXEC>>>command` | Execute shell command |
| `<<<READ_FILE>>>path` | Read file contents |
| `<<<WRITE_FILE>>>path\ncontent` | Write file |
| `<<<EDIT_FILE>>>path\n<<<OLD>>>\ntext\n<<<NEW>>>\ntext` | Edit file (search/replace) |
| `<<<LIST_DIR>>>path` | List directory |
| `<<<GLOB>>>pattern` | Find files by pattern |
| `<<<GREP>>>pattern\|path\|options` | Search file contents |
| `<<<SAVE_MEMORY>>>fact` | Save to RAG memory |
| `<<<WEB_SEARCH>>>query` | Web search |

### Permission Modes

- **default**: Prompts for approval on every operation
- **acceptEdits**: Auto-approves file operations, prompts for shell commands
- **yolo**: Auto-approves everything (dangerous commands still prompt)

### Safety Features

- **Protected paths**: `.git/`, `.ssh/`, `.env`, etc. always require confirmation
- **Dangerous command detection**: `rm -rf`, `sudo`, `chmod 777`, `git push --force` prompt even in yolo mode
- **Deny rules**: `rm -rf /`, fork bombs unconditionally blocked
- **Write-loop detection**: Blocks after 3 writes to the same file per task
- **Response-level loop detection**: Breaks after 3 identical responses
- **Checkpoint/undo**: File modifications backed up for `/undo`

## Context Management

Three-tier automatic context management:

1. **Microcompaction** (60K chars): Old tool outputs replaced with one-line summaries
2. **Model-generated compaction** (120K chars): LLM summarizes conversation into structured 9-section format
3. **Hard trim** (150K chars): Drops oldest 25% of messages as last resort

Manual compaction available via `/compact`.

## Autonomous Mode

A separate service mode where you submit a markdown spec and the system autonomously develops, tests, and presents software for your review.

```bash
# Submit a spec
python qwen-autonomous submit spec.md

# Watch progress
python qwen-autonomous status <spec_id>

# Review and approve gates
python qwen-autonomous gates
python qwen-autonomous review <gate_id> --approve

# Check event log
python qwen-autonomous events <spec_id>
```

**Pipeline:** Planner (q36_architect) → Architect → *design review gate* → Implementer → *code review gate* → Reviewer + tests → *release gate* → DONE

**Human in the loop:** Every major transition requires your explicit approval — the system blocks at review gates until you approve or reject. Rejection notes feed back into the agent for a retry. If Jira is configured (`JIRA_*` env vars), gates sync to a Jira board with native email notifications so you can approve from anywhere.

**Orchestrator daemon:** Runs as a separate systemd unit (`qwen-orchestrator.service`). Polls the SQLite task store, calls agents via the inference API, runs tests via subprocess. Independent of the interactive client.

See `docs/TUTORIAL.md` for an end-to-end walkthrough of the pipeline.

## API

The server exposes an OpenAI-compatible API. When `ADMIN_API_KEY` is set, all endpoints require an `X-Admin-Key` header.

- `GET /health` — Health check
- `GET /v1/models` — List available agents
- `POST /v1/chat/completions` — Chat completion (streaming/non-streaming)
- `POST /v1/memory` — Save to RAG memory (200K char limit, content-hash dedup)
- `POST /v1/memory/search` — Search RAG memory
- `POST /v1/memory/ingest` — Ingest PDF into memory
- `POST /v1/files/upload` — Upload file to server
- `POST /v1/autonomous/specs` — Submit a markdown spec (autonomous mode)
- `GET /v1/autonomous/specs` — List recent specs
- `GET /v1/autonomous/specs/{id}` — Spec details with gates and events
- `GET /v1/autonomous/gates` — List open review gates
- `POST /v1/autonomous/gates/{id}/respond` — Approve or reject a gate

## Project Structure

PyPA `src` layout — three packages under `src/`, declared in
`pyproject.toml`. Run `pip install -e .` after `setup.sh` to install
them into the venv.

```
qwen-server/
├── pyproject.toml              # Package metadata, deps, console scripts
├── src/
│   ├── qwen_server/            # FastAPI server + orchestrator daemon + shared modules
│   │   ├── server.py           #   FastAPI app, model configs, inference dispatch
│   │   ├── llama_server.py     #   llama-server subprocess manager (VRAM coord)
│   │   ├── orchestrator_daemon.py  # Autonomous mode coordinator (systemd entry)
│   │   ├── tool_handlers.py    #   Tool execution, permissions, file ops
│   │   ├── memory_service.py   #   ChromaDB RAG service
│   │   ├── web_search_service.py
│   │   ├── server_manager.py   #   Apple Deep Docs MCP handshake
│   │   ├── streaming.py        #   SSE chunking, ThinkingStripper
│   │   ├── external_judges.py  #   Claude / Gemini call wrappers (/review + Phase b)
│   │   ├── config.py           #   Server-side Config + agent definitions
│   │   ├── metrics.py          #   GPU sampler + request metrics
│   │   └── code_chunker.py     #   tree-sitter-aware code chunking for RAG
│   ├── qwen_client/            # Modular chat client package
│   │   ├── main.py             #   Chat loop, startup
│   │   ├── __main__.py         #   `python -m qwen_client` entry
│   │   ├── orchestrator.py     #   Agent loop, tool dispatch
│   │   ├── completion.py       #   SSE streaming, retries
│   │   ├── compaction.py       #   Context compaction
│   │   ├── commands.py         #   Slash command handlers
│   │   ├── review.py           #   /review multi-judge fan-out
│   │   ├── autonomous.py       #   `qwen-autonomous` CLI entry
│   │   ├── history.py          #   Session persistence
│   │   ├── config.py           #   Client-side configuration, constants
│   │   ├── models.py           #   Agent theme management
│   │   └── agentic/            #   RAG: scratchpad, planner, budget
│   └── qwen_autonomous/        # Autonomous mode task store + agents
│       ├── db.py               #   SQLite-backed task store (WAL, thread-safe)
│       ├── models.py           #   Pydantic models (Spec, Task, Gate, Event)
│       ├── schema.sql          #   DDL for specs, tasks, artifacts, gates, events
│       ├── planner.py          #   Planner agent (spec → YAML or clarifications)
│       ├── executor.py         #   Execution agents (architect/implementer/reviewer)
│       ├── jira_client.py      #   Jira interface (FakeJiraClient + real Atlassian)
│       └── jira_sync.py        #   Bidirectional sync (SQLite ↔ Jira)
├── tests/                      # Real dev tests (currently a placeholder)
├── scripts/                    # Operational scripts (auto-approve, stats, profiling)
├── systemd/                    # Service units (use `python -m qwen_server.X` ExecStart)
├── tools/                      # llama-server binary + shared libs
├── scraping/                   # Apple documentation scraper
├── dashboard/                  # TypeScript React dashboard
├── mac_runner/                 # Separate Swift/Xcode test runner service
└── docs/
    ├── TUTORIAL.md             #   End-to-end pipeline tutorial
    ├── CONFIGURATION.md        #   Env vars, agent-config knobs, systemd
    ├── RAG_UPDATES.md          #   RAG database + agentic query layer
    └── XCODEGEN_GUIDE.md       #   Xcode project generation (mac_runner)
```
