# -*- coding: utf-8 -*-
"""Database access for the sync stack (fork ``database/__init__.py`` port).

Changes from the fork (plan §3): the ``UpdateLibrary(video)`` discovery hack
is gone — :mod:`kofin.sync.schema` resolves and gates paths; the
``embyPathMigratedMusicDB`` migration is dropped (no legacy installs); the
mapping database is ``kofin.db`` with the fork's byte-identical schema (same
``jellyfin`` table name — renaming buys nothing and costs diff-ability).

sync.json keeps the fork's shape: pending ``Libraries``, ``RestorePoints``,
the synced ``Whitelist`` and ``SortedViews``. The settings-side
``librarySelection`` csv is the *desired* whitelist; sync.json records what
has actually been synced.

Allowed module-level state: the per-path "kofin tables ensured" guard and the
test path overrides. Both are idempotent and correct across service restarts
(the guard only skips re-running CREATE IF NOT EXISTS), so they are exempt
from the no-module-globals rule. Tests reset via :func:`reset_overrides`.
"""

import datetime
import json
import os
import sqlite3
from typing import Any, Dict, Optional

import xbmcvfs

from kofin.core.log import Logger
from kofin.sync import kofindb, schema

LOG = Logger(__name__)

ADDON_DATA = "special://profile/addon_data/plugin.video.kofin/"

KINDS = ("video", "music", "texture", "kofin")

_path_overrides: Dict[str, str] = {}
_tables_ensured: set = set()


def set_path_override(kind: str, path: str) -> None:
    """Point a database kind at an explicit file (tests/fixtures only)."""
    _path_overrides[kind] = path


def reset_overrides() -> None:
    _path_overrides.clear()
    _tables_ensured.clear()


def addon_data_path() -> str:
    return xbmcvfs.translatePath(ADDON_DATA)


def _kofin_db_path() -> str:
    return os.path.join(addon_data_path(), "kofin.db")


def resolve_path(db_file: str) -> str:
    """Resolve a kind or literal path to the sqlite file to open.

    Kind resolution goes through the schema gate — an unsupported Kodi
    database raises :class:`kofin.sync.schema.SchemaError` here, before
    anything is written.
    """
    if db_file in _path_overrides:
        return _path_overrides[db_file]

    if db_file == "kofin":
        directory = addon_data_path()
        if not xbmcvfs.exists(ADDON_DATA):
            xbmcvfs.mkdirs(ADDON_DATA)
        return os.path.join(directory, "kofin.db")

    if db_file in KINDS:
        return schema.database_path(db_file)

    return db_file  # literal path or :memory:


class Database(object):
    """This should be called like a context.
    i.e. with Database('kofin') as db:
        db.cursor
        db.conn.commit()
    """

    timeout = 120

    def __init__(self, db_file: Optional[str] = None, commit_close: bool = True):
        """file: kofin, texture, music, video, :memory: or path to file"""
        self.db_file = db_file or "video"
        self.commit_close = commit_close

    def __enter__(self) -> "Database":
        """Open the connection and return the Database class.
        This is to allow for the cursor, conn and others to be accessible.
        """
        self.path = resolve_path(self.db_file)
        self.conn = sqlite3.connect(self.path, timeout=self.timeout)
        self.cursor = self.conn.cursor()

        if self.db_file in KINDS:
            self.conn.execute(
                "PRAGMA journal_mode=WAL"
            )  # to avoid writing conflict with kodi

        LOG.debug("--->[ database: %s ] %s", self.db_file, id(self.conn))

        if self.db_file == "kofin" and self.path not in _tables_ensured:
            kofin_tables(self.cursor)
            self.conn.commit()
            _tables_ensured.add(self.path)

        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Close the connection and cursor."""
        changes = self.conn.total_changes

        if exc_type is not None:  # errors raised
            LOG.error("type: %s value: %s", exc_type, exc_val)

        if self.commit_close and changes:

            LOG.debug("[%s] %s rows updated.", self.db_file, changes)
            self.conn.commit()

        LOG.debug("---<[ database: %s ] %s", self.db_file, id(self.conn))
        self.cursor.close()
        self.conn.close()


def kofin_tables(cursor: "sqlite3.Cursor") -> None:
    """Create the mapping tables: jellyfin, view, version.

    Byte-identical to the fork's jellyfin.db schema, fork indexes included
    (plan §2). The fork's jellyfin_parent_id column migration is dropped —
    kofin has no pre-existing installs.
    """
    cursor.execute("""CREATE TABLE IF NOT EXISTS jellyfin(
        jellyfin_id TEXT UNIQUE, media_folder TEXT, jellyfin_type TEXT, media_type TEXT,
        kodi_id INTEGER, kodi_fileid INTEGER, kodi_pathid INTEGER, parent_id INTEGER,
        checksum INTEGER, jellyfin_parent_id TEXT)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS view(
        view_id TEXT UNIQUE, view_name TEXT, media_type TEXT)""")
    cursor.execute("CREATE TABLE IF NOT EXISTS version(idVersion TEXT)")

    cursor.execute("""CREATE INDEX IF NOT EXISTS idx_jellyfin_kodi
        ON jellyfin(kodi_id, media_type)""")
    cursor.execute("""CREATE INDEX IF NOT EXISTS idx_jellyfin_parent
        ON jellyfin(parent_id, media_type)""")
    cursor.execute("""CREATE INDEX IF NOT EXISTS idx_jellyfin_media_folder
        ON jellyfin(media_folder)""")
    cursor.execute("""CREATE INDEX IF NOT EXISTS idx_jellyfin_parent_id
        ON jellyfin(jellyfin_parent_id)""")


def get_sync() -> Dict[str, Any]:
    """The sync state record (pending libraries, restore points, whitelist)."""
    if not xbmcvfs.exists(ADDON_DATA):
        xbmcvfs.mkdirs(ADDON_DATA)

    sync: Dict[str, Any] = {}
    try:
        with open(os.path.join(addon_data_path(), "sync.json"), "rb") as infile:
            loaded = json.load(infile)
        if isinstance(loaded, dict):
            sync = loaded
    except Exception:
        sync = {}

    sync["Libraries"] = sync.get("Libraries", [])
    sync["RestorePoints"] = sync.get("RestorePoints", {})
    sync["Whitelist"] = list(set(sync.get("Whitelist", [])))
    sync["SortedViews"] = sync.get("SortedViews", [])

    return sync


def save_sync(sync: Dict[str, Any]) -> None:

    if not xbmcvfs.exists(ADDON_DATA):
        xbmcvfs.mkdirs(ADDON_DATA)

    sync["Date"] = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    with open(os.path.join(addon_data_path(), "sync.json"), "wb") as outfile:
        data = json.dumps(sync, sort_keys=True, indent=4, ensure_ascii=False)
        outfile.write(data.encode("utf-8"))


def get_item(kodi_id: int, media: str) -> Any:
    """Get the jellyfin reference row based on kodi id and media type."""
    with Database("kofin") as kofin_db:
        item = kofindb.JellyfinDatabase(kofin_db.cursor).get_full_item_by_kodi_id(
            kodi_id, media
        )

        if not item:
            LOG.debug("not a kofin item")

            return None

    return item
