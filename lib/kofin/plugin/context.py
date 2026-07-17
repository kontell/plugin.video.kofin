"""Context-menu entry points (invoked with a focused ListItem)."""

import sys
from typing import List, Optional, Union

import xbmc
import xbmcgui

from kofin.core import settings
from kofin.core.log import Logger
from kofin.plugin.listitems import plugin_url

LOG = Logger(__name__)


def _focused_item_id() -> str:
    listitem: Optional[xbmcgui.ListItem] = getattr(sys, "listitem", None)
    if listitem is None:
        return ""
    item_id = listitem.getProperty("kofin.id")
    if item_id:
        return item_id
    # Library items carry no kofin.id property; resolve the Kodi database id
    # through the kofin.db mapping instead.
    tag = listitem.getVideoInfoTag()
    if tag is None:
        return ""
    return lookup_item_id(tag.getDbId(), tag.getMediaType())


def lookup_item_id(dbid: int, media_type: str) -> str:
    """The Jellyfin item id for a Kodi library row, '' when not kofin's."""
    if not dbid or dbid < 0 or not media_type:
        return ""
    from kofin.sync.db import get_item

    row = get_item(dbid, media_type)
    return row.jellyfin_id if row is not None else ""


def choose_bitrate(configured: List[str]) -> Optional[str]:
    """The bitrate to transcode at; None means the user cancelled.

    With exactly one configured bitrate the selection dialog is skipped.
    """
    valid = [value for value in configured if value.isdigit() and int(value) > 0]
    if not valid:
        valid = ["10"]
    if len(valid) == 1:
        return valid[0]
    labels: List[Union[str, xbmcgui.ListItem]] = [
        "%s Mbit/s" % value for value in valid
    ]
    index = xbmcgui.Dialog().select(settings.localized(30010), labels)
    return valid[index] if index >= 0 else None


def play_with_transcode() -> None:
    item_id = _focused_item_id()
    if not item_id:
        LOG.warning("transcode context invoked without a kofin item")
        return
    bitrate = choose_bitrate(settings.get_list("contextBitrates"))
    if bitrate is None:
        return
    LOG.info("context transcode %s at %s Mbit/s", item_id, bitrate)
    xbmc.executebuiltin(
        "RunPlugin(%s)"
        % plugin_url(
            {"mode": "play", "id": item_id, "transcode": "1", "bitrate": bitrate}
        )
    )
