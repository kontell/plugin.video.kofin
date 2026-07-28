"""The lyrics directory: one list item per line of the playing song.

A skin renders lyrics with a fixedlist so Kodi animates the scroll, and a
fixedlist needs a directory to fill it. The lines come from the window
property the service published rather than from Jellyfin, so opening this
costs nothing and cannot fail on a slow server -- the service already paid
for them at playback start.

Item labels are the lines themselves; the service moves the highlight with
Control.SetFocus, so nothing here needs to know which line is current.
"""

from typing import Any, Dict

import xbmcgui
import xbmcplugin

from kofin.core import state
from kofin.core.log import Logger

LOG = Logger(__name__)

JsonDict = Dict[str, Any]


def lyrics(request: Any) -> None:
    """Serve the published lines as a directory."""
    lines = state.lyric_texts()
    LOG.debug("lyrics directory: %d lines", len(lines))
    for line in lines:
        # A blank line still has to occupy a row: the service addresses lines
        # by index, so dropping empties would shift every line after one.
        item = xbmcgui.ListItem(label=line or " ", offscreen=True)
        xbmcplugin.addDirectoryItem(request.handle, "", item, isFolder=False)
    xbmcplugin.endOfDirectory(request.handle, cacheToDisc=False)
