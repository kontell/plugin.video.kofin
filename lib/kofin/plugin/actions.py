"""Small RunPlugin actions: watched/favorite toggles, settings, library
maintenance buttons (Library tab -> IPC -> service library manager)."""

from typing import Any, List, Union

import xbmc
import xbmcgui

from kofin.core import ipc, kodirpc, settings, toast
from kofin.core.api import Api
from kofin.core.http import JellyfinError, plugin_transport
from kofin.core.log import Logger
from kofin.core.settings import Credentials
from kofin.plugin.listitems import play_path
from kofin.plugin.router import Request

LOG = Logger(__name__)


def _api() -> Api:
    return Api.from_credentials(
        plugin_transport(settings.get_bool("sslVerify")),
        Credentials.load(),
        interactive=True,
    )


def _refresh() -> None:
    xbmc.executebuiltin("Container.Refresh")


def watched(request: Request) -> None:
    item_id = request.params.get("id", "")
    try:
        _api().mark_played(item_id)
    except JellyfinError as error:
        LOG.warning("mark played failed: %s", error)
        return
    _refresh()


def unwatched(request: Request) -> None:
    item_id = request.params.get("id", "")
    try:
        _api().mark_unplayed(item_id)
    except JellyfinError as error:
        LOG.warning("mark unplayed failed: %s", error)
        return
    _refresh()


def reset_resume(request: Request) -> None:
    """Zero an item's resume point on the server, then Kodi's own copy.

    The server half is the call the service makes when Kodi resets a library
    row. The local half is the bookmark Kodi keeps for the row's plugin path:
    once kofin stops stamping a position, Kodi falls back to that bookmark
    and would resurrect the very resume the viewer just removed
    (kodirpc.clear_resume_bookmark). Server first — if that fails nothing
    local is touched, and the listing keeps telling the truth.
    """
    item_id = request.params.get("id", "")
    if not item_id:
        return
    try:
        _api().set_resume_position(item_id, 0)
    except JellyfinError as error:
        LOG.warning("resume reset failed: %s", error)
        toast.show(settings.localized(30507), toast.ERROR)
        return
    kodirpc.clear_resume_bookmark(play_path(item_id))
    _refresh()


def favorite(request: Request) -> None:
    _set_favorite(request, True)


def unfavorite(request: Request) -> None:
    _set_favorite(request, False)


def _set_favorite(request: Request, value: bool) -> None:
    item_id = request.params.get("id", "")
    try:
        _api().set_favorite(item_id, value)
    except JellyfinError as error:
        LOG.warning("favorite toggle failed: %s", error)
        return
    _refresh()


def delete_item(request: Request) -> None:
    """Delete the item from the server, after opt-in and confirmation.

    ``deleteNoConfirm`` drops the confirmation only for this path — picking
    Delete off the context menu is already a deliberate act. The
    finished-watching offer (service/player.py) asks either way.
    """
    if not settings.get_bool("enableDelete"):
        return
    item_id = request.params.get("id", "")
    name = request.params.get("name", "")
    if not settings.get_bool("deleteNoConfirm") and not xbmcgui.Dialog().yesno(
        xbmc.getLocalizedString(117),  # Delete
        settings.localized(30505) % name,
    ):
        return
    try:
        _api().delete_item(item_id)
    except JellyfinError as error:
        LOG.warning("delete failed: %s", error)
        # Was raised with neither an icon nor a sound argument, so Kodi's
        # defaults made a failed deletion the one toast in kofin that showed
        # the info glyph *and* beeped.
        toast.show(settings.localized(30507), toast.ERROR)
        return
    _refresh()


def open_settings(request: Request) -> None:
    xbmc.executebuiltin("Addon.OpenSettings(plugin.video.kofin)")


# -- Library tab buttons -------------------------------------------------------


def update_libraries(request: Request) -> None:
    """Per-library (or all) fast-sync catch-up + prune pass (S2.10)."""
    whitelist = settings.get_list("librarySelection")
    if not whitelist:
        return

    names = _selection_names(whitelist)
    choices: List[Union[str, xbmcgui.ListItem]] = [settings.localized(30267)]
    choices.extend(names)  # "All" first
    picked = xbmcgui.Dialog().multiselect(settings.localized(30270), choices)

    if not picked:  # cancelled or empty
        return

    if 0 in picked:
        # Empty payload = the full-whitelist pass (keeps the retention-repair
        # release path in the service intact).
        ipc.notify(ipc.UPDATE_LIBRARY, {})
    else:
        selected = [whitelist[index - 1] for index in picked]
        ipc.notify(ipc.UPDATE_LIBRARY, {"Id": ",".join(selected)})


def refresh_boxsets(request: Request) -> None:
    ipc.notify(ipc.REFRESH_BOXSETS, {})


def precache_art(request: Request) -> None:
    """Settings button: ask the service to seed the cast-image cache now.

    Fires and exits like every other service-owned action — the work is a
    long run of downloads and database writes, which the plugin process has
    no business holding open.
    """
    ipc.notify(ipc.PRECACHE_ART, {})


def repair_libraries(request: Request) -> None:
    """Per-library picker (or all) -> remove + re-add.

    No confirmation: the picker is already the decision, and a repair is not
    destructive — it rebuilds the same libraries from the server. The prompt
    that used to sit here borrowed the *removal* copy ("Remove %s from the
    Kodi library? The items are deleted from this device only."), which read
    as though repairing would leave the library gone.
    """
    whitelist = settings.get_list("librarySelection")
    if not whitelist:
        return

    names = _selection_names(whitelist)
    choices: List[Union[str, xbmcgui.ListItem]] = [settings.localized(30267)]
    choices.extend(names)  # "All" first
    picked = xbmcgui.Dialog().multiselect(settings.localized(30266), choices)

    if not picked:  # cancelled or empty
        return

    if 0 in picked:
        selected = list(whitelist)
    else:
        selected = [whitelist[index - 1] for index in picked]

    ipc.notify(ipc.REPAIR_LIBRARY, {"Id": ",".join(selected)})


def _selection_names(library_ids: List[str]) -> List[str]:
    """View names for the picker; falls back to the raw ids offline."""
    from kofin.sync import db as sync_db
    from kofin.sync import kofindb

    names = []
    try:
        with sync_db.Database("kofin") as opened:
            db = kofindb.JellyfinDatabase(opened.cursor)
            for library_id in library_ids:
                view = db.get_view(library_id.replace("Mixed:", ""))
                names.append(view.view_name if view else library_id)
    except Exception:
        LOG.exception("view names unavailable")
        names = list(library_ids)
    return names


# -- offline downloads (docs/offline-downloads-plan.md W1.10) ------------------


def _human_size(size_bytes: int) -> str:
    if size_bytes >= 1024**3:
        return "%.1f GB" % (size_bytes / 1024**3)
    return "%d MB" % max(1, size_bytes // 1024**2)


# What actually downloads: everything else expands to these.
DOWNLOAD_LEAF_TYPES = ("Movie", "Episode", "Audio")


def _paged_items(api: Api, params: dict) -> List[dict]:
    children: List[dict] = []
    start = 0
    while True:
        page = api.items(
            dict(
                params,
                Fields="MediaSources",
                StartIndex=start,
                Limit=200,
                EnableTotalRecordCount=True,
            )
        )
        rows = page.get("Items") or []
        children.extend(rows)
        start += len(rows)
        if not rows or start >= int(page.get("TotalRecordCount") or 0):
            break
    return children


def _expand_downloadable(api: Api, item: dict) -> List[dict]:
    """The downloadable leaves under an item: itself, or a container's
    episodes/tracks. Client-side expansion, because the server has no folder
    download — CanDownload is false for every folder type by construction
    (feasibility V1)."""
    item_type = item.get("Type")
    item_id = item.get("Id", "")
    if item_type in DOWNLOAD_LEAF_TYPES:
        return [item]
    if item_type == "Season":
        listing = api.episodes(item.get("SeriesId", ""), item_id, "MediaSources")
        return list(listing.get("Items") or [])
    if item_type == "Series":
        return _paged_items(
            api,
            {"ParentId": item_id, "IncludeItemTypes": "Episode", "Recursive": True},
        )
    if item_type == "MusicAlbum":
        return _paged_items(
            api,
            {"ParentId": item_id, "IncludeItemTypes": "Audio", "Recursive": True},
        )
    if item_type == "MusicArtist":
        # ArtistIds, not ParentId: an artist is a link target, not a folder,
        # and albums an artist merely appears on still count.
        return _paged_items(
            api,
            {"ArtistIds": item_id, "IncludeItemTypes": "Audio", "Recursive": True},
        )
    if item_type == "Playlist":
        # Playlists mix types; keep the leaves this feature downloads.
        children = _paged_items(api, {"ParentId": item_id})
        return [child for child in children if child.get("Type") in DOWNLOAD_LEAF_TYPES]
    return []


def _source_size(item: dict) -> int:
    sources = item.get("MediaSources") or [{}]
    return int(sources[0].get("Size") or 0)


def _estimated_size(item: dict) -> int:
    """What this child will actually weigh on disk.

    The source size is the honest answer for anything downloading as its
    original file. A track the music transcode re-encodes is not its FLAC,
    though: it is the target bitrate times its runtime, and quoting the
    source there offered a 12-track lossless album at 400 MB when it landed
    at 60. Which tracks transcode is the server's call at download time
    (``quality.decide``), so take the smaller of the two — a track already
    inside the cap downloads untouched, and the confirmation says "about".
    """
    original = _source_size(item)
    if item.get("Type") != "Audio":
        return original
    if not settings.get_bool("downloadsMusicTranscode"):
        return original
    seconds = int(item.get("RunTimeTicks") or 0) / 10_000_000
    kbps = settings.get_int("downloadsMusicBitrate") or 128
    if seconds <= 0:
        return original
    transcoded = int(seconds * kbps * 1000 / 8)
    return min(original, transcoded) if original else transcoded


def _download_toast(string_id: int, *values: Any) -> None:
    """A downloads toast that honours the notification opt-out.

    Only the informational ones pass through here; failures go straight to
    ``toast.show`` so the opt-out cannot swallow them (kofin.downloads
    .LOUD_STRINGS states which is which).
    """
    from kofin.downloads import notify_allowed

    if not notify_allowed(string_id):
        return
    message = settings.localized(string_id)
    toast.show(message % values if values else message, time_ms=3000)


def _free_space_note() -> str:
    from kofin.downloads import downloads_root, files

    free = files.free_bytes(downloads_root())
    return _human_size(free) if free >= 0 else "?"


def _confirm_download(item: dict, wanted: List[dict]) -> bool:
    """Whether to go ahead with a queue request — asking, or refusing.

    The question a confirmation should answer is "will this fit", not "is
    this a lot of things": keyed on the container type, a 40 GB film queued
    in silence while thirty three-minute tracks demanded an answer. So the
    request is sized against what is actually free —

      * over free space minus the reserve: refused outright, since the
        manager would only fail it item by item further down;
      * at or over ``CONFIRM_FREE_SPACE_RATIO`` of what is free: confirmed;
      * anything smaller: queued in silence, whatever the count.

    A probe that cannot answer (files.free_bytes -1: exotic mount,
    permissions) falls back to the old container prompt — no ratio is
    computable, and the alternative is a silent 40 GB.
    """
    from kofin.downloads import downloads_root, files
    from kofin.plugin.context import DOWNLOAD_CONTAINER_TYPES

    total = sum(_estimated_size(child) for child in wanted)
    free = files.free_bytes(downloads_root())

    if free < 0:
        if item.get("Type") not in DOWNLOAD_CONTAINER_TYPES:
            return True
    elif total + files.FREE_SPACE_RESERVE > free:
        toast.show(
            settings.localized(30810) % (_human_size(total), _human_size(free)),
            toast.WARNING,
        )
        return False
    elif total < free * files.CONFIRM_FREE_SPACE_RATIO:
        return True

    return bool(
        xbmcgui.Dialog().yesno(
            settings.localized(30708),
            settings.localized(30771)
            % (len(wanted), _human_size(total), _free_space_note()),
        )
    )


def download(request: Request) -> None:
    """Queue downloads for an item or a container's episodes, then hand the
    ids to the service over the guarded IPC — the manager owns everything
    after that. Big requests confirm, and ones that will not fit are
    refused (:func:`_confirm_download`)."""
    item_id = request.params.get("id", "")
    if not item_id:
        return
    try:
        api = _api()
        item = api.item(item_id)
        children = _expand_downloadable(api, item)
    except JellyfinError as error:
        LOG.warning("download expansion failed for %s: %s", item_id, error)
        toast.show(settings.localized(30018), toast.ERROR)
        return

    from kofin.downloads import store

    live = {row.jellyfin_id for row in store.rows() if row.state != store.FAILED}
    wanted = [
        child
        for child in children
        if child.get("Id")
        and child.get("Id") not in live
        and child.get("CanDownload") is not False
    ]
    if not wanted:
        _download_toast(30711, 0)
        return

    if not _confirm_download(item, wanted):
        return

    # The types travel with the ids: the manager sizes its two worker pools
    # by media kind, and the kind is not knowable from an id alone without
    # the very server round trip the queue is trying to get ahead of.
    ipc.notify(
        ipc.DOWNLOAD_ADD,
        {
            "Ids": [child["Id"] for child in wanted],
            "Types": [str(child.get("Type") or "") for child in wanted],
        },
    )
    _download_toast(30711, len(wanted))


def download_show(request: Request) -> None:
    """Toggle a show's new-episode subscription (W4.6)."""
    item_id = request.params.get("id", "")
    if not item_id:
        return
    from kofin.downloads import auto as downloads_auto

    subscribed = downloads_auto.toggle_show(item_id)
    name = request.params.get("name", "") or item_id
    _download_toast(30762 if subscribed else 30763, name)


def manage_download_shows(request: Request) -> None:
    """Settings button: review the subscribed shows; untick to stop (W4.6).

    Cancel changes nothing; OK keeps exactly what stayed ticked. Names come
    from Kodi's own rows via the mapping, so the picker works offline; a
    show the library no longer holds falls back to its raw id, which is
    still removable — the whole point of the button.
    """
    from kofin.downloads import auto as downloads_auto

    shows = downloads_auto.subscribed_shows()
    if not shows:
        toast.show(settings.localized(30766), time_ms=3000)
        return
    choices: List[Union[str, xbmcgui.ListItem]] = list(_show_names(shows))
    picked = xbmcgui.Dialog().multiselect(
        settings.localized(30758), choices, preselect=list(range(len(shows)))
    )
    if picked is None:
        return
    downloads_auto.save_subscribed_shows(shows[index] for index in picked)


def _show_names(series_ids: List[str]) -> List[str]:
    from kofin.sync.db import Database

    names: List[str] = []
    try:
        with Database("kofin") as kofin_db, Database("video") as video:
            for series_id in series_ids:
                kofin_db.cursor.execute(
                    "SELECT kodi_id FROM jellyfin "
                    "WHERE jellyfin_id = ? AND media_type = 'tvshow'",
                    (series_id,),
                )
                mapped = kofin_db.cursor.fetchone()
                name = None
                if mapped is not None and mapped[0] is not None:
                    video.cursor.execute(
                        "SELECT c00 FROM tvshow WHERE idShow = ?", (mapped[0],)
                    )
                    row = video.cursor.fetchone()
                    name = row[0] if row is not None else None
                names.append(str(name or series_id))
    except Exception:
        LOG.exception("show names unavailable")
        return list(series_ids)
    return names


def delete_all_downloads(request: Request) -> None:
    """The settings button: confirm, then let the service clear the lot.

    Counted and sized from the local store, so it works offline and states
    what is actually about to be freed. One IPC for the whole request — the
    service walks the store itself.
    """
    from kofin.downloads import store

    rows = store.rows()

    if not rows:
        toast.show(settings.localized(30807), time_ms=3000)
        return

    # What is on disk, not what was promised: a queued row has downloaded
    # nothing yet, and a finished one may have transcoded smaller than its
    # source.
    freed = sum(row.bytes_done or 0 for row in rows)

    if not xbmcgui.Dialog().yesno(
        settings.localized(30804),
        settings.localized(30806) % (len(rows), _human_size(freed)),
    ):
        return

    ipc.notify(ipc.DOWNLOAD_REMOVE_ALL)


def cancel_download(request: Request) -> None:
    """Cancel one download, or every unfinished one under a container.

    The expansion is local (kofin.db), not a server walk: cancelling has to
    work offline, and the store already knows which rows belong to the show
    or album in front of the user.
    """
    item_id = request.params.get("id", "")
    if not item_id:
        return
    from kofin.downloads import store

    targets = [item_id] if store.get(item_id) else store.container_pending_ids(item_id)
    for target in targets:
        ipc.notify(ipc.DOWNLOAD_CANCEL, {"Id": target})


def remove_download(request: Request) -> None:
    """Let the service restore the rows and delete the files. No confirmation.

    Unlike Delete on the same menu, this destroys nothing the server does not
    still have: a download is a local copy, and removing it puts the item back
    to streaming. The feedback is the listing itself — the service unstamps the
    download badge and refreshes as soon as the rows are gone — so the answer
    to "did that work" is on screen either way, without a dialog in front of it.

    A container removes everything finished under it, expanded from kofin.db
    rather than the server so the entry keeps working offline.
    """
    item_id = request.params.get("id", "")
    if not item_id:
        return
    from kofin.downloads import store

    if store.get(item_id) is not None:
        targets = [item_id]
    else:
        targets = store.container_done_ids(item_id)
    for target in targets:
        ipc.notify(ipc.DOWNLOAD_REMOVE, {"Id": target})
