"""RunScript harness: fire one of kofin's own IPC messages from inside Kodi.

Usage (EventServer builtin):
    RunScript(<this file>,<METHOD>,<key=value>,...)
e.g.  RunScript(.../kofin_ipc.py,RepairLibrary,Id=<id1>,<id2>)  -- commas inside
values are not possible through RunScript, so several ids are passed as
extra positional args and joined here.

The message goes through kofin.core.ipc.notify, which reads the nonce for a
guarded message itself; nothing leaves the Kodi process."""

import sys

import xbmc
import xbmcvfs

sys.path.insert(
    0, xbmcvfs.translatePath("special://home/addons/plugin.video.kofin/lib")
)

from kofin.core import ipc  # noqa: E402

method = sys.argv[1]
data = {}
ids = []
for arg in sys.argv[2:]:
    if arg.startswith("jsonfile="):
        # A payload with lists (DownloadAdd wants Ids and Types) cannot ride
        # RunScript's comma-split argv; read it from a file instead.
        import json

        with open(arg.split("=", 1)[1]) as handle:
            data.update(json.load(handle))
    elif "=" in arg:
        key, value = arg.split("=", 1)
        data[key] = value
    else:
        ids.append(arg)
if ids:
    data["Id"] = ",".join([data.get("Id", "")] + ids).strip(",")
xbmc.log("kofin-harness: notify %s %s" % (method, data), xbmc.LOGINFO)
ipc.notify(method, data)
xbmc.log("kofin-harness: sent", xbmc.LOGINFO)
