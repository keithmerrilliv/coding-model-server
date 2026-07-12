#!/bin/bash
# Dev launcher for the coding-model mac-runner. For production use the LaunchAgent
# template at mac_runner/com.codingmodel.runner.plist.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Script lives in bin/; cd to the repo root (one level up) so venv/ resolves.
cd "$SCRIPT_DIR/.."

ENV_FILE="${CODING_MODEL_RUNNER_ENV_FILE:-$HOME/.config/coding-model-runner/.env}"
if [ -f "$ENV_FILE" ]; then
    set -a
    . "$ENV_FILE"
    set +a
else
    echo "Warning: $ENV_FILE not found — runner will refuse to start without CODING_MODEL_RUNNER_API_KEY"
fi

export PYTHONUNBUFFERED=1

if [ -d "venv" ]; then
    # shellcheck disable=SC1091
    source venv/bin/activate
fi

echo "Starting coding-model mac-runner..."
exec python -m mac_runner.server
