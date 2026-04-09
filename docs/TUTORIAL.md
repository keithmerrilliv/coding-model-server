# Tutorial: Setting Up and Using the Qwen Multi-Agent Server

This guide walks through setting up a local LLM inference server with a multi-agent coding assistant client. By the end, you'll have a system where AI agents can read files, execute commands, write code, and search documentation — all running on your own hardware.

## Prerequisites

**Server machine (Linux):**
- NVIDIA GPU with at least 8 GB VRAM (16 GB recommended)
- 64 GB+ system RAM (192 GB for large models like 397B/480B)
- NVIDIA drivers + CUDA toolkit installed
- Python 3.10+

**Client machine (macOS or Linux):**
- Python 3.10+
- Network access to the server

## Part 1: Server Setup

### 1.1 Clone and Install

```bash
git clone <repo-url> qwen-server
cd qwen-server
./setup.sh
```

This creates a Python venv, installs dependencies (FastAPI, llama-cpp-python, ChromaDB, sentence-transformers), and sets up the directory structure.

### 1.2 Download Models

Models are GGUF files from HuggingFace. Download them to any directory and reference the paths in `server.py`. A good starting point is a single small model:

```bash
# Example: download Qwen3-Coder-30B (3B active MoE, ~22 GB)
pip install huggingface-hub
huggingface-cli download unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF \
  Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf \
  --local-dir ~/.lmstudio/models/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF
```

Set `HF_TOKEN` in your environment for authenticated downloads (unauthenticated is rate-limited).

### 1.3 Configure Your First Model

Open `server.py` and find the `Config` class. Add a model config:

```python
_MY_MODEL = _create_model_config(
    'MODEL_PATH_MY_MODEL',                    # Env var to override path
    '/path/to/Qwen3-Coder-30B-Q4_K_M.gguf',  # Default path
    20,       # n_gpu_layers — start low, increase until ~1 GB VRAM free
    32768,    # n_ctx — context window (32K is a safe start)
    2048,     # n_batch — prompt processing batch size
)
```

Then add an agent that uses it:

```python
AGENTS = {
    'implementer': _create_agent_config(
        'Implementer — My Model (description)',
        _IMPLEMENTER_SYSTEM_PROMPT,
        _MY_MODEL,
        executor=True   # Enables tool execution
    ),
}
```

### 1.4 Finding the Right GPU Layer Count

The most important tuning parameter is `n_gpu_layers`. More layers on GPU = faster inference but more VRAM.

1. Start with a low value (e.g., 4)
2. Start the server: `./start.sh`
3. Send a test request and check VRAM: `nvidia-smi`
4. If you have > 2 GB free, increase layers
5. Repeat until ~1 GB free remains

```bash
# Quick test after starting server:
curl -s http://localhost:5000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"implementer","messages":[{"role":"user","content":"Hello"}],"max_tokens":50}'
```

### 1.5 Configure Environment

```bash
cp .env.example .env
# Edit .env:
#   PORT=5000
#   HOST=0.0.0.0
```

### 1.6 Start as a Service (Recommended)

Create `/etc/systemd/system/qwen-server.service`:

```ini
[Unit]
Description=Qwen Multi-Agent Server (FastAPI)
After=network.target

[Service]
Type=simple
User=your-username
WorkingDirectory=/path/to/qwen-server
Environment=PYTHONUNBUFFERED=1
ExecStart=/path/to/qwen-server/venv/bin/python server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable qwen-server
sudo systemctl start qwen-server
journalctl -u qwen-server -f  # View logs
```

## Part 2: Client Setup

### 2.1 Configure the Client

Edit `qwen_client/config.py` or set the environment variable:

```bash
export QWEN_SERVER_IP=192.168.1.100  # Your server's IP
```

### 2.2 Start the Client

```bash
./start-client.sh
```

You'll see the available agents, permission mode, and a prompt. Type a message to start interacting.

### 2.3 Basic Usage

```
You (implementer) > Write a Python function to sort a list of dicts by a key

# The agent will:
# 1. Generate code
# 2. Write it to a file via <<<WRITE_FILE>>>
# 3. Run it via <<<REMOTE_EXEC>>> to verify
# 4. Report results

You (implementer) > @architect Design a REST API for a todo app

# Switches to architect agent and sends the message
```

### 2.4 Multi-Agent Workflows

```
You (implementer) > @architect Design a URL shortener then @implementer build it

# Architect designs, then implementer receives the design and codes it
```

### 2.5 Session Management

```
/session new my-project       # Create named session
/sessions                     # List all sessions
/session other-project        # Switch to another session
/rename better-name           # Rename current session
```

Sessions persist across restarts. Each session has independent history and tracks the last-used agent.

## Part 3: Understanding the System

### 3.1 Anatomy of a Request: From Prompt to Output

This section traces the complete lifecycle of a single user message through every component, explaining what happens at each stage and why.

#### Stage 1: User Input (Client)

When you type a message and press Enter, the client:

1. **Classifies the query** — The agentic context system (`qwen_client/agentic/context.py`) determines if your query is simple, medium, or complex. This sets the tool-use budget (how many iterations the agent gets before being forced to synthesize).
2. **Appends to history** — Your message is added to the in-memory conversation history.
3. **Checks history budget** — Budget is measured on the compressed view (what actually gets sent). At 120K chars, the model generates a conversation summary. At 150K, the oldest 25% is dropped.
4. **Sanitizes history** — Internal flags (`auto_send`, `_retried`) are stripped. Empty messages and invalid roles are filtered. Old messages are compressed (head + tail only for tool outputs > 500 chars).
5. **Injects agentic context** — The scratchpad, retrieval plan, budget warnings, and confidence gate are appended as an additional user message visible only to the model (not persisted).

#### Stage 2: HTTP Request (Network)

The client sends a streaming POST to `http://<server>:5000/v1/chat/completions`:

```json
{
  "model": "implementer",
  "messages": [{"role": "user", "content": "..."}],
  "stream": true,
  "max_tokens": 30000
}
```

This is the OpenAI-compatible chat completions API. The server authenticates via `ADMIN_API_KEY` if configured.

#### Stage 3: Server-Side Processing

The FastAPI server (`server.py`) receives the request and:

1. **Resolves the agent** — Looks up the model config (path, ngl, n_ctx, backend, etc.) from the `AGENTS` dict.
2. **Injects few-shot examples** — For short conversations (≤4 messages), format examples are prepended so the model learns the tool marker syntax from "conversation" rather than instructions alone.
3. **RAG context retrieval** — The last user message is embedded via SentenceTransformer (`all-MiniLM-L6-v2`) and queried against ChromaDB (842K+ documents). Relevant memories are injected into the system prompt. This runs async with a 2-second timeout.
4. **Token budget calculation** — The server estimates how many tokens the prompt will consume and how many remain for the response. This budget number is injected into the system prompt so the model knows how much space it has.

#### Stage 4: Model Loading (Server)

If the requested model isn't already cached:

1. **Backend coordination** — The server ensures mutual exclusion between the `llama_cpp` (in-process) and `llama_server` (subprocess) backends. If the other backend has a model loaded, it's unloaded first to free VRAM.
2. **VRAM cleanup** — `gc.collect()` + `torch.cuda.empty_cache()` releases GPU memory.
3. **Model loading** — For `llama_cpp`: the GGUF file is memory-mapped and GPU layers are offloaded. For `llama_server`: a subprocess is spawned with the configured flags (`-ngl`, `-c`, `-fa`, `--cpu-moe`, etc.) and the server polls `/health` until ready.
4. **LRU-1 cache** — Only one model is cached at a time. Loading a different model evicts the previous one.

#### Stage 5: Prefill (GPU + CPU)

This is where the prompt is processed — the most compute-intensive phase:

1. **Tokenization** — The text prompt is converted to token IDs using the model's tokenizer (part of the GGUF file).
2. **Prompt processing (prefill)** — Every token in the prompt must pass through ALL model layers sequentially. For a 15K-token prompt on Coder-Next (48 layers), this means 720K layer-evaluations. Each layer involves:
   - **Attention**: Query/Key/Value projections, attention scores, softmax, value aggregation. This runs on GPU (fast) for offloaded layers, or CPU (slow) for the rest.
   - **MoE routing** (for MoE models): A gating network selects which expert(s) to activate. With `--cpu-moe`, expert weights stay on CPU even for GPU layers — only attention runs on GPU.
   - **Feed-forward/Expert**: The selected expert processes the hidden state. With `--cpu-moe`, this is memory-bandwidth-bound (DDR5-5600 speed).
3. **KV cache population** — As each token is processed, its Key and Value vectors are stored in the KV cache for future attention. Cache type (Q4_0, Q5_0, Q8_0) determines precision and VRAM cost.
4. **Batch processing** — Tokens are processed in micro-batches (`n_ubatch`). Larger batches = fewer GPU kernel launches = faster prefill. This is why bumping ubatch from 512 to 4096 gave 4.6x prefill speedup on Coder-Next.
5. **Progress event** — The server emits an SSE progress event with prompt token count so the client can display "Prefill: 16.7K / 262K tokens".

**Performance**: Prefill speed ranges from 63 tok/s (MiniMax 230B, mostly CPU) to 1,067 tok/s (GLM 30B, all attention on GPU with ubatch=2048).

#### Stage 6: Token Generation (Autoregressive Decoding)

After prefill, the model generates one token at a time:

1. **Forward pass** — The last token's hidden state passes through all layers, producing logits (probability scores) for every token in the vocabulary (~150K tokens).
2. **Sampling** — Parameters control token selection:
   - **Temperature**: Randomness (0.0 = greedy, 1.0 = creative)
   - **Top-p / Top-k**: Filters unlikely tokens
   - **Repeat penalty**: Penalizes tokens that appeared recently (window of `repeat_last_n` tokens). Lower values (1.05) help code generation; higher values (1.15) reduce repetition in chat.
   - **Logit bias**: Hard bans on specific tokens (e.g., banning `<tool_call>` token IDs to prevent native format interference).
3. **KV cache append** — The new token's K/V vectors are added to the cache. The next token can attend to all previous tokens without recomputing them — this is why generation is fast (only 1 token processed per step vs all tokens during prefill).
4. **Stop detection** — Generation ends when the model emits an end-of-sequence token (`<|im_end|>` for ChatML) or hits `max_tokens`.

**Performance**: Generation speed is typically 9-25 tok/s depending on model size and GPU offload. This is bottlenecked by memory bandwidth (reading model weights for each token).

#### Stage 7: Streaming to Client (SSE)

Each generated token is immediately sent to the client:

1. **Thinking tag stripping** — For models that produce `<think>...</think>` reasoning blocks, the `ThinkingStripper` buffers tokens until `</think>` is seen, then emits only the content after it. The thinking is never shown to the user.
2. **SSE formatting** — Each token is wrapped in an OpenAI-compatible chunk: `data: {"choices":[{"delta":{"content":"token"}}]}\n\n`
3. **Client display** — The client prints each token immediately (`flush=True`), giving the appearance of real-time typing.
4. **Final chunk** — When generation ends, a chunk with `finish_reason` ("stop" or "length") is sent, followed by `data: [DONE]`.

#### Stage 8: Tool Extraction and Execution (Client)

After the full response is received:

1. **Pre-processing** — Strip `<tool_call>` artifacts, `<REACT>` blocks, agentic markers (SCRATCHPAD, PLAN, CONFIDENCE). Normalize git-style SEARCH/REPLACE to standard format.
2. **Regex extraction** — A regex matches `<{1,3}(TAG_NAME)>{1,3}` patterns (accepts 1-3 brackets to tolerate model variations). Content between tags is captured.
3. **Dispatch** — Each extracted tag maps to a handler: `REMOTE_EXEC` → shell execution, `WRITE_FILE` → file creation, `EDIT_FILE` → search/replace, etc.
4. **Permission checking** — Before execution:
   - Deny rules checked first (unconditional blocks: `rm -rf /`, fork bombs)
   - Dangerous commands detected (prompts even in yolo mode: `sudo`, `rm -rf`, `chmod 777`)
   - Protected paths checked (always prompts: `.git/`, `.ssh/`, `.env`)
   - Permission mode applied (`default` → prompt, `acceptEdits` → auto file ops, `yolo` → auto all)
5. **Execution** — Tool runs with timeout, output captured. File modifications create checkpoints for `/undo`.
6. **Output aggregation** — Results from all tools are combined (40KB cap per turn to prevent context overflow).

#### Stage 9: Agent Loop (Client)

If tools were executed, the cycle repeats:

1. **Tool output appended** — Added as a user message: `"Tool output:\n{results}"`
2. **Budget incremented** — The agentic context tracks iteration count.
3. **Loop detection** — Response hash compared to recent responses. If 3 identical responses detected, the loop is broken.
4. **Back to Stage 1** — `get_completion()` is called again with the updated history.

The loop continues until:
- The model produces a response with no tool markers (task complete)
- Budget is exhausted (forced synthesis)
- Turn cap reached (50 turns)
- Loop detected (3 identical responses)
- User interrupts (Ctrl+C)

#### Stage 10: Session Persistence

After each agent response:
- **History saved** — Full conversation written to `~/.qwen_sessions/<name>.json`
- **Pruning** — If history exceeds 100 messages, first 10 + last 90 are kept
- **Stats displayed** — TTFT, total duration, token throughput shown to user

### 3.2 Performance Bottlenecks at Each Stage

| Stage | Bottleneck | What helps |
|-------|-----------|------------|
| Prefill | Memory bandwidth (reading weights) | Higher `n_ubatch`, more GPU layers, `--cpu-moe` |
| Generation | Memory bandwidth (1 token at a time) | More GPU layers, faster RAM (DDR5-5600) |
| Model loading | Disk I/O + VRAM allocation | `--mmap` (already enabled), SSD storage |
| RAG retrieval | Embedding + vector search | Timeout (2s), smaller DB, faster CPU |
| Tool execution | Shell command runtime | Not tunable (depends on the command) |
| Context management | Compaction model call | Happens infrequently, acceptable |

### 3.3 The Agent Loop

The orchestrator (`qwen_client/orchestrator.py`) runs this cycle:

```
get_completion() → process tools → append output → get_completion() → ...
```

Safety mechanisms prevent infinite loops:
- **Budget system**: Per-query iteration limits based on query classification
- **Turn cap**: Absolute 50-turn limit per task
- **Response-level loop detection**: Breaks after 3 identical responses
- **Write-loop detection**: Blocks after 3 writes to the same file
- **Stall detection**: Nudges agent if it summarizes instead of acting

### 3.4 Context Management

Long conversations are managed through three tiers:

| Threshold | Action | Cost |
|-----------|--------|------|
| 120K chars | Model-generated conversation summary | 1 LLM call |
| 150K chars | Hard trim (drop oldest 25%) | None (data loss) |

Old tool outputs are compressed at send time (`_compress_history`) without modifying the in-memory history, preserving KV-cache prefix stability for llama-server models.

The `/compact` command triggers model-generated compaction manually.

### 3.5 The RAG Memory System

The server runs a ChromaDB vector database with SentenceTransformer embeddings (`all-MiniLM-L6-v2`). Agents can save facts and the system auto-retrieves relevant context for each query.

- **Save**: `<<<SAVE_MEMORY>>>` marker or `/ingest` command (content-hash dedup prevents duplicates)
- **Retrieve**: Automatic — top-K similar documents injected into system prompt
- **Storage**: `qwen_memory_db/` directory (SQLite + HNSW index)
- **Auth**: All memory API calls use `X-Admin-Key` when `ADMIN_API_KEY` is configured

For details on the database cleanup (842K to 85K documents), AST-aware code chunking, and the agentic query layer (classifier, budget, scratchpad, planner, confidence gate), see [RAG_UPDATES.md](RAG_UPDATES.md).

## Part 4: Adding New Models

### 4.1 Choose a Model

Find GGUF models on HuggingFace. Key factors:
- **Active parameters**: How much compute per token (e.g., 3B active in a 30B MoE)
- **Total size**: How much RAM/VRAM needed
- **Quantization**: Q4_K_M (good balance), Q8_0 (higher quality), IQ1_M (extreme compression)
- **Context window**: Training context size (n_ctx_train)
- **Architecture**: Check if llama-cpp-python supports it (if not, use llama_server backend)

### 4.2 Add the Model Config

In `server.py`, add a new model config:

```python
# New model: Example-70B Q4_K_M
# Download size: ~40 GB. MoE with 7B active params.
# Test ngl values to find optimal GPU layer count.
_EXAMPLE_70B = _create_model_config(
    'MODEL_PATH_EXAMPLE_70B',
    '/path/to/Example-70B-Q4_K_M.gguf',
    n_gpu_layers=10,     # Start here, increase until ~1 GB VRAM free
    n_ctx=32768,         # Start conservative, increase if VRAM allows
    n_batch=2048,
    # backend='llama_server',  # Uncomment if arch not in llama-cpp-python
    # server_extra_args=['--jinja', '--reasoning-format', 'none'],
    type_k=8, type_v=8,  # Q8_0 cache, or use 2 for Q4_0 if VRAM-tight
    # cpu_moe=True,      # For MoE models on llama_server: huge VRAM savings
)
```

### 4.3 Add the Agent

```python
AGENTS = {
    # ... existing agents ...
    'example': _create_agent_config(
        'Implementer — Example-70B Q4_K_M (7B/70B MoE, 32K ctx)',
        _IMPLEMENTER_SYSTEM_PROMPT,   # Or _ARCHITECT_SYSTEM_PROMPT for design-only
        _EXAMPLE_70B,
        executor=True
    ),
}
```

### 4.4 Add the Theme (Client)

In `qwen_client/config.py`, add to `THEME_STYLES`:

```python
"example": {"color": COLORS["CYAN"], "icon": "\U0001f4a1", "prompt": "Example"},
```

And in `qwen_client/models.py`, add to the fallback defaults.

### 4.5 Tuning VRAM

After adding the model, iterate on `n_gpu_layers`:

1. Set a low value, restart server, load the model
2. Check `nvidia-smi` — note free VRAM
3. Calculate VRAM per layer: `(total_used - baseline) / n_gpu_layers`
4. Increase layers until ~1 GB remains free
5. If VRAM is tight, switch KV cache to Q4_0: `type_k=2, type_v=2`
6. If still tight, reduce `n_ctx`

### 4.6 MoE Models: The `--cpu-moe` Advantage

For MoE (Mixture of Experts) models on the `llama_server` backend, `cpu_moe=True` is transformative. MoE models have two weight categories per layer:

- **Attention weights**: Small (~20-100 MiB/layer), needed for every token
- **Expert weights**: Large (~1,500-1,700 MiB/layer), sparsely activated

Without `--cpu-moe`, both go to GPU together, severely limiting ngl. With `--cpu-moe`, expert weights stay on CPU and only attention goes to GPU. This enables near-max GPU offload:

| Example | Without --cpu-moe | With --cpu-moe |
|---------|------------------|----------------|
| Coder-Next 80B | ngl=8, ~1.2 GB free | ngl=48, ~8 GB free |
| Nemotron 30B | ngl=28, ~2 GB free | ngl=52, 1M context, ~6.5 GB free |
| MiniMax 230B | ngl=6, ~6 GB free | ngl=62, 98K context, ~4.9 GB free |

After enabling `--cpu-moe`, use the freed VRAM for more context (`n_ctx`) rather than leaving it idle. Mamba-hybrid models like Nemotron are especially efficient because most layers don't need KV cache at all.

### 4.7 Handling Unsupported Architectures

If `llama-cpp-python` errors on model load ("unknown architecture"), use the subprocess backend:

```python
_MY_MODEL = _create_model_config(
    'MODEL_PATH',
    '/path/to/model.gguf',
    10, 32768, 2048,
    backend='llama_server',
    server_extra_args=['--jinja', '--reasoning-format', 'none'],
)
```

The `tools/llama-server` binary must be present with its shared libraries.

### 4.8 Banning Native Tool Tokens

Some models generate native `<tool_call>` tokens that interfere with the custom marker format. Use `logit_bias` to ban them:

1. Find the token IDs using the llama-server `/tokenize` endpoint
2. Add them to the model config:

```python
logit_bias=[[TOKEN_ID, -100.0], [OTHER_TOKEN_ID, -100.0]]
```

## Part 5: Maintenance

### 5.1 Monitoring

```bash
# Server logs
journalctl -u qwen-server -f

# VRAM usage
nvidia-smi

# RAG database size
du -sh qwen_memory_db/

# Active model
curl -s http://localhost:5000/health
```

### 5.2 Common Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| "Failed to create llama_context" | VRAM OOM | Reduce `n_gpu_layers` or `n_ctx` |
| Model generates `<tool_call>` | Native tokens not banned | Add `logit_bias` for the token IDs |
| Agent loops on same file | Write-loop or response-loop | Check logs; reduce `repeat_penalty` for the model |
| Slow TTFT | Long prompt + SWA model | Use `/compact` to reduce context |
| "Memory retrieval timed out" | Large ChromaDB | Increase timeout in server.py or prune old memories |

### 5.3 Updating llama-cpp-python

Pre-built CUDA wheels may not match your CUDA toolkit version. Build from source:

```bash
source venv/bin/activate
CMAKE_ARGS="-DGGML_CUDA=on" pip install llama-cpp-python==X.Y.Z --no-binary llama-cpp-python --no-cache-dir
```

Verify CUDA loaded after install:
```bash
python3 -c "import llama_cpp, os; libs=os.listdir(os.path.join(os.path.dirname(llama_cpp.__file__),'lib')); print('CUDA' if any('cuda' in l for l in libs) else 'CPU only')"
```

After upgrading, models that previously needed `llama_server` backend may work with `llama_cpp` if architecture support was added (e.g., Qwen3.5 support added in 0.3.17).

### 5.4 Database Maintenance

```bash
# Check document count
source venv/bin/activate
python3 -c "
import chromadb
c = chromadb.PersistentClient(path='qwen_memory_db')
for col in c.list_collections():
    print(f'{col.name}: {col.count():,} documents')
"

# Reclaim disk space (run when server is stopped)
sqlite3 qwen_memory_db/chroma.sqlite3 "VACUUM;"
```

### 5.5 Backup

Back up these directories:
- `qwen_memory_db/` — RAG vector database
- `~/.qwen_sessions/` — Chat session history
- `~/.qwen_checkpoints/` — File modification undo history
- `.env` — Configuration
