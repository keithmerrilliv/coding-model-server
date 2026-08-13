#!/usr/bin/env bash
# measure_model_load.sh — DEV-105
#
# Quantify the two numbers that decide the RAM-caching strategy for model swaps:
#
#   Part A (always):  SSD (cold) vs page-cache (warm) GGUF read time.
#                     -> how much of the ~31s model load is pure SSD->RAM transfer,
#                        i.e. the part that keeping models warm in RAM actually removes.
#
#   Part B (--infer): decode tok/s with --mmap vs --no-mmap on the same model.
#                     -> the real cost of the loader's expert-offload warning
#                        ("tensor overrides to CPU are used with mmap enabled —
#                         consider using --no-mmap for better performance").
#
# SAFE BY DEFAULT: Part A evicts ONLY the target file's pages (vmtouch -e /
# posix_fadvise DONTNEED) — it never calls `drop_caches`, so it will not disturb
# the model the server currently has loaded. Rootless.
#
# Usage:
#   scripts/measure_model_load.sh <gguf-file|first-shard|model-dir>
#   scripts/measure_model_load.sh <...> --infer --ngl 41 --n-cpu-moe 20
#
# Options:
#   --infer              also run Part B (needs a free GPU — see the VRAM warning)
#   --ngl N              llama-server -ngl for Part B (REQUIRED with --infer)
#   --n-cpu-moe N        llama-server --n-cpu-moe for Part B (match config.py for the model)
#   --llama-bin PATH     llama-server binary (default: tools/llama-server)
#   --port N             scratch port for Part B (default: 5099)
#   --n-predict N        decode tokens to time in Part B (default: 128)
#
# Note: for a more rigorous tok/s via the running service, scripts/benchmark_decode.py
# already warms up and takes a median — but it goes through the service, which is
# hard-wired to --mmap, so it cannot A/B the flag. That is why Part B launches its own
# llama-server.
set -uo pipefail

GGUF=""; DO_INFER=0; NGL=""; NCM=""; LLAMA_BIN="tools/llama-server"
PORT=5099; NPRED=128

usage() { sed -n '2,40p' "$0"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --infer)      DO_INFER=1 ;;
    --ngl)        NGL="${2:-}"; shift ;;
    --n-cpu-moe)  NCM="${2:-}"; shift ;;
    --llama-bin)  LLAMA_BIN="${2:-}"; shift ;;
    --port)       PORT="${2:-}"; shift ;;
    --n-predict)  NPRED="${2:-}"; shift ;;
    -h|--help)    usage; exit 0 ;;
    -*)           echo "unknown option: $1" >&2; usage; exit 1 ;;
    *)            GGUF="$1" ;;
  esac
  shift
done
[[ -z "$GGUF" ]] && { usage; exit 1; }

# ---- resolve the shard set ----------------------------------------------------
# A model may be one .gguf or a NNNNN-of-MMMMM shard set. Accept a file, a first
# shard (expand to all siblings), or a directory (all *.gguf inside).
resolve_shards() {
  local p="$1"
  if [[ -d "$p" ]]; then
    find "$p" -maxdepth 1 -name '*.gguf' | sort
  elif [[ "$p" =~ -[0-9]{5}-of-[0-9]{5}\.gguf$ ]]; then
    local dir base
    dir="$(dirname "$p")"
    base="$(basename "$p" | sed -E 's/-[0-9]{5}-of-[0-9]{5}\.gguf$//')"
    find "$dir" -maxdepth 1 -name "${base}-[0-9][0-9][0-9][0-9][0-9]-of-*.gguf" | sort
  else
    printf '%s\n' "$p"
  fi
}

mapfile -t SHARDS < <(resolve_shards "$GGUF")
[[ ${#SHARDS[@]} -eq 0 || ! -f "${SHARDS[0]}" ]] && { echo "no gguf found at: $GGUF" >&2; exit 1; }
FIRST="${SHARDS[0]}"
TOTAL_BYTES=$(du -cb "${SHARDS[@]}" 2>/dev/null | tail -1 | cut -f1)

echo "model:  ${SHARDS[0]}"
[[ ${#SHARDS[@]} -gt 1 ]] && echo "shards: ${#SHARDS[@]}"
awk -v b="$TOTAL_BYTES" 'BEGIN{printf "size:   %.1f GiB\n", b/1073741824}'

# Guard: if the file is mapped by a live process (the currently-loaded model),
# a cold read is meaningless — mapped pages will not evict.
if grep -qlF "$FIRST" /proc/*/maps 2>/dev/null; then
  echo "WARNING: this GGUF is mapped by a running process (it is the currently-loaded"
  echo "         model). Cold-read eviction will be ineffective. Pick a model that is"
  echo "         NOT loaded for a meaningful cold-vs-warm number."
fi
echo ""

timed_read() {  # read every shard start-to-end; echo elapsed seconds
  local start end s
  start=$(date +%s.%N)
  for s in "${SHARDS[@]}"; do dd if="$s" of=/dev/null bs=8M 2>/dev/null; done
  end=$(date +%s.%N)
  awk -v a="$start" -v b="$end" 'BEGIN{printf "%.2f", b-a}'
}

# ---- Part A: cold vs warm read ------------------------------------------------
echo "=== Part A: cold (SSD) vs warm (page cache) read ==="
if command -v vmtouch >/dev/null 2>&1; then
  vmtouch -qe "${SHARDS[@]}" >/dev/null 2>&1     # evict ONLY these files
  COLD=$(timed_read)                              # faults from SSD (also re-warms)
  WARM=$(timed_read)                              # served from page cache
  awk -v c="$COLD" -v w="$WARM" -v g="$TOTAL_BYTES" 'BEGIN{
    printf "  cold (SSD):    %7.2f s   %6.0f MB/s\n", c, g/1048576/c;
    printf "  warm (cache):  %7.2f s   %6.0f MB/s\n", w, g/1048576/w;
    if (w>0) printf "  per-load win:  %7.2f s   (%.1fx faster warm)\n", c-w, c/w;
    print  "";
    print  "  Interpretation: the cold time is the SSD->RAM share of the load. If it is a";
    print  "  large fraction of the observed ~31s model load, pinning this model in page";
    print  "  cache (vmtouch -tl) removes it. If small, the load cost is elsewhere";
    print  "  (parse/init, VRAM upload, swap-drain) and RAM caching will not help much.";
  }'
else
  echo "  vmtouch not installed — cannot evict to get a clean COLD read."
  echo "  install: sudo apt-get install -y vmtouch   (Debian/Ubuntu), then re-run."
  echo "  (falling back to a warm-only read as a lower bound)"
  WARM=$(timed_read)
  awk -v w="$WARM" -v g="$TOTAL_BYTES" 'BEGIN{printf "  warm read: %.2f s (%.0f MB/s)\n", w, g/1048576/w}'
fi

# ---- Part B: --mmap vs --no-mmap decode tok/s ---------------------------------
if [[ "$DO_INFER" == "1" ]]; then
  echo ""
  echo "=== Part B: inference --mmap vs --no-mmap ==="
  if [[ -z "$NGL" ]]; then
    echo "  --infer requires --ngl N (and usually --n-cpu-moe N) matching this model's" >&2
    echo "  config.py entry, or llama-server will mis-place layers and may OOM VRAM." >&2
    exit 1
  fi
  if [[ ! -x "$LLAMA_BIN" ]]; then
    echo "  llama-server not executable at: $LLAMA_BIN (use --llama-bin PATH)" >&2
    exit 1
  fi
  # VRAM contention guard (DEV-388): a resident model in the main service means this
  # second llama-server can OOM the card. The admin endpoint is auth'd, so send the
  # key (X-Admin-Key, as the bundled client does) — without it the request 401s and
  # this warning silently never fires. On any inconclusive result (401, unparseable
  # body, error) warn rather than assume idle: the guard exists to prevent an OOM, so
  # it must fail safe. Snapshot keys are running/model_path/pid (not "model"/"active").
  nl=$'\n'
  am_resp=$(curl -s -w "${nl}%{http_code}" -H "X-Admin-Key: ${ADMIN_API_KEY:-}" \
    "http://localhost:5000/v1/admin/active_model" 2>/dev/null)
  am_code=${am_resp##*"$nl"}
  am_body=${am_resp%"$nl"*}
  am_state="unknown"
  if [[ "$am_code" == "200" ]]; then
    am_state=$(printf '%s' "$am_body" | python3 -c 'import json,sys
try: s=json.load(sys.stdin)
except Exception: print("unknown"); sys.exit()
print("busy" if (s.get("running") or s.get("pid") is not None) else "idle")' 2>/dev/null || echo unknown)
  elif [[ "$am_code" == "000" ]]; then
    am_state="idle"   # main server unreachable — it holds no VRAM, so no contention
  fi
  if [[ "$am_state" == "busy" ]]; then
    echo "  WARNING: the main server has a model loaded — Part B may OOM the GPU." >&2
    echo "           Run this when the service is idle, or stop it first." >&2
  elif [[ "$am_state" == "unknown" ]]; then
    echo "  WARNING: could not read the main server's model state (HTTP ${am_code}; is ADMIN_API_KEY set?) —" >&2
    echo "           assuming it may be busy. Run when the service is idle, or stop it first." >&2
  fi

  # Warm the cache once so both runs start from RAM — isolates inference from SSD.
  command -v vmtouch >/dev/null 2>&1 && vmtouch -qt "${SHARDS[@]}" >/dev/null 2>&1

  PROMPT="Explain, in two sentences, why memory-mapped model loading interacts with CPU tensor offload."
  for FLAG in --mmap --no-mmap; do
    echo "--- $FLAG ---"
    args=( -m "$FIRST" --host 127.0.0.1 --port "$PORT" -c 4096 -ngl "$NGL" "$FLAG" )
    [[ -n "$NCM" ]] && args+=( --n-cpu-moe "$NCM" )
    log="$(mktemp /tmp/measure_ll.XXXXXX.log)"
    lstart=$(date +%s.%N)
    "$LLAMA_BIN" "${args[@]}" >"$log" 2>&1 &
    pid=$!
    ready=0
    for _ in $(seq 1 300); do
      if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then ready=1; break; fi
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    lend=$(date +%s.%N)
    if [[ "$ready" != "1" ]]; then
      echo "  server failed to become ready — last log lines:"; tail -8 "$log" | sed 's/^/    /'
      kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; rm -f "$log"; continue
    fi
    resp=$(curl -sf "http://127.0.0.1:$PORT/completion" -H 'Content-Type: application/json' \
      -d "{\"prompt\":$(printf '%s' "$PROMPT" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'),\"n_predict\":$NPRED,\"temperature\":0,\"cache_prompt\":false}" 2>/dev/null)
    tps=$(printf '%s' "$resp" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); t=d["timings"]
    print(f"{t[\"predicted_per_second\"]:.2f} tok/s  (decoded {t[\"predicted_n\"]} in {t[\"predicted_ms\"]:.0f} ms)")
except Exception as e:
    print("parse-failed:", e)' 2>/dev/null || echo "n/a")
    awk -v a="$lstart" -v b="$lend" 'BEGIN{printf "  load-to-ready: %.1f s\n", b-a}'
    echo "  decode:        $tps"
    kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; rm -f "$log"
  done
  echo ""
  echo "  If --no-mmap decodes materially faster, that is the offload warning made real;"
  echo "  weigh it against --no-mmap's ~2x RAM (private copy) and cold-load cost, which a"
  echo "  pre-warmed page cache (Part A) largely neutralizes."
fi
