# qwen-server

Qwen Multi-Agent Server with Remote Client

## Server Setup

### Installation

1. Clone the repository
2. Run the setup script:
   ```bash
   ./setup.sh
   ```
3. Configure environment variables in `.env` (copy from `.env.example`)

### Starting the Server

```bash
./start.sh
```

The server will start on port 5000 by default.

## Apple Documentation Scraper

The system includes a comprehensive Apple documentation scraping feature that can:

- Scrape documentation for any Apple framework (Metal, UIKit, AVFoundation, CoreData, Photos, etc.)
- Integrate with Cupertino and Apple Deep Docs MCP servers for comprehensive documentation access
- Ingest scraped data into the RAG database for enhanced AI knowledge
- Support environment-based configuration for server connections
- Generate structured output for each framework

For detailed information about the scraping system, see [scraping/README.md](scraping/README.md).

## High-Fidelity Documentation Ingestion

The system supports deep ingestion of technical documentation into the RAG database, moving beyond simple search snippets to full-text technical memory.

### Local Xcode Documentation Sync
Sync your local Apple documentation (Metal, RealityKit, etc.) from Xcode installations directly to the RAG server:
```bash
# On the macOS machine:
./venv/bin/python scraping/ingest_xcode_docs.py
```

### Targeted Web Scraping
Ingest the latest Metal 4 and RealityKit 4 documentation from the web:
```bash
# On the macOS machine:
pip install beautifulsoup4
./venv/bin/python scraping/scrape_apple_deep.py
```

### Deep Ingestion Tool (Agent-Accessible)
Agents can now fetch and ingest the full content of any technical URL:
- `<<<DEEP_INGEST>>>url<<<DEEP_INGEST>>>` - Fetches the URL, strips HTML noise, and stores the full text in memory.

## Client Usage

### Starting the Client

The client automatically detects your operating system and uses the appropriate Python environment:

**Cross-platform (recommended):**
```bash
./start-client.sh
```

**With specific agent model:**
```bash
./start-client.sh --model implementer
```

**Direct Python (if environment already activated):**
```bash
python3 qwen_remote.py
```

### Environment Detection

- **macOS**: Uses `myenv/bin/activate`
- **Linux**: Uses `venv/bin/activate`

### Client Commands

- `/exit` or `/quit` - Exit the client
- `/model <name>` - Switch to a different agent model
- `/resume` - Resume interrupted multi-agent tasks (e.g., after a tool execution break)
- `/history` - Show command history
- `/cupertino <query>` - Search Apple documentation (macOS only)
- `/scrape <framework>` - Scrape documentation for a specific Apple framework
- `/ingest <path>` - Ingest a PDF file into memory (supports server files or local: prefix for client files)

### PDF Ingestion Feature

The system supports ingesting PDF documents for enhanced AI knowledge and RAG (Retrieval Augmented Generation):

**Using the slash command:**
- `/ingest /path/to/server/file.pdf` - Ingest a PDF file that exists on the server
- `/ingest local:/path/to/client/file.pdf` - Ingest a PDF file from your local client machine

**Using the tool in agent responses:**
- `<<<INGEST_PDF>>>/path/to/server/file.pdf` - Ingest a server-side PDF
- `<<<INGEST_PDF>>>local:/path/to/client/file.pdf` - Ingest a client-side PDF (automatically uploads to server first)

When using the `local:` prefix, the system will:
1. Read the PDF file from your client machine
2. Securely upload it to the server
3. Ingest the uploaded file into the memory service
4. Make the content available for RAG queries

### Security Settings

Configure via environment variables:

- `ALLOW_SHELL_MODE=true` - Enable shell features (pipes, redirects, etc.)
- `COMMAND_WHITELIST=ls,pwd,cat` - Comma-separated list of allowed commands

### Performance Optimization Settings

You can optimize inference speed by adjusting these environment variables in your `.env` file:

- `MODEL_N_THREADS=0` - Number of CPU threads to use (0 = auto-detect CPU cores)
- `MODEL_N_BATCH=512` - Batch size for processing (higher = faster but more memory)
- `MODEL_FLASH_ATTENTION=true` - Enable flash attention for faster processing (if supported)
- `MODEL_USE_MMAP=true` - Use memory mapping for faster model loading
- `MODEL_USE_MLOCK=true` - Lock model in RAM to prevent swapping to disk
- `MODEL_N_CTX_BATCH=2048` - Number of tokens to process at once (higher = faster but more memory)

### Advanced Model Configuration

You can configure advanced parameters in `server.py` to control memory usage and context length.

**KV Cache Offloading (RAM vs VRAM):**
By default, the Key-Value (KV) cache is offloaded to VRAM for speed. To save VRAM (at the cost of speed), you can move it to system RAM:

```python
            'model_config': {
                # ...
                'offload_kqv': False,   # False = Store KV cache in RAM, True = VRAM (default)
            }
```

**KV Cache Quantization:**
By default, the KV cache now uses the model's standard precision (usually F16). If you are constrained by RAM or VRAM, you can enable 8-bit quantization:

```python
            'model_config': {
                # ...
                'type_k': 8,  # 8 = GGML_TYPE_Q8_0
                'type_v': 8,  # 8 = GGML_TYPE_Q8_0
            }
```

**Extending Context Length (YaRN):**
You can extend the model's context length beyond its training limit using YaRN (Yet another RoPE extensioN).

```python
        'implementer': {
            'description': 'Code implementation agent',
            # ...
            'model_config': {
                'path': '/path/to/model.gguf',
                'n_ctx': 65536,         # Desired context length
                'offload_kqv': True,    # Set to False if you run out of VRAM

                # YaRN Configuration
                'rope_scaling_type': 2, # 2 = YaRN
                'yarn_ext_factor': -1.0,# -1 = Auto-detect based on n_ctx / train_ctx
                'yarn_attn_factor': 1.0,
                'yarn_beta_fast': 32.0,
                'yarn_beta_slow': 1.0,
                'yarn_orig_ctx': 32768  # Original training context of the model
            }
        },
```

### Remote Command Execution

The agent can request to execute commands on your local machine. You will be prompted to approve each command before execution.

**Async commands** (background jobs):
- Agent uses `<<<REMOTE_EXEC_ASYNC>>>command<<<REMOTE_EXEC_ASYNC>>>`
- Returns a job ID for tracking
- Check status: `<<<REMOTE_CHECK_STATUS>>>job_id<<<REMOTE_CHECK_STATUS>>>`
- Get output: `<<<REMOTE_GET_OUTPUT>>>job_id<<<REMOTE_GET_OUTPUT>>>`

**Sync commands** (immediate):
- Agent uses `<<<REMOTE_EXEC>>>command<<<REMOTE_EXEC>>>`
- Runs with 30-second timeout

**File Reading** (safe, fast):
- Agent uses `<<<READ_FILE>>>path<<<READ_FILE>>>`
- Reads file content without using shell commands (Architect safe)