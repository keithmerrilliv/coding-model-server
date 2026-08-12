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
# the coding-model polkit rule — kept in this repo at polkit/49-coding-model.rules
# and installed to /etc/polkit-1/rules.d/ by a full redeploy. If restart-only
# starts timing out, that rule has most likely gone missing (DEV-293).
# Restart-only is enough for code changes since the venv is an editable install.
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

  # The systemd + polkit templates ship `youruser` / `/home/youruser`
  # placeholders (each unit header documents the sed recipe). This script runs
  # under sudo, so $USER/$HOME here are ROOT's — NOT the service account.
  # Installing a raw template would point ExecStart at /home/youruser/... and
  # key the polkit rule to user "youruser", neither of which exists, so every
  # service would fail on the next restart and restart-only would silently
  # regress to interactive polkit auth (DEV-293). Substitute before installing,
  # using the invoking operator (SUDO_USER) as the service account and falling
  # back to whatever User= the already-installed unit carries.
  SVC_USER="${SUDO_USER:-}"
  if [[ -z "${SVC_USER}" || "${SVC_USER}" == "root" ]]; then
    SVC_USER="$(sed -n 's|^User=\(.*\)|\1|p' "${UNIT_DIR}/coding-model-server.service" 2>/dev/null | head -1)"
  fi
  if [[ -z "${SVC_USER}" || "${SVC_USER}" == "root" || "${SVC_USER}" == "youruser" ]]; then
    echo "!! Could not determine the service account to substitute into the unit" >&2
    echo "   templates (SUDO_USER unset/root and no usable User= in the installed" >&2
    echo "   unit). Run via sudo from the service user's login so SUDO_USER is set:" >&2
    echo "     sudo bash ${BASH_SOURCE[0]}" >&2
    exit 1
  fi
  SVC_HOME="$(getent passwd "${SVC_USER}" | cut -d: -f6 || true)"
  SVC_HOME="${SVC_HOME:-/home/${SVC_USER}}"

  # Render a template to $2 with the operator's account and paths. The repo
  # path is taken from $REPO (the checkout this script runs from), not assumed
  # to be $HOME/Dev/coding-model-server, so a non-standard checkout location
  # still gets correct WorkingDirectory/ExecStart/EnvironmentFile paths. Order
  # matters: rewrite the repo prefix first, then any remaining home prefix.
  render_template() {  # render_template <src> <dst>
    sed -e "s|/home/youruser/Dev/coding-model-server|${REPO}|g" \
        -e "s|/home/youruser|${SVC_HOME}|g" \
        -e "s|^User=youruser|User=${SVC_USER}|" \
        -e "s|^Group=youruser|Group=${SVC_USER}|" \
        "$1" > "$2"
  }

  echo "==> Repo:    ${REPO}"
  echo "==> Account: User=${SVC_USER} Group=${SVC_USER} Home=${SVC_HOME} (from ${SUDO_USER:+SUDO_USER}${SUDO_USER:-installed unit})"
  echo "==> Backing up current unit files to ${BACKUP}"
  mkdir -p "${BACKUP}"
  for s in "${SERVICES[@]}"; do
    [[ -f "${UNIT_DIR}/${s}.service" ]] && cp -a "${UNIT_DIR}/${s}.service" "${BACKUP}/"
  done

  echo "==> Syncing unit files from repo (substituted; only when changed)"
  for s in "${SERVICES[@]}"; do
    src="${REPO}/systemd/${s}.service"
    dst="${UNIT_DIR}/${s}.service"
    if [[ ! -f "${src}" ]]; then
      echo "    ! repo unit missing: ${src} (skipping)"
      continue
    fi
    # Compare the SUBSTITUTED output against what's installed, not the raw
    # template — the installed unit is always substituted, so a raw compare
    # would report "changed" forever and reinstall placeholder units every run.
    rendered="$(mktemp)"
    render_template "${src}" "${rendered}"
    if cmp -s "${rendered}" "${dst}" 2>/dev/null; then
      echo "    = ${s}: already up to date"
    else
      install -m 0644 "${rendered}" "${dst}"
      echo "    + ${s}: updated"
    fi
    rm -f "${rendered}"
  done

  # The polkit rule is what makes --restart-only sudo-free. It used to exist
  # only as a hand-made file on the box, and when it went missing nothing
  # noticed: polkit silently fell back to interactive auth and restart-only
  # started half-applying (DEV-293). Sync it from the repo like a unit file.
  echo "==> Syncing polkit rule (grants sudo-free restart of these units)"
  polkit_src="${REPO}/polkit/49-coding-model.rules"
  polkit_dst="/etc/polkit-1/rules.d/49-coding-model.rules"
  if [[ ! -f "${polkit_src}" ]]; then
    echo "    ! repo rule missing: ${polkit_src} (skipping)"
  else
    # The rule gates on subject.user === "youruser"; substitute the real
    # service account or it matches nobody and restart-only silently falls back
    # to interactive auth (DEV-293 — the exact failure this rule exists to fix).
    rendered="$(mktemp)"
    sed -e "s|\"youruser\"|\"${SVC_USER}\"|g" "${polkit_src}" > "${rendered}"
    if cmp -s "${rendered}" "${polkit_dst}" 2>/dev/null; then
      echo "    = polkit rule: already up to date"
    else
      [[ -f "${polkit_dst}" ]] && cp -a "${polkit_dst}" "${BACKUP}/"
      install -m 0644 -o root -g root "${rendered}" "${polkit_dst}"
      echo "    + polkit rule: installed (polkitd picks it up automatically)"
    fi
    rm -f "${rendered}"
  fi

  echo "==> systemctl daemon-reload"
  systemctl daemon-reload
fi

# Restart in dependency order: server (owns the GPU + inference) -> orchestrator
# (calls the server) -> dashboard (polls it). Only restart what we manage.
# Capture every unit's PID first: it lets the /health poll tell a freshly
# started server from a still-draining old one (which keeps answering /health),
# and it is how the status block proves each restart actually happened.
declare -A OLD_PIDS
for s in coding-model-server coding-model-orchestrator coding-model-dashboard; do
  OLD_PIDS["${s}"]="$(systemctl show -p MainPID --value "${s}" 2>/dev/null || echo 0)"
done
OLD_SERVER_PID="${OLD_PIDS[coding-model-server]}"
echo "==> Restarting services (non-blocking; readiness confirmed by the /health poll below)"
for s in coding-model-server coding-model-orchestrator coding-model-dashboard; do
  printf '    restarting %s ...\n' "${s}"
  # --no-block skips waiting for the graceful SIGTERM drain (up to the unit's
  # TimeoutStopSec) to COMPLETE. The enqueue itself is still a D-Bus round-trip
  # to systemd, authorized by polkit, and it can fail with "Connection timed
  # out". Under `set -e` a single failure would abort the whole redeploy,
  # skipping the remaining services AND the /health poll — so retry a few times
  # and never let it kill the run. The PID comparison in the status block below
  # is the real gate and will catch a restart that didn't take.
  #
  # On the cause of that timeout: this comment used to blame D-Bus load (PID 1
  # tearing down the server's ~180 GB cgroup). That was a GUESS, and it was
  # wrong — it cost real debugging time in DEV-293. What actually happened is
  # that the polkit rule granting this user manage-units had gone missing, so
  # polkit fell back to INTERACTIVE authentication and each non-interactive
  # call blocked until the auth timeout. A missing rule looks exactly like a
  # busy bus from here. If these timeouts return, check polkit FIRST:
  #   ls -l /etc/polkit-1/rules.d/49-coding-model.rules   # repo: polkit/
  #   journalctl -b | grep -i 'polkitd.*manage-units' | tail
  # A "FAILED to authenticate" line confirms it; reinstall with a full
  # `sudo bash scripts/redeploy.sh`. Load may still be a real cause, which is
  # why the retry stays — but it is the second thing to suspect, not the first.
  enqueued=0
  for attempt in 1 2 3; do
    if systemctl restart --no-block "${s}"; then enqueued=1; break; fi
    echo "    (enqueue attempt ${attempt}/3 failed — check the polkit rule first," \
         "see the comment above; retrying in 3s)"
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

# Report by MainPID, not `is-active`. A restart that never happened leaves the
# OLD process running and perfectly healthy, so is-active says "active" and the
# deploy looks fine while the new code is nowhere near production — which is
# exactly how DEV-152 shipped to a half-applied box (DEV-293). A unit whose PID
# did not change is a FAILED deploy and must be loud, and fatal.
echo "==> Status (by MainPID — a restart that didn't take keeps the old PID)"
FAILED_UNITS=()
for s in coding-model-server coding-model-orchestrator coding-model-dashboard; do
  old="${OLD_PIDS[${s}]:-0}"
  new=""
  # --no-block means the restart may still be in flight; give it a grace window.
  for _ in $(seq 1 15); do
    new="$(systemctl show -p MainPID --value "${s}" 2>/dev/null || echo 0)"
    [[ "${new}" != "0" && "${new}" != "${old}" ]] && break
    sleep 2
  done
  state="$(systemctl is-active "${s}")"
  if [[ "${new}" != "0" && "${new}" != "${old}" ]]; then
    printf "    %-26s %-10s restarted (PID %s -> %s)\n" "${s}" "${state}" "${old}" "${new}"
  else
    printf "    %-26s %-10s !! NOT RESTARTED (still PID %s)\n" "${s}" "${state}" "${old}"
    FAILED_UNITS+=("${s}")
  fi
done

if (( ${#FAILED_UNITS[@]} > 0 )); then
  echo
  echo "!! ${#FAILED_UNITS[@]} unit(s) did not restart: ${FAILED_UNITS[*]}"
  echo "   They are still running the OLD code. This deploy did NOT take."
  if [[ "${RESTART_ONLY}" == "1" ]]; then
    echo "   Most likely the polkit rule is missing, so systemctl fell back to"
    echo "   interactive auth and the enqueue timed out. Check:"
    echo "     ls -l /etc/polkit-1/rules.d/49-coding-model.rules"
    echo "     journalctl -b | grep -i 'polkitd.*manage-units' | tail"
    echo "   Reinstall it (and the units) with:  sudo bash ${BASH_SOURCE[0]}"
  fi
  exit 1
fi

if [[ "${RESTART_ONLY}" != "1" ]]; then
  echo
  echo "Backups in ${BACKUP}"
  echo "  restore: cp ${BACKUP}/<unit> ${UNIT_DIR}/ && systemctl daemon-reload"
fi
