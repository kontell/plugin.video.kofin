"""Context-menu entry points (invoked with a focused ListItem)."""

import sys
from typing import Optional

import xbmcgui

from kofin.core.log import Logger

LOG = Logger(__name__)


def _focused_item_id() -> str:
    listitem: Optional[xbmcgui.ListItem] = getattr(sys, "listitem", None)
    return listitem.getProperty("kofin.id") if listitem is not None else ""


def play_with_transcode() -> None:
    item_id = _focused_item_id()
    LOG.info("play-with-transcode requested for %s", item_id)
    xbmcgui.Dialog().notification("Kofin", "Transcode playback arrives in step 10")
