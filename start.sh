#!/bin/bash
# Qwen Multi-Agent Server Startup Script

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load environment variables
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

# Set CUDA environment if available
if [ -d "/usr/local/cuda" ]; then
    export CUDA_HOME=/usr/local/cuda
    export LD_LIBRARY_PATH=${CUDA_HOME}/lib64:${LD_LIBRARY_PATH}
fi

# Activate virtual environment if it exists
if [ -d "venv" ]; then
    source venv/bin/activate
fi

# Try to set unlimited locked memory for mlock support
ulimit -l unlimited 2>/dev/null || echo "Warning: Could not set unlimited locked memory (ulimit -l)"

# Start the server
echo "Starting Qwen Multi-Agent Server (FastAPI)..."
python server.py
