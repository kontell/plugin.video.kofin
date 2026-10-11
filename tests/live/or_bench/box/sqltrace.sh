#!/bin/sh
# Runs ON the device: switch Kodi's database log component on for N seconds
# and save the log lines written meanwhile. Both debug settings go back to
# what they were, also when the wait is interrupted; one that could not be
# put back is named and the exit status is 1. KODI_RPC may move the port,
# never the host: the web server's credentials only go to this device.
#   sqltrace.sh <out-file> [seconds]
OUT="$1"; SECS="${2:-45}"
[ -n "$OUT" ] || { echo "usage: sqltrace.sh <out-file> [seconds]"; exit 2; }
exec python3 - "$OUT" "$SECS" "${KODI_HOME:-/storage/.kodi}" "${KODI_RPC:-localhost:8080}" <<'PY'
import base64, http.client, json, os, re, signal, sys, time
import xml.etree.ElementTree as ET

out, seconds, home = sys.argv[1], float(sys.argv[2]), sys.argv[3]
host, _, port = sys.argv[4].rpartition(":")
host = (host or sys.argv[4]).strip("[]")
# The password below goes out in a Basic header over plain HTTP, and it is
# this device's: nowhere but the loopback interface has a use for it.
if host not in ("localhost", "127.0.0.1", "::1"):
    sys.exit("sqltrace.sh: KODI_RPC must name this device (localhost[:port]), not %r" % host)
LOG = home + "/temp/kodi.log"
DATABASE = 131072  # LOGDATABASE
# Read as XML, so an escaped character in either value is the character,
# and the user name is whatever the profile has set.
gui = {
    node.get("id"): node.text or ""
    for node in ET.parse(home + "/userdata/guisettings.xml").getroot().iter("setting")
}
user = gui.get("services.webserverusername") or "kodi"
password = gui.get("services.webserverpassword", "")
auth = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()


def rpc(method, params):
    conn = http.client.HTTPConnection(host, int(port or 8080), timeout=20)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    conn.request("POST", "/jsonrpc", body, {"Content-Type": "application/json", "Authorization": "Basic " + auth})
    reply = json.loads(conn.getresponse().read())
    conn.close()
    if "error" in reply:
        raise RuntimeError("%s: %s" % (method, reply["error"]))
    return reply["result"]


def added(inode, offset):
    """The log from ``offset`` on, a line at a time. Never the whole file:
    a debug log runs to hundreds of megabytes on a device with one gigabyte.
    A Kodi restarted meanwhile moved the log aside and began another, which
    is read from its start."""
    with open(LOG, "rb") as log:
        now = os.fstat(log.fileno())
        if now.st_ino != inode or now.st_size < offset:
            offset = 0
        log.seek(offset)
        for raw in log:
            yield raw.decode("utf-8", errors="replace")


names = ("debug.setextraloglevel", "debug.extralogging")
before = {name: rpc("Settings.GetSettingValue", {"setting": name})["value"] for name in names}
signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
began = os.stat(LOG)
unrestored = []
try:
    levels = sorted(set(before["debug.setextraloglevel"] or []) | {DATABASE})
    rpc("Settings.SetSettingValue", {"setting": "debug.setextraloglevel", "value": levels})
    rpc("Settings.SetSettingValue", {"setting": "debug.extralogging", "value": True})
    time.sleep(seconds)
finally:
    # Each on its own: one that fails must not leave the other switched on.
    for name in reversed(names):
        try:
            rpc("Settings.SetSettingValue", {"setting": name, "value": before[name]})
        except Exception as error:
            unrestored.append("%s (was %r): %s" % (name, before[name], error))
    if unrestored:
        # Said here, so it is said however the wait above ended.
        print("sqltrace.sh: NOT RESTORED, set by hand: " + "; ".join(unrestored), file=sys.stderr)
kept = 0
with open(out, "w", encoding="utf-8") as target:
    for line in added(began.st_ino, began.st_size):
        target.write(re.sub(r"api_key=[0-9a-f]+", "api_key=X", line))
        kept += 1
print(kept, out)
sys.exit(1 if unrestored else 0)
PY
