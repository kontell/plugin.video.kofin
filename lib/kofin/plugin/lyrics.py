"""The lyrics directory: one list item per line of the playing song.

A skin renders lyrics with a fixedlist so Kodi animates the scroll, and a
fixedlist needs a directory to fill it. The lines come from the window
property the service published rather than from Jellyfin, so opening this
costs nothing and cannot fail on a slow server -- the service already paid
for them at playback start.

Item labels are the lines themselves. Which line is current is the
renderer's business (script.kofin.lyrics follows the clock from the timed
lines in the same property), so nothing here needs to know.
"""

from typing import Any, Dict

import xbmcgui
import xbmcplugin

from kofin.core import state
from kofin.core.log import Logger

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

# Blank rows after the last line. A list scrolls no further than its last
# item, so without a tail the song's final lines can only ever sit at the
# very bottom of the list's page -- which, when the lyrics addon has resized
# the overlay shorter than its authored height, is below what is visible at
# all. Twelve covers the deepest page any known overlay is authored with, so
# the addon can always drag the last sung line up to the visible middle.
TAIL_ROWS = 12


def lyrics(request: Any) -> None:
    """Serve the published lines as a directory."""
    lines = state.lyric_texts()
    LOG.debug("lyrics directory: %d lines", len(lines))
    for line in lines:
        # A blank line still has to occupy a row: the service addresses lines
        # by index, so dropping empties would shift every line after one.
        item = xbmcgui.ListItem(label=line or " ", offscreen=True)
        xbmcplugin.addDirectoryItem(request.handle, "", item, isFolder=False)
    for _ in range(TAIL_ROWS):
        item = xbmcgui.ListItem(label=" ", offscreen=True)
        xbmcplugin.addDirectoryItem(request.handle, "", item, isFolder=False)
    xbmcplugin.endOfDirectory(request.handle, cacheToDisc=False)
