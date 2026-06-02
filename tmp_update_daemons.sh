#!/usr/bin/env bash
#
# tmp_update_daemons.sh — TEMPORARY: sync qwen systemd units from the repo,
# reload, and restart the services onto the refactored code.
#
# Run with:  sudo bash tmp_update_daemons.sh
# Delete after use.  Safe to re-run (idempotent).
#
# What it does / does NOT do:
#   - Does NOT run pip. The venv is an editable install (.pth -> src/), so the
#     refactored modules are already importable; a restart is all that's needed.
#     (A `sudo pip install` would write root-owned files into your user venv.)
#   - Syncs the three app units (server, orchestrator, dashboard) from
#     systemd/ into /etc/systemd/system/, backing up the originals first.
#   - SKIPS qwen-monitor by default: the repo version switches it from root to
#     User=keith-merrill, which breaks RAPL power reads until you grant an ACL
#     on /sys/class/powercap/.../energy_uj. Pass SYNC_MONITOR=1 to include it.
#
set -euo pipefail

REPO="/home/keith-merrill/Dev/qwen-server"
UNIT_DIR="/etc/systemd/system"
TS="$(date +%Y%m%d_%H%M%S)"
BACKUP="/root/qwen-unit-backup-${TS}"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root:  sudo bash $0" >&2
  exit 1
fi

# App services relevant to the server/client/autonomous verification.
SERVICES=(qwen-server qwen-orchestrator qwen-dashboard)
if [[ "${SYNC_MONITOR:-0}" == "1" ]]; then
  echo "SYNC_MONITOR=1 -> including qwen-monitor (ensure RAPL ACL is set first)."
  SERVICES+=(qwen-monitor)
fi

echo "==> Backing up current unit files to ${BACKUP}"
mkdir -p "${BACKUP}"
for s in "${SERVICES[@]}"; do
  if [[ -f "${UNIT_DIR}/${s}.service" ]]; then
    cp -a "${UNIT_DIR}/${s}.service" "${BACKUP}/"
  fi
done

echo "==> Syncing unit files from repo (only when changed)"
changed=0
for s in "${SERVICES[@]}"; do
  src="${REPO}/systemd/${s}.service"
  dst="${UNIT_DIR}/${s}.service"
  if [[ ! -f "${src}" ]]; then
    echo "    ! repo unit missing: ${src} (skipping)"
    continue
  fi
  if cmp -s "${src}" "${dst}" 2>/dev/null; then
    echo "    = ${s}: already up to date"
  else
    install -m 0644 "${src}" "${dst}"
    echo "    + ${s}: updated"
    changed=1
  fi
done

echo "==> systemctl daemon-reload"
systemctl daemon-reload

# Restart in dependency order: server first (owns the GPU + inference), then
# the orchestrator (talks to the server), then the dashboard (polls it).
echo "==> Restarting services"
for s in qwen-server qwen-orchestrator qwen-dashboard; do
  echo "    restarting ${s} ..."
  systemctl restart "${s}"
done

echo "==> Waiting for qwen-server to answer /health (up to 120s)"
ok=0
for i in $(seq 1 60); do
  # /health needs no auth; bind host comes from the unit/.env (default :5000).
  if curl -fsS --max-time 3 "http://127.0.0.1:5000/health" >/dev/null 2>&1 \
     || curl -fsS --max-time 3 "http://192.168.50.101:5000/health" >/dev/null 2>&1; then
    ok=1; break
  fi
  sleep 2
done
if [[ "${ok}" == "1" ]]; then
  echo "    /health OK"
else
  echo "    !! /health did not respond — check: journalctl -u qwen-server -n 50"
fi

echo "==> Status summary"
for s in qwen-server qwen-orchestrator qwen-dashboard; do
  printf "    %-22s %s\n" "${s}" "$(systemctl is-active "${s}")"
done

echo
echo "Backups in ${BACKUP} (restore with: cp ${BACKUP}/<unit> ${UNIT_DIR}/ && systemctl daemon-reload)"
echo "Done. You can delete this script: rm ${REPO}/tmp_update_daemons.sh"
