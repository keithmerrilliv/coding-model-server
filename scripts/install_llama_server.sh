#!/usr/bin/env bash
# Install a llama.cpp build into tools/ with a rollback copy, and prove it.
#
#   bash scripts/install_llama_server.sh <build/bin dir>            # dry run: checks + what would change
#   bash scripts/install_llama_server.sh <build/bin dir> --execute  # back up, install, verify
#   bash scripts/install_llama_server.sh --rollback [<backup dir>]  # restore the newest (or a named) backup
#
# tools/ is what production loads: llama_server.py puts it first on LD_LIBRARY_PATH, which
# beats the binary's RUNPATH. So installing here IS the deploy. The libraries are versioned
# files reached through .so.0 symlinks; a build with new version numbers only takes effect
# once every symlink is repointed, and one left behind loads the OLD library beside the new
# binary. This script repoints all of them and then checks what actually gets loaded.
#
# Refuses while a llama-server child holds the GPU or a spec is mid-generation: run it when
# nothing is in flight, or at a human gate (docs/PIPELINE.md).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS="$REPO/tools"
PY="$REPO/venv/bin/python"
FAMILIES=(libggml libggml-base libggml-cpu libggml-cuda libllama libllama-common libmtmd)
fail() { echo "REFUSED: $*" >&2; exit 1; }

gpu_used() { nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1; }

restore() {
  local dir="$1"
  [[ -d "$dir" ]] || fail "no backup at $dir"
  echo "== restoring $dir"
  for f in "$dir"/*; do
    [[ -e "$f" || -L "$f" ]] || continue
    cp -a "$f" "$TOOLS/.restore.$$" && mv -fT "$TOOLS/.restore.$$" "$TOOLS/$(basename "$f")"
  done
  echo "== restored; llama-server reports:"
  LD_LIBRARY_PATH="$TOOLS" "$TOOLS/llama-server" --version 2>&1 | tail -1
}

if [[ "${1:-}" == "--rollback" ]]; then
  dir="${2:-$(ls -dt "$TOOLS"/.rollback-* 2>/dev/null | head -1)}"
  [[ -n "$dir" ]] || fail "no rollback directory under tools/"
  restore "$dir"
  exit 0
fi

BUILD="${1:-}"; MODE="${2:-dry-run}"
[[ -n "$BUILD" && -x "$BUILD/llama-server" ]] || { echo "usage: $0 <build/bin> [--execute] | --rollback [dir]" >&2; exit 2; }
BUILD="$(cd "$BUILD" && pwd)"

# --- preconditions -----------------------------------------------------------
used="$(gpu_used)"
[[ "$used" -lt 1500 ]] || fail "the GPU holds ${used} MiB: a model is loaded. Unload it (idle server, nothing in flight)."
live="$("$PY" - <<'PYEOF'
import sqlite3
c = sqlite3.connect("file:var/tasks_db/tasks.sqlite?mode=ro", uri=True)
rows = c.execute("select spec_id, gate_type from review_gates where status='pending'").fetchall()
pend = {s for s, _ in rows}
run = [i for (i, st) in c.execute("select id, status from specs where status not in "
       "('done','failed','cancelled')") if i not in pend]
print(",".join(run))
PYEOF
)"
[[ -z "$live" ]] || fail "spec(s) mid-generation, not at a human gate: $live"

# Every library family in the build must exist as one versioned file.
declare -A NEWFILE
for fam in "${FAMILIES[@]}"; do
  f="$(ls "$BUILD/$fam.so".[0-9]*.[0-9]*.[0-9]* 2>/dev/null | head -1 || true)"
  [[ -n "$f" ]] || fail "build has no versioned $fam.so.X.Y.Z"
  NEWFILE[$fam]="$f"
done
[[ -e "$BUILD/libllama-server-impl.so" ]] || fail "build has no libllama-server-impl.so"

echo "== build: $BUILD"
LD_LIBRARY_PATH="$BUILD" "$BUILD/llama-server" --version 2>&1 | tail -2
echo "== libraries that will be installed and repointed:"
for fam in "${FAMILIES[@]}"; do
  echo "   $fam.so.0 -> $(basename "${NEWFILE[$fam]}")   (now -> $(readlink "$TOOLS/$fam.so.0" || echo none))"
done
echo "   sha256 libggml-cuda: $(sha256sum "${NEWFILE[libggml-cuda]}" | cut -c1-16)"
[[ "$MODE" == "--execute" ]] || { echo "== DRY RUN. Re-run with --execute."; exit 0; }

# --- back up what is about to change ----------------------------------------
STAMP="$(date +%Y%m%dT%H%M%S)"
BK="$TOOLS/.rollback-$STAMP"
mkdir -p "$BK"
cp -a "$TOOLS/llama-server" "$TOOLS/libllama-server-impl.so" "$BK/"
for fam in "${FAMILIES[@]}"; do
  cur="$(readlink "$TOOLS/$fam.so.0")"
  cp -a "$TOOLS/$fam.so" "$TOOLS/$fam.so.0" "$TOOLS/$cur" "$BK/"
done
[[ -e "$TOOLS/libggml-cuda.so.0.24.0.pre-dev852" ]] && cp -a "$TOOLS/libggml-cuda.so.0.24.0.pre-dev852" "$BK/"
echo "== rollback copy: $BK"

# --- install: files first, then symlinks, then the launcher ------------------
for fam in "${FAMILIES[@]}"; do
  new="${NEWFILE[$fam]}"; base="$(basename "$new")"
  cp -a "$new" "$TOOLS/$base.new" && mv -f "$TOOLS/$base.new" "$TOOLS/$base"
  ln -sfn "$base" "$TOOLS/$fam.so.0"
  ln -sfn "$fam.so.0" "$TOOLS/$fam.so"
done
cp -a "$BUILD/libllama-server-impl.so" "$TOOLS/libllama-server-impl.so.new" && mv -f "$TOOLS/libllama-server-impl.so.new" "$TOOLS/libllama-server-impl.so"
cp -a "$BUILD/llama-server" "$TOOLS/llama-server.new" && mv -f "$TOOLS/llama-server.new" "$TOOLS/llama-server"

# --- prove what loads ---------------------------------------------------------
echo "== verify"
LD_LIBRARY_PATH="$TOOLS" "$TOOLS/llama-server" --version 2>&1 | tail -1
bad=0
for fam in "${FAMILIES[@]}"; do
  got="$(LD_LIBRARY_PATH="$TOOLS" ldd "$TOOLS/llama-server" 2>/dev/null | awk -v f="$fam.so.0" '$1==f {print $3}' | head -1)"
  [[ -z "$got" ]] && continue      # not every family is linked directly by the launcher
  want="$(readlink -f "$TOOLS/$fam.so.0")"
  [[ "$(readlink -f "$got")" == "$want" && "$want" == "$(readlink -f "$TOOLS/$(basename "${NEWFILE[$fam]}")")" ]] \
    || { echo "   MISMATCH $fam: ldd -> $got"; bad=1; }
done
[[ "$(sha256sum "$(readlink -f "$TOOLS/libggml-cuda.so.0")" | cut -c1-16)" == "$(sha256sum "${NEWFILE[libggml-cuda]}" | cut -c1-16)" ]] || { echo "   MISMATCH libggml-cuda sha"; bad=1; }
if [[ "$bad" -ne 0 ]]; then
  echo "== verification FAILED; rolling back"; restore "$BK"; exit 1
fi
echo "== installed. Rollback: bash scripts/install_llama_server.sh --rollback   (or: $BK)"
