#!/usr/bin/env bash
# DEV-705 — reclaim leaked coding-model-runner tart VMs on the Mac.
#
# WHY. mac_runner/vm.py destroys its VM from a `finally`, but both `tart stop`
# and `tart delete` are wrapped in try/except that only WARN. A teardown that
# fails leaks the VM silently, and nothing bounds how many accumulate — so the
# next run dies on tart's own limit:
#
#   [vm] tart run exited 1 before the guest came up
#   The number of VMs exceeds the system limit (other running VMs: cmr-…, cmr-…)
#
# That arrives at a DIFFERENT spec's test dispatch, after its context assembly
# and its file write are already spent, and reads like the attempt's fault.
#
# The runner is the only writer of the `cmr-` prefix, so anything still carrying
# it once no run is in flight is by definition a leak.
#
#   RUN ON THE MAC (macbook-pro), not on zooshly:
#     bash scripts/reclaim_tart_vms.sh            # show what is there
#     bash scripts/reclaim_tart_vms.sh --delete   # reclaim the leaked ones
set -uo pipefail

PREFIX="cmr-"
DELETE=0
[[ "${1:-}" == "--delete" ]] && DELETE=1

if ! command -v tart >/dev/null 2>&1; then
    echo "tart not found — are you on the Mac runner host?" >&2
    exit 1
fi

echo "==> all VMs tart knows about"
tart list 2>/dev/null | sed 's/^/    /' || { echo "    (tart list failed)"; exit 1; }

# Column 2 is the name in `tart list` output; skip the header row.
mapfile -t LEAKED < <(tart list 2>/dev/null | awk -v p="$PREFIX" 'NR>1 && $2 ~ "^"p {print $2}')

echo
if [[ "${#LEAKED[@]}" -eq 0 ]]; then
    echo "==> no ${PREFIX}* VMs present — nothing to reclaim"
    exit 0
fi

echo "==> runner-owned VMs (${#LEAKED[@]})"
printf '    %s\n' "${LEAKED[@]}"

echo
echo "!!  Only reclaim these when NO run is in flight. A VM belonging to a"
echo "!!  live dispatch will take that run down with it."
echo

if [[ "$DELETE" != "1" ]]; then
    echo "==> dry run. Re-run with --delete to reclaim them."
    exit 0
fi

for vm in "${LEAKED[@]}"; do
    echo "--> $vm"
    tart stop   "$vm" 2>&1 | sed 's/^/      stop:   /' || true
    tart delete "$vm" 2>&1 | sed 's/^/      delete: /' || true
done

echo
echo "==> after"
tart list 2>/dev/null | sed 's/^/    /'
