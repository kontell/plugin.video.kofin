"""The ``mode=syncplay`` plugin entry: one IPC message to the service.

The plugin invocation is transient — it cannot hold group state, an Api, or
the player — so everything SyncPlay lives in the service (phase-4 plan §2).
This handler only validates that a menu makes sense right now and sends
``SyncPlayMenu``; the service opens the menu on a dedicated worker thread.
"""

import xbmcgui
import xbmcvfs

from kofin.core import ipc, settings, state
from kofin.core.log import Logger
from kofin.plugin.router import Request

LOG = Logger(__name__)


def external_player_configured() -> bool:
    """A playercorefactory override routes video to a non-kofin external
    player, which SyncPlay cannot drive (report §9.5.5) — the root entry is
    hidden and the menu refuses."""
    return bool(
        xbmcvfs.exists("special://profile/playercorefactory.xml")
        or xbmcvfs.exists("special://masterprofile/playercorefactory.xml")
    )


def available() -> bool:
    """Whether the SyncPlay root entry should be offered (read fresh per
    root listing: the master toggle on, no external player configured)."""
    return settings.get_bool("syncPlayEnabled") and not external_player_configured()


def menu(request: Request) -> None:
    if not settings.get_bool("syncPlayEnabled"):
        return

    if external_player_configured():
        _notify(settings.localized(30575))
        return

    if not state.is_online():
        _notify(settings.localized(30574))
        return

    LOG.debug("requesting the SyncPlay menu from the service")
    ipc.notify(ipc.SYNCPLAY_MENU)


def _notify(message: str) -> None:
    xbmcgui.Dialog().notification(
        "SyncPlay", message, xbmcgui.NOTIFICATION_INFO, 4000, False
    )
