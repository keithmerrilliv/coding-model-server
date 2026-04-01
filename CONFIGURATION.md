# Configuration Reference

## Environment Variables

Set in `.env` (loaded by startup scripts) or export in your shell.

### Server
| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `5000` | Server listen port |
| `HOST` | `0.0.0.0` | Server bind address |
| `ADMIN_API_KEY` | *(empty)* | API key for authentication (empty = disabled) |

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

## Per-Model Configuration

Each model is defined via `_create_model_config()` in `server.py`:

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
