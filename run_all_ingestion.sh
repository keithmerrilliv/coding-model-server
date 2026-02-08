#!/usr/bin/env bash
# =============================================================================
# Run All Ingestion Scripts — Repopulate the RAG Database
# =============================================================================
# Usage:  ./run_all_ingestion.sh [server_ip]
#   server_ip defaults to QWEN_SERVER_IP env var, then 192.168.50.101
#
# The database should be cleared before running this.
# Scripts are run in dependency order. Each stage logs to its own file.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$SCRIPT_DIR/ingestion_logs"
mkdir -p "$LOG_DIR"

export QWEN_SERVER_IP="${1:-${QWEN_SERVER_IP:-192.168.50.101}}"
MEMORY_URL="http://${QWEN_SERVER_IP}:5000/v1/memory"

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

timestamp() { date '+%Y-%m-%d %H:%M:%S'; }

log_header() {
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"
    echo -e "${CYAN}  $1${NC}"
    echo -e "${CYAN}  $(timestamp)${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"
}

run_stage() {
    local name="$1"
    local cmd="$2"
    local logfile="$LOG_DIR/${name}.log"

    log_header "Stage: $name"
    echo -e "  Log: ${logfile}"
    echo -e "  Command: ${cmd}"
    echo ""

    local start_time=$SECONDS
    if eval "$cmd" > "$logfile" 2>&1; then
        local elapsed=$(( SECONDS - start_time ))
        echo -e "  ${GREEN}✓ $name completed in ${elapsed}s${NC}"
        return 0
    else
        local elapsed=$(( SECONDS - start_time ))
        echo -e "  ${RED}✗ $name failed after ${elapsed}s — see $logfile${NC}"
        return 1
    fi
}

# ─── Preflight check ───
echo -e "${YELLOW}Checking server connectivity...${NC}"
if ! curl -sf --max-time 5 "http://${QWEN_SERVER_IP}:5000/health" > /dev/null 2>&1; then
    echo -e "${RED}ERROR: Cannot reach server at ${QWEN_SERVER_IP}:5000${NC}"
    echo "  Ensure the server is running and QWEN_SERVER_IP is correct."
    echo "  Usage: ./run_all_ingestion.sh <server_ip>"
    exit 1
fi
echo -e "${GREEN}Server is healthy at ${QWEN_SERVER_IP}:5000${NC}"

# ─── Activate venv (tree-sitter + dependencies) ───
if [ -d "$SCRIPT_DIR/ingest_venv" ]; then
    echo -e "${YELLOW}Activating ingest_venv...${NC}"
    source "$SCRIPT_DIR/ingest_venv/bin/activate"
elif [ -d "$SCRIPT_DIR/myenv" ]; then
    echo -e "${YELLOW}Activating myenv...${NC}"
    source "$SCRIPT_DIR/myenv/bin/activate"
else
    echo -e "${YELLOW}No virtualenv found, using system Python${NC}"
fi

# ─── Clear stale progress files (DB was wiped) ───
log_header "Clearing stale progress files"
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
echo -e "  ${GREEN}✓ Progress files cleared${NC}"

# ─── Track overall stats ───
TOTAL_START=$SECONDS
STAGES_RUN=0
STAGES_OK=0
STAGES_FAIL=0

run_tracked() {
    STAGES_RUN=$((STAGES_RUN + 1))
    if run_stage "$@"; then
        STAGES_OK=$((STAGES_OK + 1))
    else
        STAGES_FAIL=$((STAGES_FAIL + 1))
    fi
}

# =============================================================================
# STAGE 1: Local Dev Projects (tree-sitter intelligent chunking)
# This is the most important — your own code for RAG context.
# Uses ingest_intelligent.py (supersedes ingest_local_dev.py).
# =============================================================================
run_tracked "local-dev-intelligent" \
    "python3 '$SCRIPT_DIR/ingest_intelligent.py'"

# =============================================================================
# STAGE 2: Apple Documentation Scraping (web)
# Scrapes priority Apple docs pages (Metal 4, RealityKit, Vision).
# =============================================================================
run_tracked "apple-docs-scrape" \
    "python3 '$SCRIPT_DIR/scrape_apple_deep.py'"

# =============================================================================
# STAGE 3: Apple Sample Code Index (web)
# Downloads and chunks Apple sample code and Metal docs from JSON API.
# =============================================================================
run_tracked "apple-sample-code" \
    "python3 '$SCRIPT_DIR/ingest_apple_sample_code.py'"

# =============================================================================
# STAGE 4: Xcode Local Documentation
# Ingests documentation bundled with Xcode from /Applications.
# =============================================================================
if [ -d "/Applications/Xcode.app" ] || [ -d "/Applications/Xcode-beta.app" ]; then
    run_tracked "xcode-docs" \
        "python3 '$SCRIPT_DIR/ingest_xcode_docs.py'"
else
    echo -e "  ${YELLOW}⊘ Skipping Xcode docs — Xcode not installed${NC}"
fi

# =============================================================================
# STAGE 5: Pre-scraped Framework Documentation (from scraping/output/)
# Ingests already-scraped JSON data for ~20 Apple frameworks.
# =============================================================================
SCRAPED_OUTPUT="$SCRIPT_DIR/scraping/output"
if [ -d "$SCRAPED_OUTPUT" ] && [ "$(ls -A "$SCRAPED_OUTPUT" 2>/dev/null)" ]; then
    # Export vars that ingest_scraped_data.py reads from .env / environment
    export QWEN_SERVER_PORT=5000

    run_tracked "scraped-frameworks" \
        "cd '$SCRIPT_DIR/scraping' && python3 ingest_scraped_data.py"
else
    echo -e "  ${YELLOW}⊘ Skipping scraped frameworks — no output data found${NC}"
fi

# =============================================================================
# Summary
# =============================================================================
TOTAL_ELAPSED=$(( SECONDS - TOTAL_START ))
TOTAL_MIN=$(( TOTAL_ELAPSED / 60 ))
TOTAL_SEC=$(( TOTAL_ELAPSED % 60 ))

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
