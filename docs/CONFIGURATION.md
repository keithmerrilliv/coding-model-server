# Configuration Reference

## Environment Variables

Set in `~/.config/coding-model-server/.env` (preferred; keeps secrets out of the repo)
or in a repo-local `.env` for dev. Loaded by startup scripts and systemd units.

Defaults below are the **code** defaults (what you get with the var unset), not
whatever `.env.example` happens to ship.

### Server
| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `5000` | Server listen port |
| `HOST` | `127.0.0.1` | Server bind address. Set to `0.0.0.0` only if you need LAN access **and** have a strong `ADMIN_API_KEY`. |
| `ADMIN_API_KEY` | *(required)* | API key for authentication. The server refuses to start without it unless `CODING_MODEL_ALLOW_UNAUTH=1` is set. Accepted as `X-Admin-Key: <key>` or `Authorization: Bearer <key>`. |
| `CODING_MODEL_ALLOW_UNAUTH` | *(unset)* | Set to `1` to permit unauthenticated operation (local dev only — never on a LAN-exposed instance). |
| `CODING_MODEL_ENV_FILE` | *(unset)* | Override the .env path loaded by `bin/start.sh` / `bin/start-client.sh`. |
| `CODING_MODEL_ALLOW_UNSANDBOXED_TESTS` | *(unset)* | Set to `1` to run LLM-generated tests without the bubblewrap+seccomp sandbox. Not recommended — tests then execute with the orchestrator's own privileges. Install `bubblewrap` (`apt install bubblewrap`) instead. |
| `CODING_MODEL_CHAT_MAX_INFLIGHT` | `5` | Concurrent in-flight chat requests allowed against the single llama-server child. |
| `CODING_MODEL_INTERNAL_HOST` | `127.0.0.1` | Host the orchestrator's internal calls bind to. Loopback by design. |
| `LLAMA_SERVER_REQUEST_TIMEOUT` | `2700` | Max seconds for a single llama-server inference call. Must be ≥ the longest `AUTONOMOUS_*_TIMEOUT` so the inner request doesn't fail before the outer role budget. |
| `ALLOW_REMOTE_EXEC_YOLO` | *(unset)* | Defense-in-depth: even in `PERMISSION_MODE=yolo`, `<<<REMOTE_EXEC>>>` shell commands still prompt unless this is set to `1`. Two opt-ins must compromise (yolo flag + this env) before LLM-emitted shell runs silently. |
| `EXTRA_AUTO_APPROVE_COMMANDS` | *(unset)* | CSV of extra base commands eligible for silent execution, for build tooling the built-in allow-list doesn't cover. See [Shell auto-approval](#shell-auto-approval). |
| `CORS_ORIGINS` | `localhost,127.0.0.1` | CSV of allowed CORS origins. Must include port (e.g. `http://localhost:3000`) for browser dashboards. `allow_credentials` is automatically disabled when `*` is in the list. |
| `INGEST_ALLOWED_DIR` | *(unset)* | Directory under which `/v1/memory/ingest` accepts paths (in addition to system temp). Realpath-resolved before the prefix check, so symlinks can't escape. |
| `MAC_RUNNER_URL` | `http://127.0.0.1:5050` | Where the orchestrator dispatches `swift_test` / `xcodebuild_test` jobs. Default assumes an SSH reverse tunnel from the Mac. |
| `MAC_RUNNER_API_KEY` | *(empty)* | Shared secret — must match `CODING_MODEL_RUNNER_API_KEY` in the Mac runner's `~/.config/coding-model-runner/.env`. |
| `CODING_MODEL_SANDBOX_NODE_ROOT` | *(auto)* | Node install root bound into the test sandbox, so `node_test`/`jest`/`vitest` specs have a `node` on PATH. Usually required: the orchestrator's systemd PATH has no Node, and nvm installs it under `/home`, which the sandbox masks with tmpfs. |
| `CODING_MODEL_NPM_INSTALL_TIMEOUT` | `300` | Seconds allowed for the `jest`/`vitest` dependency-install phase. Budgeted separately from the test timeout — a cold React install fetches a few hundred MB. |
| `CODING_MODEL_NPM_CACHE_DIR` | *(unset)* | Host directory bound in as npm's package cache. Unset means the cache lives on the sandbox's tmpfs and is discarded every run, so each spec re-downloads its tree. Setting it makes cold installs warm, at the cost of shared mutable state across specs. See [JS dependency provisioning](#js-dependency-provisioning). |

### Models
| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL_N_THREADS` | `24` | CPU threads for token generation (physical cores) |
| `MODEL_N_THREADS_BATCH` | `32` | CPU threads for prompt prefill (all threads incl. HT) |
| `MODEL_PATH_*` | *(per-config)* | Override a model's GGUF path. One var per model config: `MODEL_PATH_35B`, `MODEL_PATH_27B`, `MODEL_PATH_30B_TURBO`, `MODEL_PATH_30B_FAST`, `MODEL_PATH_30B_HD`, `MODEL_PATH_30B_FLASH`, `MODEL_PATH_80B_Q8`, `MODEL_PATH_122B`, `MODEL_PATH_230B`, `MODEL_PATH_HYBRID_30B`. (`MODEL_PATH_480B_ULTRA` is gone — the 480B config was retired by DEV-99 and nothing reads it.) |

`n_ctx`, `n_batch`, `n_ubatch`, `ngl` and the KV types are **not** env-tunable —
they are literals in each model config (see [Per-Model Configuration](#per-model-configuration)),
because every one of them is a VRAM-budget decision that has been measured.

### Client
| Variable | Default | Description |
|----------|---------|-------------|
| `CODING_MODEL_SERVER_IP` | `192.168.50.101` | Server IP address. Note the autonomous CLI defaults the same var to `127.0.0.1`. |
| `CODING_MODEL_SERVER_PORT` | `5000` | Server port used by the autonomous CLI. |
| `PERMISSION_MODE` | `default` | `default` / `acceptEdits` / `yolo` |
| `ALLOW_ALL` | *(unset)* | Legacy alias — equivalent to `PERMISSION_MODE=yolo`. |
| `ALLOW_SHELL_MODE` | `false` | Allow pipes, redirects, and shell features. **Off by default.** |
| `COMMAND_WHITELIST` | *(none)* | CSV of allowed commands (empty = all allowed) |
| `CODING_MODEL_WORKSPACE` | *(a fresh temp dir)* | Root the agent's file tools resolve relative paths against, and the only place `WRITE_FILE` / `EDIT_FILE` may write. Writes outside it are **refused in every permission mode, including `yolo`** — this is a hard gate, not a prompt. Shell commands also run with this as their CWD. Unset means a throwaway `coding-model-work-*` temp dir, so an unconfigured session cannot touch a real project. Change it at runtime with `/workspace <dir>`. The server checkout itself is permanently protected and can never be the workspace. |
| `CODING_MODEL_NATIVE_TOOLS` | *(unset)* | Set to `1` to send an OpenAI `tools` array (native function-calls for `remote_exec`) to agents that define a native-tools system prompt. Otherwise all tools are marker-based. |

### Memory / RAG
| Variable | Default | Description |
|----------|---------|-------------|
| `CODING_MODEL_MEMORY_DB` | `<repo>/var/memory_db` | ChromaDB persistence directory. |
| `MEMORY_RELEVANCE_THRESHOLD` | `0.6` | **Maximum cosine distance** for a memory to be injected into a chat request — a hit is kept when `distance <= threshold`, so RAISING this makes retrieval *looser*, not stricter. (This row previously said "minimum similarity", which inverts the meaning.) |
| `PDF_CHUNK_SIZE` | `1000` | Characters per chunk when ingesting a PDF. |
| `PDF_CHUNK_OVERLAP` | `200` | Overlap between PDF chunks. |
| `INGEST_MAX_FILE_SIZE` | `104857600` (100MB) | Per-file byte cap enforced by `/v1/memory/ingest`. `0` disables. |
| `APPLE_DEEP_DOCS_PATH` | *(unset → `tools/appledeepdoc-mcp`)* | Directory holding the Apple Deep Docs MCP server (`main.py` + `venv/`). |

### Autonomous mode (orchestrator)

**Roles and budgets**

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTONOMOUS_PLANNER_AGENT` | `dense_architect` | Agent that runs the spec → YAML planner step. |
| `AUTONOMOUS_ARCHITECT_AGENT` | `dense_architect` | Agent for the design phase. |
| `AUTONOMOUS_IMPLEMENTER_AGENT` | `implementer` | Default implementer; the architect can recommend a tier-specific override per spec. |
| `AUTONOMOUS_REVIEWER_AGENT` | `reviewer` | Reviewer agent; usually overridden to `deep_reviewer` in `.env`. |
| `AUTONOMOUS_SYNTHESIS_AGENT` | `deep_reviewer` | Agent that synthesizes per-file implementation output. |
| `AUTONOMOUS_PLANNER_TIMEOUT` | `900` | Seconds. Must be ≤ `LLAMA_SERVER_REQUEST_TIMEOUT`. |
| `AUTONOMOUS_ARCHITECT_TIMEOUT` | `2700` | Seconds. |
| `AUTONOMOUS_IMPLEMENTER_TIMEOUT` | `1800` | Seconds. |
| `AUTONOMOUS_REVIEWER_TIMEOUT` | `2700` | Seconds. |
| `AUTONOMOUS_PLANNER_MAX_TOKENS` | `4000` | Token budget for planner output. |
| `AUTONOMOUS_ARCHITECT_MAX_TOKENS` | `8000` | Token budget for architect output. |
| `AUTONOMOUS_REVIEWER_MAX_TOKENS` | `16000` | Token budget for reviewer output. |
| `AUTONOMOUS_MAX_RETRIES` | `5` | Per-task retry cap before the supervisor escalates. |

**Implementer sizing.** `AUTONOMOUS_IMPLEMENTER_MAX_TOKENS` (default `16000`) is a
floor, not the whole story — the real budget is computed per task from the file
count:

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTONOMOUS_IMPLEMENTER_MAX_TOKENS` | `16000` | Baseline token budget for implementer output. |
| `AUTONOMOUS_IMPLEMENTER_TOKENS_BASE` | `4000` | Fixed component of the computed budget. |
| `AUTONOMOUS_IMPLEMENTER_TOKENS_PER_FILE` | `1500` | Added per file in the design. |
| `AUTONOMOUS_IMPLEMENTER_MAX_TOKENS_CEILING` | `48000` | Hard cap on the computed budget. |
| `AUTONOMOUS_IMPLEMENTER_MODE` | `auto` | `auto` picks single-shot vs manifest+per-file based on file count. |
| `AUTONOMOUS_MANIFEST_FILE_THRESHOLD` | `8` | File count above which the implementer switches to manifest + per-file mode. |
| `AUTONOMOUS_MANIFEST_MAX_TOKENS` | `8000` | Token budget for the manifest pass. |
| `AUTONOMOUS_PER_FILE_MAX_TOKENS` | `16000` | Token budget per file in per-file mode. |

**Design review** (on by default — an extra review + revision loop between the
architect and the implementer):

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTONOMOUS_DESIGN_REVIEW` | `1` | Master switch. Set to `0` to disable. |
| `AUTONOMOUS_DESIGN_REVIEW_AGENT` | `reviewer` | Agent that reviews the architect's design. |
| `AUTONOMOUS_DESIGN_REVIEW_MAX_TOKENS` | `8000` | Token budget for the design review. |
| `AUTONOMOUS_DESIGN_REVIEW_MAX_REVISIONS` | `1` | How many times the architect may revise before escalating. |

**Supervisor** (meta-orchestrator that decides retry / fail / replan). **Off by
default** — without `AUTONOMOUS_SUPERVISOR=1` the daemon never calls it, so
setting `AUTONOMOUS_SUPERVISOR_AGENT` alone does nothing:

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTONOMOUS_SUPERVISOR` | `0` | Master switch. Set to `1` to enable the supervisor path. |
| `AUTONOMOUS_SUPERVISOR_AGENT` | `supervisor` | Agent used for the decision call. |
| `AUTONOMOUS_SUPERVISOR_TIMEOUT` | `600` | Seconds. |
| `AUTONOMOUS_SUPERVISOR_MAX_TOKENS` | `1500` | Decision output is small; this is a `decide()` function-call, not prose. |
| `AUTONOMOUS_MAX_SUPERVISOR_TRANSITIONS` | `8` | Cap on supervisor-driven state transitions per spec (loop guard). |

**Parse retries** — an agent whose output doesn't parse (bad YAML, missing
markers) gets re-asked rather than failing the spec:

| Variable | Default |
|----------|---------|
| `AUTONOMOUS_PLANNER_PARSE_RETRIES` | `1` |
| `AUTONOMOUS_ARCHITECT_PARSE_RETRIES` | `2` |
| `AUTONOMOUS_REVIEWER_PARSE_RETRIES` | `1` |
| `AUTONOMOUS_PER_FILE_PARSE_RETRIES` | `2` |

**Daemon and storage**

| Variable | Default | Description |
|----------|---------|-------------|
| `ORCHESTRATOR_POLL_INTERVAL` | `5` | Seconds between task-store polls. |
| `ORCHESTRATOR_LOG_LEVEL` | `INFO` | Daemon log level. |
| `CODING_MODEL_TASKS_DB` | `<repo>/var/tasks_db/tasks.sqlite` | SQLite task store. |
| `CODING_MODEL_TASKS_WORKSPACE` | `<repo>/var/tasks_db/specs` | Per-spec artifact workspace. |

### Phase b — adversarial test generation (autonomous mode, opt-in)

After the local Coding Model reviewer's tests pass on retry-0, the orchestrator
optionally calls Gemini and/or Claude to write 3–7 additional
`adversarial_test_*.py` files targeting edge cases Coding Model missed. The
combined suite re-runs; failure downgrades the verdict and falls
through to the implementer retry loop with the failing-test output as
feedback. Fail-open: per-provider errors (key, network, timeout,
parse) are caught inside the loop and the remaining providers still
run; if everything fails the original PASS stands. Reuses
`GEMINI_API_KEY` and `ANTHROPIC_API_KEY` from the `/review` block.

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTONOMOUS_ADVERSARIAL_TESTS_ENABLED` | `0` | Master switch. Set to `1` to enable. |
| `AUTONOMOUS_ADVERSARIAL_PROVIDER` | `gemini` | `gemini`, `claude`, or `both`. See provider-mode notes below. |
| `AUTONOMOUS_ADVERSARIAL_GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model used when the provider list includes `gemini`. |
| `AUTONOMOUS_ADVERSARIAL_CLAUDE_MODEL` | `claude-sonnet-4-6` | Claude model used when the provider list includes `claude`. |
| `AUTONOMOUS_ADVERSARIAL_MAX_TOKENS` | `8000` | Output token cap per provider call. |
| `AUTONOMOUS_ADVERSARIAL_TIMEOUT` | `300` | Seconds for each provider call. ThreadPoolExecutor enforces the Gemini-side timeout; the Anthropic SDK enforces its own. |

Provider-mode notes:

- `gemini` / `claude` (single-provider): files use the
  `adversarial_test_*.py` namespace.
- `both`: providers run sequentially with provider-tagged filename
  prefixes (`adversarial_test_claude_*.py`,
  `adversarial_test_gemini_*.py`) so neither can overwrite the
  other. Doubles API cost AND doubles over-specification risk —
  measure each provider's false-FAIL rate separately first.

Inspect aggregate stats with `scripts/phase_b_stats.py`. Per-spec
firings appear in the dashboard's `EventTimeline` as `AGENT_RAN`
events with `role=adversarial_test_writer` (one event per provider
in `both` mode).

### `/review` fan-out

`/review` sends the uncommitted git diff to four judges in parallel: Claude,
Gemini, and two local agents. A judge whose key or SDK is missing is skipped.

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | *(unset)* | Enables the Claude judge (and Phase b's Claude provider). |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | *(unset)* | Enables the Gemini judge (and Phase b's Gemini provider). |
| `REVIEW_CLAUDE_MODEL` | `claude-sonnet-4-6` | Claude model for the review judge. |
| `REVIEW_GEMINI_MODEL` | `gemini-3-pro` | Gemini model for the review judge. |
| `REVIEW_CODING_MODEL_REVIEWER_AGENT` | `reviewer` | First local judge. |
| `REVIEW_CODING_MODEL_DEEP_REVIEWER_AGENT` | `deep_reviewer` | Second local judge. |
| `REVIEW_TIMEOUT` | `120` | Seconds per judge. |
| `REVIEW_MAX_TOKENS` | `2000` | Output cap per judge. |
| `REVIEW_DIFF_MAX_BYTES` | `262144` | Diffs larger than this are rejected rather than truncated. |

### Jira sync (optional)

If `JIRA_URL` is set, review gates sync to a Jira board so you can approve from
anywhere (native email notifications). Without it the orchestrator falls back to
an in-memory `FakeJiraClient` and nothing external is touched.

| Variable | Default | Description |
|----------|---------|-------------|
| `JIRA_URL` | *(unset)* | Atlassian site URL. Enables the real client. |
| `JIRA_EMAIL` | *(unset)* | Account email for the API token. |
| `JIRA_API_TOKEN` | *(unset)* | API token. |
| `JIRA_PROJECT_KEY` | `AUTO` | Project key issues are created under. |
| `JIRA_SYNC_POLL_INTERVAL` | *(see `jira_sync.py`)* | Seconds between inbound-sync polls. |
| `JIRA_SYNC_BATCH_SIZE` | *(see `jira_sync.py`)* | Issues fetched per poll. |

### Dashboard (`scripts/serve_dashboard.py`)
| Variable | Default | Description |
|----------|---------|-------------|
| `CODING_MODEL_DASHBOARD_HOST` | `127.0.0.1` | Bind address for the static SPA server. Set `0.0.0.0` to opt in to LAN exposure (DEV-192). |
| `CODING_MODEL_DASHBOARD_PORT` | `3001` | Port. (Vite's dev server uses 3000.) |
| `CODING_MODEL_DASHBOARD_ROOT` | `<repo>/dashboard/dist` | Directory served. Must contain a built SPA (`npm run build`). |

### Mac runner (`mac_runner/`, runs on macOS)

Variables loaded from `~/.config/coding-model-runner/.env`.

| Variable | Default | Description |
|----------|---------|-------------|
| `CODING_MODEL_RUNNER_API_KEY` | *(required)* | Shared secret with the orchestrator's `MAC_RUNNER_API_KEY`. Runner refuses to start without it unless `CODING_MODEL_RUNNER_ALLOW_UNAUTH=1`. |
| `CODING_MODEL_RUNNER_HOST` | `127.0.0.1` | Bind address. Keep as loopback; use SSH reverse tunnel from the Linux box. |
| `CODING_MODEL_RUNNER_PORT` | `5050` | Bind port. |
| `CODING_MODEL_RUNNER_WORKTREE_ROOT` | `~/Library/Caches/coding-model-runner/worktrees` | Where per-spec git worktrees are materialized. |
| `CODING_MODEL_RUNNER_DERIVED_DATA` | `~/Library/Caches/coding-model-runner/DerivedData` | Shared xcodebuild DerivedData across runs (speeds up incremental builds). |
| `CODING_MODEL_RUNNER_REPOS_FILE` | `~/.config/coding-model-runner/repos.yml` | Symbolic-name → absolute-path map; the runner refuses any repo not listed here. |
| `CODING_MODEL_RUNNER_ENV_FILE` | *(unset)* | Override the .env path the runner loads. |
| `CODING_MODEL_RUNNER_ALLOW_UNAUTH` | *(unset)* | Set to `1` to let the runner start without an API key (dev only). |

### Planner `test_strategy` block

The planner emits a `test_strategy` map that the daemon forwards to
`run_tests()` as keyword args. Keys:

| Key | Required? | Description |
|---|---|---|
| `framework` | yes | `pytest` \| `node_test` \| `jest` \| `vitest` \| `swift_test` \| `xcodebuild_test` |
| `required` | optional, default `true` | Whether a failing test blocks the review gate |
| `repo` | swift / xcode only | Symbolic repo name from `repos.yml` |
| `base_ref` | optional, default `HEAD` | Git ref to create the worktree from |
| `scheme` | xcodebuild only, required | Xcode scheme to test |
| `destination` | xcodebuild only | e.g. `platform=iOS Simulator,name=iPhone 15,OS=latest`. Defaults to `platform=macOS`. |
| `configuration` | optional | Xcode build configuration. Defaults to `Debug`. |
| `workspace` / `project` | optional | `.xcworkspace` / `.xcodeproj` path relative to worktree. Auto-detected if omitted. |
| `filter` | optional | `swift test --filter` regex or xcodebuild `-only-testing:` fragment |

The Xcode path expects a project that is **committed to the repo** — the runner
only materializes a git worktree and applies patches; nothing regenerates an
`.xcodeproj` on the Mac side.

### JS dependency provisioning

There are two JavaScript paths, and they differ in exactly one way: whether the
spec is allowed to have dependencies.

| Framework | Dependencies | Network | Use for |
|---|---|---|---|
| `node_test` | none, ever | never | The default. Pure-logic cores testable with `node:test` + `node:assert`. |
| `vitest` / `jest` | installed per-spec | install phase only | Specs that genuinely need packages — React/JSX component tests above all. |

For `vitest`/`jest` the runner adds an install phase **before** the test run:

1. **Install phase** — `npm ci` (when the spec ships a lockfile) or `npm install`,
   inside bubblewrap with `--share-net`. This is the only part of the pipeline
   that may reach the network. Every other confinement still applies: `/home`
   and `/root` are tmpfs-masked, so the operator's `~/.npmrc` registry tokens,
   `~/.ssh` and `.env` files are invisible to it; only the spec directory is
   writable; the same seccomp denylist is loaded.
2. **Test phase** — the existing `--unshare-all` sandbox, no network, with
   `node_modules` already on disk.

**`--ignore-scripts` is passed unconditionally and there is no override.** npm
lifecycle hooks (`preinstall`/`install`/`postinstall`/`prepare`) are arbitrary
code execution chosen by whatever the model wrote into `package.json`, and they
would run at install time — while the network is open. A CLI flag also outranks
project config in npm's precedence chain, so a spec cannot re-enable them by
writing `ignore-scripts=false` into an `.npmrc` beside its `package.json`
(pinned by a test). The cost is packages needing a native build step
(node-gyp); React, react-dom, vitest, jest, jsdom and Testing Library are all
pure JS and unaffected.

Prefer **vitest** for anything with JSX/TSX — its built-in esbuild transform
needs no babel or ts-jest configuration. Pair it with `environment: 'jsdom'`
in a `vitest.config.js` for component tests.

Specs should **not** hand-write a `package-lock.json`. A lockfile that
disagrees with `package.json` makes `npm ci` fail outright, which is the usual
outcome when a model authors both; with no lockfile the installer resolves
fresh. The planner prompt states this.

If `npm` hangs and the phase dies at `CODING_MODEL_NPM_INSTALL_TIMEOUT` with no
output, suspect DNS: on systemd-resolved hosts `/etc/resolv.conf` is a symlink
into `/run`, which the sandbox does not bind, so lookups fail `EAI_AGAIN`. The
runner binds the symlink's target for `--share-net` sandboxes to prevent this.

## Shell auto-approval

Unattended shell execution is gated by an **allow-list**, not a denylist. A
command runs without a prompt only when all of the following hold
(`src/coding_model_server/tool_handlers/safety.py`):

1. The permission mode allows it at all (`yolo` + `ALLOW_REMOTE_EXEC_YOLO=1`).
2. The command contains **no** shell metacharacters — `| & ; $ \` > < ( )` — any
   of which could chain or redirect past the check.
3. Its base binary is in one of:
   - `READONLY_COMMANDS` — `ls`, `cat`, `grep`, `rg`, `stat`, `jq`, `diff`, … (cannot mutate or reach the network)
   - `BUILD_TEST_COMMANDS` — `pytest`, `make`, `cmake`, `npm`, `cargo`, `go`, `swift`, `xcodebuild`, `ruff`, `mypy`, … (these *do* execute project code by design; general-purpose sinks like `python3 -c`, `node -e`, `bash -c` are deliberately absent)
   - `git`, restricted further to read-only subcommands (`status`, `log`, `diff`, `show`, `blame`, …; `push`/`reset`/`clean`/`checkout`/`rebase` are absent by design)
   - `EXTRA_AUTO_APPROVE_COMMANDS` (your additions)

Anything unrecognised prompts. The `DENY_RULES` and dangerous-pattern lists
(`rm -rf /`, `find / -delete`, fork bombs, raw block-device writes) still exist,
but they are a **backstop** — the allow-list is the security boundary. Nothing
executes silently on the strength of "the denylist didn't match".

## Per-Model Configuration

Each model is defined via `_create_model_config()` in
`src/coding_model_server/config.py` (all 10 configs and the agent registry live
there; `server.py` is app assembly only):

```python
_MY_MODEL = _create_model_config(
    'MODEL_PATH_ENV_VAR',           # Env var to override path
    '/path/to/model.gguf',          # Default model path
    n_gpu_layers,                    # Layers offloaded to GPU (positional)
    n_ctx=32768,                     # Context window size (positional)
    n_batch=2048,                    # Prompt processing batch size (positional)
    n_ubatch=512,                    # Physical micro-batch size for prefill
    type_k=8,                        # KV cache key type: 8=Q8_0, 2=Q4_0
    type_v=8,                        # KV cache value type: 8=Q8_0, 2=Q4_0
    repeat_penalty=1.15,             # Repetition penalty (lower = more code-friendly)
    repeat_last_n=256,               # Penalty window (tokens)
    cpu_moe=False,                   # Keep ALL MoE expert weights on CPU
    n_cpu_moe=None,                  # Keep only the first N layers' experts on CPU
    server_extra_args=None,          # Extra llama-server flags (list of strings)
    logit_bias=None,                 # Token bans: [[token_id, -100.0], ...]
    draft=None,                      # Speculative-decode draft model (dict)
)
```

There is **no `backend` parameter** — every model runs on the `llama-server`
subprocess (port 8081, `tools/llama-server`). The in-process llama-cpp-python
backend was removed in April 2026.

### MoE expert offload (`cpu_moe` / `n_cpu_moe`)

For MoE models, `--cpu-moe` keeps expert weights on CPU while attention layers
go on GPU. This dramatically reduces per-layer VRAM cost, enabling near-max GPU
offload:

- Without expert offload: each GPU layer carries attention + expert weights (~1,500–1,700 MiB/layer)
- With `cpu_moe=True` (`--cpu-moe`): each GPU layer is attention only (~20–100 MiB/layer)

This is why Nemotron can run at ngl=52/52 with 1M context on an RTX 5080.

`n_cpu_moe=N` (`--n-cpu-moe N`) is the **partial** split: only the first N
layers' experts stay on CPU; the rest ride on the GPU. Lower N = more experts on
GPU = faster decode, bounded by VRAM (the KV cache competes for the same space).
It overrides `cpu_moe` when set. The three agents tuned this way
(`implementer` N=20, `fast_implementer` N=26, `native_implementer` N=20) each
traded context for decode throughput; the comment above each config records the
sweep that produced the number.

Tune with `scripts/sweep_cpu_moe.py`, which warms up and takes a median over
`--reps` — decode drifts upward ~15% over a session, so a single descending
sweep will systematically flatter whichever N it measures last.

### KV Cache Quantization

Controls VRAM usage for the attention cache:

| Type | Value | VRAM per token per layer | Use when |
|------|-------|--------------------------|----------|
| Q8_0 | `8` | ~1 byte | Plenty of VRAM, best quality |
| Q4_0 | `2` | ~0.5 bytes | VRAM-constrained, 2x context for same VRAM |

Q8_0 is the default and the preference wherever it fits: KV-quant noise produces
a diffuse quality degradation that is harder to manage than simply having less
context. Hybrid configs (Q8_0 keys + Q4_0 values) preserve attention precision
while saving VRAM.

### Speculative decoding

Two mechanisms, both optional:

- **MTP** (multi-token prediction): a model shipping a native MTP head plus
  `server_extra_args=['--spec-type', 'draft-mtp', '--spec-draft-n-max', '2']`.
  Used by `dense_architect` / `supervisor` (Qwen3.6-27B-MTP), where it roughly
  doubles decode on a dense model that has ~24 of 64 layers on CPU. Lossless.
- **`draft=`**: a separate same-tokenizer draft model (`path`, `n_gpu_layers`,
  `n_ctx`, `cpu_moe`, `draft_max`, `draft_min`, `draft_p_min`). Wired but not
  currently used by any agent — measured on the 480B in May 2026 and it
  *regressed* decode ~18%, because CPU memory-bandwidth contention between the
  target's expert evaluation and the draft's full forward pass exceeded the
  spec-decode gain.

### VRAM Budget Guidelines

For an RTX 5080 (16,303 MiB):

- The loader refuses a **reload** that would leave less than `_VRAM_MARGIN_MIB`
  (500 MiB) free. A config tuned so tight that it clears the first load but not
  a reload will brick the server after the idle watchdog reaps the child — this
  happened at `implementer` `n_cpu_moe=18` and is why it now sits at 20.
- Leave ~**1.4 GB** free in practice: that is the observed floor for a
  *model swap* (the incoming child allocates before the outgoing one has fully
  released).
- Each GPU layer costs model-dependent VRAM (check comments in model configs)
- KV cache scales linearly with `n_ctx` and `n_gpu_layers`
- Use `nvidia-smi` after loading to verify actual usage

### Repeat Penalty Tuning

| Setting | Value | Effect |
|---------|-------|--------|
| `repeat_penalty=1.15` | Default | Good for chat, can cause premature EOS on long code |
| `repeat_penalty=1.05` | Code-friendly | Better for large file writes, less repetition suppression |
| `repeat_last_n=256` | Default | Windowed penalty, prevents full-context penalty stacking |
| `repeat_last_n=-1` | Full context | Penalizes all prior tokens (can hurt long code output) |

## System Tuning (Optional)

For optimal inference performance on dedicated hardware. None of these are
automated by the repo — apply them by hand:

| Setting | Method | Effect |
|---------|--------|--------|
| CPU governor: `performance` | `cpupower frequency-set -g performance` | Prevents CPU frequency scaling |
| GPU persistence mode | `nvidia-smi -pm 1` | Faster model loading |
| THP: `always` | sysctl | Better memory access patterns |
| Swappiness: `10` | sysctl | Keeps model weights in RAM |
| RAM: XMP/EXPO enabled | BIOS | Full memory bandwidth |

## Session Storage

Sessions are stored in `~/.coding_model_sessions/`:

```
~/.coding_model_sessions/
├── default.json              # Default unnamed session
├── my_project.json           # Named session
└── metal_renderer.json       # Another named session
```

Each file contains:
```json
{
  "messages": [...],
  "last_agent": "implementer",
  "session_name": "my-project",
  "timestamp": "2026-03-31T20:00:00"
}
```

Legacy sessions (`~/.coding_model_chat_history*.json`) are auto-migrated on first startup.
