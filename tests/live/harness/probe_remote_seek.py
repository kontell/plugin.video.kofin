"""One-off: drive RemoteHandler.handle('Play', ... StartPositionTicks) and time
how long handle() blocks the calling thread -- P0.7. On the fix build the seek
runs on its own thread, so handle() returns at once; before, it polled up to
10 s on the caller. RunScript(<file>,<item_id>,<seconds>)."""

import sys
import time

import xbmc
import xbmcvfs

sys.path.insert(
    0, xbmcvfs.translatePath("special://home/addons/plugin.video.kofin/lib")
)
from kofin.service.remote import RemoteHandler  # noqa: E402

item_id = sys.argv[1]
seconds = float(sys.argv[2])
handler = RemoteHandler()
start = time.time()
handler.handle(
    "Play",
    {
        "ItemIds": [item_id],
        "PlayCommand": "PlayNow",
        "StartPositionTicks": int(seconds * 10_000_000),
    },
)
elapsed = time.time() - start
alive = handler._seek_thread is not None and handler._seek_thread.is_alive()
xbmc.log(
    "kofin-probe: handle() returned in %.2fs; seek_thread_alive=%s" % (elapsed, alive),
    xbmc.LOGINFO,
)
