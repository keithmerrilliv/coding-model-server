#!/usr/bin/env bash
#
# setup_coding_auto.sh — DEV-419 (phase 0 of DEV-400): create the coding-auto
# SSH principal on gitserver and wire the pilot repo (JSONParser.git) for it.
#
#   sudo bash git-server/setup_coding_auto.sh
#
# Root is needed for: useradd, writing another user's authorized_keys, and
# group surgery. Everything else about phase 0 (the pre-receive hook, the
# orchestrator keypair) is user-side and already handled by the operator's
# session; this script re-installs the hook anyway so a fresh box converges.
#
# Idempotent: safe to re-run.
#
# What it does:
#   1. Creates user `coding-auto` (own group, /bin/sh login shell — the real
#      confinement is the forced command, see git-shell-wrapper.coding-auto).
#   2. Installs the forced-command wrapper and authorized_keys entry
#      (restrict = no pty, no forwarding, no X11, no agent).
#   3. Grants the principal write access to JSONParser.git only: chgrp to
#      coding-auto, group-writable + setgid dirs, core.sharedRepository=group.
#      Adds youruser to the coding-auto group so objects the pipeline
#      writes stay readable/gc-able by the operator.
#   4. Installs the pre-receive hook from this directory (create-only
#      refs/auto/spec/<id>/attempt-<n> namespace for this principal).
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRINCIPAL=coding-auto
OPERATOR=youruser
REPO=/srv/private/git/JSONParser.git
PUBKEY="/home/${OPERATOR}/.ssh/coding_auto_ed25519.pub"

if [[ $EUID -ne 0 ]]; then
    echo "Needs root:  sudo bash ${BASH_SOURCE[0]}" >&2
    exit 1
fi
[[ -f "$PUBKEY" ]] || { echo "Missing $PUBKEY — generate it as ${OPERATOR} first:
  ssh-keygen -t ed25519 -C 'coding-auto orchestrator principal (DEV-419)' -f ~/.ssh/coding_auto_ed25519 -N ''" >&2; exit 1; }
[[ -d "$REPO" ]] || { echo "Missing $REPO" >&2; exit 1; }

# 1. Principal. /bin/sh, not git-shell: sshd runs the forced command via the
# login shell, and git-shell would refuse to exec our wrapper. The wrapper is
# what confines the account; the shell never sees an interactive session
# (restrict + command= in authorized_keys).
if ! id -u "$PRINCIPAL" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "/home/${PRINCIPAL}" \
            --shell /bin/sh --user-group "$PRINCIPAL"
    echo "==> created user ${PRINCIPAL}"
else
    echo "==> user ${PRINCIPAL} exists"
fi

# 2. Forced command + key.
install -o "$PRINCIPAL" -g "$PRINCIPAL" -m 755 \
    "${SCRIPT_DIR}/git-shell-wrapper.coding-auto" \
    "/home/${PRINCIPAL}/git-shell-wrapper"
install -o "$PRINCIPAL" -g "$PRINCIPAL" -m 700 -d "/home/${PRINCIPAL}/.ssh"
{
    printf 'restrict,command="/home/%s/git-shell-wrapper" ' "$PRINCIPAL"
    cat "$PUBKEY"
} > "/home/${PRINCIPAL}/.ssh/authorized_keys"
chown "$PRINCIPAL:$PRINCIPAL" "/home/${PRINCIPAL}/.ssh/authorized_keys"
chmod 600 "/home/${PRINCIPAL}/.ssh/authorized_keys"
echo "==> authorized_keys installed (restrict + forced command)"

# 3. Repo write access, scoped to the pilot repo. setgid keeps new objects in
# the coding-auto group so both sides can read what the other writes.
chgrp -R "$PRINCIPAL" "$REPO"
chmod -R g+rw "$REPO"
find "$REPO" -type d -exec chmod g+s {} +
git -C "$REPO" config core.sharedRepository group
usermod -aG "$PRINCIPAL" "$OPERATOR"
echo "==> ${REPO} group-writable by ${PRINCIPAL}; ${OPERATOR} added to group (re-login to pick it up)"

# Group ownership on the repo is not enough on its own: the parents are
# 0700 ${OPERATOR}, so the principal cannot traverse INTO them and every
# push dies at repo-open with "does not appear to be a git repository" —
# before the hook ever runs, which makes a broken setup look like a
# working one (every acceptance push "correctly rejected", for the wrong
# reason). Grant traverse to exactly this principal via ACL rather than
# `chmod o+x`: x-without-r means it can walk the path but not list it, and
# an ACL keeps the grant off every other user of a tree named "private".
REPO_PARENT="$(dirname "$REPO")"
for parent in "$(dirname "$REPO_PARENT")" "$REPO_PARENT"; do
    setfacl -m "u:${PRINCIPAL}:x" "$parent"
    echo "==> traverse ACL on ${parent} for ${PRINCIPAL}"
done

# git refuses to operate on a repo owned by another user ("detected dubious
# ownership") — here the owner is the operator and the writer is the
# pipeline principal, which is the intended arrangement, so make the
# exception explicit and scoped to this one repo in the principal's own
# gitconfig. Not --system: no other account should inherit it.
sudo -u "$PRINCIPAL" env HOME="/home/${PRINCIPAL}" \
    git config --global --replace-all safe.directory "$REPO"
echo "==> safe.directory exception for ${PRINCIPAL} on ${REPO}"

# 4. Hook (source of truth in this directory).
install -o "$OPERATOR" -g "$PRINCIPAL" -m 755 \
    "${SCRIPT_DIR}/pre-receive.coding-auto" "${REPO}/hooks/pre-receive"
echo "==> pre-receive hook installed"

cat <<'EOF'

Done. Acceptance (run as the operator, from anywhere with the private key):

  alias gpush="GIT_SSH_COMMAND='ssh -i ~/.ssh/coding_auto_ed25519 -o IdentitiesOnly=yes' \
      git -C /tmp/jsonparser-accept push ssh://coding-auto@localhost/srv/private/git/JSONParser.git"

  git clone /srv/private/git/JSONParser.git /tmp/jsonparser-accept
  gpush HEAD:refs/auto/spec/test/attempt-1     # must succeed -- CHECK THIS FIRST

Read the first result before trusting any rejection below it. A setup broken
anywhere before the hook (path traversal, ownership, key) rejects EVERY push,
so the reject cases all "pass" while proving nothing. The create case is the
canary: if it fails, fix that first and re-run the whole suite.

EOF
cat <<'EOF'
  gpush HEAD:refs/auto/spec/test/attempt-1     # must be rejected (create-only)
  gpush HEAD:refs/heads/anything               # must be rejected
  gpush HEAD:refs/tags/nope                    # must be rejected
  gpush :refs/auto/spec/test/attempt-1         # delete: must be rejected
  git -C /tmp/jsonparser-accept push origin HEAD:refs/heads/hook-canary && \
      git -C /tmp/jsonparser-accept push origin :refs/heads/hook-canary   # human unaffected
EOF
