#!/bin/bash
set -euo pipefail

# Count GitHub release-asset downloads for a repository.
#
# Usage: ./scripts/release-downloads.sh [owner/repo]
#
# With no argument, the repo is derived from the `origin` remote.
# Prints a per-release breakdown followed by the grand total.
#
# Requires: gh (authenticated), jq.
# Note: counts uploaded release assets only — not source archives or git clones.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ADDON_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

for tool in gh jq; do
  command -v "$tool" >/dev/null 2>&1 || { echo "error: '$tool' is required but not installed" >&2; exit 1; }
done

REPO="${1:-}"
if [[ -z "$REPO" ]]; then
  # Derive owner/repo from origin (handles git@github.com:owner/repo.git and https URLs).
  origin="$(git -C "$ADDON_DIR" remote get-url origin 2>/dev/null || true)"
  [[ -n "$origin" ]] || { echo "error: no 'origin' remote; pass owner/repo explicitly" >&2; exit 1; }
  REPO="$(printf '%s\n' "$origin" | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')"
fi

echo "Release downloads for $REPO"
echo "------------------------------------------"

# Per-release: tag + summed asset download counts. --paginate walks all release pages.
gh api --paginate "repos/$REPO/releases" \
  --jq '.[] | "\(.tag_name)\t\([.assets[].download_count] | add // 0)"' \
  | awk -F'\t' '{ printf "  %-12s %6d\n", $1, $2 }'

echo "------------------------------------------"
total="$(gh api --paginate "repos/$REPO/releases" --jq '[.[].assets[].download_count] | add // 0')"
printf "  %-12s %6d\n" "TOTAL" "$total"
