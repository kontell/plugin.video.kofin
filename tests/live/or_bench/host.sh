#!/bin/sh
# Run a command on the bench device through one persistent ssh master, so a
# session already authenticated survives the device dropping its storage
# (seen once on the Pi 3's SD card: sshd then refuses new logins).
#   KOFIN_BENCH_HOST=root@<device> tests/live/or_bench/host.sh '<command>'
: "${KOFIN_BENCH_HOST:?set KOFIN_BENCH_HOST to user@device}"
S="${TMPDIR:-/tmp}/kofin-bench-$(id -u).sock"
if ! ssh -S "$S" -O check "$KOFIN_BENCH_HOST" 2>/dev/null; then
  ssh -M -S "$S" -o ControlPersist=yes -o BatchMode=yes -o ConnectTimeout=8 \
    -o ServerAliveInterval=15 -o ServerAliveCountMax=8 -fN "$KOFIN_BENCH_HOST" || exit 255
fi
exec ssh -S "$S" -o BatchMode=yes "$KOFIN_BENCH_HOST" "$@"
