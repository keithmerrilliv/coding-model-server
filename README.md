# Coding Model Multi-Agent Server

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A local LLM inference server with a multi-agent CLI client. The server provides an OpenAI-compatible API backed by llama.cpp, supporting multiple model configurations with automatic VRAM management. The client is an agentic coding assistant that can execute shell commands, read/write files, search codebases, and manage long-running tasks.

## Architecture

```
Client (macOS/Linux)                     Server (Linux + GPU)
┌──────────────────────────┐            ┌───────────────────────────────────────┐
│ src/coding_model_client/ │  HTTP/SSE  │ coding_model_server.server (FastAPI    │
│  main.py                 │◄──────────►│   :5000)                              │
│  orchestrator.py         │  /v1/chat/ │  ├─ routes/ (chat, memory, autonomous,│
│  completion.py           │  completions│  │    admin, meta)                    │
│  compaction.py           │            │  ├─ llama_server backend              │
│  commands.py             │            │  │    (one llama-server subprocess)    │
│  history.py              │            │  ├─ memory_service (ChromaDB + RAG)    │
│                          │            │  ├─ web_search_service                 │
│  imports coding_model_   │            │  ├─ mcp_service (Apple Deep Docs)      │
│  server.tool_handlers    │            │  └─ orchestrator_daemon (autonomous)   │
│  and runs tools LOCALLY  │            └───────────────────────────────────────┘
└──────────────────────────┘
```

**Tools run on the client.** `tool_handlers/` ships inside the `coding_model_server`
package, but the server never executes a tool — it only emits markers in the
model's response. The client imports that package and runs shell commands and
file edits on the operator's own machine, under the operator's permission mode.

**Single backend**: Every agent runs on a `llama-server` subprocess, almost
all with expert offload (`--cpu-moe` / `--n-cpu-moe`) — attention sublayers on
GPU, MoE expert weights on CPU, so `ngl` can push nearly all attention layers
onto a 16 GB card. The old in-process `llama_cpp` backend was retired in April
2026; there is no backend switch left to make. `routes/chat.py` routes solely
through `LlamaServerManager` (`llama_server.py`).

**VRAM coordination**: Only one model is loaded at a time. A model swap waits
for any in-flight request to drain (and for the GPU to actually release VRAM)
before launching the next subprocess, so concurrent agent switches don't OOM.

**On-demand loading**: Models load when first requested and unload when
idle (30-min watchdog; in-flight requests block the kill).

## Quick Start

### Server (Linux with NVIDIA GPU)

```bash
git clone <repo-url> && cd coding-model-server
./bin/setup.sh              # Creates venv, runs `pip install -e .`, sets up .env
cp .env.example .env        # (bin/setup.sh does this too; edit IP, ports, model paths)
./bin/start.sh              # Starts on port 5000
```

Dependencies live in `pyproject.toml` (single source of truth). `bin/setup.sh`
runs `pip install -e .`, which installs the core deps, makes the three `src/`
packages importable, and wires the `coding-model-client` / `coding-model-autonomous` console
scripts. For the client's optional niceties (rich output, scraping) add the
extra: `pip install -e '.[client]'`.

The `tools/llama-server` binary must be present — it is the only inference
backend, and `setup.sh` does not fetch it for you.

Or as a systemd service:
```bash
sudo systemctl enable coding-model-server
sudo systemctl start coding-model-server
journalctl -u coding-model-server -f   # View logs
```

The repo ships four units in `systemd/`: `coding-model-server` (inference API),
`coding-model-orchestrator` (autonomous mode), `coding-model-dashboard` (static
dashboard on :3001), and `coding-model-monitor` (resource sampler).

After pulling code or editing a `systemd/*.service` unit, redeploy the running
services (syncs units, reloads, restarts server → orchestrator → dashboard in
order, waits for `/health`):
```bash
sudo bash scripts/redeploy.sh
```
No reinstall needed — the venv is an editable install, so a restart picks up
code changes. The script backs up the installed units before overwriting.

### Client (macOS or Linux)

```bash
./bin/start-client.sh                    # Default agent (implementer)
./bin/start-client.sh --model architect  # Specific agent
./bin/start-client.sh --name my-project  # Named session
```

`pip install -e .` also puts `coding-model-client` on your PATH; it takes the
same flags.

## Agents

Each agent maps to a model configuration and system prompt, both defined in
`src/coding_model_server/config.py` (`Config.AGENTS`). Switch with
`/agent <name>` or `@agent_name message`.

Decode tok/s measured end-to-end on an RTX 5080, 2026-07-14 (see
[TUTORIAL.md](docs/TUTORIAL.md#stage-6-token-generation-autoregressive-decoding)
for method, prefill figures, and the caveat about raw-vs-proxy numbers).

| Agent | Role | Model | Active/Total | Context | KV | GPU offload | Decode tok/s |
|-------|------|-------|--------------|---------|-----|-------------|-----:|
| `implementer` | Default implementation | Qwen3.6-35B-A3B UD-Q4_K_M | 3B/35B | 64K | Q8_0 | ngl 41, n_cpu_moe 20 | 75.5 |
| `deep_implementer` | Deep reasoning | Qwen3-Coder-Next Q8_0 | 3B/80B | 256K | Q8_0 | ngl 48, cpu_moe | 26.9 |
| `fast_implementer` | Fast implementation | Qwen3-Coder-30B Q4_K_M | 3B/30B | 64K | Q8_0 | ngl 49, n_cpu_moe 26 | 58.2 |
| `debugger` | Debugging | Qwen3-Coder-30B Q4_K_M | 3B/30B | 128K | Q8_0 | ngl 49, cpu_moe | 37.1 |
| `reviewer` | Code review | Qwen3-Coder-30B Q8_0 | 3B/30B | 192K | Q8_0 | ngl 49, cpu_moe | 26.0 |
| `deep_reviewer` | Deep judgment | Qwen3.5-122B-A10B Q4_K_M | 10B/122B | 256K | Q8_0 | ngl 49, cpu_moe | 20.0 |
| `architect` | System design | Qwen3.6-27B MTP Q4_K_M (dense) | 27B dense | 128K | Q4_0 | ngl 36, MTP speculative decode | 10.8 |
| `dense_architect` | Autonomous planner + architect | Qwen3.6-27B MTP Q4_K_M (dense) | 27B dense | 128K | Q4_0 | ngl 36, MTP speculative decode | 10.8 |
| `supervisor` | Retry/fail/replan decisions | Qwen3.6-27B MTP Q4_K_M (dense) | 27B dense | 128K | Q4_0 | ngl 36, MTP speculative decode | 11.4 |
| `moe_implementer` / `moe_architect` | Implementation / architecture | MiniMax M2.5 Q4_K_M | 10B/230B | 116K | Q4_0 | ngl 62, cpu_moe | 11.2 |
| `brainstorm` | Fastest brainstorm | Nemotron-3-Nano Q4_K_M | 3.5B/30B | **1M** | Q8_0 | ngl 52, cpu_moe | 40.1 |
| `native_implementer` | Implementation (native tools) | GLM-4.7-Flash Q4_K_M | 3B/30B | 64K | Q8_0 | ngl 47, n_cpu_moe 20 | 59.7 |

`supervisor` is decision-only: it is always called with native tools (a
`decide()` function call) and never gets marker-based shell tools.
`brainstorm` has no tools at all.

**Expert offload.** `cpu_moe=True` (`--cpu-moe`) keeps *all* MoE expert weights
on CPU. `n_cpu_moe=N` (`--n-cpu-moe N`) keeps only the first N layers' experts
on CPU and pushes the rest onto the GPU — faster decode, bounded by VRAM. The
three agents tuned with `n_cpu_moe` trade context for decode speed; see the
per-config comments in `config.py`, which record the measurements. Nemotron's
Mamba-hybrid architecture (only 6/52 layers need a KV cache) allows a full 1M
native context on an RTX 5080. KV-cache preference is Q8_0 wherever it fits —
KV-quant noise produces diffuse quality degradation that is harder to manage
than a smaller context.

**Legacy names** still resolve (`Config.AGENT_ALIASES`) so old sessions and
`.env` defaults keep working, though they aren't listed in `/v1/models`:
`q36_architect` → `dense_architect`, `m25_architect` → `moe_architect`,
`m25_implementer` → `moe_implementer`, `glm` → `native_implementer`,
`nemotron` → `brainstorm`.

## Client Commands

### General
| Command | Description |
|---------|-------------|
| `/help` | Show all commands and available agents |
| `/exit`, `/quit` | Exit the CLI |
| `/agent <name>` | Switch agent |
| `/clear` | Clear conversation history |
| `/resume` | Resume interrupted multi-agent tasks |
| `/history`, `/history clear` | Show or clear command history |
| `@agent msg` | Switch agent and send message in one go (multiple `@`s allowed) |

### Session & Display
| Command | Description |
|---------|-------------|
| `/sessions` | List all saved sessions with metadata |
| `/session <name>` | Switch to an existing session |
| `/session new <name>` | Create and switch to a new session |
| `/rename <name>` | Rename current session (migrates file) |
| `/context` | Show context window usage |
| `/compact` | Model-generated conversation summary |
| `/verbose` | Toggle verbose vs compact tool-output display |

### Security
| Command | Description |
|---------|-------------|
| `/permissions` | Cycle permission mode (default/acceptEdits/yolo) |
| `/workspace [dir]` | Show or set where the agent may write. Defaults to a temp dir; writes outside it are refused in every mode. |
| `/undo` | Revert the last file modification |

### Tools
| Command | Description |
|---------|-------------|
| `/review` | Fan the uncommitted git diff out to 4 judges (Claude, Gemini, `reviewer`, `deep_reviewer`) |
| `/ingest <path>` | Ingest a PDF into RAG memory (`local:` prefix for client-side files) |
| `/ingest-code <dir>` | Ingest a codebase with AST-aware chunking |
| `/cupertino <query>` | Search Apple docs (macOS, local MCP) |
| `/apple <tool> <args>` | Apple Deep Docs MCP (server-side) |
| `/scrape [framework]` | Run the documentation scraper (default: Metal) |

## Tool System

Agents execute tools by emitting markers in their responses. The full reference
the model sees is `Config.BASE_TOOLS` in `src/coding_model_server/config.py`.

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
| `<<<CUPERTINO>>>query` | Apple docs (local MCP) |
| `<<<APPLE_DEEP_DOCS>>>{"tool":...}` | Apple docs (server MCP) |
| `<<<INGEST_PDF>>>path` | Ingest a PDF into memory |
| `<<<SCRATCHPAD>>>` | Update working memory (FACTS, OPEN_QUESTIONS, DEAD_ENDS) |
| `<<<PLAN>>>` | Create/update a retrieval plan |
| `<<<CONFIDENCE>>>N` | Report confidence 0–100 |

Interactive agents also get a **continuation protocol**: told the response won't
fit the output budget, the model ends with `<<<CONTINUE>>>` and a `REMAINING:`
list, and the client automatically requests the rest, feeding the model's own
REMAINING list back to it (up to 5 continuations). The same happens on a hard
cut-off (`finish_reason == "length"`). Programmatic callers get a
budget-guidance variant with no continuation protocol, because a single-shot
request has no continuation turn.

### Permission Modes

- **default**: Prompts for approval on every operation
- **acceptEdits**: Auto-approves file operations, prompts for shell commands
- **yolo**: Auto-approves file operations. Shell commands *still* prompt unless
  `ALLOW_REMOTE_EXEC_YOLO=1` is also set — and even then, only allow-listed
  commands run silently.

### Safety Features

- **Allow-list for unattended shell** (not a denylist): a command runs without a
  prompt only if its base binary is in `READONLY_COMMANDS`, `BUILD_TEST_COMMANDS`,
  or is a read-only `git` subcommand — and contains no shell metacharacters
  (`| & ; $ \` > < ( )`), which could chain past the check. Anything unrecognised
  prompts. Extend with `EXTRA_AUTO_APPROVE_COMMANDS`.
- **Protected paths**: always require confirmation, in every permission mode.
  Covers version control and key material (`.git/`, `.ssh/`, `.gnupg/`), system
  dirs (`/etc/`, `/usr/`, `/bin/`, `/sbin/`, `/root/`, `/var/db/`), macOS secret
  stores (Keychains, Safari/Chrome/Firefox cookie and password stores), cloud and
  registry credentials (`~/.aws/`, `~/.config/gcloud/`, `~/.kube/`, `~/.docker/`,
  `~/.netrc`, `~/.git-credentials`, `~/.npmrc`, `~/.pypirc`), and files like
  `.env`, `id_rsa`, `authorized_keys`.

  Both the candidate path *and* the protected roots are resolved through
  `realpath`, so a symlink can't smuggle a path past the check — and a
  platform symlink (macOS maps `/etc` to `/private/etc`) can't accidentally
  disable one either. Matching is boundary-aware, so `/usrfoo` does not match
  `/usr`.

  This list matters more than it looks: reads are **not** confined to the
  workspace (the agent needs to read source elsewhere for context), and the agent
  also has outbound-fetch tools — so an unprompted read of a secret store is the
  first half of an exfiltration primitive.
- **Dangerous command warnings**: `rm -r`, `sudo`, `chmod 777`, `git push --force`
  warn even in yolo mode.
- **Deny rules**: `rm -rf /`, `rm -rf ~`, `find / -delete`, `shutil.rmtree('/')`,
  raw block-device writes, fork bombs — unconditionally blocked. These are a
  *backstop*, not the security boundary; the allow-list above is.
- **Write-loop detection**: Blocks after 3 writes to the same file per task
- **Response-level loop detection**: Breaks after 3 identical responses
- **Checkpoint/undo**: File modifications backed up for `/undo`

## Context Management

Two automatic tiers, on top of a per-request compression pass:

- **Compression** (every request): older tool outputs (>500 chars) and large
  assistant messages (>2000 chars) are truncated to head+tail summaries in the
  outgoing view. The stored history is untouched.
- **Tier 1 — model-generated compaction** (120K chars): the LLM summarizes the
  conversation into a structured 9-section format.
- **Tier 2 — hard trim** (150K chars, `HISTORY_CHAR_BUDGET`): drops the oldest
  25% of messages as a last resort.

Manual compaction available via `/compact`.

## Autonomous Mode

A separate service mode where you submit a markdown spec and the system autonomously develops, tests, and presents software for your review.

```bash
coding-model-autonomous submit spec.md          # Submit a spec
coding-model-autonomous status <spec_id>        # Watch progress (omit id to list)
coding-model-autonomous gates                   # Review and approve gates
coding-model-autonomous review <gate_id> --approve [--notes ...]
coding-model-autonomous events <spec_id>        # Event log (alias: logs)
```

**Pipeline:** Planner (`dense_architect`) → *plan approval gate* → Architect →
*design review gate* → Implementer → *code review gate* → Reviewer + tests →
*release gate* → DONE. A planner that needs more information opens a
*clarification* gate instead of guessing.

**Human in the loop:** Every major transition requires your explicit approval — the system blocks at review gates until you approve or reject. Rejection notes feed back into the agent for a retry. If Jira is configured (`JIRA_*` env vars), gates sync to a Jira board with native email notifications so you can approve from anywhere.

**Sandboxed tests:** LLM-generated tests run under bubblewrap (`--unshare-all`)
with a seccomp-BPF filter. Set `CODING_MODEL_ALLOW_UNSANDBOXED_TESTS=1` to opt
out (not recommended — tests then run with the orchestrator's privileges).

**Orchestrator daemon:** Runs as a separate systemd unit (`coding-model-orchestrator.service`). Polls the SQLite task store, calls agents via the inference API, runs tests via subprocess. Independent of the interactive client.

See `docs/TUTORIAL.md` for an end-to-end walkthrough of the pipeline.

## API

The server exposes an OpenAI-compatible API. When `ADMIN_API_KEY` is set, every
endpoint except `/`, `/health`, and `/v1/models` requires an `X-Admin-Key`
header (or `Authorization: Bearer <key>`).

| Endpoint | Purpose |
|---|---|
| `GET /health` | Health check (`status`, `model_loaded`, `agents`) |
| `GET /v1/models` | List available agents |
| `POST /v1/chat/completions` | Chat completion (streaming/non-streaming) |
| `POST /v1/memory` | Save to RAG memory (200K char limit, content-hash dedup) |
| `POST /v1/memory/search` | Search RAG memory |
| `POST /v1/memory/ingest` | Ingest a PDF into memory |
| `POST /v1/files/upload` | Upload a file to the server |
| `POST /v1/tools/search` | Web search |
| `POST /v1/tools/apple_deep_docs` | Apple Deep Docs MCP call |
| `POST /v1/autonomous/specs` | Submit a markdown spec |
| `GET /v1/autonomous/specs` | List recent specs |
| `GET /v1/autonomous/specs/{id}` | Spec details with gates and events |
| `GET /v1/autonomous/specs/{id}/events` | Event log for a spec |
| `GET /v1/autonomous/gates` | List open review gates |
| `GET /v1/autonomous/gates/{id}` | Gate detail |
| `POST /v1/autonomous/gates/{id}/respond` | Approve or reject a gate |
| `GET /v1/admin/metrics` | Request metrics |
| `GET /v1/admin/gpu_stats` | GPU sampler output |
| `GET /v1/admin/active_model` | Which model is currently loaded |

## Project Structure

PyPA `src` layout — three packages under `src/`, declared in
`pyproject.toml`. Run `pip install -e .` after `bin/setup.sh` to install
them into the venv.

```
coding-model-server/
├── pyproject.toml              # Package metadata, deps, console scripts
├── src/
│   ├── coding_model_server/     # FastAPI server + orchestrator daemon + shared modules
│   │   ├── server.py           #   FastAPI app assembly, CORS, router wiring
│   │   ├── routes/             #   Endpoints: chat, memory, autonomous, admin, meta
│   │   ├── config.py           #   Config: model configs, agent registry, system prompts
│   │   ├── llama_server.py     #   llama-server subprocess manager (VRAM coord)
│   │   ├── runtime.py          #   Shared singletons, auth, in-flight limits
│   │   ├── schemas.py          #   Pydantic request/response models
│   │   ├── orchestrator_daemon.py  # Autonomous mode coordinator (systemd entry)
│   │   ├── tool_handlers/      #   Tool dispatch, permissions, file ops, shell,
│   │   │                       #   safety gates — imported and RUN BY THE CLIENT
│   │   ├── tool_state.py       #   Permission mode, write counts, checkpoints
│   │   ├── memory_service.py   #   ChromaDB RAG service
│   │   ├── web_search_service.py
│   │   ├── mcp_service.py      #   Apple Deep Docs MCP client (JSON-RPC handshake)
│   │   ├── streaming.py        #   SSE chunking, ThinkingStripper
│   │   ├── external_judges.py  #   Claude / Gemini call wrappers (/review + Phase b)
│   │   ├── metrics.py          #   GPU sampler + request metrics
│   │   └── code_chunker.py     #   tree-sitter-aware code chunking for RAG
│   ├── coding_model_client/     # Modular chat client package
│   │   ├── main.py             #   Chat loop, startup
│   │   ├── __main__.py         #   `python -m coding_model_client` entry
│   │   ├── orchestrator.py     #   Agent loop, tool dispatch, continuation handling
│   │   ├── completion.py       #   SSE streaming, retries, history compression
│   │   ├── compaction.py       #   Context compaction
│   │   ├── commands.py         #   Slash command handlers
│   │   ├── review.py           #   /review multi-judge fan-out
│   │   ├── autonomous.py       #   `coding-model-autonomous` CLI entry
│   │   ├── services.py         #   Server-side service calls (ingest, scrape, MCP)
│   │   ├── history.py          #   Session persistence
│   │   ├── config.py           #   Client-side configuration, constants
│   │   ├── models.py           #   Agent theme management
│   │   └── agentic/            #   RAG: scratchpad, planner, budget, confidence
│   └── coding_model_autonomous/ # Autonomous mode task store + agents
│       ├── db.py               #   SQLite-backed task store (WAL, thread-safe)
│       ├── models.py           #   Pydantic models (Spec, Task, Gate, Event)
│       ├── schema.sql          #   DDL for specs, tasks, artifacts, gates, events
│       ├── planner.py          #   Planner agent (spec → YAML or clarifications)
│       ├── executor.py         #   Execution agents (architect/implementer/reviewer)
│       ├── supervisor.py       #   Meta-orchestrator (retry / fail / replan)
│       ├── seccomp_filter.py   #   seccomp-BPF filter for sandboxed test runs
│       ├── jira_client.py      #   Jira interface (FakeJiraClient + real Atlassian)
│       └── jira_sync.py        #   Bidirectional sync (SQLite ↔ Jira)
├── tests/                      # pytest suite (~30 modules; `pytest` from the repo root)
├── bin/                        # Entry-point scripts: setup.sh, start*.sh
├── scripts/                    # Operational scripts (redeploy, benchmarks, sweeps, stats)
├── systemd/                    # Service units (use `python -m coding_model_server.X` ExecStart)
├── tools/                      # llama-server binary + shared libs, appledeepdoc-mcp
├── scraping/                   # Apple documentation scraper
├── dashboard/                  # TypeScript React dashboard
├── mac_runner/                 # Separate Swift/Xcode test runner service
├── docs/
│   ├── TUTORIAL.md             #   End-to-end pipeline tutorial
│   ├── CONFIGURATION.md        #   Env vars, agent-config knobs, systemd
│   └── RAG_UPDATES.md          #   RAG database + agentic query layer
├── var/                        # Runtime state, git-ignored: tasks_db/, memory_db/, server_stats.csv
└── .archive/                   # Superseded backups, git-ignored
```

## License

[MIT](LICENSE) © Keith Merrill
