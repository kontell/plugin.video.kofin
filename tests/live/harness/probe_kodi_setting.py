"""Live probe of kodi_setting's split between an absent setting and a failed
call.

RunScript(<file>). Logs the raw answer for videoplayer.queuetimesize — the
real tenths on Piers, None ("setting absent") on Omega — and whether it is
the FAILED sentinel, which a healthy Kodi never answers.
"""

import sys

import xbmc
import xbmcvfs

sys.path.insert(
    0, xbmcvfs.translatePath("special://home/addons/plugin.video.kofin/lib")
)
from kofin.core import kodirpc  # noqa: E402

value = kodirpc.kodi_setting("videoplayer.queuetimesize")
xbmc.log(
    "kofin-probe: videoplayer.queuetimesize=%r failed=%s absent=%s"
    % (value, value is kodirpc.FAILED, value is None),
    xbmc.LOGINFO,
)
