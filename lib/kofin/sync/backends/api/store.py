"""Atomic publication and recovery state for every native kind, in Kofin's
database only.

Three facts, stored once each. ``api_item`` (the shared catalogue table)
holds one payload per item and its desired/applied generations.
``api_entry`` holds membership as intervals: a row says which scanner
directory an item belonged to from generation ``added`` until generation
``removed``, so a directory snapshot at any retained generation is a WHERE
clause rather than a copy of every payload. ``api_native`` is the validated
cache of Kodi identities with a small summary of what was applied -- the
keys Kofin owns and the userdata it last wrote -- never a second copy of the
payload.
"""

import hashlib
import json
import time
from collections import abc
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from kofin.sync.catalogue import Catalogue, ItemState
from kofin.sync.model import MediaItem
from kofin.core.log import Logger
from kofin.sync.private import Database
from . import paths
from .records import Record

LOG = Logger(__name__)

KINDS = (
    "Movie",
    "Series",
    "Season",
    "Episode",
    "MusicVideo",
    "BoxSet",
    "Audio",
    "MusicAlbum",
    "MusicArtist",
)


def namespace(server_id, user_id):
    if not server_id or not user_id:
        raise ValueError("server and user identity required")
    return hashlib.sha256((server_id + "/" + user_id).encode()).hexdigest()[:32]


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()[:24]


def payload_hash(payload):
    """The payload's digest, without ``UserData.Key``.

    The key names the row Jellyfin keeps the user's data under, not anything
    about the item, and the server does not give one answer for it: an album
    whose data was saved under ``<artist>-<album>`` before its MusicBrainz
    release group was known answers with that key or with the release
    group's, depending on Jellyfin's user data cache, which would move an
    unchanged album's hash."""
    userdata = payload.get("UserData") if isinstance(payload, dict) else None
    if isinstance(userdata, dict) and "Key" in userdata:
        payload = dict(
            payload, UserData={k: v for k, v in userdata.items() if k != "Key"}
        )
    return _digest(payload)


def hash_matches(acknowledged, payload) -> bool:
    """Whether an acknowledged digest is this payload's. One made before the
    key left the digest is of the whole payload, and still counts: refusing
    it would have the next repair rewrite every row once."""
    return bool(acknowledged) and acknowledged in (
        payload_hash(payload),
        _digest(payload),
    )


def tombstone(kind, item_id):
    """The payload kept for an applied removal: enough to replay, no more."""
    return {"Id": item_id, "Type": kind}


def parent_of(kind, payload) -> str:
    """The directory key an item is filed under, '' for a kind with none."""
    if kind in ("Season", "Episode"):
        if not payload.get("SeriesId"):
            raise ValueError("%s %s has no series" % (kind, payload.get("Id")))
        return str(payload["SeriesId"])
    if kind == "Audio":
        return paths.song_folder(payload)
    return ""


@dataclass(frozen=True)
class Entry:
    item_id: str
    kind: str
    library: str
    parent_id: str


@dataclass(frozen=True)
class Mapping:
    kodi_id: Optional[int]
    applied: Dict[str, Any]
    kind: str


# The scanner format of a library whose rows are being removed for a
# rebuild (library.rebuild_stale). It is below every real format, and it is
# in the store because the removal outlives a service restart.
REBUILDING = 0


class Placements(abc.Mapping):
    """The catalogue's placements at one generation, read by the item: a
    pass's own items up front (``preload``), any other as it is asked for,
    and every one at once for a caller that walks the map, or after
    ``WHOLE_AFTER`` single reads, when a caller is reading most of it. A
    pass that holds a few items reads a few rows, where the whole map is
    every item of every library."""

    WHOLE_AFTER = 200

    def __init__(self, store, generation, item_ids: Iterable[str] = ()):
        self._store = store
        self._generation = generation
        self._known: Dict[str, Optional[Entry]] = {}
        self._whole: Optional[Dict[str, Entry]] = None
        self._misses = 0
        self.preload(item_ids)

    def preload(self, item_ids: Iterable[str]) -> None:
        if self._whole is not None:
            return
        wanted = [i for i in item_ids if i not in self._known]
        if not wanted:
            return
        found = self._store.entries_of(wanted, self._generation)
        for item_id in wanted:
            self._known[item_id] = found.get(item_id)

    def whole(self) -> Dict[str, Entry]:
        if self._whole is None:
            self._whole = self._store.entries_at(self._generation)
        return self._whole

    def __getitem__(self, item_id: str) -> Entry:
        if self._whole is not None:
            return self._whole[item_id]
        if item_id not in self._known:
            self._misses += 1
            if self._misses > self.WHOLE_AFTER:
                return self.whole()[item_id]
            self.preload([item_id])
        found = self._known[item_id]
        if found is None:
            raise KeyError(item_id)
        return found

    def __iter__(self):
        return iter(self.whole())

    def __len__(self) -> int:
        return len(self.whole())


class Store(Catalogue):
    def _prepare(self, cursor):
        super()._prepare(cursor)
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS api_root(
                namespace TEXT PRIMARY KEY, current INTEGER NOT NULL DEFAULT 0,
                pinned INTEGER, server TEXT NOT NULL,
                watermark TEXT NOT NULL DEFAULT '', enumerated REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS api_entry(
                namespace TEXT NOT NULL, item_id TEXT NOT NULL,
                added INTEGER NOT NULL, removed INTEGER,
                kind TEXT NOT NULL, library TEXT NOT NULL, parent_id TEXT NOT NULL,
                PRIMARY KEY(namespace,item_id,added));
            CREATE INDEX IF NOT EXISTS api_entry_live ON api_entry(namespace, removed);
            CREATE INDEX IF NOT EXISTS api_entry_parent ON api_entry(namespace, kind, parent_id);
            CREATE TABLE IF NOT EXISTS api_native(
                namespace TEXT NOT NULL, item_id TEXT NOT NULL, kind TEXT NOT NULL,
                kodi_id INTEGER, applied TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(namespace,item_id));
            CREATE TABLE IF NOT EXISTS api_binding(
                namespace TEXT NOT NULL, path TEXT NOT NULL, content TEXT NOT NULL,
                PRIMARY KEY(namespace,path));
            CREATE TABLE IF NOT EXISTS api_salt(
                namespace TEXT NOT NULL, folder TEXT NOT NULL, salt INTEGER NOT NULL,
                PRIMARY KEY(namespace,folder));
            CREATE TABLE IF NOT EXISTS api_walk(
                namespace TEXT NOT NULL, library TEXT NOT NULL, position INTEGER NOT NULL,
                folder TEXT NOT NULL, label TEXT NOT NULL,
                PRIMARY KEY(namespace,library,position));
            CREATE TABLE IF NOT EXISTS api_expected(
                namespace TEXT NOT NULL, item_id TEXT NOT NULL, generation INTEGER NOT NULL,
                expires REAL NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(namespace,item_id));
            CREATE TABLE IF NOT EXISTS api_local_userdata(
                namespace TEXT NOT NULL, item_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(namespace,item_id));
            CREATE TABLE IF NOT EXISTS api_setup(
                id INTEGER PRIMARY KEY CHECK(id=1), namespace TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS api_held(
                setting TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS api_library(
                namespace TEXT NOT NULL, library TEXT NOT NULL,
                scanner INTEGER NOT NULL,
                PRIMARY KEY(namespace,library));
            CREATE TABLE IF NOT EXISTS api_features(
                namespace TEXT PRIMARY KEY, features TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS api_listing(
                namespace TEXT NOT NULL, library TEXT NOT NULL, fields TEXT NOT NULL,
                PRIMARY KEY(namespace,library));
        """)

    # -- the features of the Kodi the last pass ran on -----------------------

    def features_applied(self) -> Optional[FrozenSet[str]]:
        """The feature set (capabilities.features) the last completed pass
        shaped its state by; None before any pass stamped one."""
        stamp = self._applied()
        if stamp is None:
            return None
        names = stamp.get("features")
        if not isinstance(names, list):
            return None
        return frozenset(name for name in names if isinstance(name, str))

    def serializer_applied(self) -> Optional[int]:
        """The serializer (metadata.SERIALIZER) the last completed pass
        built its desired states with; None before any pass stamped one,
        and for a stamp of a build that recorded the features alone."""
        stamp = self._applied()
        number = (stamp or {}).get("serializer")
        if isinstance(number, int) and not isinstance(number, bool):
            return number
        return None

    def _applied(self) -> Optional[Dict[str, Any]]:
        with Database() as db:
            self._prepare(db.cursor)
            row = db.cursor.execute(
                "SELECT features FROM api_features WHERE namespace=?",
                (self.namespace,),
            ).fetchone()
        if row is None:
            return None
        try:
            stamp = json.loads(row[0])
        except ValueError:
            return None
        if isinstance(stamp, list):
            # A build before the serializer was stamped wrote the list.
            return {"features": stamp}
        return stamp if isinstance(stamp, dict) else None

    def remember_features(self, features, serializer: int) -> None:
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute(
                "INSERT OR REPLACE INTO api_features VALUES (?,?)",
                (
                    self.namespace,
                    json.dumps(
                        {"features": sorted(features), "serializer": serializer}
                    ),
                ),
            )

    # -- the scanner format a library's rows were filed under ----------------

    def scanner_format(self, library) -> int:
        """1 for a library with no record: its rows were filed before the
        record existed (metadata.SCANNER_FORMAT)."""
        with Database() as db:
            self._prepare(db.cursor)
            row = db.cursor.execute(
                "SELECT scanner FROM api_library WHERE namespace=? AND library=?",
                (self.namespace, library),
            ).fetchone()
        return int(row[0]) if row else 1

    def set_scanner_format(self, library, value):
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute(
                "INSERT OR REPLACE INTO api_library VALUES (?,?,?)",
                (self.namespace, library, int(value)),
            )

    def rebuilding(self) -> List[str]:
        """The libraries recorded as being rebuilt (REBUILDING): their rows
        are being removed so that the listing after imports them again."""
        with Database() as db:
            self._prepare(db.cursor)
            rows = db.cursor.execute(
                "SELECT library FROM api_library WHERE namespace=? AND scanner=?",
                (self.namespace, REBUILDING),
            ).fetchall()
        return [row[0] for row in rows]

    # -- what the status dialog counts -----------------------------------------

    def summary(self) -> Dict[Tuple[str, str], Tuple[int, int, int]]:
        """(library, kind) -> (items in the catalogue, of them in Kodi, of
        them settled without a row: what Kodi's scanner cannot file)."""
        with Database() as db:
            self._prepare(db.cursor)
            rows = db.cursor.execute(
                """SELECT e.library, e.kind, COUNT(*),
                SUM(CASE WHEN n.kodi_id IS NOT NULL THEN 1 ELSE 0 END),
                SUM(CASE WHEN n.item_id IS NOT NULL AND n.kodi_id IS NULL
                    AND i.status='applied' THEN 1 ELSE 0 END)
                FROM api_entry e
                JOIN api_item i ON i.namespace=e.namespace AND i.item_id=e.item_id
                LEFT JOIN api_native n
                ON n.namespace=e.namespace AND n.item_id=e.item_id
                WHERE e.namespace=? AND e.removed IS NULL
                GROUP BY e.library, e.kind""",
                (self.namespace,),
            ).fetchall()
        return {
            (row[0], row[1]): (int(row[2]), int(row[3] or 0), int(row[4] or 0))
            for row in rows
        }

    def unfiled(
        self, pairs: Iterable[Tuple[str, str]]
    ) -> List[Tuple[str, str, Dict[str, Any]]]:
        """(library, kind, payload) of the items of these ``(library, kind)``
        pairs that ``summary`` counts as settled without a row. The caller
        names the kinds it shows: a music library settles a guest artist
        or an album this way by the hundred, and their payloads stay put."""
        wanted = sorted(set(pairs))
        if not wanted:
            return []
        with Database() as db:
            self._prepare(db.cursor)
            rows = db.cursor.execute(
                """SELECT e.library, e.kind, i.payload
                FROM api_entry e
                JOIN api_item i ON i.namespace=e.namespace AND i.item_id=e.item_id
                JOIN api_native n ON n.namespace=e.namespace AND n.item_id=e.item_id
                WHERE e.namespace=? AND e.removed IS NULL
                AND n.kodi_id IS NULL AND i.status='applied' AND (%s)"""
                % " OR ".join("(e.library=? AND e.kind=?)" for _ in wanted),
                [self.namespace] + [value for pair in wanted for value in pair],
            ).fetchall()
        return [(row[0], row[1], json.loads(row[2])) for row in rows]

    def leaving(self) -> Set[str]:
        """The libraries with a removal still pending."""
        return {entry.library for entry in self.tombstones().values() if entry.library}

    def outstanding(self) -> Tuple[int, int, str]:
        """(items pending, of them failed at their last try, one failure)."""
        with Database() as db:
            self._prepare(db.cursor)
            pending, failed = db.cursor.execute(
                """SELECT COUNT(*), SUM(CASE WHEN error<>'' THEN 1 ELSE 0 END)
                FROM api_item WHERE namespace=? AND status='pending'""",
                (self.namespace,),
            ).fetchone()
            row = db.cursor.execute(
                """SELECT error FROM api_item
                WHERE namespace=? AND status='pending' AND error<>'' LIMIT 1""",
                (self.namespace,),
            ).fetchone()
        return int(pending or 0), int(failed or 0), row[0] if row else ""

    def has_rows(self, library) -> bool:
        """Whether any live item of the library has a native row."""
        with Database() as db:
            self._prepare(db.cursor)
            return bool(
                db.cursor.execute(
                    """SELECT 1 FROM api_native n JOIN api_entry e
                    ON e.namespace=n.namespace AND e.item_id=n.item_id
                    WHERE n.namespace=? AND n.kodi_id IS NOT NULL
                    AND e.removed IS NULL AND e.library=? LIMIT 1""",
                    (self.namespace, library),
                ).fetchone()
            )

    def removing(self, libraries) -> bool:
        """Whether a removal is still pending for an item last placed in
        one of these libraries."""
        libraries = list(libraries)
        if not libraries:
            return False
        with Database() as db:
            self._prepare(db.cursor)
            return bool(
                db.cursor.execute(
                    """SELECT 1 FROM api_item i JOIN api_entry e
                    ON e.namespace=i.namespace AND e.item_id=i.item_id
                    WHERE i.namespace=? AND i.operation='remove'
                    AND i.status='pending' AND e.library IN (%s)
                    AND e.added=(SELECT MAX(added) FROM api_entry x
                        WHERE x.namespace=e.namespace AND x.item_id=e.item_id)
                    LIMIT 1""" % ",".join("?" * len(libraries)),
                    [self.namespace] + libraries,
                ).fetchone()
            )

    # -- Kodi settings a scan owes back --------------------------------------

    def hold_setting(self, setting, value):
        """Record the value a Kodi setting had before a scan changed it. The
        first record wins: a hold a stopped Kodi left behind is the user's
        value, and the one read after it is the pass's own."""
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute(
                "INSERT OR IGNORE INTO api_held VALUES (?,?)",
                (setting, json.dumps(value)),
            )

    def held_settings(self) -> Dict[str, Any]:
        with Database() as db:
            self._prepare(db.cursor)
            return {
                setting: json.loads(value)
                for setting, value in db.cursor.execute(
                    "SELECT setting, value FROM api_held"
                ).fetchall()
            }

    def release_setting(self, setting):
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute("DELETE FROM api_held WHERE setting=?", (setting,))

    # -- lifecycle -----------------------------------------------------------

    def initialize(self, server):
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute(
                "INSERT OR IGNORE INTO api_root(namespace,server) VALUES (?,?)",
                (self.namespace, server),
            )
            db.cursor.execute(
                "UPDATE api_root SET server=? WHERE namespace=?",
                (server, self.namespace),
            )
        self._enable_incremental_vacuum()

    def _enable_incremental_vacuum(self):
        """Reclaim freed pages as they go; needs one VACUUM to take effect."""
        with Database() as db:
            mode = db.conn.execute("PRAGMA auto_vacuum").fetchone()[0]
            if mode == 2:
                return
            db.conn.commit()
            db.conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            db.conn.execute("VACUUM")

    def prepared(self):
        with Database() as db:
            self._prepare(db.cursor)
            row = db.cursor.execute(
                "SELECT namespace FROM api_setup WHERE id=1"
            ).fetchone()
            return row[0] if row else None

    def prepare_empty(self):
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute("INSERT INTO api_setup VALUES (1,?)", (self.namespace,))

    # -- publication ---------------------------------------------------------

    def publish(
        self,
        items,
        membership=None,
        library=None,
        removed=(),
        complete_libraries=None,
        complete_boxsets=False,
    ):
        """A validated listing, or a batch of explicit item changes.

        The caller must finish fetching and validating first: one transaction
        commits the desired payloads, their membership and every tombstone.
        ``library`` says the items are that library's complete listing;
        ``complete_libraries`` says the same for a union; ``complete_boxsets``
        says every collection is present. Nothing else implies deletion.
        """
        media = [MediaItem.from_dto(item) for item in items]
        parents = {}
        for medium in media:
            if medium.kind not in KINDS or not medium.payload.get("Name"):
                raise ValueError("complete %s metadata required" % medium.kind)
            parents[medium.item_id] = parent_of(medium.kind, medium.payload)
        if len({m.item_id for m in media}) != len(media):
            raise ValueError("duplicate identities")
        with Database() as db:
            cursor = db.cursor
            self._prepare(cursor)
            # executescript in _prepare commits implicitly; start only after DDL.
            cursor.execute("BEGIN IMMEDIATE")
            row = cursor.execute(
                "SELECT current, pinned FROM api_root WHERE namespace=?",
                (self.namespace,),
            ).fetchone()
            if not row:
                raise ValueError("directory not initialized")
            current, pinned = row
            following = current + 1
            incoming = {m.item_id for m in media}
            if library is None and not complete_libraries and not complete_boxsets:
                # Only a complete listing deletes by absence; a batch of
                # changes needs no placement but its own.
                live = self._live_entries(cursor, incoming | set(removed))
            else:
                live = {
                    e.item_id: e for e in self._entries(cursor, "removed IS NULL", ())
                }
            deleting = {i for i in removed if i in live}
            for item_id, entry in live.items():
                if item_id in incoming:
                    continue
                if library is not None and entry.library == library:
                    deleting.add(item_id)
                elif complete_libraries and entry.library in complete_libraries:
                    deleting.add(item_id)
                elif complete_boxsets and entry.kind == "BoxSet":
                    deleting.add(item_id)
            changed = False
            for medium in media:
                old = live.get(medium.item_id)
                item_library: Optional[str]
                if medium.kind == "BoxSet":
                    item_library = ""
                else:
                    item_library = (
                        membership.get(medium.item_id)
                        if isinstance(membership, dict)
                        else membership
                    )
                    if item_library is None and library is not None:
                        item_library = library
                    if item_library is None and old is not None:
                        item_library = old.library
                    if not item_library:
                        raise ValueError(
                            "new %s %s requires a selected library"
                            % (medium.kind, medium.item_id)
                        )
                parent = parents[medium.item_id]
                before = cursor.execute(
                    "SELECT desired FROM api_item WHERE namespace=? AND item_id=?",
                    (self.namespace, medium.item_id),
                ).fetchone()
                generation = self.stage_in(cursor, medium)
                if before is None or before[0] != generation:
                    changed = True
                entry = Entry(medium.item_id, medium.kind, item_library, parent)
                if old != entry:
                    changed = True
                    if old is not None:
                        cursor.execute(
                            "UPDATE api_entry SET removed=? WHERE namespace=? AND item_id=? AND removed IS NULL",
                            (following, self.namespace, medium.item_id),
                        )
                    cursor.execute(
                        "INSERT INTO api_entry VALUES (?,?,?,NULL,?,?,?)",
                        (
                            self.namespace,
                            medium.item_id,
                            following,
                            medium.kind,
                            item_library,
                            parent,
                        ),
                    )
                    if (
                        old is not None
                        and before is not None
                        and before[0] == generation
                    ):
                        # A move between libraries changes the native tags
                        # without touching the payload: it must still be work.
                        generation += 1
                        cursor.execute(
                            "UPDATE api_item SET desired=?,status='pending' WHERE namespace=? AND item_id=?",
                            (generation, self.namespace, medium.item_id),
                        )
                deleting.discard(medium.item_id)
            for item_id in sorted(deleting):
                entry = live[item_id]
                changed = True
                stored = cursor.execute(
                    "SELECT payload FROM api_item WHERE namespace=? AND item_id=?",
                    (self.namespace, item_id),
                ).fetchone()
                payload = (
                    json.loads(stored[0]) if stored else tombstone(entry.kind, item_id)
                )
                self.stage_in(cursor, MediaItem.from_dto(payload, entry.kind), "remove")
                cursor.execute(
                    "UPDATE api_entry SET removed=? WHERE namespace=? AND item_id=? AND removed IS NULL",
                    (following, self.namespace, item_id),
                )
            if not changed:
                return current
            cursor.execute(
                "UPDATE api_root SET current=? WHERE namespace=?",
                (following, self.namespace),
            )
            # A closed interval is reachable only through a retained
            # generation; the pin and the current one are the only two. A
            # pending removal keeps its last placement until it is applied.
            self._collect(cursor, pinned if pinned is not None else following)
            return following

    def _collect(self, cursor, floor, item_id=None):
        query = (
            "DELETE FROM api_entry WHERE namespace=? AND removed IS NOT NULL AND removed<=?"
            " AND item_id NOT IN (SELECT item_id FROM api_item WHERE namespace=?"
            " AND operation='remove' AND status='pending')"
        )
        params: List[Any] = [self.namespace, floor, self.namespace]
        if item_id is not None:
            query += " AND item_id=?"
            params.append(item_id)
        cursor.execute(query, params)

    def _entries(self, cursor, where, params) -> List[Entry]:
        rows = cursor.execute(
            "SELECT item_id, kind, library, parent_id FROM api_entry WHERE namespace=? AND "
            + where,
            (self.namespace,) + tuple(params),
        ).fetchall()
        return [Entry(*row) for row in rows]

    def _live_entries(self, cursor, item_ids) -> Dict[str, Entry]:
        ids = sorted(item_ids)
        found: Dict[str, Entry] = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            for entry in self._entries(
                cursor,
                "removed IS NULL AND item_id IN (%s)" % ",".join("?" * len(chunk)),
                chunk,
            ):
                found[entry.item_id] = entry
        return found

    def _generation(self, cursor, pinned):
        row = cursor.execute(
            "SELECT current, pinned FROM api_root WHERE namespace=?",
            (self.namespace,),
        ).fetchone()
        if not row:
            raise ValueError("no committed directory")
        current, pin = row
        return pin if pinned and pin is not None else current

    def entries(self, pinned=True, kind=None) -> Dict[str, Entry]:
        """Membership at the pinned generation (or the current one), of one
        kind when asked."""
        with Database() as db:
            self._prepare(db.cursor)
            generation = self._generation(db.cursor, pinned)
            where = "added<=? AND (removed IS NULL OR removed>?)"
            params: tuple = (generation, generation)
            if kind is not None:
                where += " AND kind=?"
                params += (kind,)
            return {e.item_id: e for e in self._entries(db.cursor, where, params)}

    def placements(self, item_ids: Iterable[str] = ()) -> "Placements":
        """Membership at the pinned generation, read by the item: ``item_ids``
        now, the rest as it is asked for (``Placements``)."""
        with Database() as db:
            self._prepare(db.cursor)
            generation = self._generation(db.cursor, True)
        return Placements(self, generation, item_ids)

    def entries_of(self, item_ids: Iterable[str], generation) -> Dict[str, Entry]:
        """The placements of ``item_ids`` at ``generation``, a few hundred to
        a query."""
        ids = sorted(set(item_ids))
        found: Dict[str, Entry] = {}
        with Database() as db:
            self._prepare(db.cursor)
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                for entry in self._entries(
                    db.cursor,
                    "added<=? AND (removed IS NULL OR removed>?) AND item_id IN (%s)"
                    % ",".join("?" * len(chunk)),
                    [generation, generation] + chunk,
                ):
                    found[entry.item_id] = entry
        return found

    def entries_at(self, generation) -> Dict[str, Entry]:
        with Database() as db:
            self._prepare(db.cursor)
            return {
                e.item_id: e
                for e in self._entries(
                    db.cursor,
                    "added<=? AND (removed IS NULL OR removed>?)",
                    (generation, generation),
                )
            }

    def entry(self, item_id) -> Optional[Entry]:
        """The current placement of one item, tombstoned items excluded."""
        with Database() as db:
            self._prepare(db.cursor)
            rows = self._entries(db.cursor, "item_id=? AND removed IS NULL", (item_id,))
        return rows[0] if rows else None

    def known(self, item_ids: Iterable[str]) -> Set[str]:
        """Which of ``item_ids`` the catalogue places now, as ``entry`` reads
        one: an item whose removal is pending is not one of them."""
        ids = list(item_ids)
        found: Set[str] = set()
        with Database() as db:
            self._prepare(db.cursor)
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                found.update(
                    row[0]
                    for row in db.cursor.execute(
                        "SELECT item_id FROM api_entry WHERE namespace=?"
                        " AND removed IS NULL AND item_id IN (%s)"
                        % ",".join("?" * len(chunk)),
                        [self.namespace] + chunk,
                    )
                )
        return found

    def states(self, item_ids: Iterable[str]) -> Dict[str, ItemState]:
        """``state`` for many items, a few hundred to a query; an item the
        catalogue has no row of is left out."""
        ids = list(item_ids)
        result: Dict[str, ItemState] = {}
        with Database() as db:
            self._prepare(db.cursor)
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                for row in db.cursor.execute(
                    "SELECT item_id, desired, applied, operation, status, error"
                    " FROM api_item WHERE namespace=? AND item_id IN (%s)"
                    % ",".join("?" * len(chunk)),
                    [self.namespace] + chunk,
                ):
                    result[row[0]] = ItemState(*row[1:])
        return result

    def tombstones(self) -> Dict[str, Entry]:
        """The last known placement of every item with a pending removal."""
        with Database() as db:
            self._prepare(db.cursor)
            # From the pending removals (api_item_status): joined the other
            # way, SQLite walks every placement with a subquery each.
            rows = db.cursor.execute(
                """SELECT e.item_id, e.kind, e.library, e.parent_id FROM api_entry e
                WHERE e.namespace=? AND e.item_id IN (SELECT item_id FROM api_item
                    WHERE namespace=? AND status='pending' AND operation='remove')
                AND e.added=(SELECT MAX(added) FROM api_entry x
                    WHERE x.namespace=e.namespace AND x.item_id=e.item_id)""",
                (self.namespace, self.namespace),
            ).fetchall()
            return {row[0]: Entry(*row) for row in rows}

    def payload(self, item_id: str) -> Dict[str, Any]:
        """One item's desired payload."""
        with Database() as db:
            self._prepare(db.cursor)
            row = db.cursor.execute(
                "SELECT payload FROM api_item WHERE namespace=? AND item_id=?",
                (self.namespace, item_id),
            ).fetchone()
        return json.loads(row[0]) if row else {}

    def payloads(self, item_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        """The desired payloads of ``item_ids``, a few hundred to a query."""
        ids = list(item_ids)
        result: Dict[str, Dict[str, Any]] = {}
        if not ids:
            return result
        with Database() as db:
            self._prepare(db.cursor)
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                rows = db.cursor.execute(
                    "SELECT item_id, payload FROM api_item WHERE namespace=? AND item_id IN (%s)"
                    % ",".join("?" * len(chunk)),
                    [self.namespace] + chunk,
                ).fetchall()
                for item_id, payload in rows:
                    result[item_id] = json.loads(payload)
        return result

    def pending_work(self) -> List[Tuple[str, int, str, Dict[str, Any]]]:
        """Every pending item with its generation and operation, in id order.

        A removal carries its tombstone (it names what it removes); an
        upsert carries ``{}``, its payload read on demand through a
        ``PayloadWindow``, so a first import or a repair never holds the
        catalogue in memory.
        """
        with Database() as db:
            self._prepare(db.cursor)
            rows = db.cursor.execute(
                """SELECT item_id, desired, operation,
                CASE WHEN operation='remove' THEN payload END
                FROM api_item WHERE namespace=? AND status='pending' ORDER BY item_id""",
                (self.namespace,),
            ).fetchall()
        return [
            (item_id, generation, operation, json.loads(payload) if payload else {})
            for item_id, generation, operation, payload in rows
        ]

    def records(
        self,
        kind=None,
        library=None,
        parent_id=None,
        item_ids=None,
        pinned=True,
        payloads=True,
    ) -> Dict[str, Record]:
        """Payloads of the members a scanner callback or a repair pass needs.

        With ``payloads=False`` the records come without their payloads and
        read them on first use, one at a time.
        """
        where = ["e.added<=?", "(e.removed IS NULL OR e.removed>?)"]
        t0 = time.monotonic()
        with Database() as db:
            t1 = time.monotonic()
            self._prepare(db.cursor)
            t2 = time.monotonic()
            generation = self._generation(db.cursor, pinned)
            t3 = time.monotonic()
            params: List[Any] = [generation, generation]
            if kind is not None:
                kinds = (kind,) if isinstance(kind, str) else tuple(kind)
                where.append("e.kind IN (%s)" % ",".join("?" * len(kinds)))
                params.extend(kinds)
            if library is not None:
                where.append("e.library=?")
                params.append(library)
            if parent_id is not None:
                where.append("e.parent_id=?")
                params.append(parent_id)
            if item_ids is not None:
                ids = list(item_ids)
                if not ids:
                    return {}
                where.append("e.item_id IN (%s)" % ",".join("?" * len(ids)))
                params.extend(ids)
            rows = db.cursor.execute(
                """SELECT e.item_id, e.kind, e.library, e.parent_id, %s, i.desired
                FROM api_entry e JOIN api_item i ON i.namespace=e.namespace AND i.item_id=e.item_id
                WHERE e.namespace=? AND """ % ("i.payload" if payloads else "NULL")
                + " AND ".join(where),
                [self.namespace] + params,
            ).fetchall()
        t4 = time.monotonic()
        if t4 - t0 > 1.0:
            LOG.debug(
                "slow records(%s): open %.2f prepare %.2f generation %.2f query %.2f s",
                kind,
                t1 - t0,
                t2 - t1,
                t3 - t2,
                t4 - t3,
            )
        return {
            row[0]: Record(
                row[0],
                row[1],
                row[2],
                row[3],
                json.loads(row[4]) if row[4] is not None else None,
                row[5],
                loader=None if row[4] is not None else self.payload,
            )
            for row in rows
        }

    def folders(self, kind, library, pinned=True) -> List[str]:
        """The directory keys live items of ``kind`` file under, without
        loading a payload."""
        with Database() as db:
            self._prepare(db.cursor)
            generation = self._generation(db.cursor, pinned)
            rows = db.cursor.execute(
                """SELECT DISTINCT parent_id FROM api_entry WHERE namespace=? AND kind=?
                AND library=? AND added<=? AND (removed IS NULL OR removed>?)""",
                (self.namespace, kind, library, generation, generation),
            ).fetchall()
        return [row[0] for row in rows if row[0]]

    def libraries(self, kind) -> List[str]:
        """The libraries with a live item of ``kind``."""
        with Database() as db:
            self._prepare(db.cursor)
            rows = db.cursor.execute(
                "SELECT DISTINCT library FROM api_entry WHERE namespace=? AND kind=? AND removed IS NULL",
                (self.namespace, kind),
            ).fetchall()
        return sorted(row[0] for row in rows if row[0])

    def item(self, item_id) -> Optional[Dict[str, Any]]:
        with Database() as db:
            self._prepare(db.cursor)
            row = db.cursor.execute(
                "SELECT payload FROM api_item WHERE namespace=? AND item_id=? AND operation='upsert'",
                (self.namespace, item_id),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def server(self):
        with Database() as db:
            self._prepare(db.cursor)
            row = db.cursor.execute(
                "SELECT server FROM api_root WHERE namespace=?", (self.namespace,)
            ).fetchone()
        return row[0] if row else ""

    def generation(self):
        with Database() as db:
            self._prepare(db.cursor)
            row = db.cursor.execute(
                "SELECT current FROM api_root WHERE namespace=?", (self.namespace,)
            ).fetchone()
        return row[0] if row else 0

    def pin(self):
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute(
                "UPDATE api_root SET pinned=COALESCE(pinned,current) WHERE namespace=? AND current>0",
                (self.namespace,),
            )
            return self._generation(db.cursor, True)

    def unpin(self):
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute(
                "UPDATE api_root SET pinned=NULL WHERE namespace=?", (self.namespace,)
            )

    # -- native identities ---------------------------------------------------

    def populated(self, kind=None):
        """Whether any owned item of ``kind`` (or any kind) has a native row."""
        with Database() as db:
            self._prepare(db.cursor)
            query = "SELECT 1 FROM api_native WHERE namespace=? AND kodi_id IS NOT NULL"
            params: Tuple[Any, ...] = (self.namespace,)
            if kind is not None:
                query += " AND kind=?"
                params += (kind,)
            return bool(db.cursor.execute(query + " LIMIT 1", params).fetchone())

    def mapping(self, item_id) -> Optional[Mapping]:
        with Database() as db:
            self._prepare(db.cursor)
            row = db.cursor.execute(
                "SELECT kodi_id, applied, kind FROM api_native WHERE namespace=? AND item_id=?",
                (self.namespace, item_id),
            ).fetchone()
        if not row:
            return None
        return Mapping(row[0], json.loads(row[1] or "{}"), row[2])

    def mappings(self, kind=None, library=None) -> Dict[str, Mapping]:
        where = ["n.namespace=?"]
        params: List[Any] = [self.namespace]
        if kind is not None:
            where.append("n.kind=?")
            params.append(kind)
        if library is not None:
            where.append(
                "EXISTS (SELECT 1 FROM api_entry e WHERE e.namespace=n.namespace"
                " AND e.item_id=n.item_id AND e.removed IS NULL AND e.library=?)"
            )
            params.append(library)
        with Database() as db:
            self._prepare(db.cursor)
            rows = db.cursor.execute(
                "SELECT n.item_id, n.kodi_id, n.applied, n.kind FROM api_native n WHERE "
                + " AND ".join(where),
                params,
            ).fetchall()
        return {
            row[0]: Mapping(row[1], json.loads(row[2] or "{}"), row[3]) for row in rows
        }

    def placed_in(self, kind, library, parent_ids: Iterable[str]) -> List[str]:
        """The items of ``kind`` the catalogue places now under ``parent_ids``
        of ``library``, a few hundred parents to a query (api_entry_parent)."""
        parents = sorted(set(parent_ids))
        found: List[str] = []
        with Database() as db:
            self._prepare(db.cursor)
            for start in range(0, len(parents), 500):
                chunk = parents[start : start + 500]
                found.extend(
                    row[0]
                    for row in db.cursor.execute(
                        "SELECT item_id FROM api_entry WHERE namespace=? AND kind=?"
                        " AND parent_id IN (%s) AND library=? AND removed IS NULL"
                        % ",".join("?" * len(chunk)),
                        [self.namespace, kind] + chunk + [library],
                    )
                )
        return found

    def mappings_of(self, item_ids: Iterable[str]) -> Dict[str, Mapping]:
        """The mappings of ``item_ids``, a few hundred to a query; an item
        with no mapping is left out."""
        ids = list(item_ids)
        found: Dict[str, Mapping] = {}
        with Database() as db:
            self._prepare(db.cursor)
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                for row in db.cursor.execute(
                    "SELECT item_id, kodi_id, applied, kind FROM api_native"
                    " WHERE namespace=? AND item_id IN (%s)"
                    % ",".join("?" * len(chunk)),
                    [self.namespace] + chunk,
                ):
                    found[row[0]] = Mapping(row[1], json.loads(row[2] or "{}"), row[3])
        return found

    def mapped_to(self, kind, kodi_id) -> List[str]:
        """The items of ``kind`` acknowledged against Kodi's row ``kodi_id``."""
        with Database() as db:
            self._prepare(db.cursor)
            rows = db.cursor.execute(
                "SELECT item_id FROM api_native WHERE namespace=? AND kind=? AND kodi_id=?",
                (self.namespace, kind, kodi_id),
            ).fetchall()
        return sorted(row[0] for row in rows)

    def movies_with_extras(
        self, has_extras: Callable[[Dict[str, Any]], bool]
    ) -> Tuple[Dict[str, Entry], Set[str]]:
        """The movies at the pinned generation, as ``entries`` reads them,
        and which of them an acknowledgement lists extras for
        (metadata.asset_files), in one read (Native.bind_movie_folders).

        An acknowledgement older than the file list carries the assets token
        alone, which cannot tell extras from versions, and a movie whose
        token never moved is never planned again to gain the list. Such a
        movie is asked of its payload (``has_extras``), a chunk at a time, so
        the read holds no library's payloads."""
        with Database() as db:
            self._prepare(db.cursor)
            generation = self._generation(db.cursor, True)
            entries = {
                e.item_id: e
                for e in self._entries(
                    db.cursor,
                    "added<=? AND (removed IS NULL OR removed>?) AND kind=?",
                    (generation, generation, "Movie"),
                )
            }
            rows = db.cursor.execute(
                "SELECT item_id, applied FROM api_native"
                " WHERE namespace=? AND kind='Movie'",
                (self.namespace,),
            ).fetchall()
            extras = set()
            older = []
            for item_id, applied in rows:
                acknowledged = json.loads(applied or "{}")
                if "asset_files" in acknowledged:
                    if (acknowledged["asset_files"] or {}).get("extras"):
                        extras.add(item_id)
                elif acknowledged.get("assets") and item_id in entries:
                    older.append(item_id)
            for start in range(0, len(older), 500):
                chunk = older[start : start + 500]
                payloads = db.cursor.execute(
                    "SELECT item_id, payload FROM api_item WHERE namespace=? AND item_id IN (%s)"
                    % ",".join("?" * len(chunk)),
                    [self.namespace] + chunk,
                ).fetchall()
                for item_id, payload in payloads:
                    if has_extras(json.loads(payload)):
                        extras.add(item_id)
        return entries, extras

    def remember(self, item_id, generation, kodi_id, applied, kind=None):
        """Acknowledge readback of exactly the desired generation."""
        return self.remember_many([(item_id, generation, kodi_id, applied, kind)]) == 1

    def remember_many(self, rows):
        """One transaction for a batch of acknowledgements; returns how many
        matched their desired generation. A pass over a real catalogue is
        thousands of these, and one commit each cost more than the pass."""
        count = 0
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute("BEGIN IMMEDIATE")
            for item_id, generation, kodi_id, applied, kind in rows:
                db.cursor.execute(
                    """UPDATE api_item SET applied=desired,status='applied',error=''
                    WHERE namespace=? AND item_id=? AND desired=? AND operation='upsert'""",
                    (self.namespace, item_id, generation),
                )
                if not db.cursor.rowcount:
                    continue
                count += 1
                if kind is None:
                    kind = db.cursor.execute(
                        "SELECT kind FROM api_item WHERE namespace=? AND item_id=?",
                        (self.namespace, item_id),
                    ).fetchone()[0]
                db.cursor.execute(
                    """INSERT INTO api_native(namespace,item_id,kind,kodi_id,applied) VALUES (?,?,?,?,?)
                    ON CONFLICT(namespace,item_id) DO UPDATE SET kind=excluded.kind,
                    kodi_id=excluded.kodi_id, applied=excluded.applied""",
                    (self.namespace, item_id, kind, kodi_id, encode(applied or {})),
                )
        return count

    def forget(self, item_id, generation):
        """Acknowledge a removal: the native identity goes, the payload shrinks."""
        return self.forget_many([(item_id, generation)]) == 1

    def forget_many(self, rows):
        """One transaction for a batch of removal acknowledgements; returns
        how many matched their desired generation."""
        count = 0
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute("BEGIN IMMEDIATE")
            root = db.cursor.execute(
                "SELECT current, pinned FROM api_root WHERE namespace=?",
                (self.namespace,),
            ).fetchone()
            for item_id, generation in rows:
                row = db.cursor.execute(
                    "SELECT kind FROM api_item WHERE namespace=? AND item_id=? AND desired=? AND operation='remove'",
                    (self.namespace, item_id, generation),
                ).fetchone()
                if not row:
                    continue
                count += 1
                db.cursor.execute(
                    """UPDATE api_item SET applied=desired,status='applied',error='',payload=?
                    WHERE namespace=? AND item_id=?""",
                    (encode(tombstone(row[0], item_id)), self.namespace, item_id),
                )
                db.cursor.execute(
                    "DELETE FROM api_native WHERE namespace=? AND item_id=?",
                    (self.namespace, item_id),
                )
                db.cursor.execute(
                    "DELETE FROM api_expected WHERE namespace=? AND item_id=?",
                    (self.namespace, item_id),
                )
            if count and root:
                # One collection for the batch: the query's "still pending"
                # subquery scans the item table.
                self._collect(db.cursor, root[1] if root[1] is not None else root[0])
        return count

    def invalidate(self, item_ids: Iterable[str]):
        """Put applied upserts back to pending; their desired state is unchanged."""
        ids = list(item_ids)
        if not ids:
            return
        with Database() as db:
            self._prepare(db.cursor)
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                db.cursor.execute(
                    "UPDATE api_item SET status='pending' WHERE namespace=? AND operation='upsert'"
                    " AND item_id IN (%s)" % ",".join("?" * len(chunk)),
                    [self.namespace] + chunk,
                )

    # -- scanner bindings, watermarks --------------------------------------------

    def bindings(self) -> Dict[str, str]:
        with Database() as db:
            self._prepare(db.cursor)
            return dict(
                db.cursor.execute(
                    "SELECT path, content FROM api_binding WHERE namespace=?",
                    (self.namespace,),
                ).fetchall()
            )

    def bind(self, path, content):
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute(
                "INSERT OR REPLACE INTO api_binding VALUES (?,?,?)",
                (self.namespace, path, content),
            )

    def salt(self, folder) -> int:
        """How many times a music directory's listing has been salted: a
        directory whose scan left its songs missing is one Kodi
        skips "due to no change" (its hash was stored by a scan stopped
        before the songs were written); salting the dates moves the hash."""
        with Database() as db:
            self._prepare(db.cursor)
            row = db.cursor.execute(
                "SELECT salt FROM api_salt WHERE namespace=? AND folder=?",
                (self.namespace, folder),
            ).fetchone()
        return int(row[0]) if row else 0

    def salts(self) -> Dict[str, int]:
        """Every salted directory with its salt, in one read."""
        with Database() as db:
            self._prepare(db.cursor)
            rows = db.cursor.execute(
                "SELECT folder, salt FROM api_salt WHERE namespace=?",
                (self.namespace,),
            ).fetchall()
        return {folder: int(salt) for folder, salt in rows}

    def plan_walk(self, library, rows):
        """The album directories a scan of the library is to visit, in order,
        each with the label it is listed under. The pass writes the plan
        before the scan and the provider lists it a part at a time
        (``walk_part``), reading no payload."""
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute("BEGIN IMMEDIATE")
            db.cursor.execute(
                "DELETE FROM api_walk WHERE namespace=? AND library=?",
                (self.namespace, library),
            )
            db.cursor.executemany(
                "INSERT INTO api_walk VALUES (?,?,?,?,?)",
                [
                    (self.namespace, library, position, folder, label)
                    for position, (folder, label) in enumerate(rows)
                ],
            )

    def walk_part(self, library, index, size) -> List[Tuple[str, str]]:
        """(folder, label) of the directories in one part of the plan."""
        with Database() as db:
            self._prepare(db.cursor)
            rows = db.cursor.execute(
                """SELECT folder, label FROM api_walk WHERE namespace=? AND library=?
                AND position>=? AND position<? ORDER BY position""",
                (self.namespace, library, index * size, (index + 1) * size),
            ).fetchall()
        return [(folder, label) for folder, label in rows]

    def walk_size(self, library) -> int:
        """How many directories the library's plan names."""
        with Database() as db:
            self._prepare(db.cursor)
            row = db.cursor.execute(
                "SELECT COUNT(*) FROM api_walk WHERE namespace=? AND library=?",
                (self.namespace, library),
            ).fetchone()
        return int(row[0]) if row else 0

    def bump_salt(self, folder) -> int:
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute(
                """INSERT INTO api_salt VALUES (?,?,1)
                ON CONFLICT(namespace, folder) DO UPDATE SET salt=salt+1""",
                (self.namespace, folder),
            )
        return self.salt(folder)

    def bind_many(self, pairs):
        """``bind`` for a batch of (path, content), one transaction."""
        pairs = list(pairs)
        if not pairs:
            return
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.executemany(
                "INSERT OR REPLACE INTO api_binding VALUES (?,?,?)",
                [(self.namespace, path, content) for path, content in pairs],
            )

    def unbind(self, prefix):
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute(
                "DELETE FROM api_binding WHERE namespace=? AND SUBSTR(path,1,?)=?",
                (self.namespace, len(prefix), prefix),
            )

    def watermark(self):
        with Database() as db:
            self._prepare(db.cursor)
            row = db.cursor.execute(
                "SELECT watermark, enumerated FROM api_root WHERE namespace=?",
                (self.namespace,),
            ).fetchone()
        return (row[0], row[1]) if row else ("", 0.0)

    def set_watermark(self, watermark=None, enumerated=None):
        with Database() as db:
            self._prepare(db.cursor)
            if watermark is not None:
                db.cursor.execute(
                    "UPDATE api_root SET watermark=? WHERE namespace=?",
                    (watermark, self.namespace),
                )
            if enumerated is not None:
                db.cursor.execute(
                    "UPDATE api_root SET enumerated=? WHERE namespace=?",
                    (enumerated, self.namespace),
                )

    def listings(self) -> Dict[str, str]:
        """library -> what its last published listing asked the server for
        (library.LISTING); a library with none is missing."""
        with Database() as db:
            self._prepare(db.cursor)
            rows = db.cursor.execute(
                "SELECT library, fields FROM api_listing WHERE namespace=?",
                (self.namespace,),
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def remember_listing(self, libraries: Iterable[str], fields: str) -> None:
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.executemany(
                "INSERT OR REPLACE INTO api_listing VALUES (?,?,?)",
                [(self.namespace, library, fields) for library in libraries],
            )

    # -- userdata echoes and the local outbox --------------------------------

    def expect(self, item_id, generation, userdata):
        self.expect_many([(item_id, generation, userdata)])

    def expect_many(self, expectations):
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.executemany(
                "INSERT OR REPLACE INTO api_expected VALUES (?,?,?,?,?)",
                [
                    (
                        self.namespace,
                        item_id,
                        generation,
                        time.time() + 120,
                        encode(userdata),
                    )
                    for item_id, generation, userdata in expectations
                ],
            )

    def is_echo(self, item_id, field, value):
        with Database() as db:
            self._prepare(db.cursor)
            row = db.cursor.execute(
                """SELECT e.payload FROM api_expected e JOIN api_item i USING(namespace,item_id)
                WHERE e.namespace=? AND e.item_id=? AND e.expires>?
                AND e.generation=i.desired""",
                (self.namespace, item_id, time.time()),
            ).fetchone()
        if not row:
            return False
        expected = json.loads(row[0]).get(field)
        if field == "resume":
            # Match native readback's precision; runtime is not a local edit.
            return bool(
                expected is not None
                and abs(expected.get("position", 0) - value["position"]) < 1
            )
        return bool(expected == value)

    def local(self, item_id, values):
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute("BEGIN IMMEDIATE")
            row = db.cursor.execute(
                "SELECT payload FROM api_local_userdata WHERE namespace=? AND item_id=?",
                (self.namespace, item_id),
            ).fetchone()
            payload = json.loads(row[0]) if row else {}
            payload.update(values)
            db.cursor.execute(
                "INSERT OR REPLACE INTO api_local_userdata VALUES (?,?,?)",
                (self.namespace, item_id, encode(payload)),
            )

    def local_pending(self):
        with Database() as db:
            self._prepare(db.cursor)
            return [
                (item_id, json.loads(payload))
                for item_id, payload in db.cursor.execute(
                    "SELECT item_id,payload FROM api_local_userdata WHERE namespace=?",
                    (self.namespace,),
                ).fetchall()
            ]

    def local_done(self, item_id, values):
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute(
                "DELETE FROM api_local_userdata WHERE namespace=? AND item_id=? AND payload=?",
                (self.namespace, item_id, encode(values)),
            )
