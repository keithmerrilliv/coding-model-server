#!/bin/bash
# Dev launcher for the qwen mac-runner. For production use the LaunchAgent
# template at mac_runner/com.qwen.runner.plist.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ENV_FILE="${QWEN_RUNNER_ENV_FILE:-$HOME/.config/qwen-runner/.env}"
if [ -f "$ENV_FILE" ]; then
    set -a
    . "$ENV_FILE"
    set +a
else
    echo "Warning: $ENV_FILE not found — runner will refuse to start without QWEN_RUNNER_API_KEY"
fi

export PYTHONUNBUFFERED=1

if [ -d "venv" ]; then
    # shellcheck disable=SC1091
    source venv/bin/activate
fi

echo "Starting qwen mac-runner..."
exec python -m mac_runner.server
