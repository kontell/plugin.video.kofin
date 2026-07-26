"""Context-menu entry points (invoked with a focused ListItem)."""

import sys
from typing import List, Optional, Tuple, Union

import xbmc
import xbmcgui

from kofin.core import settings, toast
from kofin.core.api import Api
from kofin.core.http import Http, JellyfinError
from kofin.core.log import Logger
from kofin.core.settings import Credentials
from kofin.plugin.listitems import PLAYABLE_TYPES, plugin_url

LOG = Logger(__name__)


def _api() -> Api:
    return Api.from_credentials(
        Http(settings.get_bool("sslVerify")), Credentials.load()
    )


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
    """The bitrate token to transcode at; None means nothing to offer.

    A token of '0' means the source bitrate — a transcode capped at whatever
    the MediaSource reports. With exactly one configured bitrate the dialog is
    skipped. No valid bitrate means no transcode: addon.xml hides the context
    item in that case, so falling back to an invented default would only
    surface a bitrate the user never chose.
    """
    valid = [value for value in configured if _bitrate_value(value) is not None]
    if not valid:
        return None
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


# Same reach the listing-level toggle had: anything playable, plus the
# containers Jellyfin tracks played state for.
WATCHED_TYPES = PLAYABLE_TYPES | {"Series", "Season", "BoxSet"}


def _manage_options(item: dict) -> List[Tuple[str, dict]]:
    """The (label, RunPlugin params) pairs for the Jellyfin actions menu.

    Every server-side action for an item lives here, watched state included:
    a listing's own context entries are pinned to the very top of Kodi's menu
    (``CGUIMediaWindow::OnPopupMenu`` builds plugin items, then the global
    menu, then window buttons, then addon extensions), and up there kofin's
    watched toggle sat above Kodi's Play wearing the same wording as Kodi's own
    "Mark as watched" further down. Delete is offered only when the user has
    opted in on the Advanced tab; the watched and favorite labels reflect the
    server-reported state queried when the menu opened.
    """
    item_id = item.get("Id", "")
    userdata = item.get("UserData") or {}
    is_favorite = bool(userdata.get("IsFavorite"))
    options: List[Tuple[str, dict]] = []

    if item.get("Type") in WATCHED_TYPES:
        played = bool(userdata.get("Played"))
        options.append(
            (
                settings.localized(30509 if played else 30508),
                {"mode": "unwatched" if played else "watched", "id": item_id},
            )
        )

    fav_label = xbmc.getLocalizedString(14077 if is_favorite else 14076)
    fav_mode = "unfavorite" if is_favorite else "favorite"
    options.append((fav_label, {"mode": fav_mode, "id": item_id}))

    if settings.get_bool("enableDelete"):
        options.append(
            (
                xbmc.getLocalizedString(117),  # Delete
                {"mode": "delete", "id": item_id, "name": item.get("Name", "")},
            )
        )

    options.append((settings.localized(30504), {"mode": "settings"}))
    return options


def manage() -> None:
    """Open the "Jellyfin actions" menu for the focused kofin item."""
    item_id = _focused_item_id()
    if not item_id:
        LOG.warning("jellyfin actions invoked without a kofin item")
        return

    try:
        item = _api().item(item_id)
    except JellyfinError as error:
        LOG.warning("manage: item fetch failed: %s", error)
        # As with the delete failure: no icon and no sound argument meant
        # Kodi's defaults showed this failure as info, with a beep.
        toast.show(settings.localized(30507), toast.ERROR)
        return

    options = _manage_options(item)
    index = xbmcgui.Dialog().contextmenu([label for label, _ in options])
    if index < 0:
        return

    _, params = options[index]
    LOG.info("manage: %s for %s", params.get("mode"), item_id)
    xbmc.executebuiltin("RunPlugin(%s)" % plugin_url(params))
