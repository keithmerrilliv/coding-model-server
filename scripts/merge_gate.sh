#!/usr/bin/env bash
# scripts/merge_gate.sh — DEV-773
#
# Merge a branch into main only when the MERGED tree passes ruff, mypy and
# pytest — the same three checks CI runs. Every earlier local gate was a
# hand-typed chain and each of its four documented misses (a `;`, an `||`
# arm, a pipe, and gating main-before-the-merge) came from retyping it.
#
# The trial merge and the checks happen in a temporary worktree, never in
# this checkout: this checkout is the editable install the running services
# import, so an ungated merge must not sit in it for even a second. main only
# moves once the gate is green, by fast-forward to the already-gated commit.
#
#   bash scripts/merge_gate.sh [--dry-run] [--no-push] <branch>
#
# --dry-run  run the checks on the merged tree, then discard the merge.
# --no-push  merge on green but do not push.
#
# Exit codes: 0 merged (or dry-run green), 1 gate red, 2 refused.
set -u

DRY_RUN=0
PUSH=1
BRANCH=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-push) PUSH=0 ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
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
MAIN_OLD="$(git rev-parse --verify --quiet refs/heads/main)" \
  || { echo "refused: no local branch named main" >&2; exit 2; }

LOG_DIR="${TMPDIR:-/tmp}/merge_gate"
mkdir -p "$LOG_DIR"
# Resolved, because git reports worktree paths resolved and the reaping below
# matches on them.
LOG_DIR="$(cd "$LOG_DIR" && pwd -P)" || exit 2
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUFF_LOG="$LOG_DIR/ruff_$STAMP.txt"
MYPY_LOG="$LOG_DIR/mypy_$STAMP.txt"
PYTEST_LOG="$LOG_DIR/pytest_$STAMP.txt"

# Untracked state of this checkout that the suite reads: the daemon loads
# .env at import, and the replay tests read the spec archive by a path
# relative to the working directory. Without them the suite still passes but
# those tests skip, so a guard regression on the archive would go unseen.
LIVE_ONLY=".env var/tasks_db/specs"

GATE_DIR=""
WT=""
CHECK_PID=""

# Remove one gate directory and the worktree inside it.
discard() {
  local dir="$1" tree="$2" rel
  # The links go first so that no recursive delete ever meets them.
  for rel in $LIVE_ONLY; do
    [ -L "$tree/$rel" ] && rm -f "$tree/$rel"
  done
  git worktree remove --force "$tree" >/dev/null 2>&1
  rm -rf "$dir"
}

# Runs on every exit, including the signal traps below. It only ever removes
# the temporary worktree: nothing in this checkout was changed on the way
# here, so there is nothing in it to restore.
cleanup() {
  if [ -n "$CHECK_PID" ]; then
    kill "$CHECK_PID" 2>/dev/null
    wait "$CHECK_PID" 2>/dev/null
  fi
  [ -n "$GATE_DIR" ] || return 0
  cd "$ROOT" || return 0
  discard "$GATE_DIR" "$WT"
  git worktree prune
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# The path of the worktree that has main checked out; empty when none has.
main_checkout() {
  local path="" line
  while IFS= read -r line; do
    case "$line" in
      "worktree "*) path="${line#worktree }" ;;
      "branch refs/heads/main") printf '%s\n' "$path"; return 0 ;;
    esac
  done < <(git worktree list --porcelain)
}

refuse_foreign_main() {
  echo "refused: main is checked out in another worktree ($1) — advancing main" >&2
  echo "  from here would leave that tree behind its own HEAD. Run the gate there." >&2
}

# A hard kill (SIGKILL, power loss) skips the trap. Reap what such a run left
# behind: a gate directory is named after the PID that owns it, so one whose
# owner is gone is garbage. One whose directory is gone too is pruned.
git worktree prune
while IFS= read -r line; do
  case "$line" in
    "worktree $LOG_DIR/wt."*/*)
      stale="${line#worktree }"
      owner="${stale#"$LOG_DIR"/wt.}"
      stale_dir="$LOG_DIR/wt.${owner%%/*}"
      owner="${owner%%.*}"
      case "$owner" in
        ''|*[!0-9]*) ;;
        *) kill -0 "$owner" 2>/dev/null || discard "$stale_dir" "$stale" ;;
      esac ;;
  esac
done < <(git worktree list --porcelain)

MAIN_WT="$(main_checkout)"
if [ "$DRY_RUN" -eq 0 ] && [ -n "$MAIN_WT" ] && [ "$MAIN_WT" != "$ROOT" ]; then
  refuse_foreign_main "$MAIN_WT"
  exit 2
fi

# Throwaway merge: the temporary worktree becomes the merged tree, nothing is
# committed until the gate says so. The message is given explicitly because a
# detached HEAD would otherwise be named in it ("... into HEAD").
# The worktree keeps this checkout's directory name, one level down in a
# directory unique to this run: the pipeline recognises a spec that targets
# this repository by that name, and the suite should see the name it sees in
# the checkout.
GATE_DIR="$(mktemp -d "$LOG_DIR/wt.$$.XXXXXX")" || { echo "refused: cannot create a temporary directory under $LOG_DIR" >&2; exit 2; }
WT="$GATE_DIR/$(basename "$ROOT")"
git worktree add --quiet --detach "$WT" "$MAIN_OLD" \
  || { echo "refused: cannot create a temporary worktree at $WT" >&2; exit 2; }
WT_REAL="$(cd "$WT" && pwd -P)" || exit 2

if ! git -C "$WT" merge --no-ff --no-commit --quiet -m "Merge branch '$BRANCH'" "$BRANCH"; then
  echo "refused: merge of $BRANCH into main does not apply cleanly" >&2
  exit 2
fi
if git -C "$WT" diff --cached --quiet; then
  echo "nothing to merge: $BRANCH is already in main"
  exit 0
fi

# Paths in the checkout holding main that the fast-forward would have to
# overwrite: tracked files with staged or unstaged changes, and untracked
# files, restricted to the paths the merge changes. Everything else in that
# checkout survives a fast-forward untouched and cannot reach the merge
# commit, so it is no reason to refuse.
collisions() {
  local changed dirty
  changed="$(git -C "$WT" -c core.quotePath=false diff --cached --name-only "$MAIN_OLD")"
  dirty="$(git -C "$ROOT" -c core.quotePath=false diff --name-only HEAD;
           git -C "$ROOT" -c core.quotePath=false ls-files --others --exclude-standard)"
  [ -n "$changed" ] && [ -n "$dirty" ] || return 0
  grep -Fx -f <(printf '%s\n' "$changed") <(printf '%s\n' "$dirty")
}

if [ "$DRY_RUN" -eq 0 ] && [ "$MAIN_WT" = "$ROOT" ]; then
  COLLIDING="$(collisions)"
  if [ -n "$COLLIDING" ]; then
    echo "refused: local changes to files this merge would overwrite — commit, stash or wait for the other session" >&2
    printf '%s\n' "$COLLIDING" | sed 's/^/  /' >&2
    exit 2
  fi
fi

# The venv's editable install points at THIS checkout's src, so without help
# every check would import main-before-the-merge and report on the wrong
# tree. PYTHONPATH outranks the install; prove it took before trusting a
# result.
export PYTHONPATH="$WT_REAL/src:$WT_REAL"
cd "$WT_REAL" || exit 2
IMPORTED="$("$PY" -c 'import os, coding_model_autonomous as m; print(os.path.realpath(m.__file__))' 2>&1)"
case "$IMPORTED" in
  "$WT_REAL"/*) ;;
  *) echo "refused: the checks would not import the merged tree" >&2
     echo "  coding_model_autonomous resolves to: $IMPORTED" >&2
     echo "  expected under: $WT_REAL" >&2
     exit 2 ;;
esac

# A check runs in the background so that the shell is in `wait`, which a
# signal interrupts at once; with the check in the foreground the trap would
# not run until the check had finished, minutes later. Bash starts a
# background command with SIGINT and SIGQUIT ignored, and the suite's own
# subprocesses would inherit that, so the check gets the defaults back.
run_check() {
  local log="$1" rc
  shift
  ( trap - INT QUIT; exec "$@" ) > "$log" 2>&1 &
  CHECK_PID=$!
  wait "$CHECK_PID"
  rc=$?
  CHECK_PID=""
  return "$rc"
}

# Each check writes to its own file and its OWN exit code is captured.
# No pipes, no `||` arms, no `;` between a check and the decision.
run_check "$RUFF_LOG" "$PY" -m ruff check .
RUFF_RC=$?
run_check "$MYPY_LOG" "$PY" -m mypy
MYPY_RC=$?
for rel in $LIVE_ONLY; do
  if [ -e "$ROOT/$rel" ] && [ ! -e "$WT_REAL/$rel" ]; then
    mkdir -p "$(dirname "$WT_REAL/$rel")"
    ln -s "$ROOT/$rel" "$WT_REAL/$rel"
  fi
done
# `network` tests (a real npm install) are CI's job: here they cost ~20 s and
# their timing depends on the registry, not on the change (DEV-836).
# Eight xdist workers: measured on this box, 16 is no faster, because the
# real-sandbox and subprocess tests are the long poles, not the core count.
run_check "$PYTEST_LOG" "$PY" -m pytest -q -m "not network" -n 8
PYTEST_RC=$?
cd "$ROOT" || exit 2

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
  echo "NOT MERGED: gate red"
  exit 1
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY RUN: gate green, trial merge discarded, main untouched"
  exit 0
fi

# The commit takes the index, which is exactly the merge the checks ran on;
# anything a test wrote into the temporary worktree stays out of it.
git -C "$WT" commit --quiet --no-edit || { echo "merge commit failed" >&2; exit 1; }
NEW="$(git -C "$WT" rev-parse HEAD)"

# The gated commit merges the main the gate started from. If main has moved
# since, the tree that was checked is not the tree main would get.
MAIN_NOW="$(git rev-parse --verify --quiet refs/heads/main)"
if [ "$MAIN_NOW" != "$MAIN_OLD" ]; then
  echo "NOT MERGED: main moved during the gate (${MAIN_OLD:0:9} -> ${MAIN_NOW:0:9}); run the gate again" >&2
  exit 2
fi

# main advances only by fast-forward to the gated commit, so this checkout
# goes from one committed tree to another and is never half-merged. Where
# main is not checked out no working tree follows it, and the ref alone
# moves, by compare-and-swap against the main the gate started from.
PARK_REF="refs/merge-gate/$BRANCH"
MAIN_WT="$(main_checkout)"
if [ "$MAIN_WT" = "$ROOT" ]; then
  FF_LOG="$LOG_DIR/ff_$STAMP.txt"
  git merge --ff-only --quiet "$NEW" > "$FF_LOG" 2>&1
  FF_RC=$?
  if [ "$FF_RC" -ne 0 ]; then
    # Git checks every path before it writes one, so a refused fast-forward
    # has changed nothing here. Keep the gated commit reachable: the
    # temporary worktree that holds it is about to go.
    git update-ref "$PARK_REF" "$NEW"
    echo "NOT MERGED: gate green, but this checkout cannot fast-forward to the gated commit:" >&2
    sed 's/^/    /' "$FF_LOG" >&2
    echo "  the commit is kept at $PARK_REF (${NEW:0:9}). Commit or stash the files" >&2
    echo "  named above, then finish with: git merge --ff-only $PARK_REF" >&2
    exit 2
  fi
elif [ -n "$MAIN_WT" ]; then
  git update-ref "$PARK_REF" "$NEW"
  refuse_foreign_main "$MAIN_WT"
  echo "  the gated commit is kept at $PARK_REF (${NEW:0:9}); finish there with:" >&2
  echo "  git merge --ff-only $PARK_REF" >&2
  exit 2
else
  if ! git update-ref -m "merge_gate: merge $BRANCH" refs/heads/main "$NEW" "$MAIN_OLD"; then
    echo "NOT MERGED: main moved during the gate; run the gate again" >&2
    exit 2
  fi
fi
git update-ref -d "$PARK_REF" 2>/dev/null

echo "MERGED: $(git log -1 --format='%h %s' "$NEW")"
if [ "$PUSH" -eq 1 ]; then
  git push --quiet origin main && echo "PUSHED origin/main" || { echo "push failed — main is merged locally" >&2; exit 1; }
fi
exit 0
