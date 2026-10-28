"""Live lever: force a full node regeneration, so a before/after
byte-comparison of the generated tree is about the builder, not about
whether it ran.

RunScript(<file>).
"""

import sys

import xbmc
import xbmcvfs

sys.path.insert(
    0, xbmcvfs.translatePath("special://home/addons/plugin.video.kofin/lib")
)
from kofin.core import settings  # noqa: E402
from kofin.sync.views import Views  # noqa: E402

settings.set_str("viewsHash", "")
Views().get_nodes()
xbmc.log("kofin-probe: node tree regenerated (viewsHash cleared)", xbmc.LOGINFO)
