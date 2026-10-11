#!/bin/sh
# Runs ON the device: switch Kodi's database log component on for N seconds
# and save the log lines written meanwhile. Both debug settings go back to
# what they were, also when the wait is interrupted.
#   sqltrace.sh <out-file> [seconds]
OUT="$1"; SECS="${2:-45}"
[ -n "$OUT" ] || { echo "usage: sqltrace.sh <out-file> [seconds]"; exit 2; }
exec python3 - "$OUT" "$SECS" "${KODI_HOME:-/storage/.kodi}" "${KODI_RPC:-localhost:8080}" <<'PY'
import base64, http.client, json, re, signal, sys, time

out, seconds, home = sys.argv[1], float(sys.argv[2]), sys.argv[3]
host, _, port = sys.argv[4].partition(":")
LOG = home + "/temp/kodi.log"
DATABASE = 131072  # LOGDATABASE
gui = open(home + "/userdata/guisettings.xml", encoding="utf-8").read()
password = re.search(r"services.webserverpassword[^>]*>([^<]*)<", gui).group(1)
auth = base64.b64encode(("kodi:" + password).encode()).decode()


def rpc(method, params):
    conn = http.client.HTTPConnection(host, int(port or 8080), timeout=20)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    conn.request("POST", "/jsonrpc", body, {"Content-Type": "application/json", "Authorization": "Basic " + auth})
    reply = json.loads(conn.getresponse().read())
    conn.close()
    if "error" in reply:
        raise RuntimeError("%s: %s" % (method, reply["error"]))
    return reply["result"]


def lines():
    with open(LOG, encoding="utf-8", errors="replace") as log:
        return log.read().splitlines()


names = ("debug.setextraloglevel", "debug.extralogging")
before = {name: rpc("Settings.GetSettingValue", {"setting": name})["value"] for name in names}
signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
start = len(lines())
try:
    levels = sorted(set(before["debug.setextraloglevel"] or []) | {DATABASE})
    rpc("Settings.SetSettingValue", {"setting": "debug.setextraloglevel", "value": levels})
    rpc("Settings.SetSettingValue", {"setting": "debug.extralogging", "value": True})
    time.sleep(seconds)
finally:
    for name in reversed(names):
        rpc("Settings.SetSettingValue", {"setting": name, "value": before[name]})
kept = [re.sub(r"api_key=[0-9a-f]+", "api_key=X", line) for line in lines()[start:]]
with open(out, "w", encoding="utf-8") as target:
    target.write("\n".join(kept) + "\n")
print(len(kept), out)
PY
