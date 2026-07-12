#!/bin/bash
# Coding Model Remote Client Startup Script

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Script lives in bin/; cd to the repo root (one level up) so the relative
# paths below (venv/, myenv/, .env) resolve correctly.
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

# Detect OS and set Python executable
if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS
    echo "Detected macOS"
    if [ -d "myenv/bin" ]; then
        PYTHON_EXE="./myenv/bin/python3"
    elif [ -d "venv/bin" ]; then
        PYTHON_EXE="./venv/bin/python3"
    else
        echo "Warning: Neither myenv nor venv found"
        exit 1
    fi
else
    # Linux or other
    echo "Detected Linux"
    if [ -d "venv/bin" ]; then
        PYTHON_EXE="./venv/bin/python3"
    elif [ -d "myenv/bin" ]; then
        PYTHON_EXE="./myenv/bin/python3"
    else
        echo "Warning: venv not found"
        exit 1
    fi
fi

# Start the client
echo "Starting Coding Model Remote Client..."
$PYTHON_EXE -m coding_model_client "$@"
