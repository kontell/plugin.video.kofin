"""Whether the SyncPlay root entry is offered at all (P1.5).

Split from ``plugin/syncplay.py`` so the service (settings_apply's root-menu
mirror) does not import the plugin package for two settings-and-profile
reads. The plugin route re-exports both names.
"""

import xbmcvfs

from kofin.core import settings


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
