#!/usr/bin/env bash
# Scheduled Apple-docs refresh. Designed to run unattended on zooshly, where
# both the RAG and the server live — the scraper is stdlib-only and the
# ingester needs requests, so nothing here depends on the Mac being awake.
#
# WHY A SANITY GATE EXISTS
#
# The two previous Apple-docs integrations in this repo (cupertino, the Apple
# Deep Docs MCP) both rotted to unusable, and neither said so. scrape_generic_
# docs.py still exits zero today while recording the string "failed" for every
# symbol. Paired with run_all_ingestion.sh's "clear the database first", that
# was one command away from wiping the collection and repopulating it with
# nothing.
#
# This script is exposed to the same rot: it parses an UNDOCUMENTED Apple JSON
# schema. If Apple renames primaryContentSections, render_symbol quietly yields
# empty pages, the crawl "succeeds", and a refresh would replace good content
# with nothing. So the run compares itself against the last success and REFUSES
# to touch the RAG if the harvest collapses. Failing loudly is the only thing
# that keeps a scheduled job honest — first-party-ness is not protection.
#
# REPLACE, NOT APPEND
#
# Per framework: delete where framework=X, then ingest X. Content-hash dedup
# alone is not enough — when Apple edits a page the new text hashes
# differently, so the old chunk survives forever and retrieval sees both. Over
# a year of runs that rebuilds exactly the untraceable layer-cake this whole
# effort removed.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVER="${CODING_MODEL_SERVER_IP:-127.0.0.1}"
PORT="${CODING_MODEL_SERVER_PORT:-5000}"
STATE="${APPLE_DOCS_STATE_DIR:-$REPO/var/apple_docs}"
WORK="$STATE/run-$(date +%Y%m%d-%H%M%S)"
STAMP="$STATE/last_success.json"
# Fraction of the previous harvest below which we refuse to publish.
MIN_RATIO="${APPLE_DOCS_MIN_RATIO:-0.8}"

mkdir -p "$WORK"
exec >>"$STATE/refresh.log" 2>&1
echo "=== refresh started $(date '+%F %T') -> $WORK ==="

PY="$REPO/venv/bin/python"
[ -x "$PY" ] || PY=python3
KEY="$(grep -m1 '^ADMIN_API_KEY=' "$REPO/.env" 2>/dev/null | cut -d= -f2-)"
if [ -z "$KEY" ]; then echo "!! no ADMIN_API_KEY; aborting"; exit 2; fi

CAPS=(
  "Metal:4200" "MetalKit:300" "MetalFX:400" "MetalPerformanceShaders:1200"
  "CompositorServices:500" "RealityKit:6300" "ARKit:2400" "ModelIO:800"
  "SwiftUI:8700" "Swift:3000" "Foundation:3000"
)

INGEST_BATCH="apple-docs-$(date +%Y-%m-%d)" \
  "$PY" "$SCRIPT_DIR/scrape_apple_docs_json.py" \
    --out "$WORK" --delay 0.3 --skip-stubs "${CAPS[@]}"
echo "=== crawl rc=$? at $(date '+%T') ==="

# ── sanity gate ──────────────────────────────────────────────────────────────
# Compare this harvest against the last published one, per framework. A
# framework that collapses is not ingested; if MANY collapse, treat it as a
# schema break and publish nothing.
GATE="$("$PY" - "$WORK" "$STAMP" "$MIN_RATIO" <<'PYEOF'
import json, sys, pathlib
work, stamp_path, min_ratio = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), float(sys.argv[3])
counts = {}
for f in sorted(work.glob("*.jsonl")):
    counts[f.stem] = sum(1 for _ in f.open())
prev = {}
if stamp_path.exists():
    prev = json.loads(stamp_path.read_text()).get("counts", {})
ok, skip = [], []
for fw, n in counts.items():
    p = prev.get(fw)
    if p and n < p * min_ratio:
        skip.append(f"{fw}({n} vs {p})")
    elif n == 0:
        skip.append(f"{fw}(empty)")
    else:
        ok.append(fw)
verdict = "ABORT" if (prev and len(skip) > len(ok)) else "OK"
print(json.dumps({"verdict": verdict, "publish": ok, "skip": skip, "counts": counts}))
PYEOF
)"
VERDICT="$(echo "$GATE" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["verdict"])')"
echo "  gate: $GATE"

if [ "$VERDICT" = "ABORT" ]; then
  echo "!! most frameworks collapsed vs the last run — treating as a schema break."
  echo "!! RAG left untouched. Inspect $WORK before re-running."
  exit 3
fi

# ── publish: replace each framework in place ─────────────────────────────────
PUBLISH="$(echo "$GATE" | "$PY" -c 'import json,sys;print(" ".join(json.load(sys.stdin)["publish"]))')"
RC=0
for FW in $PUBLISH; do
  DEL="$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 \
        -H "X-Admin-Key: $KEY" -H 'Content-Type: application/json' \
        -d "{\"where\":{\"framework\":\"$FW\"}}" \
        "http://$SERVER:$PORT/v1/memory/delete")"
  if [ "$DEL" != "200" ]; then
    # A 404 means the server predates the delete endpoint. Do NOT silently
    # fall back to appending — that is the accumulation this exists to stop.
    echo "!! delete $FW returned HTTP $DEL — refusing to append; skipping"
    RC=4; continue
  fi
  ADMIN_API_KEY="$KEY" "$PY" "$SCRIPT_DIR/ingest_apple_docs_json.py" \
      --server "$SERVER" --port "$PORT" "$WORK/$FW.jsonl" || RC=5
done

if [ $RC -eq 0 ]; then
  echo "$GATE" | "$PY" -c '
import json,sys,time,pathlib
d=json.load(sys.stdin); d["published_at"]=time.strftime("%F %T")
pathlib.Path(sys.argv[1]).write_text(json.dumps(d, indent=2))' "$STAMP"
  echo "=== published $(echo "$PUBLISH" | wc -w) frameworks; stamp updated ==="
  # Keep the three most recent working dirs for post-mortems.
  ls -1dt "$STATE"/run-* 2>/dev/null | tail -n +4 | xargs -r rm -rf
fi

echo "=== refresh done rc=$RC $(date '+%F %T') ==="
exit $RC
