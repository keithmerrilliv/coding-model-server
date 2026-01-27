#!/bin/bash
# Qwen Remote Client Startup Script

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load environment variables
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

# Detect OS and activate appropriate virtual environment
if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS
    echo "Detected macOS"
    if [ -f "myenv/bin/activate" ]; then
        echo "Using myenv/bin/activate"
        source myenv/bin/activate
    elif [ -f "venv/bin/activate" ]; then
        echo "Using venv/bin/activate"
        source venv/bin/activate
    else
        echo "Warning: Neither myenv/bin/activate nor venv/bin/activate found"
        exit 1
    fi
else
    # Linux or other
    echo "Detected Linux - using venv"
    if [ -d "venv" ]; then
        source venv/bin/activate
    else
        echo "Warning: venv not found"
        exit 1
    fi
fi

# Start the client
echo "Starting Qwen Remote Client..."
python3 qwen_remote.py "$@"
