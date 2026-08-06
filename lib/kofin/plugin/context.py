"""Context-menu entry points (invoked with a focused ListItem)."""

import sys
from typing import Dict, List, Optional, Tuple, Union

import xbmc
import xbmcgui

from kofin.core import kodirpc, settings, toast
from kofin.core.api import Api
from kofin.core.http import Http, JellyfinError
from kofin.core.log import Logger
from kofin.core.settings import Credentials
from kofin.plugin.listitems import PLAYABLE_TYPES, plugin_url

LOG = Logger(__name__)

# How long to wait for the outgoing playback to actually stop. Measured, the
# handover completes in well under 300 ms; the ceiling only stops a wedged
# player from stranding the context item.
STOP_TIMEOUT_SECONDS = 3.0
STOP_POLL_SECONDS = 0.05


def _api() -> Api:
    return Api.from_credentials(
        Http(settings.get_bool("sslVerify")), Credentials.load()
    )


def _focused_listitem() -> Optional[xbmcgui.ListItem]:
    return getattr(sys, "listitem", None)


def _focused_dynamic_id() -> str:
    """The Jellyfin id of the focused item when it is one kofin listed itself.

    Only kofin's own listings build their items, so only they carry the
    property: a synced library's rows are Kodi's, and reach their Jellyfin id
    through the database mapping instead. That makes this the test for "is this
    item from a dynamic library", which decides whether the watched toggle is
    worth offering.
    """
    listitem = _focused_listitem()
    if listitem is None:
        return ""
    return listitem.getProperty("kofin.id")


def _focused_item_id() -> str:
    item_id = _focused_dynamic_id()
    if item_id:
        return item_id
    # Library items carry no kofin.id property; resolve the Kodi database id
    # through the kofin.db mapping instead.
    listitem = _focused_listitem()
    tag = listitem.getVideoInfoTag() if listitem is not None else None
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
    """Parse a context-bitrate token (Mbit/s, '0' == no override); None if junk."""
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _bitrate_label(value: str) -> str:
    if _bitrate_value(value) == 0:
        return settings.localized(30206)  # Default (max streaming bitrate)
    return "%s Mbit/s" % value


def choose_bitrate(configured: List[str]) -> Optional[str]:
    """The bitrate token to transcode at; None means nothing to offer.

    A token of '0' overrides nothing: ``deviceprofile.build`` falls back to the
    max-streaming-bitrate setting, and only when that is unset too does the
    server size the transcode itself. It never meant the source bitrate when a
    cap was configured, and now means it nowhere -- hence the label. With
    exactly one configured bitrate the dialog is skipped. No valid bitrate
    means no transcode: addon.xml hides the context item in that case, so
    falling back to an invented default would only surface a bitrate the user
    never chose.
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


def _focused_resume() -> Tuple[str, float]:
    """(the ``dbid`` play param, the position that item would resume at).

    The two travel together so the prompt cannot quote one position while the
    play route starts at another: ``play.resume_start_ticks`` reads a readable
    library row's bookmark out of Kodi's own database and everything else --
    a kofin listing, a row kofin cannot read -- off the resolved item, whose
    resume point ``listitems.resume_of`` already pulled back by the Advanced-tab
    offset. So the dbid is handed on only where the bookmark is the answer, and
    the seconds are read from whichever source that leaves in play.
    """
    listitem = _focused_listitem()
    tag = listitem.getVideoInfoTag() if listitem is not None else None
    if tag is None:
        return "", 0.0
    dbid, media = tag.getDbId(), tag.getMediaType()
    if dbid and dbid > 0 and media in kodirpc.RESUME_QUERY:
        return str(dbid), kodirpc.resume_seconds(dbid, media) or 0.0
    return "", max(tag.getResumeTime(), 0.0)


def _resume_label(seconds: float) -> str:
    """Kodi's "Resume from HH:MM:SS", stamped the way Kodi stamps it.

    ``SecondsToTimeString(..., TIME_FORMAT_HH_MM_SS)`` zero-pads all three
    fields. The string is a fmt template ("Resume from {0:s}") that a
    translation may write differently, so a substitution that does not take
    falls back to the bare time rather than to a label with braces in it.
    """
    whole = int(round(seconds))
    stamp = "%02d:%02d:%02d" % (whole // 3600, whole % 3600 // 60, whole % 60)
    try:
        return xbmc.getLocalizedString(12022).format(stamp)  # Resume from {0:s}
    except (IndexError, KeyError, ValueError):
        return stamp


def choose_resume(resume_seconds: float) -> Optional[bool]:
    """Whether to resume; None when the user backed out of the question.

    Kodi never asks it on this path. It prompts for the playbacks it starts
    itself, and the transcode item starts its own, so a partially watched item
    transcoded from the context menu used to silently restart. Same wording,
    options and order as ``CGUIWindowVideoBase::ShowResumeMenu``; nothing to
    ask means resume=False.
    """
    if resume_seconds <= 0:
        return False
    index = xbmcgui.Dialog().contextmenu(
        [
            _resume_label(resume_seconds),
            xbmc.getLocalizedString(12021),  # Play from beginning
        ]
    )
    if index < 0:
        return None
    return index == 0


def play_with_transcode() -> None:
    item_id = _focused_item_id()
    if not item_id:
        LOG.warning("transcode context invoked without a kofin item")
        return
    dbid, resume_seconds = _focused_resume()
    resume = choose_resume(resume_seconds)
    if resume is None:
        return
    bitrate = choose_bitrate(settings.get_list("contextBitrates"))
    if bitrate is None:
        return
    params: Dict[str, str] = {
        "mode": "play",
        "id": item_id,
        "transcode": "1",
        "bitrate": bitrate,
    }
    if dbid:
        params["dbid"] = dbid
    # The answer is stated outright, because Kodi will not carry it: PlayMedia's
    # own "resume" flag is gated on GetItemResumeInformation().isResumable, and
    # the bare plugin:// path it builds an item from has no resume information
    # to find ("LoadDetails: Unsupported item type"), so the flag downgrades
    # itself to noresume and the play route is handed resume:false either way.
    # A start position in the params owes Kodi nothing: the play route resolves
    # the stream at that offset and stamps it on the item Kodi then seeks.
    if resume:
        params["startticks"] = str(int(resume_seconds * 10_000_000))
    else:
        params["fromstart"] = "1"
    LOG.info(
        "context transcode %s at %s Mbit/s (%s)",
        item_id,
        bitrate,
        "resume" if resume else "from start",
    )
    # PlayMedia, not RunPlugin: playback Kodi starts is resolved through
    # setResolvedUrl, the path every other kofin playback takes, and the one
    # whose resume point Kodi acts on.
    _stop_current_playback()
    xbmc.executebuiltin("PlayMedia(%s)" % plugin_url(params))


def _stop_current_playback() -> None:
    """Stop whatever is playing, and wait for it to be gone.

    ``PlayMedia`` on a bare ``plugin://`` path handed to Kodi while something
    else is still playing loses a race: the outgoing player's stop is queued to
    the application thread, and that thread does not get to it until *after* it
    has opened the new video player -- measured at 103-105 ms past
    ``VideoPlayer::OpenFile``, on every run. Processing it then closes the
    player that just opened, and because the demuxer open is still in flight
    the play dies as "OpenDemuxStream - Error creating demuxer" rather than as
    a stop, so it reads as a broken stream.

    Library playback does not go through ``PlayMedia`` -- Kodi opens the
    ``videodb://`` item and sequences the handover itself -- which is why this
    context item was the only route that failed, and only while music was
    playing. Stopping first costs nothing: the outgoing playback was about to
    be replaced anyway, and doing it here rather than inside Kodi's open lets
    the service report the stop against the item it actually belongs to.
    """
    player = xbmc.Player()
    if not player.isPlaying():
        return
    player.stop()
    monitor = xbmc.Monitor()
    waited = 0.0
    while waited < STOP_TIMEOUT_SECONDS:
        if monitor.waitForAbort(STOP_POLL_SECONDS):
            return
        waited += STOP_POLL_SECONDS
        if not player.isPlaying():
            return
    LOG.warning("playback did not stop in %.1fs; starting anyway", waited)


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


def _manage_options(item: dict, dynamic: bool) -> List[Tuple[str, dict]]:
    """The (label, RunPlugin params) pairs for the Jellyfin actions menu.

    Every server-side action for an item lives here: a listing's own context
    entries are pinned to the very top of Kodi's menu
    (``CGUIMediaWindow::OnPopupMenu`` builds plugin items, then the global
    menu, then window buttons, then addon extensions), and up there kofin's
    watched toggle sat above Kodi's Play wearing the same wording as Kodi's own
    "Mark as watched" further down. Delete is offered only when the user has
    opted in on the Advanced tab; the watched and favorite labels reflect the
    server-reported state queried when the menu opened.

    ``dynamic`` says the item came from one of kofin's own listings rather than
    from a synced library, and only there is the watched toggle the only way to
    reach the server. A library row already has Kodi's own "Mark as watched",
    which ``service/kodiuserdata.py`` forwards to Jellyfin — including the
    per-episode announcements a whole season fires — so offering a second,
    server-named entry beside it only asked the viewer to tell two identical
    actions apart.
    """
    item_id = item.get("Id", "")
    userdata = item.get("UserData") or {}
    is_favorite = bool(userdata.get("IsFavorite"))
    options: List[Tuple[str, dict]] = []

    if dynamic and item.get("Type") in WATCHED_TYPES:
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

    options = _manage_options(item, dynamic=bool(_focused_dynamic_id()))
    index = xbmcgui.Dialog().contextmenu([label for label, _ in options])
    if index < 0:
        return

    _, params = options[index]
    LOG.info("manage: %s for %s", params.get("mode"), item_id)
    xbmc.executebuiltin("RunPlugin(%s)" % plugin_url(params))
