#!/usr/bin/env bash
# DEV-725 — let the resource monitor read CPU package power.
#
# READ THIS BEFORE RUNNING IT. This loosens a security mitigation.
#
# /sys/class/powercap/intel-rapl:*/energy_uj is root-only on purpose. The
# restriction is the fix for PLATYPUS (CVE-2020-8694): unprivileged RAPL
# readings are a power side channel precise enough to recover AES-NI keys and
# defeat KASLR from an ordinary user account. Making the counter readable
# hands that channel to every process running as the group below.
#
# On a single-user box you control, that is usually an acceptable trade for
# knowing what your machine costs to run. On a box where you run other
# people's code — and this box DOES run model-authored code, albeit inside a
# sandbox and a VM — it is a real consideration. Decide deliberately.
#
# This grants read to ONE GROUP, not to the world. The narrower the better.
#
#   bash scripts/enable_rapl_reading.sh            # show what would change
#   sudo bash scripts/enable_rapl_reading.sh --execute
#
# To undo: sudo rm /etc/udev/rules.d/99-rapl-readable.rules && reboot
set -uo pipefail

GROUP="${RAPL_GROUP:-$(id -gn "${SUDO_USER:-$USER}")}"
RULE=/etc/udev/rules.d/99-rapl-readable.rules
EXECUTE=0
[[ "${1:-}" == "--execute" ]] && EXECUTE=1

echo "==> current state"
for f in /sys/class/powercap/intel-rapl:*/energy_uj; do
    [[ -e "$f" ]] || continue
    printf '    %s  %s\n' "$(stat -c '%A %U:%G' "$f")" "$f"
done
echo
echo "==> would grant GROUP READ to: $GROUP"
echo "    via $RULE"
echo
echo '    SUBSYSTEM=="powercap", KERNEL=="intel-rapl:*", MODE="0440", GROUP="'"$GROUP"'"'
echo

if [[ "$EXECUTE" != "1" ]]; then
    echo "==> dry run. Re-run with sudo and --execute to apply."
    exit 0
fi

if [[ "$(id -u)" != "0" ]]; then
    echo "--execute needs root: sudo bash $0 --execute" >&2
    exit 1
fi

printf 'SUBSYSTEM=="powercap", KERNEL=="intel-rapl:*", MODE="0440", GROUP="%s"\n' \
    "$GROUP" > "$RULE"
echo "    wrote $RULE"

udevadm control --reload-rules
udevadm trigger --subsystem-match=powercap
echo "    udev rules reloaded and triggered"

# udev does not always re-apply to already-registered devices; do it directly
# so the effect is immediate rather than only after the next boot.
for f in /sys/class/powercap/intel-rapl:*/energy_uj; do
    [[ -e "$f" ]] || continue
    chgrp "$GROUP" "$f" && chmod 0440 "$f"
done

echo
echo "==> after"
for f in /sys/class/powercap/intel-rapl:*/energy_uj; do
    [[ -e "$f" ]] || continue
    printf '    %s  %s\n' "$(stat -c '%A %U:%G' "$f")" "$f"
done

echo
echo "==> restart the monitor so it picks the sensor up"
echo "    systemctl restart coding-model-monitor"
echo
echo "==> then confirm a real number is landing (cpu_watts must be non-empty"
echo "    and non-zero; a 14900KF never idles at 0 W):"
echo "    tail -3 $(dirname "$0")/../var/server_stats.csv"
