#!/usr/bin/env bash
# DEV-721 — warm the SwiftPM clone cache the VM dispatch pushes into the guest.
#
# WHY. The guest is minted per run, so its package cache is cold every time and
# xcodebuild re-fetches the whole graph. Guest egress measures ~15x slower than
# the host's (8.5 MB clone: 2s on the host, 31s in the guest), which is enough
# to push resolution past RESOLVE_TIMEOUT and spend 300s of the test's budget.
#
# Resolution runs HERE, on the host, where the network is fast. The runner then
# rsyncs the result into the guest, which reads it from disk.
#
# The cache is only ever read by the dispatch, never written back from the
# guest, so nothing the LLM-authored test code produces reaches the host and
# DEV-422 containment is unchanged.
#
#   RUN ON THE MAC (macbook-pro):
#     bash scripts/warm_package_cache.sh <repo-path> <scheme>          # dry run
#     bash scripts/warm_package_cache.sh <repo-path> <scheme> --execute
#
# Then point the runner at it and restart:
#     CODING_MODEL_RUNNER_VM_PACKAGE_CACHE=~/Library/Caches/coding-model-runner/pkgcache
#
# Safe to re-run: xcodebuild resolves incrementally into the same directory.
set -uo pipefail

CACHE="${CODING_MODEL_RUNNER_VM_PACKAGE_CACHE:-$HOME/Library/Caches/coding-model-runner/pkgcache}"

REPO="${1:-}"
SCHEME="${2:-}"
EXECUTE=0
[[ "${3:-}" == "--execute" ]] && EXECUTE=1

if [[ -z "$REPO" || -z "$SCHEME" ]]; then
    echo "usage: bash scripts/warm_package_cache.sh <repo-path> <scheme> [--execute]" >&2
    echo "   e.g. bash scripts/warm_package_cache.sh ~/Dev/ElectricSheep ElectricSheep --execute" >&2
    exit 2
fi
if [[ ! -d "$REPO" ]]; then
    echo "!!  no such repo: $REPO" >&2
    exit 1
fi

echo "==> repo:   $REPO"
echo "==> scheme: $SCHEME"
echo "==> cache:  $CACHE"

if [[ -d "$CACHE" ]]; then
    echo "==> current size: $(du -sh "$CACHE" 2>/dev/null | cut -f1)"
else
    echo "==> cache does not exist yet"
fi

# -clonedSourcePackagesDirPath is the same flag the dispatch passes in the
# guest; resolving with it here is what makes the two halves line up.
CMD=(xcodebuild -resolvePackageDependencies
     -scheme "$SCHEME"
     -clonedSourcePackagesDirPath "$CACHE")

echo
if [[ "$EXECUTE" != "1" ]]; then
    echo "    would run, from $REPO:"
    echo "      ${CMD[*]}"
    echo
    echo "==> dry run. Re-run with --execute to apply."
    exit 0
fi

mkdir -p "$CACHE" || exit 1
echo "--> ${CMD[*]}"
START=$(date +%s)
( cd "$REPO" && "${CMD[@]}" ) 2>&1 | tail -20
RC=${PIPESTATUS[0]}
echo
echo "==> xcodebuild exited $RC in $(( $(date +%s) - START ))s"
echo "==> cache is now $(du -sh "$CACHE" 2>/dev/null | cut -f1)"
if [[ "$RC" != "0" ]]; then
    echo "!!  resolution failed — the cache may be incomplete. The dispatch"
    echo "!!  treats a partial cache as a speed-up, not a correctness claim:"
    echo "!!  anything missing is still fetched in the guest."
fi
exit "$RC"
