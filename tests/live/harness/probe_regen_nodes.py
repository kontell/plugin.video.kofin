"""S1-P1.5/S1-P1.12 live lever: force a full node regeneration.

RunScript(<file>). Clears the viewsHash fingerprint and runs the same
serverless regeneration the settings apply uses, so the generated tree is
rewritten by the installed build — the before/after byte-comparison is
then about the builder, not about whether it ran.
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
