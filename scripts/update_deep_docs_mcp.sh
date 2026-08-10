#!/usr/bin/env bash
# Update the vendored Apple Deep Docs MCP, but only keep the update if it still works.
#
# WHY THIS EXISTS
#
# tools/appledeepdoc-mcp is a checkout of a THIRD-PARTY repo
# (github.com/Ahrentlov/appledeepdoc-mcp). It was pinned at ee6107e from
# 2025-11-05 — nine months stale — because nothing ever updated it. That is the
# same "nobody runs it, so nobody notices it rotted" pattern that left two Apple
# doc integrations dead (DEV-471).
#
# NOTE it is deliberately NOT a data refresh. The MCP caches nothing that goes
# stale: swift_evolution.py fetches live from download.swift.org with a 1-hour
# cache, and the only bundled data file in the tree is README.md. What ages here
# is third-party CODE.
#
# WHY THE SMOKE TEST IS THE POINT
#
# Auto-pulling someone else's code into a path the agents depend on is only
# acceptable if a broken pull cannot survive. So: pull, exercise the service,
# and roll back to the previous commit if it stops answering. An MCP that starts
# but returns nothing is the exact failure mode this repo keeps hitting, so
# "it started" is not the test — "it returned proposals" is.
set -uo pipefail

REPO="${REPO:-/home/youruser/Dev/coding-model-server}"
MCP="$REPO/tools/appledeepdoc-mcp"
SERVER="${CODING_MODEL_SERVER_IP:-127.0.0.1}"
PORT="${CODING_MODEL_SERVER_PORT:-5000}"
STATE="${MCP_UPDATE_STATE_DIR:-$REPO/var/mcp_update}"
DRY_RUN="${DRY_RUN:-0}"

mkdir -p "$STATE"
exec >>"$STATE/update.log" 2>&1
echo "=== MCP update started $(date '+%F %T') ==="

[ -d "$MCP/.git" ] || { echo "!! $MCP is not a git checkout; aborting"; exit 2; }

KEY="${ADMIN_API_KEY:-$(grep -m1 '^ADMIN_API_KEY=' "$REPO/.env" 2>/dev/null | cut -d= -f2-)}"
[ -n "$KEY" ] || { echo "!! no ADMIN_API_KEY; aborting"; exit 2; }

OLD="$(git -C "$MCP" rev-parse HEAD)"
git -C "$MCP" fetch --quiet origin || { echo "!! fetch failed"; exit 3; }
UPSTREAM="$(git -C "$MCP" rev-parse '@{u}' 2>/dev/null || echo "$OLD")"

if [ "$OLD" = "$UPSTREAM" ]; then
  echo "  already current at ${OLD:0:9} — nothing to do"
  echo "=== done $(date '+%F %T') ==="
  exit 0
fi
echo "  ${OLD:0:9} -> ${UPSTREAM:0:9}"
if [ "$DRY_RUN" = "1" ]; then
  echo "  DRY_RUN: stopping before pull"; exit 0
fi

# --ff-only: never merge third-party history. A diverging upstream means someone
# committed locally, and that needs a human, not an unattended merge.
if ! git -C "$MCP" pull --quiet --ff-only; then
  echo "!! pull is not a fast-forward — local commits present? leaving at ${OLD:0:9}"
  exit 4
fi

# Reinstall deps only if the manifest moved; the MCP carries its own venv.
if git -C "$MCP" diff --name-only "$OLD" HEAD | grep -qE 'pyproject.toml|requirements.*\.txt'; then
  echo "  dependency manifest changed — reinstalling into the MCP venv"
  "$MCP/venv/bin/pip" install --quiet -e "$MCP" || echo "  (pip install reported an error; smoke test will judge)"
fi

# The MCP is a child of the server, so it only picks up new code on restart.
echo "  restarting coding-model-server"
systemctl restart coding-model-server || { echo "!! restart failed"; }
sleep 20

smoke() {
  local body="$1" expect="$2" label="$3"
  local out
  out="$(curl -s --max-time 60 -H "X-Admin-Key: $KEY" -H 'Content-Type: application/json' \
        -d "$body" "http://$SERVER:$PORT/v1/tools/apple_deep_docs" 2>/dev/null)"
  if printf '%s' "$out" | grep -q "$expect"; then
    echo "  smoke OK: $label"; return 0
  fi
  echo "  smoke FAIL: $label — got: $(printf '%s' "$out" | head -c 160)"; return 1
}

FAILED=0
# 1. The catalogue still names the tools the agent prompt advertises.
for t in search_swift_evolution fetch_apple_documentation search_swift_repos; do
  curl -s --max-time 60 -H "X-Admin-Key: $KEY" "http://$SERVER:$PORT/v1/tools/apple_deep_docs" \
    | grep -q "$t" || { echo "  smoke FAIL: $t missing from tools/list"; FAILED=1; }
done
# 2. A tool that must return CONTENT, not just start. "it started" is not the test.
smoke '{"tool":"search_swift_evolution","arguments":{"feature":"actors"}}' 'se_number' \
      'search_swift_evolution returns proposals' || FAILED=1

if [ "$FAILED" -ne 0 ]; then
  echo "!! smoke test failed after update — rolling back to ${OLD:0:9}"
  git -C "$MCP" reset --hard --quiet "$OLD"
  systemctl restart coding-model-server
  echo "=== rolled back $(date '+%F %T') ==="
  exit 5
fi

printf '{"updated_at":"%s","from":"%s","to":"%s"}\n' \
  "$(date '+%F %T')" "$OLD" "$(git -C "$MCP" rev-parse HEAD)" > "$STATE/last_update.json"
echo "=== updated OK $(date '+%F %T') ==="
