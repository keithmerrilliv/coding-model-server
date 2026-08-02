#!/usr/bin/env bash
#
# Show which keys are set in an .env file — names and value LENGTHS only, never
# values. Use this instead of hand-rolling a redaction when you need to check
# whether a variable is present, spelled right, or empty.
#
#   bash scripts/env_keys.sh                  # repo .env, then the config one
#   bash scripts/env_keys.sh path/to/.env     # a specific file
#
# Why this exists (DEV-91): someone checking whether .env was git-tracked ran a
# grep with an inline redaction — `sed 's/=.*TOKEN.*/=<redacted>/'` — that did
# not match the line it was meant to catch. It failed OPEN: the pattern silently
# passed the real Jira API token through to the terminal, where it was captured
# in scrollback and a session transcript, and the token had to be rotated.
#
# The lesson is the general one about redaction: a filter that decides what to
# HIDE fails open when its pattern misses, and you cannot see that it missed.
# A filter that decides what to SHOW fails closed. This script only ever emits
# the part left of the first "=", so there is no pattern to get wrong.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $# -gt 0 ]]; then
  FILES=("$@")
else
  FILES=("${REPO}/.env" "${HOME}/.config/coding-model-server/.env")
fi

for f in "${FILES[@]}"; do
  echo "==> ${f}"
  if [[ ! -f "${f}" ]]; then
    echo "    (not present)"
    continue
  fi
  if [[ ! -r "${f}" ]]; then
    # Expected for the server's own .env once systemd's InaccessiblePaths is
    # applied (DEV-174) — the manager reads it pre-namespace, we cannot.
    echo "    (not readable by $(id -un))"
    continue
  fi

  # Everything left of the first "=" on a non-comment line. The value is
  # dropped by construction, not by matching.
  while IFS= read -r key; do
    # Length only — enough to spot an empty, truncated or padded value, and
    # useless to someone reading over your shoulder. Trailing whitespace is
    # called out rather than folded into the count: several values in this
    # repo's .env are space-padded, and a stray space is exactly the kind of
    # thing that makes a token or URL fail for no visible reason.
    read -r len pad < <(awk -F= -v k="${key}" '$1 == k {
        v = substr($0, index($0, "=") + 1)
        pad = sub(/[ \t]+$/, "", v)
        gsub(/^["'\'']|["'\'']$/, "", v)
        pad += sub(/[ \t]+$/, "", v)
        print length(v), (pad ? "padded" : "-")
        exit
      }' "${f}")
    note=""
    [[ "${pad}" == "padded" ]] && note="  (trailing whitespace)"
    if [[ "${len}" == "0" ]]; then
      printf '    %-40s (set but EMPTY)%s\n' "${key}" "${note}"
    else
      printf '    %-40s %s chars%s\n' "${key}" "${len}" "${note}"
    fi
  done < <(grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "${f}" | tr -d '=')
done
