# -*- coding: utf-8 -*-
"""Wipe-to-pristine cleanup behind Settings → Account → "Clean databases".

The migration cleaner (docs/clean-databases-plan.md): removes every trace of
kofin *and* jellyfin-kodi from Kodi's own databases, nodes and playlists, for
the two logged-out moments that need it — arriving from jellyfin-kodi, and
leaving kofin before an uninstall.

Semantics are deliberately *restore to pristine*, never "delete what looks
like ours": jellyfin-kodi direct-path rows are indistinguishable from native
library rows, and both addons' music rows share the ``/Audio/`` URL shape, so
row-level differentiation is unsound. Pristine is defined by the L2 fixtures:
``test_clean.py`` proves a cleaned database dumps byte-identical to a
creation-time one for every gated schema — including a database jellyfin-kodi's
own reset left *below* pristine (it deletes MyMusic's creation-time seed rows;
plan G2).

File sweeps are prefix-gated like every other deletion path in the repo:
``kofin*``/``jellyfin*`` entries only, so hand-made nodes and playlists
survive. The one exception is :func:`remove_all_nodes` — the explicit
user-nodes toggle — whose entire point is reverting the node tree to Kodi's
shipped defaults.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from typing import List, Optional, Tuple

import xbmcvfs

from kofin.core import settings
from kofin.core.log import Logger
from kofin.sync import schema
from kofin.sync.db import Database, addon_data_path
from kofin.sync.kodidb.texture import TextureCache
from kofin.sync.playlists import FOLDER_NAME as MANAGED_PLAYLIST_FOLDER

LOG = Logger(__name__)

LIBRARY_ROOT = "special://profile/library/"
VIDEO_NODES = "special://profile/library/video/"
MUSIC_NODES = "special://profile/library/music/"
PLAYLISTS_ROOT = "special://profile/playlists/"
THUMBNAILS = "special://thumbnails/"

# Settings the sync stack derives state from. Stale values after a wipe would
# resurrect the previous selection on the next login (librarySelection is the
# desired whitelist) or suppress node regeneration (viewsHash).
SYNC_SETTINGS = (
    "librarySelection",
    "lastIncrementalSync",
    "viewsHash",
    "syncedLibraries",
    "syncStatus",
)

# Both addons' cached server art shares this URL shape (every image kind lives
# under /Items/<id>/Images/); addon icons and skin assets do not, so the purge
# never touches them — unlike jellyfin-kodi's all-or-nothing texture reset
# (plan G4).
SERVER_ART_PATTERNS = ("http://%/Items/%", "https://%/Items/%")

_OWNED_PREFIXES = ("kofin", "jellyfin")


def preflight() -> None:
    """Validate every schema gate before anything is deleted.

    Never delete blind: the same rule as writing. Raises
    :class:`schema.SchemaError` and the caller aborts with nothing touched.
    """
    for kind in ("video", "music", "texture"):
        schema.check(kind)


def _tables(cursor: sqlite3.Cursor) -> List[str]:
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'"
    )
    return [row[0] for row in cursor.fetchall()]


def wipe_video(cursor: sqlite3.Cursor) -> None:
    """Every row written since creation goes; creation-time content stays.

    ``version`` is the schema's identity. ``videoversiontype`` keeps its seed
    rows — owner 0 is Kodi's own set — and loses only user-created types
    (``VIDEO_ASSET_OWNER_USER`` rows kofin or Kodi added since), which is what
    restores the seed-fixture state (plan G6).
    """
    for table in _tables(cursor):
        if table == "version":
            continue
        if table == "videoversiontype":
            cursor.execute("DELETE FROM videoversiontype WHERE owner != 0")
            continue
        cursor.execute("DELETE FROM %s" % table)


def wipe_music(cursor: sqlite3.Cursor) -> None:
    """Wipe plus re-seed.

    jellyfin-kodi's reset proved a blanket DELETE lands *below* pristine
    (plan G2), so the creation-time rows come back from
    :data:`schema.MUSIC_SEED_SQL` — which also repairs a database that other
    addon already damaged. The version is read off the database itself, not
    re-discovered: in production the gate already ran on open, and the row is
    the file's identity either way.
    """
    cursor.execute("SELECT idVersion FROM version")
    row = cursor.fetchone()
    version = int(row[0]) if row else 0
    seeds = schema.MUSIC_SEED_SQL.get(version)
    if seeds is None:
        raise schema.SchemaUnsupported("music", version)
    for table in _tables(cursor):
        if table == "version":
            continue
        cursor.execute("DELETE FROM %s" % table)
    for statement in seeds:
        cursor.execute(statement)


def clean_video_database() -> None:
    with Database("video") as opened:
        wipe_video(opened.cursor)
    LOG.info("video database wiped to pristine")


def clean_music_database() -> None:
    with Database("music") as opened:
        wipe_music(opened.cursor)
    LOG.info("music database wiped to pristine")


def music_debris_present() -> bool:
    """Whether either addon's music rows exist — the music prompt's default.

    Plugin rows are kofin's; ``/Audio/`` stream URLs are the shared shape of
    kofin direct rows and jellyfin-kodi addon-mode rows (the same family
    ``prune_orphan_paths`` matches in queries_music).
    """
    with Database("music") as opened:
        opened.cursor.execute(
            "SELECT EXISTS(SELECT 1 FROM path WHERE "
            "strPath LIKE 'plugin://plugin.video.kofin/%' "
            "OR strPath LIKE 'http://%/Audio/%' "
            "OR strPath LIKE 'https://%/Audio/%')"
        )
        return bool(opened.cursor.fetchone()[0])


def downloads_present() -> int:
    """How many downloads the store knows about, 0 if it cannot be read.

    Read before anything is deleted, so the prompt can be skipped entirely
    when there is nothing to offer.
    """
    try:
        from kofin.downloads import store

        return len(store.rows())
    except Exception:
        LOG.exception("download rows unavailable")
        return 0


def remove_downloads() -> int:
    """Delete every downloaded file, returning how many rows were swept.

    **Must run before :func:`remove_kofin_state`.** The ``download`` table
    lives in kofin.db, so once that file goes the only record of which files
    on disk were ours goes with it — which is precisely the orphaning this
    exists to prevent: before this, a clean deleted the mapping and left the
    media sitting there, unreferenced and unmanaged, for good.

    Deliberately not a recursive delete of the downloads root. The root is
    user-configurable, and pointing it at a folder shared with other media is
    allowed; each row's own files go, then empty directories are pruned back
    towards (never including) the root. Nothing repoints the Kodi rows first,
    as the full remove path does — the video database is about to be wiped
    wholesale, so there is nothing left to point anywhere.

    The one thing it takes beyond the rows is kofin's three category
    directories, and only if they are empty afterwards: this and "delete all
    downloads" are the two paths that mean *everything*, and they are the
    exception to the floor the per-row deletes stop at. By name and empty —
    a shared root keeps everything else in it.

    Errors reading the store are deliberately *not* swallowed. Without the
    rows there is no way to know which files were ours, so continuing would
    delete kofin.db and orphan them permanently while reporting success. The
    caller turns the raise into an error dialog and aborts with kofin.db still
    on disk; the wipe is idempotent, so running it again is the fix. A single
    file that will not delete is different, and only costs its own row.
    """
    from kofin.downloads import downloads_root, store
    from kofin.downloads.manager import delete_media_files, sweep_category_dirs

    swept = 0
    for row in store.rows():
        try:
            delete_media_files(row)
            swept += 1
        except Exception:
            LOG.exception("could not delete download %s", row.jellyfin_id)
    try:
        sweep_category_dirs(downloads_root())
    except Exception:  # pragma: no cover - a leftover folder is not a failure
        LOG.exception("could not sweep the download category folders")
    LOG.info("removed %d download(s) from disk", swept)
    return swept


def remove_kofin_state() -> List[str]:
    """Delete kofin.db (and WAL litter) plus sync.json.

    The mapping database must die with the Kodi rows: full sync skips items
    whose stored checksum matches (full_sync.py), so a surviving kofin.db
    turns the next login into an empty library that reports "synced".
    """
    removed: List[str] = []
    base = addon_data_path()
    for name in ("kofin.db", "kofin.db-wal", "kofin.db-shm", "sync.json"):
        removed.extend(_remove_file(os.path.join(base, name)))
    return removed


def remove_jellyfin_state(database_dir: Optional[str] = None) -> List[str]:
    """Delete jellyfin-kodi's mapping database files.

    They live in ``Database/`` — outside addon_data — so Kodi's uninstall
    never offers them, and jellyfin-kodi's own reset drops the tables but
    leaves the file shell and its WAL litter behind (plan G3).
    """
    base = database_dir or xbmcvfs.translatePath(schema.DATABASE_DIR)
    removed: List[str] = []
    for name in ("jellyfin.db", "jellyfin.db-wal", "jellyfin.db-shm"):
        removed.extend(_remove_file(os.path.join(base, name)))
    return removed


def clear_sync_settings() -> None:
    for setting_id in SYNC_SETTINGS:
        settings.set_str(setting_id, "")


def sweep_nodes(video_root: Optional[str] = None) -> List[str]:
    """Prefix-owned node entries at the video node root.

    Covers the ``kofin`` tree, the legacy flat layout (loose ``kofin*``
    folders and ``kofin_*.xml`` at the root), and jellyfin-kodi's
    ``jellyfin*`` files and folders — the sweep its own reset runs, for the
    user who uninstalled it without ever running that reset.
    """
    root = video_root or xbmcvfs.translatePath(VIDEO_NODES)
    return _sweep_prefixed(root)


def sweep_music_nodes(music_root: Optional[str] = None) -> List[str]:
    """The same sweep at the *music* node root.

    Kodi keeps a second node tree there and the video sweep never reaches it,
    so the ``kofin`` folder under it walked straight through a "Clean
    databases" — one stale file when the music side was a single Downloaded
    node, a whole per-library tree now.
    """
    root = music_root or xbmcvfs.translatePath(MUSIC_NODES)
    return _sweep_prefixed(root)


def remove_all_nodes(library_root: Optional[str] = None) -> List[str]:
    """The user-nodes toggle: delete the whole video and music node trees.

    Kodi reverts to its shipped default nodes. Removes hand-made and
    node-editor files by design — the dialog says so before this runs.
    """
    base = library_root or xbmcvfs.translatePath(LIBRARY_ROOT)
    removed: List[str] = []
    for sub in ("video", "music"):
        path = os.path.join(base, sub)
        if os.path.isdir(path):
            shutil.rmtree(path)
            LOG.info("removed node tree %s", path)
            removed.append(path)
    return removed


def sweep_playlists(playlists_root: Optional[str] = None) -> List[str]:
    """Both addons' generated playlists: the managed folders and the flat files.

    The folder is the ownership boundary in both trees — sibling files under
    ``playlists/music/`` and ``playlists/video/`` are the user's and stay. The
    two managed folders are named rather than swept: ``_sweep_prefixed``
    matches case-sensitively and the folder is ``Kofin``, so a prefix sweep
    walks straight past it.

    The flat sweep stays for what the folders replaced — jellyfin-kodi's
    ``jellyfin*.xsp`` and the ``kofin*.xsp`` kofin itself wrote there before
    ``views.PLAYLIST_FOLDER``.
    """
    base = playlists_root or xbmcvfs.translatePath(PLAYLISTS_ROOT)
    removed = _sweep_prefixed(os.path.join(base, "video"))
    for tree in ("video", "music"):
        managed = os.path.join(base, tree, MANAGED_PLAYLIST_FOLDER)
        if os.path.isdir(managed):
            shutil.rmtree(managed)
            LOG.info("removed %s", managed)
            removed.append(managed)
    return removed


def purge_server_art(thumbnails_dir: Optional[str] = None) -> int:
    """Drop cached server images: rows and their files.

    The ``sizes`` rows cascade through Kodi's own ``textureDelete`` trigger;
    the files sit at the CRC-named ``cachedurl`` under ``Thumbnails/``.
    """
    thumbs = thumbnails_dir or xbmcvfs.translatePath(THUMBNAILS)
    removed = 0
    with Database("texture") as opened:
        cache = TextureCache(opened.cursor)
        for pattern in SERVER_ART_PATTERNS:
            for _url, cachedurl in cache.remove_like(pattern):
                removed += 1
                _remove_file(os.path.join(thumbs, cachedurl))
    LOG.info("purged %d cached server images", removed)
    return removed


def _sweep_prefixed(
    root: str, prefixes: Tuple[str, ...] = _OWNED_PREFIXES
) -> List[str]:
    removed: List[str] = []
    if not os.path.isdir(root):
        return removed
    for entry in sorted(os.listdir(root)):
        if not entry.startswith(prefixes):
            continue
        path = os.path.join(root, entry)
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        LOG.info("removed %s", path)
        removed.append(path)
    return removed


def _remove_file(path: str) -> List[str]:
    if not os.path.isfile(path):
        return []
    os.remove(path)
    LOG.info("removed %s", path)
    return [path]
