#!/usr/bin/env bash
#
# redeploy.sh — sync the coding-model systemd units from this repo, reload, and restart
# the services so they pick up code or unit-file changes.
#
#   sudo bash scripts/redeploy.sh              # full: sync unit files + reload + restart (root)
#   bash scripts/redeploy.sh --restart-only    # code-only: restart the services (no sudo; polkit rule)
#
# Safe to re-run (idempotent). No pip step: the venv is an editable install
# (.pth -> src/), so code changes are already importable — a restart is enough.
# (A `sudo pip install` would write root-owned files into your user venv.)
#
# What it does:
#   - Backs up the currently-installed unit files, then copies the repo's
#     systemd/*.service into /etc/systemd/system/ (only when changed).
#   - daemon-reload, then restarts server -> orchestrator -> dashboard in
#     dependency order.
#   - Polls /health until the server answers.
#
# coding-model-monitor is skipped by default: the repo version runs as the invoking
# user (not root), which needs an ACL on /sys/class/powercap/.../energy_uj for
# RAPL power reads. Pass SYNC_MONITOR=1 once that ACL is in place.
#
set -euo pipefail

# Repo root = parent of this script's dir (no hardcoded paths).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"
UNIT_DIR="/etc/systemd/system"
TS="$(date +%Y%m%d_%H%M%S)"
BACKUP="/root/coding-model-unit-backup-${TS}"

# Mode: full redeploy (syncs unit files + daemon-reload -> needs root) vs
# restart-only (just bounce the services). Restart-only needs NO sudo thanks to
# the coding-model polkit rule (/etc/polkit-1/rules.d/49-coding-model.rules), and
# is enough for code changes since the venv is an editable install.
RESTART_ONLY=0
[[ "${1:-}" == "--restart-only" || "${1:-}" == "-r" ]] && RESTART_ONLY=1

if [[ "${RESTART_ONLY}" == "1" ]]; then
  echo "==> restart-only mode (no unit sync / daemon-reload; sudo-free via polkit)"
else
  if [[ $EUID -ne 0 ]]; then
    echo "Full redeploy syncs unit files + daemon-reload, which need root:" >&2
    echo "  sudo bash ${BASH_SOURCE[0]}" >&2
    echo "Code-only change? No sudo needed:  bash ${BASH_SOURCE[0]} --restart-only" >&2
    exit 1
  fi

  # App services to sync/restart. Add coding-model-monitor only on explicit opt-in.
  SERVICES=(coding-model-server coding-model-orchestrator coding-model-dashboard)
  if [[ "${SYNC_MONITOR:-0}" == "1" ]]; then
    echo "SYNC_MONITOR=1 -> including coding-model-monitor (ensure the RAPL ACL is set first)."
    SERVICES+=(coding-model-monitor)
  fi

  echo "==> Repo:    ${REPO}"
  echo "==> Backing up current unit files to ${BACKUP}"
  mkdir -p "${BACKUP}"
  for s in "${SERVICES[@]}"; do
    [[ -f "${UNIT_DIR}/${s}.service" ]] && cp -a "${UNIT_DIR}/${s}.service" "${BACKUP}/"
  done

  echo "==> Syncing unit files from repo (only when changed)"
  for s in "${SERVICES[@]}"; do
    src="${REPO}/systemd/${s}.service"
    dst="${UNIT_DIR}/${s}.service"
    if [[ ! -f "${src}" ]]; then
      echo "    ! repo unit missing: ${src} (skipping)"
    elif cmp -s "${src}" "${dst}" 2>/dev/null; then
      echo "    = ${s}: already up to date"
    else
      install -m 0644 "${src}" "${dst}"
      echo "    + ${s}: updated"
    fi
  done

  echo "==> systemctl daemon-reload"
  systemctl daemon-reload
fi

# Restart in dependency order: server (owns the GPU + inference) -> orchestrator
# (calls the server) -> dashboard (polls it). Only restart what we manage.
# Capture the server PID first so the /health poll can distinguish the freshly
# started process from a still-draining old one (which keeps answering /health).
OLD_SERVER_PID="$(systemctl show -p MainPID --value coding-model-server 2>/dev/null || echo 0)"
echo "==> Restarting services (non-blocking; readiness confirmed by the /health poll below)"
for s in coding-model-server coding-model-orchestrator coding-model-dashboard; do
  printf '    restarting %s ...\n' "${s}"
  # --no-block skips waiting for the graceful SIGTERM drain (up to the unit's
  # TimeoutStopSec) to COMPLETE. But the enqueue is still a D-Bus round-trip to
  # systemd (via polkit), and under load — e.g. while PID 1 is tearing down the
  # server's ~180 GB cgroup — that call itself can return "Connection timed out"
  # (non-zero). Under `set -e` a single such blip would abort the whole redeploy,
  # skipping the remaining services AND the /health poll below. So retry the
  # enqueue a few times, and never let it kill the run: the PID-change + /health
  # poll is the real readiness gate and will catch a restart that didn't take.
  enqueued=0
  for attempt in 1 2 3; do
    if systemctl restart --no-block "${s}"; then enqueued=1; break; fi
    echo "    (enqueue attempt ${attempt}/3 failed — systemd/D-Bus busy; retrying in 3s)"
    sleep 3
  done
  [[ "${enqueued}" == "1" ]] \
    || echo "    !! ${s}: could not enqueue restart after 3 tries — see /health + status below"
done

# Health host/port from .env (HOST/PORT are what the server binds), with
# sensible fallbacks — never a hardcoded LAN IP.
HOST="127.0.0.1"; PORT="5000"; SERVER_IP=""
ENV_FILE=""
for candidate in "${HOME}/.config/coding-model-server/.env" "${REPO}/.env"; do
  [[ -f "${candidate}" ]] && ENV_FILE="${candidate}" && break
done
if [[ -n "${ENV_FILE}" ]]; then
  HOST="$(grep -E '^HOST=' "${ENV_FILE}" | tail -1 | cut -d= -f2- | tr -d '"' || true)"; HOST="${HOST:-127.0.0.1}"
  PORT="$(grep -E '^PORT=' "${ENV_FILE}" | tail -1 | cut -d= -f2- | tr -d '"' || true)"; PORT="${PORT:-5000}"
  SERVER_IP="$(grep -E '^CODING_MODEL_SERVER_IP=' "${ENV_FILE}" | tail -1 | cut -d= -f2- | tr -d '"' || true)"
fi
# 0.0.0.0 isn't dialable; probe loopback instead.
[[ "${HOST}" == "0.0.0.0" ]] && HOST="127.0.0.1"

echo "==> Waiting for /health (up to 120s) on ${HOST}:${PORT}${SERVER_IP:+ / ${SERVER_IP}:${PORT}}"
ok=0
for _ in $(seq 1 60); do
  cur_pid="$(systemctl show -p MainPID --value coding-model-server 2>/dev/null || echo 0)"
  # Ready only when a NEW process (PID changed) is answering /health — so a
  # slow-draining old process can't produce a false "ready".
  if [[ "${cur_pid}" != "0" && "${cur_pid}" != "${OLD_SERVER_PID}" ]] \
     && { curl -fsS --max-time 3 "http://${HOST}:${PORT}/health" >/dev/null 2>&1 \
          || { [[ -n "${SERVER_IP}" ]] && curl -fsS --max-time 3 "http://${SERVER_IP}:${PORT}/health" >/dev/null 2>&1; }; }; then
    ok=1; break
  fi
  sleep 2
done
if [[ "${ok}" == "1" ]]; then
  echo "    /health OK"
else
  echo "    !! /health did not respond — check: journalctl -u coding-model-server -n 50"
fi

echo "==> Status"
for s in coding-model-server coding-model-orchestrator coding-model-dashboard; do
  printf "    %-22s %s\n" "${s}" "$(systemctl is-active "${s}")"
done

if [[ "${RESTART_ONLY}" != "1" ]]; then
  echo
  echo "Backups in ${BACKUP}"
  echo "  restore: cp ${BACKUP}/<unit> ${UNIT_DIR}/ && systemctl daemon-reload"
fi
