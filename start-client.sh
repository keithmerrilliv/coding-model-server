#!/bin/bash
# Qwen Remote Client Startup Script

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load environment variables
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
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
echo "Starting Qwen Remote Client..."
$PYTHON_EXE qwen_remote.py "$@"
