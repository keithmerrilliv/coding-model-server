# Coding Model Multi-Agent Server

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A local, self-hosted multi-agent coding system. You give it a markdown spec; a
pipeline of specialised agents plans, designs, implements, compiles, tests and
repairs the result, stopping at review gates for a human decision. Everything
runs on your own hardware — an OpenAI-compatible llama.cpp server with automatic
VRAM management underneath, and an agentic CLI client on top that executes
shell commands, edits files and searches codebases locally.

It is built around the assumption that **the agents will be wrong**, and that
the interesting engineering is in what catches them: compiling before a human is
asked to review, routing a recurring diagnostic back to whoever actually caused
it, rolling back a "repair" that made the build worse, and refusing to tell a
model its code failed to compile when it did not.

**Two things are not in this repository and you must supply them before
anything runs:** a llama.cpp `llama-server` build (`tools/`, see
[TUTORIAL §5.3](docs/TUTORIAL.md#53-updating-the-llama-server-binary)) and GGUF
model weights. "Coding Model" is this project's name for the server and client
together.

[Results so far](#results-so-far), including the failures, are below.

## What changed since v0.1.0

**v0.1.0 (2026-08-19) was a proof of concept.** It worked: two specs had gone
from markdown to an approved, test-passing patch against a real macOS app with
no human writing code. But it worked on two specs, and what it proved was that
the shape was right — not that the thing was reliable. Everything since has
been finding out why two was not enough.

**v0.2.0 is the same idea with a kernel under it.** More than 220 commits since the v0.1.0 tag, 118 tickets in the changelog.
The decisions that kept killing runs moved out of the daemon's catch sites into
four typed modules (~3,300 lines) with their own tests, behind a fault-injecting
test tier. To be accurate about it: the daemon did not shrink — it is larger
than it was. What moved was the *deciding*, not the line count. It is still the
dispatch loop, and it is still the biggest file here:

| | What it owns | Why it exists |
|---|---|---|
| `workspace.py` | One door for every artifact write | A reviewer overwrote the implementer's file at the same path and delivery shipped the overwrite — a 6-line stub in place of a 419-line suite, past a green gate ([DEV-602](https://keith-merrill4.atlassian.net/browse/DEV-602)) |
| `outcome.py` | One classifier, one disposition | Sixteen catch sites each decided for themselves whether a failure was the model's fault. A dead server was charged to the implementer ([DEV-629](https://keith-merrill4.atlassian.net/browse/DEV-629)) |
| `context.py` | One repository read, one prompt budget | Seven per-role fetches with seven outage behaviours; per-section limits that nothing summed, until a prompt passed 1 MB into an HTTP 413 ([DEV-632](https://keith-merrill4.atlassian.net/browse/DEV-632), [DEV-633](https://keith-merrill4.atlassian.net/browse/DEV-633)) |
| `retry_policy.py` | A retry must differ from the attempt before it | Five attempts, four agents, six distinct signatures for what were two defects ([DEV-631](https://keith-merrill4.atlassian.net/browse/DEV-631)) |

Underneath them, a **seam tier** ([DEV-634](https://keith-merrill4.atlassian.net/browse/DEV-634)):
83 tests that inject one fault each — a dead runner, a truncated completion, a
413, a sandbox that will not provision — through the *real* dispatch loop. Those
faults used to be found by live runs, hours at a time.

**The pipeline now works on itself.** From run 17 the target became this
repository: specs that modify the daemon, run in its own sandbox, land on a
`pipeline/` branch. Run 17 was the first time it changed its own daemon and
delivered with nobody editing the code. Run 39 was the first Swift suite it
wrote and ran to green end to end. Most of the tickets above were found that
way, by the thing being fixed.

**What has not changed:**

- **No per-agent success rates.** Rotation is failure-triggered, so an agent's
  position in it confounds its rate ([DEV-530](https://keith-merrill4.atlassian.net/browse/DEV-530)).
  Every dispatch now records what preceded it; the comparison itself is not run.
- **Delivery lands a branch, not a merge.** A human reviews and merges, always.
- **The Mac runner is a single point of truth that can lag.** A run once built a
  whole feature slice on a clone two slices behind origin, and every stage
  reported success ([DEV-701](https://keith-merrill4.atlassian.net/browse/DEV-701));
  a read now reports which commit served it, but nothing yet refuses to dispatch.
- **It still needs a human at the gates.** That is the design, not a gap.

Full ticket-by-ticket detail: [CHANGELOG.md](CHANGELOG.md).


**Contents:** [What changed since v0.1.0](#what-changed-since-v010) · [Architecture](#architecture) · [Quick Start](#quick-start) ·
[Agents](#agents) · [Client Commands](#client-commands) ·
[Tool System](#tool-system) · [Context Management](#context-management) ·
[Autonomous Mode](#autonomous-mode) · [API](#api) ·
[Project Structure](#project-structure) · [License](#license)

## Architecture

```
┌───────────────────────────────────┐                        ┌─────────────────────────────────────┐
│ Client (macOS/Linux)              │                        │ Server (Linux + GPU)                │
├───────────────────────────────────┤                        ├─────────────────────────────────────┤
│ src/coding_model_client/          │                        │ coding_model_server.server          │
│   main.py                         │                        │   (FastAPI :5000)                   │
│   orchestrator.py                 │                        │                                     │
│   completion.py                   │        HTTP/SSE        │ ├─ routes/ (chat, memory,           │
│   compaction.py                   │◄──────────────────────►│ │    autonomous, admin, meta)       │
│   commands.py                     │  /v1/chat/completions  │ ├─ llama_server backend             │
│   history.py                      │                        │ │    (one llama-server subprocess)  │
│                                   │                        │ ├─ memory_service (ChromaDB + RAG)  │
│ imports                           │                        │ ├─ web_search_service               │
│ coding_model_server.tool_handlers │                        │ ├─ mcp_service (Apple Deep Docs)    │
│ and runs tools LOCALLY            │                        │ └─ orchestrator_daemon (autonomous) │
└───────────────────────────────────┘                        └─────────────────────────────────────┘
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

### Prerequisites

Read this first — two of these are **not** installed for you, and the server
will not start without them.

| | |
|---|---|
| **OS / hardware** | Linux with an NVIDIA GPU for the server. The client runs on macOS or Linux. |
| **Python** | 3.10 or newer; 3.12 is what CI and mypy run against |
| **CUDA** | **12.8 — not 13.x.** CUDA 13.x has a compiler bug that silently disables MMQ kernels and costs roughly 7× on prefill (llama.cpp #18331, #18398). |
| **`tools/llama-server`** | **You must supply this.** A llama.cpp build plus its shared libraries. It is the only inference backend, it is not in this repo, and `setup.sh` does not fetch it. `docs/TUTORIAL.md` §5.3 says where to download one or how to build it. |
| **Model weights** | **You must supply these.** No GGUF ships here. `.env.example` lists the model slots; every one is optional, so start with a single small model and add more later. |
| **VRAM** | Whatever you have. Most agents run with expert offload (`--cpu-moe`), keeping attention on the GPU and MoE experts on CPU, so a 16 GB card runs models far larger than it could hold. |

```bash
git clone https://github.com/keithmerrilliv/coding-model-server.git
cd coding-model-server
./bin/setup.sh              # venv + `pip install -e .` + seeds .env
```

Then edit your `.env` before first launch. **`ADMIN_API_KEY` is required** — every
launch path refuses to start with it empty or left at a placeholder:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # generate one
```

For a purely local experiment, `CODING_MODEL_ALLOW_UNAUTH=1` permits an empty
key, but only when `HOST` is loopback.

```bash
./bin/start.sh              # starts on port 5000
curl localhost:5000/health  # sanity check
```

Dependencies live in `pyproject.toml` (single source of truth). `bin/setup.sh`
runs `pip install -e .`, which installs the core deps, makes the three `src/`
packages importable, and wires the `coding-model-client` / `coding-model-autonomous` console
scripts. For the client's optional niceties (rich output, scraping) add the
extra: `pip install -e '.[client]'`.

Or as a systemd service. The units in `systemd/` are **templates** — they ship
with `/home/youruser` placeholders, so substitute before installing:

```bash
sed -e "s|/home/youruser|$HOME|g" -e "s|^User=youruser|User=$USER|" \
    -e "s|^Group=youruser|Group=$USER|" systemd/coding-model-server.service \
  | sudo tee /etc/systemd/system/coding-model-server.service
sudo systemctl daemon-reload
sudo systemctl enable --now coding-model-server
journalctl -u coding-model-server -f   # view logs
```

`systemd/` ships four services you will want — `coding-model-server` (inference
API), `coding-model-orchestrator` (autonomous mode), `coding-model-dashboard`
(static dashboard on :3001) and `coding-model-monitor` (resource sampler) — plus
two optional timer-driven jobs, `coding-model-apple-docs-refresh` and
`coding-model-mcp-update`, which only matter if you use the Apple-docs RAG
collection. Every one is a template; substitute as above.

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
./bin/start-client.sh                          # Default agent (implementer)
./bin/start-client.sh --model dense_architect  # Specific agent
./bin/start-client.sh --name my-project        # Named session
```

`pip install -e .` also puts `coding-model-client` on your PATH; it takes the
same flags.

## Agents

Each agent maps to a model configuration and system prompt, both defined in
`src/coding_model_server/config.py` (`Config.AGENTS`). Switch with
`/agent <name>` or `@agent_name message`.

Decode tok/s measured end-to-end on an RTX 5080, 2026-07-14 (see
[TUTORIAL.md](docs/TUTORIAL.md#stage-5-prefill-gpu--cpu)
for method, prefill figures, and the caveat about raw-vs-proxy numbers).

| Agent | Role | Model | Active/Total | Context | KV | GPU offload | Decode tok/s |
|-------|------|-------|--------------|---------|-----|-------------|-----:|
| `implementer` | Default implementation | Qwen3.6-35B-A3B UD-Q4_K_M | 3B/35B | 64K | Q8_0 | ngl 41, n_cpu_moe 20 | 75.5 |
| `deep_implementer` | Deep reasoning | Qwen3-Coder-Next Q8_0 | 3B/80B | 256K | Q8_0 | ngl 48, cpu_moe | 26.9 |
| `fast_implementer` | Fast implementation | Qwen3-Coder-30B Q4_K_M | 3B/30B | 64K | Q8_0 | ngl 49, n_cpu_moe 26 | 58.2 |
| `debugger` | Debugging | Qwen3-Coder-30B Q4_K_M | 3B/30B | 128K | Q8_0 | ngl 49, cpu_moe | 37.1 |
| `reviewer` | Code review | Qwen3-Coder-30B Q8_0 | 3B/30B | 192K | Q8_0 | ngl 49, cpu_moe | 26.0 |
| `deep_reviewer` | Deep judgment | Qwen3.5-122B-A10B Q4_K_M | 10B/122B | 256K | Q8_0 | ngl 49, cpu_moe | 20.0 |
| `dense_architect` | Planner + architect (interactive `architect` alias) | Qwen3.6-27B MTP Q4_K_M (dense) | 27B dense | 64K | Q4_0 | ngl 66, **n_cpu_ffn 33**, MTP speculative decode | 17.4 † |
| `supervisor` | Retry/fail/replan decisions | Qwen3.6-27B MTP Q4_K_M (dense) | 27B dense | 64K | Q4_0 | ngl 66, **n_cpu_ffn 33**, MTP speculative decode | 11.4 ‡ |
| `moe_implementer` / `moe_architect` | Implementation / architecture | MiniMax M2.5 Q4_K_M | 10B/230B | 116K | Q4_0 | ngl 62, cpu_moe | 11.2 |
| `brainstorm` | Fastest brainstorm | Nemotron-3-Nano Q4_K_M | 3.5B/30B | **1M** | Q8_0 | ngl 52, cpu_moe | 40.1 |
| `native_implementer` | Implementation (native tools) | GLM-4.7-Flash Q4_K_M | 3B/30B | 64K | Q8_0 | ngl 47, n_cpu_moe 20 | 59.7 |

† Measured 2026-09-19 through the managed path on a 53,333-token architect
prompt ([DEV-744](https://keith-merrill4.atlassian.net/browse/DEV-744)); the old
rung read 8.3–9.0 t/s at the same depth. The other rows in this table do not
state their prompt depth and predate that measurement, so they are not directly
comparable to it — re-measuring the roster on one basis is
[DEV-95](https://keith-merrill4.atlassian.net/browse/DEV-95).

‡ `supervisor` shares `_DENSE_27B` with `dense_architect`, so its serving config
changed identically, but only the architect was re-measured. This figure
predates [DEV-744](https://keith-merrill4.atlassian.net/browse/DEV-744).

`supervisor` is decision-only: it is always called with native tools (a
`decide()` function call) and never gets marker-based shell tools.
`brainstorm` has no tools at all.

Four eval-only agents are also registered (so they appear in `/v1/models`) but
are left out of the table above: `devstral_implementer` (Devstral Small 2 24B,
DEV-414 eval), `dense_architect_nothink` (`dense_architect` with
`enable_thinking=False`, the DEV-556 eval arm), `qwen38_architect`
(Qwen3.8-27B with embedded MTP, the DEV-615 architect-eval candidate), and
`glimmer_architect` (Muse-Glimmer-30B, the DEV-692 architect candidate — 64K
Q4_0 ctx, ngl 40 `--swa-full`, 13.2 decode).

`glimmer_implementer` is the same model in the implementer prompt, and since
DEV-692 item 3 it **is** routed: it sits in the implementer rotation directly
behind `deep_implementer`, the first Meta model the rotation has had. That
membership is **retry-only** — it is deliberately absent from
`ALLOWED_IMPLEMENTER_AGENTS` and the complexity-tier map, so neither an
architect recommendation nor a tier default can put it on attempt 1. Attempt 1
is the only attempt whose agent is chosen rather than rotated into, which makes
it the only one comparable across runs (DEV-431), and keeping a new model off it
leaves that comparison intact.

`glimmer_architect` stays unrouted, and for a concrete reason: DEV-727 found
that Glimmer 500s inside llama-server whenever it emits a tool call in harmony
recipient syntax, which it starts doing once a conversation carries a tool
result — round 1 of the architect's DEV-714 tool loop. The implementer never
builds that shape (one fresh `[system, user]` pair per call), which is what
makes the rotation membership safe; a test pins that property so a future
implementer tool loop cannot ship without revisiting it. `ARCHITECT_AGENT` still
points at `dense_architect`; reach the architect arm by pinning
`AUTONOMOUS_ARCHITECT_AGENT=glimmer_architect`, or address either directly.
Registration is also what arms the prompt-fit check — the allocator reads each
agent's window off its config — so even an unrouted agent is a known quantity to
the router rather than an unknown one. Note that Muse **Spark** itself is hosted
and cannot be served here; Glimmer is its open-weight distill, and there is
deliberately no `spark` alias.

**Expert offload.** `cpu_moe=True` (`--cpu-moe`) keeps *all* MoE expert weights
on CPU. `n_cpu_moe=N` (`--n-cpu-moe N`) keeps only the first N layers' experts
on CPU and pushes the rest onto the GPU — faster decode, bounded by VRAM. The
three agents tuned with `n_cpu_moe` trade context for decode speed; see the
per-config comments in `config.py`, which record the measurements. Nemotron's
Mamba-hybrid architecture (only 6/52 layers need a KV cache) allows a full 1M
native context on an RTX 5080. KV-cache preference is Q8_0 wherever it fits —
KV-quant noise produces diffuse quality degradation that is harder to manage
than a smaller context.

**Legacy names** resolve server-side (`Config.AGENT_ALIASES`) — the API's
`request.model` and the `.env` `AUTONOMOUS_*_AGENT` defaults accept them, though
they aren't listed in `/v1/models`:
`architect` → `dense_architect`, `q36_architect` → `dense_architect_nothink`,
`m25_architect` → `moe_architect`, `m25_implementer` → `moe_implementer`,
`glm` → `native_implementer`, `nemotron` → `brainstorm`,
`glimmer` → `glimmer_implementer`. The interactive client
(`--model`, `/agent`, `@name`) currently needs the canonical name.

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
| `/apple <tool> <args>` | Apple Deep Docs MCP (server-side) |
| `/scrape [framework]` | Run the documentation scraper. No argument = ALL frameworks (~30k pages — takes hours) |

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
coding-model-autonomous cancel <spec_id> --reason "…"   # Stop a run; open gates cancelled, reason recorded
coding-model-autonomous swap-reset              # Clear a wedged model swap without a restart
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

**[docs/PIPELINE.md](docs/PIPELINE.md) is the map** — state machine, the
build-output classifier, where a failure routes and who pays for it, and the
budget table. Read it before changing anything in the orchestrator.
`docs/TUTORIAL.md` is the end-to-end walkthrough.

### Results so far

Honest version, because the failures are the useful part.

**What works unattended.** Two specs have gone from markdown spec to an
approved, test-passing patch against a real macOS Swift app — a Stop button
that cancels in-flight generation, and a top-k sampling mask — every line
written by the pipeline. The first was as clean as it sounds: design approved
first attempt, implementer's first attempt compiled, suite green. The second
earned it the hard way: one design rejection and four code-review rejections,
each rejection's notes feeding the retry — including one where the reviewer's
note contained the exact fix. No human edited the code; the review notes are
where the human steering lives. Each approved run now lands on a
`pipeline/<spec_id>` branch of the target repo, force-pushed and
pipeline-owned; the operator reviews and merges (DEV-535).

**The spec that taught the most.** A harder greenfield spec — the logic core of
a Centipede clone: seeded mushroom field, chain locomotion, split-on-hit, 17
acceptance criteria — failed **eleven times before run 12 completed it**, and
its sibling (the complete game, WebGPU) completed the next day:

| Run | How far it got |
|----|----|
| 1 | Five implementer attempts, none compiled |
| 3 | Reached synthesis; three design-level Swift errors consumed the retry budget |
| 5 | **First end-to-end run.** 21 tests executed, 12 passed — failures behavioural, not structural |
| 6 | Two words from compiling: a struct reached through `Dictionary` needed `Equatable` |
| 7–8 | Died on generated files missing `import Foundation`; a repair round then made the build *worse* |
| 9 | **Compiled.** All 19 tests launched, then the process trapped — one inverted conditional emptied a collection every tick |
| 10 | Reached the synthesis repair round, which invented a file redeclaring a protected type it was never shown; two independent defences both missed it |
| 11 | Six designs of fix-what-was-named, break-what-was-adjacent; one design declared the same type three times with its own deliberation left in the document |
| 12 | **Completed.** Eight designs — the eighth a verbatim transcription of a document the reviewer corrected by hand — then five implementer attempts to 20/20 tests green |
| 13 | **Completed** — the sibling spec, a full WebGPU Centipede, greenfield: three designs, five attempts, 16 criterion tests green, done in one evening |

Run 9's defect had been flagged by the Swift compiler, on the exact line, as an
unused-binding warning — in output the pipeline captured, parsed for errors, and
discarded the warnings from. That is now a blocking check.

The completions were not clean either, and the dirt is the interesting part.
Run 12's reviewer wrote a suite that passed 20/20 — then its static review
invented a defect at a line number past the end of the file, and the retry
machinery destroyed the passing implementation on that phantom's authority.
Three guards came out of the incident: citations are now verified against the
file's actual line count, a FAIL verdict over a green run goes to a human
adjudication gate instead of a silent retry, and a reviewer test file that
does not even parse is charged to the harness, not the implementation. The
design phases taught their own lesson: iterating rejection notes rotated
defects for six rounds; handing the architect a corrected document and
demanding character-for-character transcription ended it in one.

**Run 14 — a different app, and another spec-shaped failure.** A separate spec
(DEV-566) on the ElectricSheep app: contain an MLX runtime error to the test
that triggers it, so one bad `float64` fixture call can no longer kill the
XCTest host and report a dozen unrelated tests as 0.000s failures. Its first
attempt failed in a way the model never caused — the spec omitted a
change-surface table, which silently disarmed the guard that hands the
implementer the files it may modify (DEV-492/DEV-571), so five attempts
hallucinated rewrites of an unrelated `ForcingStrategy.swift` instead of
touching the real code. Naming the change surface in the spec, plus a corrected
type inventory (DEV-534), fixed it: the re-run completed successfully, the
containment landing against a real `mlx-swift` dependency with the three
existing test files held as the regression contract.

**The pattern worth stating:** nearly every failure has been a *system* defect,
not a model-capability one. A gate that claimed a build succeeded when nothing
had compiled. A repair prompt telling the model its code "passes most tests"
about code that did not build. A reviewer's approval notes accepted by the API,
mirrored to the issue tracker, and read by no agent. Each was found by running
the thing end to end against a real compiler and reading what it actually said.

**Runs 17 to 49 — the pipeline turned on itself, then went back to Swift.**
After run 16 the target became this repository: specs that modify the daemon,
run in its own sandbox, land on a `pipeline/` branch. Every fix since is a
system defect found that way, and the kernel refactor
([CHANGELOG](CHANGELOG.md)) is their sum. Runs 31 and 39 are the exceptions
that matter — both went back to the Swift target on the Mac runner, and run 39
is the first time a suite this pipeline wrote and ran in Swift went green end
to end. Runs 42 to 49 are the v0.3.0 proving window: every target was Swift,
two runs delivered on their own, three were finished by hand, and every
failure was in the implementer or repair loop, none in serving. Read together
they are one gap, not eight (DEV-772).

| Run | What happened |
|----|----|
| 17 | **First self-delivery.** The pipeline changed its own daemon and delivered the branch with nobody editing the code (DEV-597, DEV-574). |
| 18–19 | Manifest mode regenerated existing files from priors: its path filter expected dicts and received dataclasses, so no file was ever fetched (DEV-604). Run 19's change-surface table was written as prose, matched zero rows, and silently disarmed every guard keyed on it (DEV-621). |
| 20 | Pipeline-written write guards landed on main (DEV-602); the sandbox could not import the repository's own package, so every attempt failed the same way regardless of the code (DEV-626). |
| 21 | An implementer prompt passed 1 MB into an HTTP 413 — per-section knobs that nothing summed (DEV-627); anchored edits failed byte-exact transcription of an 18-line anchor three retries running (DEV-635, DEV-638). |
| 23 | The reviewer wrote a file at the implementer's path and nothing objected: the collision guard from run 17's incident had shipped, and none of the sixteen write sites called it. This is the run that made the refactor a plan rather than a list. |
| 24 | The artifact ledger redirected the reviewer's write, visible on the gate — phase 1 proven. Three different models guessed a `src.` import root the sandbox does not have (DEV-644). |
| 25 | A transport failure was a no-verdict and charged nothing; a re-emitted stub was refused against the repository baseline; the synthesis corpus came from the ledger, not a directory walk — phase 2 proven. |
| 26 | A spec against the 320 KB daemon ran with no environment override for the first time, and synthesis merged six failed attempts into a passing one — phase 3 proven. |
| 28 | Synthesis was asked to re-emit a 145,825-character file inside a 32,000-token budget; 77 minutes later two stubs had been refused. The arithmetic is now checked before the call (DEV-649). |
| 29 | Five attempts, four agents, six distinct failure signatures for what were two defects; refused at DEV-649's guard. The run the invariance detector was built from (DEV-631). |
| 30 | **Delivered.** A pipeline-written change to its own placeholder-path guard (DEV-656) went from spec to branch end to end. |
| 31 | **Delivered.** Centipede logic core slice 7 on the Mac runner: one build-failure retry (rotated, only the cited file regenerated), 52 tests green, three files pushed — the proving run for phases 4–6 (DEV-666, DEV-667, DEV-668). It also found the coarse-key defect in DEV-672. |
| 32 | **Delivered by synthesis, landed by hand.** The DEV-660 import-root hint, self-target. The first attempt edited a base fetched from the Mac's stale clone and silently reverted DEV-672 with every new test green (DEV-674, fixed and redeployed mid-run; DEV-675 filed); two later attempts were lost to rejection notes that anchored on the prior artifact instead of HEAD, one to the planned-output check (DEV-677); every retry was rerouted to the same agent by the context fit check (DEV-676). Synthesis merged six attempts into the exact four edits; the full suite was run on the artifact before release. No delivery remote is configured for the self-target repo, so the branch was landed by hand. |
| 33 | **Cancel drill.** The DEV-661 spec cancelled from the CLI with the implementer's model call in flight: the spec went CANCELLED at once, the in-flight pass finished and was discarded, its gate was born cancelled, the next swap went through (DEV-583, DEV-493, DEV-567, DEV-582 proven). Three residuals filed (DEV-678, DEV-679, DEV-680). |
| 34 | **Delivered by synthesis, landed by hand.** The DEV-661 testability rule (a Python design's seams must import the code under test). The implementer did not emit the test file in five of six attempts, and on the sixth emitted it and dropped the module instead, every one rerouted to the same agent by the context fit check (DEV-676), so the invariance detector correctly saw one agent; synthesis produced the right symbols and eight passing tests but re-emitted the module whole. Landed as the spec's minimal edits. The new rule immediately bounced the seam tier's own fixture design for lacking an import line. |
| 35 | **Cancel drill.** The DEV-653 spec cancelled from the CLI with the implementer's model call in flight. The spec held CANCELLED, the in-flight pass finished and was discarded with nothing recorded (DEV-678), the epic got exactly one mirror note (DEV-679) carrying the reason, and the collapse landed resolution `Won't Do` with a `pipeline-failed` label whose note matched what was actually set (DEV-680, DEV-482). |
| 36 | **Delivered by synthesis, landed by hand.** DEV-653, a terminal spec status retires its open gates. Six implementer attempts on two agents (the fit check rerouted the third before it was ever called, DEV-676): the first emitted a new file as edit blocks, three carried the same omission (a `cancel_spec` adjustment the spec itself had got wrong), and two failed collection. The existing-tests guard (DEV-675) ran 687 repository tests beside the spec's ten on every attempt and the gate reported both counts; a targeted retry carried its uncited test file forward (DEV-677). Synthesis merged the six attempts into all four edits, with the full suite green. Delivery failed on a deploy-key permission (DEV-686), so the branch was landed by hand. Filed from the run: DEV-683, DEV-684, DEV-685. |
| 37 | **Cancelled — unimplementable, and that was the finding.** The DEV-681 overlay-ref spec. Its first design lacked the seams' import line and was bounced by the DEV-661 rule, proving that ticket. Then two structural defects surfaced: no agent's window holds the 356 KB daemon beside the protected context, so the architect designed without seeing a file it had to edit and silently omitted that edit (DEV-683); and the sandbox collected the workspace's own `test_runner.py` as a test module, so no attempt could ever pass (DEV-688, DEV-689). Cancelled after two attempts rather than burning the rotation on a failure no model could fix. It did prove DEV-676's `sole_fit`: `deep_implementer` was named the only agent whose window holds the prompt. |
| 38 | **Failed.** A self-target spec to record the overlay ref every sandbox was built from. Plan, clarification and design all approved first time; then three code-review rejections and a synthesis that was refused at DEV-649's own arithmetic guard for offering a fragment of the file it was asked to re-emit. No test ever ran. The guard built in run 28 stopped run 38 from shipping a stub — working as designed, and still a failed run. |
| 39 | **Delivered — the first Swift suite this pipeline has ever run to green.** Centipede logic core slice 8, wave progression, on the Mac runner. It earned it: the plan was rejected once, the design once, and code review **three times** (five rejected gate rows in the database, two of them DEV-684's duplicate synthetic gates), with rotation running `implementer` → `deep_implementer` → `moe_implementer` and never repeating an agent. Three pre-gate build checks; the second passed. Pushed to `pipeline/spec_a5b68678`. An earlier attempt at the same slice was cancelled (DEV-698). Its scare — six tests apparently deleted — was the Mac's clone being two slices behind origin, not data loss (DEV-701). The third rejection was also the release's last unmanufacturable proof: `implementer` and `moe_implementer` had produced the same coarse key, the retry loop called it invariant and handed off to synthesis at retry 2 of 5 (DEV-631, DEV-667), and the synthesis is what delivered. |
| 40 | **Delivered, landed by hand.** Count the test declarations an attempt adds, so a zero or negative delta is visible rather than inferred. Plan rejected once, design once, code review three times. The build check reported both populations separately — 14 new tests passing beside 5 failing, against 13 selected existing tests — which is the DEV-675 guard reading out loud. Landed as `cd3bd71c`; the self-target repo still has no delivery remote. |
| 41 | **Delivered — the v0.2.0 proving run, and the first time the pipeline amended green tests on purpose.** Centipede logic core slice 9 — proximity scoring for the spider, the spider eating mushrooms — on the Mac runner, 54 minutes end to end. Plan rejected once (the planner had substituted the test framework, DEV-709); design rejected three times — twice by the testability check, once by the reviewer — then approved with four conditions (DEV-546), all four honoured; implementer and reviewer each first attempt. The spec authorised a one-line change to two existing assertions and that is exactly what landed: two assertions changed, 81 lines appended, every delivered file grew, suite 57 → 64 green. First workload on the 64K / 46-layer architect (DEV-707): four passes, no abort. Pushed to `pipeline/spec_078b8056`. Filed from the run: DEV-709, DEV-710, DEV-711. |
| 42 | **Delivered.** Electric Sheep metrics-bridge dedup and loop cancel (DEV-592, DEV-593) on the Mac runner, 2026-09-18: five files to `pipeline/spec_a3f6c9e7`, all four new tests and the whole existing suite green in the VM; merged to ElectricSheep main by hand. The first run under the post-v0.2.0 pipeline shape. Two operator interventions were needed to finish it, both traced to the planner emitting bare filenames that nothing resolved (DEV-601, raised to High and fixed the same evening). Closed on its evidence: DEV-699, DEV-712, DEV-714, DEV-722. Filed: DEV-728, DEV-730. |
| 43 | **Failed at synthesis.** Electric Sheep audio-strike race (DEV-594, DEV-200, DEV-201). Plan rejected once for a one-letter typo in a new test path, which `_missing_planned_outputs` would have charged to the implementer (DEV-733); design approved with four conditions. Two implementer attempts, synthesis and its repair all died in the same six-line test helper the spec had left to the model; the production refactor was correct on attempt 0 and never broke. The retry-2 artifact was corrected by hand, verified in the VM, and merged as ElectricSheep main `ab3e7ec`. Filed: DEV-732 through DEV-738. |
| 44 | **Failed.** Electric Sheep audio lifecycle (DEV-595, DEV-207), the first run on llama-server v0.4.1 and the `--n-cpu-ffn` architect rung (DEV-744). Swift 6 actor isolation defeated all five implementers and synthesis: `@MainActor` referenced from nonisolated contexts, one annotation each, missed by every model (DEV-753). The repair round made it worse (6 → 36 diagnostics) and correctly rolled back. A cold SwiftPM resolve ate the whole 1200 s test budget on one attempt (DEV-752), and the Swift test counter read 12 snake_case tests as 0 (DEV-751). Serving evidence was clean: eight model loads, zero OOM, architect at 19 tok/s sustained. Filed: DEV-750 through DEV-753. |
| 45 | **Failed.** Centipede slice 9 re-run as a controlled old-binary/new-binary comparison. Three attempts on small ordinary defects, invariant guard to synthesis at 2 of 5, repair rolled back. The finding was in the harness: ANSI colour escapes in `swift_test` output blinded the diagnostic extractor, so the repair gate compared 0 → 0 and could never register an improvement on a colourised build (DEV-755, fixed the same day with synthesis and repair outputs now retained under `retry_history/`). Also found: three of four delivered-but-unmerged Centipede branches had decayed into pure deletions against main and were not merged (DEV-756). Filed: DEV-754, DEV-755, DEV-756. |
| 46 | **Failed at synthesis, and the cause was the operator.** Electric Sheep triple buffering, MTKView path only (DEV-590). The plan was approved with a condition to add a file to the implement phase, and an approval condition reaches the models, not the plan (DEV-546): the file was never editable, nobody was served it whole, and synthesis reconstructed it from a partial read and emitted `@ObservedObject` on a non-observable type. What the run did prove: the DEV-755 retention is live and showed the rollback was correct; the DEV-631 invariant guard fired on the right key; `glimmer_implementer` completed 16 K visible tokens with no 502 (DEV-747). Filed: DEV-757, DEV-758, DEV-759, DEV-760. |
| 47 | **Failed at synthesis; hand-delivered.** The same spec re-issued with a change-surface table and a `draw(in:)` criterion (DEV-761). Gates were clean first time. Two of three attempts were lost to an ambiguous SEARCH on a file with two byte-identical `#if os()` bodies, and the second never saw the first's feedback (DEV-763); the third and the repair both tripped on an unqualified static member, and the repair inverted the fix (DEV-764). Retry 1 was five small fixes from delivery: corrected by hand, 6/6 green in the VM, merged as ElectricSheep main `6506baf`. A 30 s tunnel flap had aborted the first submission outright (DEV-762). |
| 48 | **Failed at synthesis; hand-fixed to green, not merged.** Centipede renderer slice 1 (DEV-765): an offscreen Metal 3 renderer with pixel-asserted tests, `swift_test` on the host, built to avoid every cause behind 44 through 47. Sources compiled; 14 test-file errors (`throws` missing on `@Test` functions, a missing `Hashable`), and the repair round edited the renderer instead of the cited test file, 14 → 14 (DEV-767). `deep_implementer` lost retry 0 to a FILE marker used as an edit header (DEV-770). Shipped from it before the next run: Swift rules in every Swift prompt, a static-member precheck, and a cite-or-refuse repair round (DEV-764, DEV-767). |
| 49 | **Delivered — the v0.3.0 proving run, and the first autonomous delivery on llama-server v0.4.1.** The identical spec on the DEV-764/767 pipeline. `moe_implementer` at retry 3 compiled and passed on its first build; the reviewer re-ran 5 new and 20 existing tests green; release approved; four files pushed to `pipeline/spec_4baf2650` and merged to Centipede main `5185167` (+431/−0, five new tests). Retry 0 fell to DEV-770 again, fixed and deployed that afternoon. Closed on its evidence: DEV-744, DEV-765, DEV-769. |

No per-agent success rates appear here, on purpose: rotation is
failure-triggered, so an agent's position in it confounds its rate (DEV-530).
Every dispatch now records what preceded it and how the agent was assigned,
which is what a fair comparison needs; the comparison itself has not been run.

Every defect above is tracked, with the evidence that produced it, and the fix
is pinned by a regression test.

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
| `POST /v1/memory/delete` | Delete memories by id and/or metadata filter |
| `POST /v1/memory/ingest` | Ingest a PDF into memory |
| `POST /v1/files/upload` | Upload a file to the server |
| `POST /v1/tools/search` | Web search |
| `POST /v1/tools/apple_deep_docs` | Apple Deep Docs MCP call |
| `POST /v1/autonomous/specs` | Submit a markdown spec |
| `GET /v1/autonomous/specs` | List recent specs |
| `GET /v1/autonomous/specs/{id}` | Spec details with gates and events |
| `GET /v1/autonomous/specs/{id}/events` | Event log for a spec |
| `POST /v1/autonomous/specs/{id}/cancel` | Cancel a running spec (DEV-583) |
| `POST /v1/admin/swap/reset` | Clear leaked in-flight reservations on the model server (DEV-583) |
| `GET /v1/autonomous/gates` | List open review gates |
| `GET /v1/autonomous/gates/{id}` | Gate detail |
| `POST /v1/autonomous/gates/{id}/respond` | Approve or reject a gate |
| `GET /v1/admin/metrics` | Request metrics |
| `GET /v1/admin/gpu_stats` | GPU sampler output |
| `GET /v1/admin/rag_stats` | RAG retrieval outcomes (skipped/empty/injected/…) for the dashboard |
| `GET /v1/admin/active_model` | Which model is currently loaded |

## Project Structure

PyPA `src` layout — three packages under `src/`, declared in
`pyproject.toml`. Run `pip install -e .` after `bin/setup.sh` to install
them into the venv.

```
coding-model-server/
├── pyproject.toml              # Package metadata, deps, console scripts
├── requirements.txt            # Thin `-e .` pointer (pyproject is the source of truth)
├── requirements-client.txt     # Thin `-e .[client]` pointer
├── QWEN.md                     # Agent context / project notes (CLAUDE.md-style)
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
│       ├── test_runner.py      #   Sandboxed test dispatch (bwrap+seccomp; swift/node/pytest)
│       ├── seccomp_filter.py   #   seccomp-BPF filter for sandboxed test runs
│       ├── jira_client.py      #   Jira interface (FakeJiraClient + real Atlassian)
│       └── jira_sync.py        #   Bidirectional sync (SQLite ↔ Jira)
├── tests/                      # pytest suite (169 modules plus the seam tier; `pytest` from the repo root)
├── bin/                        # Entry-point scripts: setup.sh, start*.sh
├── scripts/                    # Operational scripts (redeploy, benchmarks, sweeps, stats)
├── systemd/                    # Service units (use `python -m coding_model_server.X` ExecStart)
├── polkit/                     # polkit rule: sudo-free restart of the units (redeploy.sh)
├── git-server/                 # git-shell wrapper + pre-receive hook for pipeline attempt branches
├── tools/                      # llama-server binary + shared libs, appledeepdoc-mcp (gitignored — you supply them)
├── scraping/                   # Apple documentation scraper
├── dashboard/                  # TypeScript React dashboard
├── mac_runner/                 # Separate Swift/Xcode test runner service
├── docs/
│   ├── TUTORIAL.md             #   End-to-end pipeline tutorial
│   ├── PIPELINE.md             #   Pipeline state machine + failure routing (the map)
│   ├── CONFIGURATION.md        #   Env vars, agent-config knobs, systemd
│   ├── RAG_UPDATES.md          #   RAG database + agentic query layer
│   ├── SECURITY_MIGRATION.md   #   The loopback + admin-key hardening, and how to undo it
│   ├── specs/                  #   Specs the pipeline has been run against (author's projects)
│   └── designs/                #   Design proposals that preceded larger tickets
├── var/                        # Runtime state, git-ignored: tasks_db/, memory_db/, server_stats.csv
└── .archive/                   # Superseded backups, git-ignored
```

## License

[MIT](LICENSE) © Keith Merrill
