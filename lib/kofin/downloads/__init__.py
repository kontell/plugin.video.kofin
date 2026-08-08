"""Offline downloads (docs/offline-downloads-plan.md).

Shell code, not transplant: the store (kofin.db ``download`` table), the
file layout, and the service-side manager live here. The Kodi-database
repoint lives with the other database writers in ``kofin.sync.kodidb``.
"""

import os

TAG = "Kofin Downloads"

ADDON_DATA = "special://profile/addon_data/plugin.video.kofin/"


def downloads_root() -> str:
    """The absolute downloads root: the setting, or the profile default.

    Here rather than in the manager because the sync writers resolve it too
    (the post-write re-assert, plan W1.8), and they must not drag the whole
    manager module in to do it.
    """
    import xbmcvfs

    from kofin.core import settings

    configured = settings.get_str("downloadsPath")
    if configured:
        return xbmcvfs.translatePath(configured).rstrip("/")
    return os.path.join(xbmcvfs.translatePath(ADDON_DATA), "downloads")
