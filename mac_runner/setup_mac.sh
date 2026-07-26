#!/bin/bash
#
# Bootstrap the Mac runner + reverse tunnel. Run ON THE MAC, from the repo root:
#
#   bash mac_runner/setup_mac.sh keith-merrill@10.0.0.123
#
# The argument is the Linux box as user@host. DERIVE it rather than trusting a
# value written down anywhere — that LAN has been re-homed repeatedly, and a
# stale address is why this setup stalled for weeks (DEV-83/DEV-109). On the
# Linux box, `ip route get <this-mac-ip>` prints the interface AND the source
# address to use; only the interface sharing the Mac's subnet has a route back.
#
# Idempotent: safe to re-run after a LAN change or a failed attempt. It backs up
# anything it replaces and never prints the API key.
set -euo pipefail

TARGET="${1:-}"
if [[ -z "${TARGET}" || "${TARGET}" != *@* ]]; then
  echo "usage: bash mac_runner/setup_mac.sh <linux-user>@<linux-host>" >&2
  echo "   e.g. bash mac_runner/setup_mac.sh keith-merrill@10.0.0.123" >&2
  exit 2
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CFG="${HOME}/.config/coding-model-runner"
AGENTS="${HOME}/Library/LaunchAgents"
KEY="${HOME}/.ssh/mac_runner_tunnel"
TS="$(date +%Y%m%d_%H%M%S)"

say() { printf '\n==> %s\n' "$1"; }

# ── 1. connectivity ──────────────────────────────────────────────────────────
say "Checking SSH to ${TARGET}"
if [[ ! -f "${KEY}" ]]; then
  echo "    ! no tunnel key at ${KEY}" >&2
  echo "      ssh-keygen -t ed25519 -f ${KEY} -N '' -C mac-runner-tunnel" >&2
  echo "      then send ${KEY}.pub to the Linux box and have it installed" >&2
  echo "      RESTRICTED (see the tunnel plist, step 6 / DEV-126)." >&2
  exit 1
fi
# The tunnel key is confined with command="/bin/false", so a successful auth
# EXITS IMMEDIATELY with no output. That is the pass condition, not a failure.
if ssh -i "${KEY}" -o BatchMode=yes -o ConnectTimeout=10 "${TARGET}" true 2>/dev/null; then
  echo "    tunnel key authenticates (and correctly gives no shell)"
else
  rc=$?
  # 255 = ssh error (auth/network). Anything else is the remote command's exit
  # status, which /bin/false makes 1 — that means auth SUCCEEDED.
  if [[ ${rc} -eq 255 ]]; then
    echo "    ! ssh failed. Common causes, in order:" >&2
    echo "      - the Linux ufw does not allow port 22 from THIS Mac's subnet" >&2
    echo "        (a mismatch TIMES OUT rather than being refused)" >&2
    echo "      - ${KEY}.pub was never added to the Linux authorized_keys" >&2
    echo "      - wrong address: re-derive it, do not reuse an old one" >&2
    exit 1
  fi
  echo "    tunnel key authenticates (command=/bin/false exited ${rc}, expected)"
fi

# ── 2. runner config ─────────────────────────────────────────────────────────
say "Installing runner config into ${CFG}"
mkdir -p "${CFG}"; chmod 700 "${CFG}"

# The server's key is authoritative (DEV-83): if the two ever differ, dispatch
# fails SILENTLY rather than reporting an auth error. Fetched as a file so the
# value is never displayed, pasted, or left in shell history.
if [[ -f "${CFG}/.env" ]]; then
  cp -a "${CFG}/.env" "${CFG}/.env.bak.${TS}"
  echo "    backed up existing .env -> .env.bak.${TS}"
fi
scp -q -i "${KEY}" "${TARGET}:~/mac-runner-key.env" "${CFG}/.env.new" 2>/dev/null || {
  # The restricted tunnel key cannot run scp (no shell). Fall back to the admin key.
  scp -q "${TARGET}:~/mac-runner-key.env" "${CFG}/.env.new"
}
printf 'CODING_MODEL_RUNNER_HOST=127.0.0.1\nCODING_MODEL_RUNNER_PORT=5050\n' >> "${CFG}/.env.new"
mv "${CFG}/.env.new" "${CFG}/.env"
chmod 600 "${CFG}/.env"
awk -F= '/^[A-Z_]/{v=substr($0,index($0,"=")+1); print "    " $1 " (" length(v) " chars)"}' "${CFG}/.env"
echo "    (API key should be 43 chars — a different width means it did not copy intact)"

if [[ ! -f "${CFG}/repos.yml" ]]; then
  cp "${REPO}/mac_runner/repos.example.yml" "${CFG}/repos.yml"
  echo "    seeded repos.yml from the example — EDIT IT for your real repo paths"
else
  echo "    repos.yml already present, left alone"
fi

# ── 3. autossh ───────────────────────────────────────────────────────────────
say "Checking autossh"
AUTOSSH="$(command -v autossh || true)"
if [[ -z "${AUTOSSH}" ]]; then
  echo "    ! not installed. Run: brew install autossh" >&2
  exit 1
fi
echo "    ${AUTOSSH}"

# ── 4. LaunchAgents ──────────────────────────────────────────────────────────
say "Installing LaunchAgents into ${AGENTS}"
mkdir -p "${AGENTS}"
for p in com.codingmodel.runner com.codingmodel.tunnel; do
  src="${REPO}/mac_runner/${p}.plist"
  dst="${AGENTS}/${p}.plist"
  [[ -f "${dst}" ]] && cp -a "${dst}" "${dst}.bak.${TS}"
  cp "${src}" "${dst}"
  # launchd does not search PATH for argv[0]; pin the real autossh location.
  /usr/bin/sed -i '' "s|/opt/homebrew/bin/autossh|${AUTOSSH}|" "${dst}"
  /usr/bin/sed -i '' "s|LINUX_USER@LINUX_HOST|${TARGET}|" "${dst}"
  if ! /usr/bin/plutil -lint "${dst}" >/dev/null; then
    echo "    ! ${p}.plist is malformed — launchd would refuse it" >&2
    exit 1
  fi
  echo "    ${p}.plist installed and lints clean"
done

if grep -q "LINUX_USER@LINUX_HOST" "${AGENTS}/com.codingmodel.tunnel.plist"; then
  echo "    ! placeholder still present in the tunnel plist" >&2
  exit 1
fi

say "Bootstrapping agents"
for p in com.codingmodel.runner com.codingmodel.tunnel; do
  launchctl bootout "gui/$(id -u)/${p}" 2>/dev/null || true   # ignore "not loaded"
  launchctl bootstrap "gui/$(id -u)" "${AGENTS}/${p}.plist"
  echo "    ${p} bootstrapped"
done

# ── 5. verify ────────────────────────────────────────────────────────────────
say "Verifying"
sleep 3
launchctl list | grep codingmodel || echo "    ! neither agent is listed"

if curl -fsS --max-time 5 http://127.0.0.1:5050/health >/dev/null 2>&1; then
  echo "    runner answers on this Mac's loopback:5050"
else
  echo "    ! runner not answering locally — check:" >&2
  echo "      tail -20 ~/Library/Logs/coding-model-runner.err.log" >&2
fi

cat <<'DONE'

Next, from the LINUX box (the tunnel is what makes this reachable there):

    curl -s http://127.0.0.1:5050/health

If that answers, the tunnel is up and the runner is reachable end to end.
If it does not, the tunnel is the suspect, not the runner:

    tail -20 ~/Library/Logs/coding-model-tunnel.err.log

"Permission denied (publickey)" means the tunnel key is not installed (or not
installed restricted) on the Linux side. "Host key verification failed" means
this Mac has never connected by hand — BatchMode cannot answer that prompt.
DONE
