"""Directory listings: the addon root and server library browsing."""

import xbmcgui
import xbmcplugin

from kofin.core.log import Logger
from kofin.plugin.router import Request

LOG = Logger(__name__)

BASE_URL = "plugin://plugin.video.kofin/"


def root(request: Request) -> None:
    if request.handle < 0:
        return
    items = [
        (
            BASE_URL + "?mode=browse",
            _folder_item("Kofin — phase 1 skeleton"),
            True,
        ),
    ]
    xbmcplugin.addDirectoryItems(request.handle, items, len(items))
    xbmcplugin.setContent(request.handle, "files")
    xbmcplugin.endOfDirectory(request.handle)


def browse(request: Request) -> None:
    if request.handle < 0:
        return
    xbmcplugin.endOfDirectory(request.handle)


def _folder_item(label: str) -> xbmcgui.ListItem:
    return xbmcgui.ListItem(label)
