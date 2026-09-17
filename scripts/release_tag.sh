#!/usr/bin/env bash
# Tag a release from CHANGELOG.md and publish it on GitHub.
#
#   bash scripts/release_tag.sh v0.2.0            # dry run: checks + shows what would happen
#   bash scripts/release_tag.sh v0.2.0 --execute  # date the changelog heading, commit, tag, push, gh release
#
# Refuses unless: on main, clean tree, in sync with origin/main, CI green for HEAD,
# and CHANGELOG.md has a "## <version> — unreleased" heading. The visibility flip
# of the repository is not this script's business.
set -euo pipefail

VERSION="${1:-}"
MODE="${2:-dry-run}"
[[ -n "$VERSION" ]] || { echo "usage: $0 vX.Y.Z [--execute]" >&2; exit 2; }
[[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must look like v0.2.0" >&2; exit 2; }

cd "$(git rev-parse --show-toplevel)"
TODAY="$(date +%F)"
NOTES="$(mktemp)"; trap 'rm -f "$NOTES"' EXIT

fail() { echo "REFUSED: $*" >&2; exit 1; }

# --- preconditions -----------------------------------------------------------
[[ "$(git branch --show-current)" == "main" ]] || fail "not on main"
[[ -z "$(git status --porcelain)" ]] || fail "working tree is not clean"
git fetch -q origin
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || fail "main is not in sync with origin/main"
git rev-parse -q --verify "refs/tags/$VERSION" >/dev/null && fail "tag $VERSION already exists"

CI_STATUS="$(gh run list --branch main --commit "$(git rev-parse HEAD)" --limit 1 --json conclusion,status --jq '.[0] | .status + "/" + (.conclusion // "")' 2>/dev/null || echo "unknown")"
[[ "$CI_STATUS" == "completed/success" ]] || fail "CI for HEAD is '$CI_STATUS', not completed/success"

HEADING_RE="^## ${VERSION//./\\.} — unreleased"
grep -qE "$HEADING_RE" CHANGELOG.md || fail "CHANGELOG.md has no '## $VERSION — unreleased' heading"

# --- release notes = the changelog section for this version ------------------
awk -v re="$HEADING_RE" '
  $0 ~ re { on=1; next }
  on && /^## / { exit }
  on { print }
' CHANGELOG.md > "$NOTES"
[[ -s "$NOTES" ]] || fail "extracted release notes are empty"

echo "== $VERSION — preconditions pass (HEAD $(git rev-parse --short HEAD), CI $CI_STATUS)"
echo "== release notes: $(wc -l < "$NOTES") lines, $(grep -c '^- \[DEV-' "$NOTES") ticket lines"

if [[ "$MODE" != "--execute" ]]; then
  echo "== DRY RUN. Would:"
  echo "   1. sed CHANGELOG.md heading -> '## $VERSION — $TODAY'"
  echo "   2. git commit -m 'DEV-671: $VERSION release notes dated'"
  echo "   3. git tag -a $VERSION -F <notes>"
  echo "   4. git push origin main $VERSION"
  echo "   5. gh release create $VERSION --title $VERSION -F <notes>"
  echo "   Re-run with --execute to do it."
  exit 0
fi

# --- execute -----------------------------------------------------------------
sed -i -E "s/${HEADING_RE}.*/## $VERSION — $TODAY/" CHANGELOG.md
git add CHANGELOG.md
git commit -q -m "DEV-671: $VERSION release notes dated $TODAY"
git tag -a "$VERSION" -F "$NOTES"
git push -q origin main "$VERSION"
gh release create "$VERSION" --title "$VERSION" -F "$NOTES"
echo "== $VERSION tagged at $(git rev-parse --short HEAD) and released."
