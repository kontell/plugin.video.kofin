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


def _bitrate_value(value: str) -> Optional[float]:
    """Parse a context-bitrate token (Mbit/s, '0' == source); None if junk."""
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _bitrate_label(value: str) -> str:
    if _bitrate_value(value) == 0:
        return settings.localized(30206)  # Source (original) bitrate
    return "%s Mbit/s" % value


def choose_bitrate(configured: List[str]) -> Optional[str]:
    """The bitrate token to transcode at; None means the user cancelled.

    A token of '0' means the source bitrate (unlimited) — the same result as
    force transcode. With exactly one configured bitrate the dialog is skipped.
    """
    valid = [value for value in configured if _bitrate_value(value) is not None]
    if not valid:
        valid = ["10"]
    if len(valid) == 1:
        return valid[0]
    labels: List[Union[str, xbmcgui.ListItem]] = [
        _bitrate_label(value) for value in valid
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


def browse_extras() -> None:
    """Open the extras listing for the focused library show/season."""
    item_id = _focused_item_id()
    if not item_id:
        LOG.warning("extras context invoked without a kofin item")
        return
    LOG.info("context extras for %s", item_id)
    xbmc.executebuiltin(
        "ActivateWindow(Videos,%s,return)"
        % plugin_url({"mode": "extras", "id": item_id})
    )
