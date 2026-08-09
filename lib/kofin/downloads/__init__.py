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


# What ``downloadsNotify`` never silences: something went wrong and the
# toast is the only place it says so. The syncplay notification setting
# draws the same line (syncplay/manager.py) — an opt-out that swallowed
# failures would turn "my download did nothing" into an unanswerable
# question. 30766 is here for the same reason in reverse: it is the only
# answer the manage-shows button gives when the list is empty.
LOUD_STRINGS = frozenset(
    {
        30018,  # server request failed
        30713,  # download failed
        30715,  # not enough free space
        30717,  # downloads folder not writable
        30720,  # not available offline
        30766,  # no shows are set to download new episodes
    }
)


def notify_allowed(string_id: int) -> bool:
    """Whether this toast may be shown, given the notification opt-out."""
    if string_id in LOUD_STRINGS:
        return True

    from kofin.core import settings

    return settings.get_bool("downloadsNotify")
