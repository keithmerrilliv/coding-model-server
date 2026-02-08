#!/usr/bin/env bash
# =============================================================================
# Run All Ingestion Scripts — Repopulate the RAG Database (Parallel)
# =============================================================================
# Usage:  ./run_all_ingestion.sh [server_ip]
#   server_ip defaults to QWEN_SERVER_IP env var, then 192.168.50.101
#
# The database should be cleared before running this.
# All stages run in parallel since they use independent data sources.
# Each stage logs to its own file.
# =============================================================================
set -uo pipefail
# NOTE: no `set -e` — stages are allowed to fail without killing the whole script

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$SCRIPT_DIR/ingestion_logs"
mkdir -p "$LOG_DIR"

export QWEN_SERVER_IP="${1:-${QWEN_SERVER_IP:-192.168.50.101}}"
MEMORY_URL="http://${QWEN_SERVER_IP}:5000/v1/memory"

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

timestamp() { date '+%Y-%m-%d %H:%M:%S'; }

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

# Associative arrays for PID tracking
declare -A STAGE_PIDS
declare -A STAGE_LOGS

echo ""
echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  Launching all stages in parallel — $(timestamp)${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"

# =============================================================================
# STAGE 1: Local Dev Projects (tree-sitter intelligent chunking)
# =============================================================================
STAGE_LOGS[local-dev-intelligent]="$LOG_DIR/local-dev-intelligent.log"
echo -e "  Starting: ${YELLOW}local-dev-intelligent${NC}"
python3 "$SCRIPT_DIR/ingest_intelligent.py" > "${STAGE_LOGS[local-dev-intelligent]}" 2>&1 &
STAGE_PIDS[local-dev-intelligent]=$!

# =============================================================================
# STAGE 2: Apple Documentation Scraping (web)
# =============================================================================
STAGE_LOGS[apple-docs-scrape]="$LOG_DIR/apple-docs-scrape.log"
echo -e "  Starting: ${YELLOW}apple-docs-scrape${NC}"
python3 "$SCRIPT_DIR/scrape_apple_deep.py" > "${STAGE_LOGS[apple-docs-scrape]}" 2>&1 &
STAGE_PIDS[apple-docs-scrape]=$!

# =============================================================================
# STAGE 3: Apple Sample Code Index (web)
# =============================================================================
STAGE_LOGS[apple-sample-code]="$LOG_DIR/apple-sample-code.log"
echo -e "  Starting: ${YELLOW}apple-sample-code${NC}"
python3 "$SCRIPT_DIR/ingest_apple_sample_code.py" > "${STAGE_LOGS[apple-sample-code]}" 2>&1 &
STAGE_PIDS[apple-sample-code]=$!

# =============================================================================
# STAGE 4: Xcode Local Documentation
# =============================================================================
if [ -d "/Applications/Xcode.app" ] || [ -d "/Applications/Xcode-beta.app" ]; then
    STAGE_LOGS[xcode-docs]="$LOG_DIR/xcode-docs.log"
    echo -e "  Starting: ${YELLOW}xcode-docs${NC}"
    python3 "$SCRIPT_DIR/ingest_xcode_docs.py" > "${STAGE_LOGS[xcode-docs]}" 2>&1 &
    STAGE_PIDS[xcode-docs]=$!
else
    echo -e "  ${YELLOW}Skipping Xcode docs — Xcode not installed${NC}"
fi

# =============================================================================
# STAGE 5: Pre-scraped Framework Documentation (from scraping/output/)
# =============================================================================
SCRAPED_OUTPUT="$SCRIPT_DIR/output"
if [ -d "$SCRAPED_OUTPUT" ] && [ "$(ls -A "$SCRAPED_OUTPUT" 2>/dev/null)" ]; then
    export QWEN_SERVER_PORT=5000
    STAGE_LOGS[scraped-frameworks]="$LOG_DIR/scraped-frameworks.log"
    echo -e "  Starting: ${YELLOW}scraped-frameworks${NC}"
    # ingest_scraped_data.py now auto-discovers all frameworks when called without args
    (cd "$SCRIPT_DIR" && python3 ingest_scraped_data.py) > "${STAGE_LOGS[scraped-frameworks]}" 2>&1 &
    STAGE_PIDS[scraped-frameworks]=$!
else
    echo -e "  ${YELLOW}Skipping scraped frameworks — no output data found${NC}"
fi

echo ""
echo -e "${CYAN}All stages launched. Waiting for completion...${NC}"
echo ""

# ─── Wait for all stages and collect results ───
STAGES_OK=0
STAGES_FAIL=0

for stage in "${!STAGE_PIDS[@]}"; do
    pid=${STAGE_PIDS[$stage]}
    if wait "$pid"; then
        echo -e "  ${GREEN}✓ ${stage} completed${NC} (log: ${STAGE_LOGS[$stage]})"
        STAGES_OK=$((STAGES_OK + 1))
    else
        echo -e "  ${RED}✗ ${stage} failed${NC} — see ${STAGE_LOGS[$stage]}"
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
