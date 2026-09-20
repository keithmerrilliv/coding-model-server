#!/usr/bin/env bash
# scripts/merge_gate.sh — DEV-773
#
# Merge a branch into main only when the MERGED tree passes ruff, mypy and
# pytest — the same three checks CI runs. Every earlier local gate was a
# hand-typed chain and each of its four documented misses (a `;`, an `||`
# arm, a pipe, and gating main-before-the-merge) came from retyping it.
#
#   bash scripts/merge_gate.sh [--dry-run] [--no-push] <branch>
#
# --dry-run  run the checks on the merged tree, then abort the merge.
# --no-push  merge on green but do not push.
#
# Exit codes: 0 merged (or dry-run green), 1 gate red, 2 refused to start.
set -u

DRY_RUN=0
PUSH=1
BRANCH=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-push) PUSH=0 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    -*) echo "unknown flag: $arg" >&2; exit 2 ;;
    *) BRANCH="$arg" ;;
  esac
done

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
cd "$ROOT" || exit 2
PY="$ROOT/venv/bin/python"

[ -n "$BRANCH" ] || { echo "usage: $0 [--dry-run] [--no-push] <branch>" >&2; exit 2; }
[ "$BRANCH" != "main" ] || { echo "refused: the branch to merge cannot be main" >&2; exit 2; }
git rev-parse --verify --quiet "refs/heads/$BRANCH" >/dev/null \
  || { echo "refused: no local branch named $BRANCH" >&2; exit 2; }
[ -x "$PY" ] || { echo "refused: $PY is not executable" >&2; exit 2; }

# A dirty tree means the number would describe a tree that never existed
# (another session may be editing underneath us). Refuse rather than guess.
if [ -n "$(git status --porcelain)" ]; then
  echo "refused: working tree is not clean — commit, stash or wait for the other session" >&2
  git status --short >&2
  exit 2
fi

START_BRANCH="$(git branch --show-current)"
git checkout --quiet main || exit 2

# Throwaway merge: the working tree becomes the merged tree, nothing is
# committed until the gate says so.
if ! git merge --no-ff --no-commit --quiet "$BRANCH"; then
  echo "refused: merge of $BRANCH into main does not apply cleanly" >&2
  git merge --abort 2>/dev/null
  git checkout --quiet "$START_BRANCH"
  exit 2
fi
if git diff --cached --quiet; then
  echo "nothing to merge: $BRANCH is already in main"
  git merge --abort 2>/dev/null
  git checkout --quiet "$START_BRANCH"
  exit 0
fi

LOG_DIR="${TMPDIR:-/tmp}/merge_gate"
mkdir -p "$LOG_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUFF_LOG="$LOG_DIR/ruff_$STAMP.txt"
MYPY_LOG="$LOG_DIR/mypy_$STAMP.txt"
PYTEST_LOG="$LOG_DIR/pytest_$STAMP.txt"

# Each check writes to its own file and its OWN exit code is captured.
# No pipes, no `||` arms, no `;` between a check and the decision.
"$PY" -m ruff check . > "$RUFF_LOG" 2>&1
RUFF_RC=$?
"$PY" -m mypy > "$MYPY_LOG" 2>&1
MYPY_RC=$?
"$PY" -m pytest -q > "$PYTEST_LOG" 2>&1
PYTEST_RC=$?

SUMMARY="$(grep -E '^(=+ )?[0-9]+ (passed|failed)' "$PYTEST_LOG" | tail -1)"
[ -n "$SUMMARY" ] || SUMMARY="$(tail -1 "$PYTEST_LOG")"

OK=1
[ "$RUFF_RC" -eq 0 ] || OK=0
[ "$MYPY_RC" -eq 0 ] || OK=0
[ "$PYTEST_RC" -eq 0 ] || OK=0

echo "GATE ok=$OK ruff=$RUFF_RC mypy=$MYPY_RC pytest=$PYTEST_RC branch=$BRANCH"
echo "  pytest: $SUMMARY"
[ "$RUFF_RC" -eq 0 ] || { echo "  ruff:"; tail -20 "$RUFF_LOG" | sed 's/^/    /'; }
[ "$MYPY_RC" -eq 0 ] || { echo "  mypy:"; tail -20 "$MYPY_LOG" | sed 's/^/    /'; }
[ "$PYTEST_RC" -eq 0 ] || { echo "  pytest tail:"; tail -20 "$PYTEST_LOG" | sed 's/^/    /'; }
echo "  logs: $RUFF_LOG $MYPY_LOG $PYTEST_LOG"

if [ "$OK" -ne 1 ]; then
  git merge --abort 2>/dev/null
  git checkout --quiet "$START_BRANCH"
  echo "NOT MERGED: gate red"
  exit 1
fi

if [ "$DRY_RUN" -eq 1 ]; then
  git merge --abort 2>/dev/null
  git checkout --quiet "$START_BRANCH"
  echo "DRY RUN: gate green, merge aborted, main untouched"
  exit 0
fi

git commit --quiet --no-edit || { echo "merge commit failed" >&2; exit 1; }
echo "MERGED: $(git log -1 --format='%h %s')"
if [ "$PUSH" -eq 1 ]; then
  git push --quiet origin main && echo "PUSHED origin/main" || { echo "push failed — main is merged locally" >&2; exit 1; }
fi
exit 0
