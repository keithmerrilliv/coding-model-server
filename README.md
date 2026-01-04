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
