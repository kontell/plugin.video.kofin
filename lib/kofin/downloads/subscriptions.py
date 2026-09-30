# -*- coding: utf-8 -*-
"""Mirror playlist and music-library download subscriptions.

The claim table records every standing order for a track. A download row has
only one origin; claims keep an overlap from being deleted when the first
subscription drops it. Every removal still goes through the manager.
"""

import os
import uuid
from typing import Any, Dict, Iterable, List, Set, Tuple

from kofin.core import ipc, settings
from kofin.core.log import Logger
from kofin.downloads import store
from kofin.sync.db import Database

LOG = Logger(__name__)

PLAYLIST_SETTING = "downloadsPlaylists"
LIBRARY_SETTING = "downloadsMusicLibraries"
MAX_SUBSCRIPTION_ITEMS = 50000
NOTIFY_BATCH_SIZE = 200


def subscribed(setting_id: str) -> List[str]:
    return [part for part in settings.get_str(setting_id).split(",") if part]


def save(setting_id: str, ids: Iterable[str]) -> None:
    settings.set_str(setting_id, ",".join(dict.fromkeys(item for item in ids if item)))


def toggle(setting_id: str, item_id: str) -> bool:
    current = subscribed(setting_id)
    if item_id in current:
        save(setting_id, (value for value in current if value != item_id))
        return False
    save(setting_id, current + [item_id])
    return True


def owner(kind: str, item_id: str) -> str:
    return "auto:%s:%s" % (kind, item_id)


def reconcile(owner_id: str, item_ids: Iterable[str], request_name: str = "") -> None:
    """Apply a complete membership snapshot, then queue its diff.

    The caller must supply a successful *complete* listing. An exception or
    capped page must never be turned into a partial deletion order.
    """
    wanted = set(item_ids)
    if len(wanted) > MAX_SUBSCRIPTION_ITEMS:
        raise ValueError("subscription %s exceeds item limit" % owner_id)
    if not settings.get_bool("downloadsEnabled"):
        return
    with Database("kofin") as opened:
        queue, delete = _reconcile_on(opened.cursor, owner_id, wanted)
    if queue:
        _notify_queue(owner_id, queue, request_name)
    if delete:
        ordered = sorted(delete)
        for start in range(0, len(ordered), NOTIFY_BATCH_SIZE):
            ipc.notify(
                ipc.DOWNLOAD_REMOVE,
                {
                    "Ids": ordered[start : start + NOTIFY_BATCH_SIZE],
                    "Subscription": True,
                },
            )
    if queue or delete:
        LOG.info(
            "subscription %s: %d queued, %d removed", owner_id, len(queue), len(delete)
        )


def _owner_name(owner_id: str) -> str:
    """Use the local playlist filename or library title for the final toast."""
    _, _, suffix = owner_id.partition(":")
    kind, _, item_id = suffix.partition(":")
    if not item_id:
        return owner_id
    with Database("kofin") as opened:
        if kind == "playlist":
            opened.cursor.execute(
                "SELECT filename FROM playlist_state WHERE jellyfin_id = ?", (item_id,)
            )
            row = opened.cursor.fetchone()
            if row and row[0]:
                return str(os.path.splitext(row[0])[0])
        elif kind == "musiclibrary":
            opened.cursor.execute(
                "SELECT view_name FROM view WHERE view_id = ?", (item_id,)
            )
            row = opened.cursor.fetchone()
            if row and row[0]:
                return str(row[0])
    return item_id


def _notify_queue(owner_id: str, item_ids: Iterable[str], name: str = "") -> None:
    """One request across IPC batches, hence one result toast after all songs."""
    ordered = sorted(item_ids)
    request_id = "%s:%s" % (owner_id, uuid.uuid4().hex)
    request_name = name or _owner_name(owner_id)
    for start in range(0, len(ordered), NOTIFY_BATCH_SIZE):
        batch = ordered[start : start + NOTIFY_BATCH_SIZE]
        ipc.notify(
            ipc.DOWNLOAD_ADD,
            {
                "Ids": batch,
                "Types": ["Audio"] * len(batch),
                "Origin": owner_id,
                "Request": request_id,
                "RequestName": request_name,
            },
        )


def _reconcile_on(
    cursor: Any, owner_id: str, wanted: Set[str]
) -> Tuple[List[str], List[str]]:
    cursor.execute(
        "SELECT jellyfin_id FROM download_subscription WHERE owner = ?", (owner_id,)
    )
    previous = {row[0] for row in cursor.fetchall()}
    added = wanted - previous
    removed = previous - wanted
    cursor.executemany(
        "INSERT OR IGNORE INTO download_subscription(owner, jellyfin_id) VALUES (?, ?)",
        ((owner_id, item_id) for item_id in added),
    )
    cursor.executemany(
        "DELETE FROM download_subscription WHERE owner = ? AND jellyfin_id = ?",
        ((owner_id, item_id) for item_id in removed),
    )
    queue: List[str] = []
    delete: List[str] = []
    for batch in _batches(wanted):
        marks = ",".join("?" for _ in batch)
        cursor.execute(
            "SELECT jellyfin_id, state FROM download WHERE jellyfin_id IN (%s)" % marks,
            batch,
        )
        downloads = dict(cursor.fetchall())
        queue.extend(
            item_id
            for item_id in batch
            if item_id not in downloads or downloads[item_id] == store.FAILED
        )
    for batch in _batches(removed):
        marks = ",".join("?" for _ in batch)
        cursor.execute(
            "SELECT jellyfin_id FROM download_subscription WHERE jellyfin_id IN (%s)"
            % marks,
            batch,
        )
        claimed = {row[0] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT jellyfin_id, origin FROM download WHERE jellyfin_id IN (%s)"
            % marks,
            batch,
        )
        delete.extend(
            item_id
            for item_id, origin in cursor.fetchall()
            if item_id not in claimed
            and str(origin).startswith(("auto:playlist:", "auto:musiclibrary:"))
        )
    return queue, delete


def _batches(item_ids: Iterable[str]) -> Iterable[List[str]]:
    ordered = sorted(item_ids)
    for start in range(0, len(ordered), 500):
        yield ordered[start : start + 500]


def release(owner_id: str) -> None:
    """Stop mirroring but leave already downloaded files on the device."""
    with Database("kofin") as opened:
        opened.cursor.execute(
            "DELETE FROM download_subscription WHERE owner = ?", (owner_id,)
        )


def reconcile_playlist_memberships(memberships: Dict[str, List[str]]) -> None:
    """Apply only confirmed audio snapshots after their playlist writes commit."""
    active = set(subscribed(PLAYLIST_SETTING))
    for playlist_id, item_ids in memberships.items():
        if playlist_id not in active:
            continue
        try:
            reconcile(owner("playlist", playlist_id), item_ids)
        except Exception:
            LOG.exception("playlist subscription failed for %s", playlist_id)


def reconcile_playlist_direct(api: Any, playlist_id: str) -> None:
    """Sync one subscription when playlist file materialization is disabled."""
    if playlist_id not in subscribed(PLAYLIST_SETTING):
        return
    from kofin.sync import playlists

    try:
        playlist = api.item(playlist_id)
        items = playlists._iter_playlist_items(api, playlist_id)
        if playlists.playlist_side(playlist.get("MediaType") or "", items) == "Audio":
            reconcile(
                owner("playlist", playlist_id),
                playlists._audio_ids(items),
                str(playlist.get("Name") or ""),
            )
    except Exception:
        LOG.exception("playlist subscription failed for %s", playlist_id)


def reconcile_playlists_direct(api: Any) -> None:
    for playlist_id in subscribed(PLAYLIST_SETTING):
        reconcile_playlist_direct(api, playlist_id)


def remove_confirmed_playlist(playlist_id: str) -> None:
    """Release an audio standing order only for a confirmed deletion event."""
    if playlist_id in subscribed(PLAYLIST_SETTING):
        reconcile(owner("playlist", playlist_id), [])


def claim_new_library_songs(entries: Iterable[Any]) -> None:
    """Claim newly written songs without walking every subscribed library."""
    if not settings.get_bool("downloadsEnabled"):
        return
    libraries = set(subscribed(LIBRARY_SETTING))
    songs = {
        entry.item_id
        for entry in entries
        if getattr(entry, "type", "") == "Audio" and entry.item_id
    }
    if not libraries or not songs:
        return
    queued: Dict[str, List[str]] = {}
    with Database("kofin") as opened:
        for item_id in songs:
            opened.cursor.execute(
                "SELECT media_folder FROM jellyfin WHERE jellyfin_id = ?", (item_id,)
            )
            row = opened.cursor.fetchone()
            if not row or row[0] not in libraries:
                continue
            owner_id = owner("musiclibrary", row[0])
            opened.cursor.execute(
                "INSERT OR IGNORE INTO download_subscription(owner, jellyfin_id) "
                "VALUES (?, ?)",
                (owner_id, item_id),
            )
            opened.cursor.execute(
                "SELECT state FROM download WHERE jellyfin_id = ?", (item_id,)
            )
            download = opened.cursor.fetchone()
            if download is None or download[0] == store.FAILED:
                queued.setdefault(owner_id, []).append(item_id)
    for owner_id, item_ids in queued.items():
        _notify_queue(owner_id, item_ids)


def _library_items(api: Any, library_id: str) -> List[str]:
    items: List[str] = []
    start = 0
    seen = set()
    while True:
        page = api.items(
            {
                "ParentId": library_id,
                "IncludeItemTypes": "Audio",
                "Recursive": True,
                "StartIndex": start,
                "Limit": 200,
                "EnableTotalRecordCount": True,
            }
        )
        if "Items" not in page or not isinstance(page["Items"], list):
            raise ValueError("music library %s returned no items list" % library_id)
        if any(not isinstance(item, dict) for item in page["Items"]):
            raise ValueError("music library %s returned malformed items" % library_id)
        total = int(page.get("TotalRecordCount") or 0)
        if total > MAX_SUBSCRIPTION_ITEMS:
            raise ValueError("music library %s exceeds item limit" % library_id)
        rows = page["Items"]
        if start == 0 and not rows and total > 0:
            raise ValueError(
                "music library %s returned an incomplete first page" % library_id
            )
        fresh = [row for row in rows if row.get("Id") and row["Id"] not in seen]
        if rows and not fresh:
            raise ValueError("repeated music-library page for %s" % library_id)
        for row in fresh:
            seen.add(row["Id"])
            if row.get("CanDownload") is not False:
                items.append(row["Id"])
        if len(seen) > MAX_SUBSCRIPTION_ITEMS:
            raise ValueError("music library %s exceeds item limit" % library_id)
        start += len(rows)
        if not rows:
            if total and start < total:
                raise ValueError(
                    "music library %s returned an incomplete page" % library_id
                )
            return items
        if total and start >= total:
            return items


def reconcile_music_libraries(api: Any) -> None:
    if not settings.get_bool("downloadsEnabled"):
        return
    for library_id in subscribed(LIBRARY_SETTING):
        try:
            reconcile(
                owner("musiclibrary", library_id), _library_items(api, library_id)
            )
        except Exception:
            LOG.exception("music-library subscription failed for %s", library_id)
