#!/usr/bin/env bash
# Unattended overnight Apple-docs refresh: crawl, then provision the RAG.
#
# Caps are derived per framework rather than set uniformly. Apple's index
# endpoint (developer.apple.com/tutorials/data/index/<fw>) gives the exact tree
# size in one request, and a first sweep measured how much of each framework is
# prose-less. Cap ~= indexed_swift_nodes * 0.7 (share that are fetchable pages,
# measured on MetalKit: 126 crawled vs 180 indexed) * (1 - no_prose_rate).
#
# The two giants are deliberately UNDER-capped: Swift (31,156 nodes) and
# Foundation (37,597) are 65% of the whole tree and the least relevant to the
# Metal work this RAG exists to serve. Uncapping them would swamp the
# collection with general-purpose API pages and push graphics content out of
# the fixed top-5 retrieval slots.
set -uo pipefail   # no -e: a partial crawl should still get ingested

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:?usage: overnight_refresh.sh <outdir> [server]}"
SERVER="${2:-192.168.1.3}"
LOG="$OUT/../overnight.log"
mkdir -p "$OUT"

exec >>"$LOG" 2>&1
echo "=== overnight refresh started $(date '+%Y-%m-%d %H:%M:%S') ==="

CAPS=(
  "Metal:3500" "MetalKit:300" "MetalFX:400" "MetalPerformanceShaders:1200"
  "CompositorServices:500" "RealityKit:5000" "ARKit:1800" "ModelIO:700"
  "SwiftUI:5000" "Swift:3000" "Foundation:3000"
)

INGEST_BATCH="apple-docs-$(date +%Y-%m)" \
python3 "$SCRIPT_DIR/scrape_apple_docs_json.py" \
    --out "$OUT" --delay 0.3 --skip-stubs "${CAPS[@]}"
SCRAPE_RC=$?
echo "=== crawl finished rc=$SCRAPE_RC at $(date '+%H:%M:%S') ==="

shopt -s nullglob
FILES=("$OUT"/*.jsonl)
if [ ${#FILES[@]} -eq 0 ]; then
  echo "!! no .jsonl produced — nothing to ingest, leaving the RAG untouched"
  exit 1
fi
echo "=== ingesting ${#FILES[@]} framework files (crawl rc=$SCRAPE_RC) ==="

# Ingest even on a non-zero crawl rc: rc=1 means some fetches gave up, which
# makes the sweep incomplete, not the captured pages invalid. Dedup is by
# content hash server-side, so re-ingesting overlap is harmless.
ADMIN_API_KEY="$(ssh -o BatchMode=yes zooshly \
    'grep -m1 "^ADMIN_API_KEY=" ~/Dev/coding-model-server/.env | cut -d= -f2-')" \
python3 "$SCRIPT_DIR/ingest_apple_docs_json.py" --server "$SERVER" "$OUT"
INGEST_RC=$?

echo "=== done $(date '+%Y-%m-%d %H:%M:%S')  crawl_rc=$SCRAPE_RC ingest_rc=$INGEST_RC ==="
exit $INGEST_RC
