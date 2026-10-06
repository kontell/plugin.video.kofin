"""Kofin-owned SQLite and sync state, independent of Kodi's native databases.

The private schema is shared by the SQL and API distributions. Native database
paths, versions and creation-time seed data belong exclusively to the SQL
adapter. Test overrides and the per-path schema guard are restart-safe: their
keys are complete private paths, and reset_overrides clears both.
"""

import datetime
import json
import os
import sqlite3
from typing import Any, Dict, Optional

import xbmcvfs

from kofin.core.log import Logger
from kofin.sync import kofindb

LOG = Logger(__name__)
ADDON_DATA = "special://profile/addon_data/plugin.video.kofin/"
_path_overrides: Dict[str, str] = {}
_tables_ensured: set = set()


def set_path_override(kind: str, path: str) -> None:
    if kind != "kofin":
        raise ValueError("only Kofin-owned storage is supported")
    _path_overrides[kind] = path


def reset_overrides() -> None:
    _path_overrides.clear()
    _tables_ensured.clear()


def addon_data_path() -> str:
    return xbmcvfs.translatePath(ADDON_DATA)


def resolve_path(db_file: str = "kofin") -> str:
    if db_file != "kofin":
        raise ValueError("only Kofin-owned storage is supported")
    if db_file in _path_overrides:
        return _path_overrides[db_file]
    directory = addon_data_path()
    if not xbmcvfs.exists(ADDON_DATA):
        xbmcvfs.mkdirs(ADDON_DATA)
    return os.path.join(directory, "kofin.db")


class Database(object):
    """This should be called like a context.
    i.e. with Database('kofin') as db:
        db.cursor
        db.conn.commit()
    """

    timeout = 120

    def __init__(self, db_file: Optional[str] = None, commit_close: bool = True):
        """Open Kofin's own database; native kinds and literal paths are refused."""
        self.db_file = db_file or "kofin"
        self.commit_close = commit_close

    def __enter__(self) -> "Database":
        """Open the connection and return the Database class.
        This is to allow for the cursor, conn and others to be accessible.
        """
        self.path = self._resolve_path()
        self.conn = sqlite3.connect(self.path, timeout=self.timeout)
        try:
            self.cursor = self.conn.cursor()

            self._prepare()
        except BaseException:
            # __exit__ never runs when __enter__ raises, so the handle would
            # leak with the WAL lock held for the rest of the process.
            self.conn.close()
            raise

        return self

    def _resolve_path(self) -> str:
        return resolve_path(self.db_file)

    def _prepare(self) -> None:
        # WAL is configured only for Kofin's own file here.
        self.conn.execute("PRAGMA journal_mode=WAL")
        if self.path not in _tables_ensured:
            kofin_tables(self.cursor)
            self.conn.commit()
            _tables_ensured.add(self.path)

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Close the connection and cursor.

        The exception path rolls back to the last commit rather than
        committing: the fork committed unconditionally, which persisted the
        half of a multi-table write that had executed before a mid-item
        failure (audit finding #15). The writer passes commit per page /
        per COMMIT_INTERVAL and their restore points name the page being
        processed, so a rollback re-runs at most one page of idempotent
        writes on resume.
        """
        try:
            changes = self.conn.total_changes

            if exc_type is not None:  # errors raised
                LOG.error("type: %s value: %s", exc_type, exc_val)
                self.conn.rollback()
            elif self.commit_close and changes:

                LOG.debug("[%s] %s rows updated.", self.db_file, changes)
                self.conn.commit()
        finally:
            LOG.debug("---<[ database: %s ] %s", self.db_file, id(self.conn))
            self.cursor.close()
            self.conn.close()


def kofin_tables(cursor: "sqlite3.Cursor") -> None:
    """Create the mapping tables: jellyfin, view, version, boxset_state.

    jellyfin/view/version are byte-identical to the fork's jellyfin.db
    schema, fork indexes included (plan §2). The fork's jellyfin_parent_id
    column migration is dropped — kofin has no pre-existing installs.
    boxset_state is a kofin addition (docs/boxsets-robustness-plan.md).
    """
    cursor.execute("""CREATE TABLE IF NOT EXISTS jellyfin(
        jellyfin_id TEXT UNIQUE, media_folder TEXT, jellyfin_type TEXT, media_type TEXT,
        kodi_id INTEGER, kodi_fileid INTEGER, kodi_pathid INTEGER, parent_id INTEGER,
        checksum INTEGER, jellyfin_parent_id TEXT)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS view(
        view_id TEXT UNIQUE, view_name TEXT, media_type TEXT)""")
    cursor.execute("CREATE TABLE IF NOT EXISTS version(idVersion TEXT)")
    # Each set's MyVideos link count measured at its last successful
    # membership pass. Drift detection compares against it, so a set heals
    # even when its server Etag never moves (a member removed and re-added
    # comes back as a fresh movie row with no idSet and no Etag movement).
    # No timestamps: the L2 suite diffs whole database dumps byte-for-byte.
    # Absent rows read as "unknown" and force one relink per set — the
    # upgrade migration.
    cursor.execute("""CREATE TABLE IF NOT EXISTS boxset_state(
        jellyfin_id TEXT PRIMARY KEY, linked_count INTEGER NOT NULL)""")
    # Jellyfin playlists are Kodi files, not MyVideos/MyMusic rows, so they
    # do not belong in the jellyfin mapping table (no kodi_id). checksum is
    # the server Etag; filename is the basename under the managed folder.
    cursor.execute("""CREATE TABLE IF NOT EXISTS playlist_state(
        jellyfin_id TEXT PRIMARY KEY, media_type TEXT NOT NULL,
        filename TEXT NOT NULL, checksum TEXT NOT NULL)""")
    # Ordered Audio membership lets a download repoint rewrite just the
    # affected managed playlists without asking Jellyfin while offline.
    cursor.execute("""CREATE TABLE IF NOT EXISTS playlist_item(
        playlist_id TEXT NOT NULL, jellyfin_id TEXT NOT NULL,
        position INTEGER NOT NULL, PRIMARY KEY(playlist_id, position))""")
    cursor.execute("""CREATE INDEX IF NOT EXISTS playlist_item_song
        ON playlist_item(jellyfin_id)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS download_subscription(
        owner TEXT NOT NULL, jellyfin_id TEXT NOT NULL,
        PRIMARY KEY(owner, jellyfin_id))""")
    cursor.execute("""CREATE INDEX IF NOT EXISTS download_subscription_item
        ON download_subscription(jellyfin_id)""")
    # Offline downloads (docs/offline-downloads-plan.md, storage decisions).
    # Rows leave only on remove; ``state`` walks queued|active|done|failed.
    # ``series_id`` is denormalized so the tvshow tag injection is one
    # indexed lookup; ``rel_path`` is relative to the downloads root, which
    # is a setting and may move; ``quality`` records the request that
    # produced the file (nothing acts on it yet — it exists so a later
    # needs-redownload feature is a diff, not a guess); ``userdata_json``
    # snapshots the server's UserData at queue time for the phase-2
    # replay-conflict rule.
    cursor.execute("""CREATE TABLE IF NOT EXISTS download(
        jellyfin_id TEXT PRIMARY KEY, media_type TEXT, series_id TEXT,
        state TEXT, origin TEXT, rel_path TEXT, container TEXT,
        size_expected INTEGER, size_actual INTEGER, quality TEXT,
        bytes_done INTEGER, userdata_json TEXT,
        queued_at INTEGER, done_at INTEGER, error TEXT,
        restore_filename TEXT, segments_json TEXT, restore_path TEXT,
        request_id TEXT, request_name TEXT)""")
    # CREATE IF NOT EXISTS never revisits an existing table, and the download
    # table can materialize on a dev box between stacked PRs; additive columns
    # keep that cheap. A new column goes in the CREATE above *and* here.
    _ensure_columns(
        cursor,
        "download",
        {
            "restore_filename": "TEXT",
            "segments_json": "TEXT",
            "restore_path": "TEXT",
            "request_id": "TEXT",
            "request_name": "TEXT",
        },
    )

    cursor.execute("""CREATE INDEX IF NOT EXISTS idx_jellyfin_kodi
        ON jellyfin(kodi_id, media_type)""")
    cursor.execute("""CREATE INDEX IF NOT EXISTS idx_jellyfin_parent
        ON jellyfin(parent_id, media_type)""")
    cursor.execute("""CREATE INDEX IF NOT EXISTS idx_jellyfin_media_folder
        ON jellyfin(media_folder)""")
    cursor.execute("""CREATE INDEX IF NOT EXISTS idx_jellyfin_parent_id
        ON jellyfin(jellyfin_parent_id)""")
    # Userdata a playback produced while the server was unreachable, replayed
    # on the next connect (docs/offline-downloads-plan.md W2.4). One row per
    # item — a later event coalesces onto it — and NULL means "unchanged".
    cursor.execute("""CREATE TABLE IF NOT EXISTS pending_userdata(
        jellyfin_id TEXT PRIMARY KEY, media_type TEXT, played INTEGER,
        position_ticks INTEGER, event_at INTEGER, attempts INTEGER,
        server_snapshot TEXT)""")

    cursor.execute("""CREATE INDEX IF NOT EXISTS idx_download_series
        ON download(series_id, state)""")
    cursor.execute("""CREATE INDEX IF NOT EXISTS idx_download_state
        ON download(state, queued_at)""")
    # Every completed song asks "is the rest of my request still coming"
    # (store.request_counts), which on a hundred-track playlist is a hundred
    # scans of the table without this.
    cursor.execute("""CREATE INDEX IF NOT EXISTS idx_download_request
        ON download(request_id)""")


def _ensure_columns(
    cursor: "sqlite3.Cursor", table: str, columns: Dict[str, str]
) -> None:
    """ALTER TABLE ADD COLUMN for any listed column the table lacks."""
    cursor.execute("PRAGMA table_info(%s)" % table)
    present = {row[1] for row in cursor.fetchall()}
    for name, declaration in columns.items():
        if name in present:
            continue
        try:
            cursor.execute(
                "ALTER TABLE %s ADD COLUMN %s %s" % (table, name, declaration)
            )
        except sqlite3.OperationalError as error:
            # Two connections race this at service start (the main thread
            # and a download worker both open kofin.db): each reads the
            # PRAGMA before the other's ALTER commits, and the loser's
            # "duplicate column name" is the winner's success — seen live
            # the moment a second column joined this list.
            if "duplicate column" not in str(error).lower():
                raise


class SyncStateCorrupt(Exception):
    """sync.json exists but cannot be parsed.

    Raised rather than defaulted: an empty whitelist born from a bad read
    makes ``fields.find_library`` answer {} and the writers skip their items
    with the watermark moving past them — silent loss, the "film added on the
    22nd still missing on the 25th" shape. Raising instead lands each item in
    the workers' unapplied/recovery path, which is loud and heals.
    """


def get_sync() -> Dict[str, Any]:
    """The sync state record (pending libraries, restore points, whitelist).

    A missing or empty file answers defaults — a fresh install, or the
    truncate-then-crash a pre-atomic ``save_sync`` could leave behind.
    Content that exists but does not parse as a JSON object raises
    :class:`SyncStateCorrupt` (see its docstring for why defaulting is the
    dangerous answer).
    """
    if not xbmcvfs.exists(ADDON_DATA):
        xbmcvfs.mkdirs(ADDON_DATA)

    path = os.path.join(addon_data_path(), "sync.json")
    try:
        with open(path, "rb") as infile:
            raw = infile.read()
    except FileNotFoundError:
        raw = b""
    except OSError as error:
        raise SyncStateCorrupt("sync.json unreadable: %s" % error) from error

    sync: Dict[str, Any] = {}
    if raw.strip():
        try:
            loaded = json.loads(raw)
        except ValueError as error:
            raise SyncStateCorrupt("sync.json corrupt: %s" % error) from error
        if not isinstance(loaded, dict):
            raise SyncStateCorrupt(
                "sync.json corrupt: expected an object, found %s" % type(loaded)
            )
        sync = loaded

    sync["Libraries"] = sync.get("Libraries", [])
    sync["RestorePoints"] = sync.get("RestorePoints", {})
    sync["Whitelist"] = list(set(sync.get("Whitelist", [])))
    sync["SortedViews"] = sync.get("SortedViews", [])

    return sync


def save_sync(sync: Dict[str, Any]) -> None:
    """Write the record atomically: temp file, fsync, ``os.replace``.

    Writer threads call :func:`get_sync` per item while the library thread
    saves; the fork's in-place truncate-and-write let a reader land inside
    the window, parse half a file, and carry on with an empty whitelist.
    With the rename a reader sees the old record or the new one, never a
    mixture. The fsync keeps a power loss from replacing the record with an
    empty file on filesystems that defer allocation.
    """
    if not xbmcvfs.exists(ADDON_DATA):
        xbmcvfs.mkdirs(ADDON_DATA)

    sync["Date"] = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    path = os.path.join(addon_data_path(), "sync.json")
    temp_path = path + ".tmp"
    data = json.dumps(sync, sort_keys=True, indent=4, ensure_ascii=False)
    with open(temp_path, "wb") as outfile:
        outfile.write(data.encode("utf-8"))
        outfile.flush()
        os.fsync(outfile.fileno())
    os.replace(temp_path, path)


def get_item(kodi_id: int, media: str) -> Any:
    """Get the jellyfin reference row based on kodi id and media type."""
    from kofin.buildconfig import BACKEND

    if BACKEND != "sql":
        return None
    with Database("kofin") as kofin_db:
        item = kofindb.JellyfinDatabase(kofin_db.cursor).get_full_item_by_kodi_id(
            kodi_id, media
        )

        if not item:
            LOG.debug("not a kofin item")

            return None

    return item
