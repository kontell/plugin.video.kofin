# -*- coding: utf-8 -*-
"""Mirror playlist and music-library download subscriptions.

The claim table records every standing order for a track. A download row has
only one origin; claims keep an overlap from being deleted when the first
subscription drops it. Every removal still goes through the manager.
"""

from typing import Any, Iterable, List

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


def reconcile(owner_id: str, item_ids: Iterable[str], cursor: Any = None) -> None:
    """Apply a complete membership snapshot, then queue its diff.

    The caller must supply a successful *complete* listing. An exception or
    capped page must never be turned into a partial deletion order.
    """
    wanted = set(item_ids)
    if len(wanted) > MAX_SUBSCRIPTION_ITEMS:
        raise ValueError("subscription %s exceeds item limit" % owner_id)
    if not settings.get_bool("downloadsEnabled"):
        return
    if cursor is None:
        with Database("kofin") as opened:
            queue, delete = _reconcile_on(opened.cursor, owner_id, wanted)
    else:
        queue, delete = _reconcile_on(cursor, owner_id, wanted)
    if queue:
        ordered = sorted(queue)
        for start in range(0, len(ordered), NOTIFY_BATCH_SIZE):
            batch = ordered[start : start + NOTIFY_BATCH_SIZE]
            ipc.notify(
                ipc.DOWNLOAD_ADD,
                {"Ids": batch, "Types": ["Audio"] * len(batch), "Origin": owner_id},
            )
    if delete:
        ordered = sorted(delete)
        for start in range(0, len(ordered), NOTIFY_BATCH_SIZE):
            ipc.notify(
                ipc.DOWNLOAD_REMOVE,
                {"Ids": ordered[start : start + NOTIFY_BATCH_SIZE]},
            )
    if queue or delete:
        LOG.info(
            "subscription %s: %d queued, %d removed", owner_id, len(queue), len(delete)
        )


def _reconcile_on(cursor: Any, owner_id: str, wanted: set):
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
    for item_id in wanted:
        cursor.execute("SELECT state FROM download WHERE jellyfin_id = ?", (item_id,))
        row = cursor.fetchone()
        if row is None or row[0] == store.FAILED:
            queue.append(item_id)
    for item_id in removed:
        cursor.execute(
            "SELECT 1 FROM download_subscription WHERE jellyfin_id = ? LIMIT 1",
            (item_id,),
        )
        if cursor.fetchone():
            continue
        cursor.execute("SELECT origin FROM download WHERE jellyfin_id = ?", (item_id,))
        row = cursor.fetchone()
        if row and str(row[0]).startswith(("auto:playlist:", "auto:musiclibrary:")):
            delete.append(item_id)
    return queue, delete


def release(owner_id: str) -> None:
    """Stop mirroring but leave already downloaded files on the device."""
    with Database("kofin") as opened:
        opened.cursor.execute(
            "DELETE FROM download_subscription WHERE owner = ?", (owner_id,)
        )


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
        total = int(page.get("TotalRecordCount") or 0)
        if total > MAX_SUBSCRIPTION_ITEMS:
            raise ValueError("music library %s exceeds item limit" % library_id)
        rows = page.get("Items") or []
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
        if not rows or (total and start >= total):
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
