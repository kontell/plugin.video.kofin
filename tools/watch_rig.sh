#!/usr/bin/env bash
# Per-minute visual + state snapshot of all four members during a run.
#
# P1D and PIERS share one X display, so an X-level grab cannot tell them apart.
# Each Kodi's own TakeScreenshot builtin writes to its own screenshot path
# (debug.screenshotpath differs per instance), so the EventServer is the only way
# to get one image per instance. Android members go through adb screencap.
set -u
OUT="${1:?usage: watch_rig.sh <outdir> [ticks]}"
TICKS="${2:-40}"
KD=/media/bluecon/dev/kodi-drive
mkdir -p "$OUT"

KD_REMOTE=/media/minipie/bluecon/dev/kodi-drive
shot_local() {   # name esport remote_dir ts
    local name=$1 esport=$2 dir=$3 ts=$4
    ssh -o BatchMode=yes conor@p1d "mkdir -p '$dir'; find '$dir' -name '*.png' -delete" 2>/dev/null
    # P1D has services.esallinterfaces=False: the EventServer only accepts
    # packets from localhost, so the builtin is fired ON the box, not at it.
    ssh -o BatchMode=yes conor@p1d \
        "KODI_HOST=127.0.0.1 KODI_ESPORT=$esport python3 $KD_REMOTE/bin/kodi-builtin TakeScreenshot" \
        >/dev/null 2>&1
    sleep 4
    local newest
    newest=$(ssh -o BatchMode=yes conor@p1d "ls -t '$dir'/*.png 2>/dev/null | head -1")
    if [ -z "$newest" ]; then
        # Known limitation: two Kodis share display :0, and the occluded one
        # has no renderable surface, so TakeScreenshot writes nothing. Verified
        # 2026-08-31: both EventServers listen (9777 localhost, 9778 all) and the
        # screenshot folder is correct, yet only the foreground instance yields a
        # PNG. The state line below still covers the hidden one.
        echo "$ts $name: no screenshot (occluded on the shared display)" >> "$OUT/state.log"
        return
    fi
    scp -q -o BatchMode=yes "conor@p1d:$newest" "$OUT/${ts}-${name}.png" 2>/dev/null
    ssh -o BatchMode=yes conor@p1d "rm -f '$newest'" 2>/dev/null
}

state() {        # host label
    python3 - "$1" "$2" <<'PY'
import base64, json, sys, urllib.request
host, name = sys.argv[1], sys.argv[2]
def call(m, p=None):
    r = urllib.request.Request("http://%s/jsonrpc" % host,
        data=json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p or {}}).encode(),
        headers={"Content-Type":"application/json",
                 "Authorization":"Basic "+base64.b64encode(b"kodi:kodi").decode()})
    with urllib.request.urlopen(r, timeout=8) as f:
        return json.loads(f.read().decode()).get("result")
try:
    pl = call("Player.GetActivePlayers")
    if not pl:
        print("%-7s IDLE" % name); raise SystemExit
    pid = pl[0]["playerid"]
    t = call("Player.GetProperties", {"playerid": pid, "properties": ["time","speed"]})["time"]
    it = call("Player.GetItem", {"playerid": pid, "properties": ["title"]})["item"]
    dlg = call("XBMC.GetInfoBooleans", {"booleans":["Window.IsActive(selectdialog)"]})["Window.IsActive(selectdialog)"]
    print("%-7s %-12s %02d:%02d.%03d speed=%s%s" % (name, (it.get("label") or "?")[:12],
          t["minutes"], t["seconds"], t["milliseconds"], call("Player.GetProperties",
          {"playerid": pid, "properties":["speed"]})["speed"], "  DIALOG!" if dlg else ""))
except Exception as e:
    print("%-7s ERR %s" % (name, e))
PY
}

for i in $(seq 1 "$TICKS"); do
    ts=$(date +%H%M%S)
    { echo "--- $ts ---"
      state 192.168.1.112:8080 P1D
      state 192.168.1.112:8081 PIERS
      state 192.168.1.198:8080 BRAVIA
      state 192.168.1.150:8080 TAB
    } >> "$OUT/state.log" 2>&1
    shot_local P1D   9777  /home/conor/tmp/kodi "$ts" &
    shot_local PIERS 9778  /home/conor/.var/app/tv.kodi.Kodi/data/temp "$ts" &
    adb -s 192.168.1.198:34793 exec-out screencap -p > "$OUT/${ts}-BRAVIA.png" 2>/dev/null &
    adb -s 192.168.1.150:35177 exec-out screencap -p > "$OUT/${ts}-TAB.png" 2>/dev/null &
    wait
    sleep 55
done
