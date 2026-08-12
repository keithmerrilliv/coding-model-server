#!/usr/bin/env bash
# Validate a mac_runner sandbox profile's security properties (DEV-527).
#
# Runs the given profile under sandbox-exec with the same -D params the runner
# passes (frameworks.wrap_sandbox) and asserts the write/network/read boundary
# holds — and that a real toolchain compile still works inside it. It only ever
# READS the profile and runs throwaway commands, so it is safe to run while the
# runner is live (it does not dispatch a build).
#
#   bash mac_runner/validate_sandbox_profile.sh                            # live sandbox.sb
#   bash mac_runner/validate_sandbox_profile.sh mac_runner/some-other.sb   # a candidate
#
# Exit 0 iff every security assertion holds.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE="${1:-$HERE/sandbox.sb}"
SBEXEC="/usr/bin/sandbox-exec"

[ -f "$PROFILE" ]  || { echo "no such profile: $PROFILE"; exit 2; }
[ -x "$SBEXEC" ]   || { echo "sandbox-exec not found at $SBEXEC"; exit 2; }

WT="$(mktemp -d "${TMPDIR:-/tmp}/dev527-worktree.XXXXXX")"
DD="$(mktemp -d "${TMPDIR:-/tmp}/dev527-derived.XXXXXX")"
CACHE_SCRATCH="$HOME/Library/Caches/coding-model-runner-dev527-validate"
mkdir -p "$CACHE_SCRATCH"
trap 'rm -rf "$WT" "$DD" "$CACHE_SCRATCH"' EXIT

echo "profile: $PROFILE"
echo "worktree: $WT"
echo

# Run a command inside the profile with the runner's parameters.
sb() {
  "$SBEXEC" -f "$PROFILE" \
    -D "HOME=$HOME" -D "WORKTREE=$WT" -D "DERIVED_DATA=$DD" \
    -D "SIGNING_KEYCHAIN=/nonexistent/no-signing-keychain" \
    "$@"
}

PASS=0; FAIL=0
ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }

# assert a write is DENIED: the command must fail AND leave no file behind.
deny_write() {
  local target="$1" label="$2"
  rm -f "$target" 2>/dev/null
  if sb /bin/sh -c "echo pwned > '$target'" 2>/dev/null || [ -e "$target" ]; then
    bad "$label — write to $target was ALLOWED (escape vector)"
    rm -f "$target" 2>/dev/null
  else
    ok "$label — write to $target denied"
  fi
}

# assert a write is ALLOWED.
allow_write() {
  local target="$1" label="$2"
  if sb /usr/bin/touch "$target" 2>/dev/null && [ -e "$target" ]; then
    ok "$label — write to $target allowed"
    rm -f "$target" 2>/dev/null
  else
    bad "$label — write to $target was DENIED (would break builds)"
  fi
}

# assert a read is DENIED (skip if the path doesn't exist on this host).
deny_read() {
  local target="$1" label="$2"
  [ -e "$target" ] || { echo "  skip  $label — $target absent"; return; }
  if sb /bin/cat "$target" >/dev/null 2>&1 || sb /bin/ls "$target" >/dev/null 2>&1; then
    bad "$label — read of $target was ALLOWED (exfil surface)"
  else
    ok "$label — read of $target denied"
  fi
}

echo "── write boundary (the escape vector) ──"
deny_write  "/opt/homebrew/bin/dev527_probe" "PATH implant (/opt/homebrew/bin)"
deny_write  "/usr/local/bin/dev527_probe"    "PATH implant (/usr/local/bin)"
deny_write  "$HOME/.zshrc.dev527_probe"      "dotfile implant (\$HOME)"
allow_write "$WT/probe"                       "build output (WORKTREE)"
allow_write "$DD/probe"                       "derived data (DERIVED_DATA)"
allow_write "$CACHE_SCRATCH/probe"            "module cache (~/Library/Caches)"

echo "── network boundary ──"
if sb /usr/bin/python3 -c "import socket; socket.create_connection(('1.1.1.1',53),3).close()" 2>/dev/null; then
  bad "outbound IP socket was ALLOWED (exfil surface)"
else
  ok "outbound IP socket denied"
fi

echo "── read boundary (credential stores) ──"
deny_read "$HOME/.ssh"                       "~/.ssh"
deny_read "$HOME/.aws"                       "~/.aws"
deny_read "$HOME/.claude"                    "~/.claude (DEV-527 addition)"
deny_read "$HOME/.gitconfig"                 "~/.gitconfig (DEV-527 addition)"
# system surface must stay readable, or every build breaks.
if sb /bin/cat /etc/hosts >/dev/null 2>&1; then
  ok "system read (/etc/hosts) still allowed"
else
  bad "system read (/etc/hosts) DENIED — profile is too tight, will break builds"
fi

echo "── toolchain still works under the profile ──"
cat > "$WT/hello.swift" <<'SWIFT'
print("dev527 ok")
SWIFT
if command -v swiftc >/dev/null 2>&1; then
  # Private module cache under WORKTREE so this never contends with a live build.
  if sb /usr/bin/swiftc -module-cache-path "$WT/mcache" \
        -o "$WT/hello" "$WT/hello.swift" 2>"$WT/swiftc.err" \
     && sb "$WT/hello" 2>/dev/null | grep -q "dev527 ok"; then
    ok "swiftc compile + run succeeds under the profile (no network needed)"
  else
    bad "swiftc failed under the profile — a legit build path is blocked; see $WT/swiftc.err"
    sed 's/^/        /' "$WT/swiftc.err" 2>/dev/null | head -20
  fi
else
  echo "  skip  swiftc not on PATH"
fi

echo
echo "==== $PASS passed, $FAIL failed ===="
[ "$FAIL" -eq 0 ]
