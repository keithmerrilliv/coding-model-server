#!/usr/bin/env bash
# DEV-297 — stop allowing RDP (3389) and iperf (5201) from Anywhere.
#
# WHY THIS MATTERS. Both rules allow Anywhere on IPv4 *and* IPv6, and both
# ports have a live listener. On IPv4 the router's NAT keeps them effectively
# LAN-only. IPv6 HAS NO NAT: this host holds global addresses in a Comcast
# /64, so "Anywhere (v6)" permits inbound from the public internet, subject
# only to whether the gateway happens to firewall inbound IPv6. This is the
# box holding the model weights, the admin API key, and the Jira/Anthropic/
# Gemini credentials.
#
# WHAT THIS DOES. Replaces both Anywhere rules with LAN-scoped ones, matching
# how samba, the coding-model API and the dashboard are already scoped.
#
#   Run as:  sudo bash scripts/fix_ufw_exposure.sh          # apply
#            bash scripts/fix_ufw_exposure.sh --dry-run     # show, change nothing
set -uo pipefail

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

# Tailscale's CGNAT range. ON by default, deliberately: it keeps remote RDP
# working over an authenticated overlay instead of the public internet, which
# is strictly safer than today and avoids discovering the change by being
# locked out. Set to 0 if you want LAN-only and nothing else.
ALLOW_TAILSCALE=1

LAN_RANGES=(192.168.1.0/24 10.0.0.0/24)
[[ "$ALLOW_TAILSCALE" == "1" ]] && LAN_RANGES+=(100.64.0.0/10)
PORTS=(3389 5201)

run() {
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "    WOULD RUN: $*"
    else
        "$@"
    fi
}

if [[ "$DRY_RUN" == "0" && "$(id -u)" != "0" ]]; then
    echo "This needs root. Re-run with: sudo bash $0" >&2
    exit 1
fi

# ufw status needs root. Without it the command returns nothing, and a check
# that cannot look must say so rather than print an empty list that reads as
# "all clear" — the exact failure mode DEV-701 was filed for.
CAN_READ_UFW=0
if ufw status >/dev/null 2>&1; then CAN_READ_UFW=1; fi

show_status() {
    if [[ "$CAN_READ_UFW" == "1" ]]; then
        ufw status numbered 2>/dev/null | sed 's/^/    /'
    else
        echo "    (cannot read ufw without root — re-run under sudo to see the rules)"
    fi
}

echo "==> before"
show_status

echo
echo "==> live listeners on the affected ports"
ss -tlnH 2>/dev/null | awk '{print $4}' | grep -E ':(3389|5201)$' | sed 's/^/    /' \
    || echo "    (none — the rules are stale and could simply be deleted)"

echo
echo "==> removing the Anywhere rules"
for port in "${PORTS[@]}"; do
    # One `ufw allow <port>/tcp` covers v4 and v6, but the list may hold more
    # than one variant. Delete until ufw says there is nothing left to delete,
    # bounded so a surprise cannot spin.
    for _ in 1 2 3 4 5; do
        out="$(run ufw delete allow "${port}/tcp" 2>&1)"
        echo "    ${port}/tcp: ${out%%$'\n'*}"
        [[ "$DRY_RUN" == "1" ]] && break
        [[ "$out" == *"Could not delete"* || "$out" == *"not found"* ]] && break
    done
done

echo
echo "==> re-adding, scoped"
for port in "${PORTS[@]}"; do
    case "$port" in
        3389) label="RDP" ;;
        5201) label="iperf" ;;
        *)    label="port ${port}" ;;
    esac
    for net in "${LAN_RANGES[@]}"; do
        run ufw allow from "$net" to any port "$port" proto tcp \
            comment "${label} LAN-only (DEV-297)"
    done
done

echo
echo "==> stale rules from the retired 192.168.50.0/24 subnet"
stale=""
[[ "$CAN_READ_UFW" == "1" ]] && \
    stale="$(ufw status numbered 2>/dev/null | grep -n '192\.168\.50\.' || true)"
if [[ "$CAN_READ_UFW" != "1" ]]; then
    echo "    UNKNOWN — cannot read ufw without root. Not the same as none."
elif [[ -z "$stale" ]]; then
    echo "    none"
else
    echo "$stale" | sed 's/^/    /'
    echo
    echo "    NOT deleted automatically: ufw renumbers after every delete, so"
    echo "    a scripted sweep by number is a footgun. Delete them by hand,"
    echo "    HIGHEST NUMBER FIRST, re-reading the list between each:"
    echo "        sudo ufw status numbered"
    echo "        sudo ufw delete <n>"
fi

echo
echo "==> after"
show_status

cat <<'NOTES'

==> still yours to confirm

1. VERIFY FROM OUTSIDE — this cannot be tested from inside the network.
   From a phone on CELLULAR (not wifi). Connected = still exposed; refused or
   timed out = correct:

       nc -6 -vz -w5 2601:646:8685:4f70:9141:3686:18d1:8d9 3389

   NOTE: the address in the Jira ticket (::9021) is STALE. This host's current
   global v6 addresses are:
       2601:646:8685:4f70::a5cb
       2601:646:8685:4f70:9141:3686:18d1:8d9
       2601:646:8685:4f70:2094:fcf4:f8af:5b21
   Test whichever is stable; the ::a5cb /128 is the likeliest to persist.

2. CONFIRM RDP STILL WORKS from the LAN, and over Tailscale if you use it
   remotely (this script allows 100.64.0.0/10 by default — set
   ALLOW_TAILSCALE=0 at the top for LAN-only).

3. CLOUDXR, left alone on purpose: 48010/tcp and 47995:48012/udp are still
   Anywhere (v6), and 48010 has a live listener. They look deliberate for
   AVP streaming. If that is no longer in use, they are the next thing to
   scope or drop.

4. If RDP is not actually in use, deleting the rule and stopping the listener
   beats scoping it.
NOTES
