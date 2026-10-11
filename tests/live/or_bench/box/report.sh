#!/bin/sh
# Runs ON the box: the phase lines of a run. report.sh <label>
R="${KOFIN_BENCH_DIR:-/storage/kofin-bench}/runs/$1"
L=$R/kodi.log
T0=$(cat $R/t0)
python3 - "$L" "$T0" <<'PY'
import re, sys, time, datetime
log, t0 = sys.argv[1], int(sys.argv[2])
pat = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.(\d{3}) T:(\d+)\s+(\w+) <general>: (.*)")
keep = re.compile(r"kofin service|kofin.sync.backends.api.(library|native|patch|removal|readback|store|progress)|API |MusicInfoScanner|CMusicInfoScanner|music scan|Finished scan|Starting scan|My Music: Scanning|ReloadSkin|Traceback|Error|EXCEPTION")
drop = re.compile(r"patching \d+ audio rows|http|HTTP")
first = None
for line in open(log, encoding="utf-8", errors="replace"):
    m = pat.match(line)
    if not m: continue
    msg = m.group(5)
    if not keep.search(msg) or drop.search(msg): continue
    if m.group(4) == "debug" and "kofin" not in msg and "Scanner" not in msg: continue
    ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) + int(m.group(2)) / 1000
    msg = re.sub(r"[0-9a-f]{32}", lambda g: g.group(0)[:6] + "…", msg)
    print("%7.1f %-5s %s" % (ts - t0, m.group(4)[:5], msg[:200]))
PY
