#!/usr/bin/env bash
# =============================================================================
# Run All Ingestion Scripts — Repopulate the RAG Database (Parallel)
# =============================================================================
# Usage:  ./run_all_ingestion.sh [server_ip]
#   server_ip defaults to CODING_MODEL_SERVER_IP env var, then 192.168.50.101
#
# DO NOT clear the database before running this. That instruction used to live
# here, and combined with scrapers that exited zero while producing nothing
# (DEV-471) it was one command away from wiping the collection and repopulating
# it with almost nothing.
#
# Apple documentation is NOT handled here any more. It has its own path that
# replaces per framework instead of appending, and refuses to publish a
# collapsed harvest:  scraping/refresh_apple_docs.sh  (run weekly by
# coding-model-apple-docs-refresh.timer). The stages below cover local code,
# sample code and Xcode docs only.
# All stages run in parallel since they use independent data sources.
# Each stage logs to its own file.
# =============================================================================
set -uo pipefail
# NOTE: no `set -e` — stages are allowed to fail without killing the whole script

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$SCRIPT_DIR/ingestion_logs"
mkdir -p "$LOG_DIR"

export CODING_MODEL_SERVER_IP="${1:-${CODING_MODEL_SERVER_IP:-192.168.50.101}}"
MEMORY_URL="http://${CODING_MODEL_SERVER_IP}:5000/v1/memory"

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

timestamp() { date '+%Y-%m-%d %H:%M:%S'; }

# ─── Preflight check ───
echo -e "${YELLOW}Checking server connectivity...${NC}"
if ! curl -sf --max-time 5 "http://${CODING_MODEL_SERVER_IP}:5000/health" > /dev/null 2>&1; then
    echo -e "${RED}ERROR: Cannot reach server at ${CODING_MODEL_SERVER_IP}:5000${NC}"
    echo "  Ensure the server is running and CODING_MODEL_SERVER_IP is correct."
    echo "  Usage: ./run_all_ingestion.sh <server_ip>"
    exit 1
fi
echo -e "${GREEN}Server is healthy at ${CODING_MODEL_SERVER_IP}:5000${NC}"

# ─── Activate venv (tree-sitter + dependencies) ───
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
if [ -d "$REPO_ROOT/ingest_venv" ]; then
    echo -e "${YELLOW}Activating ingest_venv...${NC}"
    source "$REPO_ROOT/ingest_venv/bin/activate"
elif [ -d "$REPO_ROOT/myenv" ]; then
    echo -e "${YELLOW}Activating myenv...${NC}"
    source "$REPO_ROOT/myenv/bin/activate"
else
    echo -e "${YELLOW}No virtualenv found, using system Python${NC}"
fi

# ─── Clear stale progress files (DB was wiped) ───
echo -e "${CYAN}Clearing stale progress files${NC}"
progress_files=(
    "$SCRIPT_DIR/ingest_progress.json"
    "$SCRIPT_DIR/ingest_xcode_docs_progress.json"
    "$SCRIPT_DIR/ingest_local_dev_progress.json"
    "$SCRIPT_DIR/ingest_intelligent_progress.json"
)
for pf in "${progress_files[@]}"; do
    if [ -f "$pf" ]; then
        echo "  Removing: $(basename "$pf")"
        rm "$pf"
    fi
done
echo -e "  ${GREEN}Progress files cleared${NC}"

# ─── Track overall stats ───
TOTAL_START=$SECONDS

# Parallel indexed arrays for PID tracking (bash 3.2 compatible)
STAGE_NAMES=()
STAGE_PIDS=()
STAGE_LOGS=()

echo ""
echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  Launching all stages in parallel — $(timestamp)${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"

# =============================================================================
# STAGE 1: Local Dev Projects (tree-sitter intelligent chunking)
# =============================================================================
echo -e "  Starting: ${YELLOW}local-dev-intelligent${NC}"
python3 "$SCRIPT_DIR/ingest_intelligent.py" > "$LOG_DIR/local-dev-intelligent.log" 2>&1 &
STAGE_NAMES+=("local-dev-intelligent"); STAGE_PIDS+=($!); STAGE_LOGS+=("$LOG_DIR/local-dev-intelligent.log")

# =============================================================================
# STAGE 2: Apple Documentation — REMOVED (DEV-471)
# =============================================================================
# This ran scrape_apple_deep.py, which fetched nine hardcoded developer.apple.com
# HTML URLs, swallowed every error in a bare `except Exception`, and printed
# "Mission Complete" regardless. Its Metal/RealityKit coverage is superseded by
# scrape_apple_docs_json.py, which walks Apple's documentation JSON with real
# provenance; its Vision URLs are the only thing lost, and Vision can be added
# to the framework list if wanted.
echo -e "  Skipping: apple-docs-scrape — use scraping/refresh_apple_docs.sh"

# =============================================================================
# STAGE 3: Apple Sample Code Index (web)
# =============================================================================
echo -e "  Starting: ${YELLOW}apple-sample-code${NC}"
python3 "$SCRIPT_DIR/ingest_apple_sample_code.py" > "$LOG_DIR/apple-sample-code.log" 2>&1 &
STAGE_NAMES+=("apple-sample-code"); STAGE_PIDS+=($!); STAGE_LOGS+=("$LOG_DIR/apple-sample-code.log")

# =============================================================================
# STAGE 4: Xcode Local Documentation
# =============================================================================
if [ -d "/Applications/Xcode.app" ] || [ -d "/Applications/Xcode-beta.app" ]; then
    echo -e "  Starting: ${YELLOW}xcode-docs${NC}"
    python3 "$SCRIPT_DIR/ingest_xcode_docs.py" > "$LOG_DIR/xcode-docs.log" 2>&1 &
    STAGE_NAMES+=("xcode-docs"); STAGE_PIDS+=($!); STAGE_LOGS+=("$LOG_DIR/xcode-docs.log")
else
    echo -e "  ${YELLOW}Skipping Xcode docs — Xcode not installed${NC}"
fi

# =============================================================================
# STAGE 5: Pre-scraped Framework Documentation (from scraping/output/)
# =============================================================================
SCRAPED_OUTPUT="$SCRIPT_DIR/output"
if [ -d "$SCRAPED_OUTPUT" ] && [ "$(ls -A "$SCRAPED_OUTPUT" 2>/dev/null)" ]; then
    export CODING_MODEL_SERVER_PORT=5000
    echo -e "  Starting: ${YELLOW}scraped-frameworks${NC}"
    # ingest_scraped_data.py now auto-discovers all frameworks when called without args
    (cd "$SCRIPT_DIR" && python3 ingest_scraped_data.py) > "$LOG_DIR/scraped-frameworks.log" 2>&1 &
    STAGE_NAMES+=("scraped-frameworks"); STAGE_PIDS+=($!); STAGE_LOGS+=("$LOG_DIR/scraped-frameworks.log")
else
    echo -e "  ${YELLOW}Skipping scraped frameworks — no output data found${NC}"
fi

echo ""
echo -e "${CYAN}All stages launched. Waiting for completion...${NC}"
echo ""

# ─── Wait for all stages and collect results ───
STAGES_OK=0
STAGES_FAIL=0

for i in "${!STAGE_NAMES[@]}"; do
    stage="${STAGE_NAMES[$i]}"
    pid="${STAGE_PIDS[$i]}"
    log="${STAGE_LOGS[$i]}"
    if wait "$pid"; then
        echo -e "  ${GREEN}✓ ${stage} completed${NC} (log: ${log})"
        STAGES_OK=$((STAGES_OK + 1))
    else
        echo -e "  ${RED}✗ ${stage} failed${NC} — see ${log}"
        STAGES_FAIL=$((STAGES_FAIL + 1))
    fi
done

# =============================================================================
# Summary
# =============================================================================
TOTAL_ELAPSED=$(( SECONDS - TOTAL_START ))
TOTAL_MIN=$(( TOTAL_ELAPSED / 60 ))
TOTAL_SEC=$(( TOTAL_ELAPSED % 60 ))
STAGES_RUN=$((STAGES_OK + STAGES_FAIL))

echo ""
echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  INGESTION COMPLETE${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"
echo -e "  Total time:    ${TOTAL_MIN}m ${TOTAL_SEC}s"
echo -e "  Stages run:    ${STAGES_RUN}"
echo -e "  ${GREEN}Succeeded:     ${STAGES_OK}${NC}"
if [ "$STAGES_FAIL" -gt 0 ]; then
    echo -e "  ${RED}Failed:        ${STAGES_FAIL}${NC}"
fi
echo -e "  Logs:          ${LOG_DIR}/"
echo ""
