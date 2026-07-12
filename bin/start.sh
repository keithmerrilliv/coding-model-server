#!/bin/bash
# Coding Model Multi-Agent Server Startup Script

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Script lives in bin/; cd to the repo root (one level up) so the relative
# paths below (venv/, .env, python -m ...) resolve correctly.
cd "$SCRIPT_DIR/.."

# Load environment variables. Prefer ~/.config/coding-model-server/.env (keeps secrets
# out of the repo); fall back to a repo-local .env for development only.
ENV_FILE="${CODING_MODEL_ENV_FILE:-$HOME/.config/coding-model-server/.env}"
if [ -f "$ENV_FILE" ]; then
    set -a
    . "$ENV_FILE"
    set +a
elif [ -f .env ]; then
    echo "Warning: loading .env from repo — move secrets to $HOME/.config/coding-model-server/.env"
    set -a
    . .env
    set +a
fi

# Ensure Python output is unbuffered
export PYTHONUNBUFFERED=1

# Check if port is available
PORT=${PORT:-5000}
if ss -lnt "sport = :$PORT" | grep -q ":$PORT"; then
    echo "Error: Port $PORT is already in use. Cannot start server."
    exit 1
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

# Set environment variables for optimal performance
# Use physical core count (24) — hyperthreads hurt llama.cpp throughput
PHYS_CORES=24
export OMP_NUM_THREADS=$PHYS_CORES
export OPENBLAS_NUM_THREADS=$PHYS_CORES
export MKL_NUM_THREADS=$PHYS_CORES
export VECLIB_MAXIMUM_THREADS=$PHYS_CORES
export NUMEXPR_NUM_THREADS=$PHYS_CORES

# Start the server
echo "Starting Coding Model Multi-Agent Server (FastAPI)..."
python -m coding_model_server.server
