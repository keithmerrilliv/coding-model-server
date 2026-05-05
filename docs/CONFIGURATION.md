# Configuration Reference

## Environment Variables

Set in `~/.config/qwen-server/.env` (preferred; keeps secrets out of the repo)
or in a repo-local `.env` for dev. Loaded by startup scripts and systemd units.

### Server
| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `5000` | Server listen port |
| `HOST` | `127.0.0.1` | Server bind address. Set to `0.0.0.0` only if you need LAN access **and** have a strong `ADMIN_API_KEY`. |
| `ADMIN_API_KEY` | *(required)* | API key for authentication. The server refuses to start without it unless `QWEN_ALLOW_UNAUTH=1` is set. |
| `QWEN_ALLOW_UNAUTH` | *(unset)* | Set to `1` to permit unauthenticated operation (local dev only — never on a LAN-exposed instance). |
| `QWEN_ENV_FILE` | *(unset)* | Override the .env path loaded by `start.sh` / `start-client.sh`. |
| `QWEN_ALLOW_UNSANDBOXED_TESTS` | *(unset)* | Set to `1` to run LLM-generated tests without the bubblewrap sandbox. Not recommended — tests then execute with the orchestrator's own privileges. Install `bubblewrap` (`apt install bubblewrap`) instead. |
| `LLAMA_SERVER_REQUEST_TIMEOUT` | `2700` | Max seconds for a single llama-server inference call. Must be ≥ the longest `AUTONOMOUS_*_TIMEOUT` so the inner request doesn't fail before the outer role budget. |
| `ALLOW_REMOTE_EXEC_YOLO` | *(unset)* | Defense-in-depth: even in `PERMISSION_MODE=yolo`, `<<<REMOTE_EXEC>>>` shell commands still prompt unless this is set to `1`. Two opt-ins must compromise (yolo flag + this env) before LLM-emitted shell runs silently. |
| `CORS_ORIGINS` | `localhost,127.0.0.1` | CSV of allowed CORS origins. Must include port (e.g. `http://localhost:3000`) for browser dashboards. `allow_credentials` is automatically disabled when `*` is in the list. |
| `INGEST_ALLOWED_DIR` | *(unset)* | Directory under which `/v1/memory/ingest` accepts paths (in addition to system temp). Realpath-resolved before the prefix check, so symlinks can't escape. |
| `MAC_RUNNER_URL` | `http://127.0.0.1:5050` | Where the orchestrator dispatches `swift_test` / `xcodebuild_test` jobs. Default assumes an SSH reverse tunnel from the Mac. |
| `MAC_RUNNER_API_KEY` | *(empty)* | Shared secret — must match `QWEN_RUNNER_API_KEY` in the Mac runner's `~/.config/qwen-runner/.env`. |

### Mac runner (`mac_runner/`, runs on macOS)

Variables loaded from `~/.config/qwen-runner/.env`.

| Variable | Default | Description |
|----------|---------|-------------|
| `QWEN_RUNNER_API_KEY` | *(required)* | Shared secret with the orchestrator's `MAC_RUNNER_API_KEY`. Runner refuses to start without it unless `QWEN_RUNNER_ALLOW_UNAUTH=1`. |
| `QWEN_RUNNER_HOST` | `127.0.0.1` | Bind address. Keep as loopback; use SSH reverse tunnel from the Linux box. |
| `QWEN_RUNNER_PORT` | `5050` | Bind port. |
| `QWEN_RUNNER_WORKTREE_ROOT` | `~/Library/Caches/qwen-runner/worktrees` | Where per-spec git worktrees are materialized. |
| `QWEN_RUNNER_DERIVED_DATA` | `~/Library/Caches/qwen-runner/DerivedData` | Shared xcodebuild DerivedData across runs (speeds up incremental builds). |
| `QWEN_RUNNER_REPOS_FILE` | `~/.config/qwen-runner/repos.yml` | Symbolic-name → absolute-path map; the runner refuses any repo not listed here. |
| `QWEN_RUNNER_ALLOW_UNAUTH` | *(unset)* | Set to `1` to let the runner start without an API key (dev only). |

### Planner `test_strategy` block

The planner emits a `test_strategy` map that the daemon forwards to
`run_tests()` as keyword args. Keys:

| Key | Required? | Description |
|---|---|---|
| `framework` | yes | `pytest` \| `jest` \| `swift_test` \| `xcodebuild_test` |
| `required` | optional, default `true` | Whether a failing test blocks the review gate |
| `repo` | swift / xcode only | Symbolic repo name from `repos.yml` |
| `base_ref` | optional, default `HEAD` | Git ref to create the worktree from |
| `scheme` | xcodebuild only, required | Xcode scheme to test |
| `destination` | xcodebuild only | e.g. `platform=iOS Simulator,name=iPhone 15,OS=latest`. Defaults to `platform=macOS`. |
| `configuration` | optional | Xcode build configuration. Defaults to `Debug`. |
| `workspace` / `project` | optional | `.xcworkspace` / `.xcodeproj` path relative to worktree. Auto-detected if omitted. |
| `filter` | optional | `swift test --filter` regex or xcodebuild `-only-testing:` fragment |

### Model Defaults
| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL_N_THREADS` | `24` | CPU threads for token generation (physical cores) |
| `MODEL_N_THREADS_BATCH` | `32` | CPU threads for prompt prefill (all threads incl. HT) |
| `MODEL_N_BATCH` | `2048` | Batch size for prompt processing |
| `MODEL_CONTEXT_SIZE` | `524288` | Default context size (overridden per-model) |

### Client
| Variable | Default | Description |
|----------|---------|-------------|
| `QWEN_SERVER_IP` | `192.168.50.101` | Server IP address |
| `PERMISSION_MODE` | `default` | `default` / `acceptEdits` / `yolo` |
| `ALLOW_SHELL_MODE` | `true` | Allow pipes, redirects, and shell features |
| `COMMAND_WHITELIST` | *(none)* | CSV of allowed commands (empty = all allowed) |

### Autonomous mode (orchestrator)
| Variable | Default | Description |
|----------|---------|-------------|
| `AUTONOMOUS_PLANNER_AGENT` | `q36_architect` | Agent that runs the spec → YAML planner step. |
| `AUTONOMOUS_ARCHITECT_AGENT` | `q36_architect` | Agent for the design phase. |
| `AUTONOMOUS_IMPLEMENTER_AGENT` | `implementer` | Default implementer; the architect can recommend a tier-specific override per spec. |
| `AUTONOMOUS_REVIEWER_AGENT` | `reviewer` | Reviewer agent; usually overridden to `deep_reviewer` in `.env`. |
| `AUTONOMOUS_SUPERVISOR_AGENT` | `supervisor` | Meta-orchestrator that decides retry / fail / replan paths. |
| `AUTONOMOUS_PLANNER_TIMEOUT` | `900` | Seconds. Must be ≤ `LLAMA_SERVER_REQUEST_TIMEOUT`. |
| `AUTONOMOUS_ARCHITECT_TIMEOUT` | `2700` | Seconds. |
| `AUTONOMOUS_IMPLEMENTER_TIMEOUT` | `1800` | Seconds. |
| `AUTONOMOUS_REVIEWER_TIMEOUT` | `2700` | Seconds. |
| `AUTONOMOUS_PLANNER_MAX_TOKENS` | `4000` | Token budget for planner output. |
| `AUTONOMOUS_ARCHITECT_MAX_TOKENS` | `8000` | Token budget for architect output. |
| `AUTONOMOUS_IMPLEMENTER_MAX_TOKENS` | `16000` | Token budget for implementer output. |
| `AUTONOMOUS_REVIEWER_MAX_TOKENS` | `16000` | Token budget for reviewer output. |
| `AUTONOMOUS_MAX_RETRIES` | `3` | Per-task retry cap before the supervisor escalates. |

### Phase b — adversarial test generation (autonomous mode, opt-in)

After the local Qwen reviewer's tests pass on retry-0, the orchestrator
optionally calls Gemini and/or Claude to write 3–7 additional
`adversarial_test_*.py` files targeting edge cases Qwen missed. The
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

## Per-Model Configuration

Each model is defined via `_create_model_config()` in `src/qwen_server/server.py`:

```python
_MY_MODEL = _create_model_config(
    'MODEL_PATH_ENV_VAR',           # Env var to override path
    '/path/to/model.gguf',          # Default model path
    n_gpu_layers,                    # Layers offloaded to GPU
    n_ctx=32768,                     # Context window size
    n_batch=2048,                    # Prompt processing batch size
    n_ubatch=512,                    # Physical micro-batch size for prefill
    backend='llama_cpp',             # 'llama_cpp' or 'llama_server'
    type_k=8,                        # KV cache key type: 8=Q8_0, 2=Q4_0
    type_v=8,                        # KV cache value type: 8=Q8_0, 2=Q4_0
    repeat_penalty=1.15,             # Repetition penalty (lower = more code-friendly)
    repeat_last_n=256,               # Penalty window (tokens)
    cpu_moe=False,                   # Keep MoE expert weights on CPU (llama_server only)
    server_extra_args=None,          # Extra llama-server flags (list of strings)
    logit_bias=None,                 # Token bans: [[token_id, -100.0], ...]
    yarn=False,                      # Enable YaRN RoPE scaling for extended context
)
```

### Backend Selection

- **`llama_cpp`**: In-process via llama-cpp-python (0.3.19+). Use for models with supported architectures including Qwen3, Qwen3.5, and Coder families.
- **`llama_server`**: Subprocess on port 8081 via `tools/llama-server`. Required for:
  - Nemotron (nemotron_h_moe architecture)
  - MiniMax M2.5 (native Jinja template needed)
  - GLM-4.7-Flash (glm4 template)
  - Coder-Next (qwen3next architecture)
  - Any model needing `--jinja` or `--chat-template` flags

### MoE CPU Offload (`cpu_moe`)

For MoE models on the `llama_server` backend, `cpu_moe=True` passes `--cpu-moe` to keep expert weights on CPU while putting attention layers on GPU. This dramatically reduces per-layer VRAM cost, enabling near-max GPU offload:

- Without `--cpu-moe`: each GPU layer includes attention + expert weights (~1,500-1,700 MiB/layer)
- With `--cpu-moe`: each GPU layer is attention only (~20-100 MiB/layer)

This is why Nemotron can run at ngl=52/52 with 1M context on an RTX 5080.

### KV Cache Quantization

Controls VRAM usage for the attention cache:

| Type | Value | VRAM per token per layer | Use when |
|------|-------|--------------------------|----------|
| Q8_0 | `8` | ~1 byte | Plenty of VRAM, best quality |
| Q4_0 | `2` | ~0.5 bytes | VRAM-constrained, 2x context for same VRAM |

Hybrid configs (Q8_0 keys + Q4_0 values) preserve attention precision while saving VRAM.

### VRAM Budget Guidelines

For an RTX 5080 (16,303 MiB):

- Leave at least **800 MiB free** for compute buffers and system overhead
- Each GPU layer costs model-dependent VRAM (check comments in model configs)
- KV cache scales linearly with `n_ctx` and `n_gpu_layers`
- Use `nvidia-smi` after loading to verify actual usage
- **Never enable `use_mlock=True`** — causes CUDA OOM during model swaps

### Repeat Penalty Tuning

| Setting | Value | Effect |
|---------|-------|--------|
| `repeat_penalty=1.15` | Default | Good for chat, can cause premature EOS on long code |
| `repeat_penalty=1.05` | Code-friendly | Better for large file writes, less repetition suppression |
| `repeat_last_n=256` | Default | Windowed penalty, prevents full-context penalty stacking |
| `repeat_last_n=-1` | Full context | Penalizes all prior tokens (can hurt long code output) |

## System Tuning (Optional)

For optimal inference performance on dedicated hardware:

| Setting | Method | Effect |
|---------|--------|--------|
| CPU governor: `performance` | systemd service | Prevents CPU frequency scaling |
| GPU persistence mode | `nvidia-smi -pm 1` | Faster model loading |
| THP: `always` | sysctl | Better memory access patterns |
| Swappiness: `10` | sysctl | Keeps model weights in RAM |
| RAM: XMP/EXPO enabled | BIOS | Full memory bandwidth |

## Session Storage

Sessions are stored in `~/.qwen_sessions/`:

```
~/.qwen_sessions/
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

Legacy sessions (`~/.qwen_chat_history*.json`) are auto-migrated on first startup.
