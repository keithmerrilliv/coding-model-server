#!/usr/bin/env bash
# DEV-705 — bring the Mac runner up to date and reclaim leaked tart VMs.
#
#   RUN ON THE MAC (macbook-pro), from the runner's checkout:
#     bash scripts/mac_update_runner.sh            # show what it would do
#     bash scripts/mac_update_runner.sh --execute  # pull, reclaim, restart
#
# Three steps, in this order, because each depends on the one before it:
#   1. pull   — the runner's VM leak guard ships in mac_runner/vm.py
#   2. reclaim — leaked cmr-* VMs block every xcodebuild_test dispatch, and
#                until step 1 is running nothing sweeps them automatically
#   3. restart — launchd reloads the runner so the new code is live
#
# Safe to re-run. Dry run by default: it prints every command and changes
# nothing until --execute.
set -uo pipefail

EXECUTE=0
[[ "${1:-}" == "--execute" ]] && EXECUTE=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"
LABEL="com.codingmodel.runner"

run() {
    if [[ "$EXECUTE" == "1" ]]; then
        echo "--> $*"
        "$@"
    else
        echo "    would run: $*"
    fi
}

echo "==> repo: ${REPO}"
cd "${REPO}" || exit 1

echo
echo "==> 1. pull"
if [[ -n "$(git status --porcelain)" ]]; then
    echo "!!  working tree is DIRTY — commit or stash before pulling:"
    git status --short | sed 's/^/      /'
    [[ "$EXECUTE" == "1" ]] && exit 1
fi
run git pull --ff-only

echo
echo "==> 2. reclaim leaked VMs"
echo "!!  Only reclaim when NO run is in flight — a VM belonging to a live"
echo "!!  dispatch will take that run down with it."
if command -v tart >/dev/null 2>&1; then
    tart list 2>/dev/null | sed 's/^/    /'
    run bash "${SCRIPT_DIR}/reclaim_tart_vms.sh" --delete
else
    echo "    tart not found — skipping (is this the runner host?)"
fi

echo
echo "==> 3. restart the runner"
run launchctl kickstart -k "gui/$(id -u)/${LABEL}"

echo
if [[ "$EXECUTE" != "1" ]]; then
    echo "==> dry run. Re-run with --execute to apply."
    exit 0
fi

echo "==> health"
sleep 2
curl -s -m 5 http://127.0.0.1:5050/health || echo "    (no answer on :5050 yet — give launchd a moment)"
echo
