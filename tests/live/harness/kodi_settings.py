"""RunScript harness: set Kodi system settings from inside Kodi through the
in-process JSON-RPC, for a profile whose webserver is off (every new profile
starts that way, so the first thing a fresh test profile needs is this).

    RunScript(<file>,services.webserverpassword=kodi,services.webserver=true,debug.showloginfo=true)

``true``/``false`` become booleans, digits integers, anything else a string.
"""

import json
import sys

import xbmc


def log(msg):
    xbmc.log("kofin-harness: " + msg, xbmc.LOGINFO)


for pair in sys.argv[1:]:
    key, raw = pair.split("=", 1)
    if raw.lower() in ("true", "false"):
        value = raw.lower() == "true"
    elif raw.isdigit():
        value = int(raw)
    else:
        value = raw
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "Settings.SetSettingValue",
        "params": {"setting": key, "value": value},
    }
    response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    shown = "<set>" if "password" in key else raw
    log("%s=%s -> %s" % (key, shown, response.get("result", response.get("error"))))
