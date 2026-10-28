#!/bin/bash
# Stage a tree and push it, with the device-side scripts, to the bench
# device. The staged build is installed by box/bench.sh.
#   KOFIN_BENCH_HOST=root@<device> tests/live/or_bench/deploy.sh [TREE]
set -euo pipefail
: "${KOFIN_BENCH_HOST:?set KOFIN_BENCH_HOST to user@device}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="${1:-$(cd "$HERE/../../.." && pwd)}"
DIR="${KOFIN_BENCH_DIR:-/storage/kofin-bench}"
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
python3 "$SRC/tools/build.py" --stage "$T/a" --profile api
tar czf "$T/stage.tgz" -C "$T/a" .
"$HERE/host.sh" "mkdir -p $DIR/runs $DIR/tmp"
tar czf - -C "$HERE/box" . | "$HERE/host.sh" "tar xzf - -C $DIR"
"$HERE/host.sh" "cat > $DIR/stage.tmp && mv $DIR/stage.tmp $DIR/stage.tgz" < "$T/stage.tgz"
echo "staged $(git -C "$SRC" rev-parse --short HEAD 2>/dev/null)$(git -C "$SRC" diff --quiet 2>/dev/null || echo +dirty) to $KOFIN_BENCH_HOST:$DIR"
