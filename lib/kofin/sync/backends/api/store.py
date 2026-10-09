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
payload. The 0.90.0 store kept a 38.7 MB JSON blob per generation beside the
payloads it duplicated; `kofin.db` reached 235 MB on 1,792 movies.
"""

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from kofin.sync.catalogue import Catalogue
from kofin.sync.model import MediaItem
from kofin.core.log import Logger
from kofin.sync.private import Database
from . import paths

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
# A season or episode is filed under its show; a song under its album (or
# the singles folder of its artist); nothing else has a parent.
PARENTED = ("Season", "Episode", "Audio")


def namespace(server_id, user_id):
    if not server_id or not user_id:
        raise ValueError("server and user identity required")
    return hashlib.sha256((server_id + "/" + user_id).encode()).hexdigest()[:32]


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def payload_hash(payload):
    return hashlib.sha256(encode(payload).encode()).hexdigest()[:24]


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


class Record:
    """One member of the desired view.

    ``item`` is the payload, read on first use when the record was built
    without one. A pass over a whole catalogue holds a record for every
    pending item, and their payloads together weigh 3.5 times their JSON
    (200 MB for 6,600 video items): held all at once they wedged a 1 GB
    device. The pass reads them through a ``PayloadWindow`` instead.
    """

    __slots__ = (
        "item_id",
        "kind",
        "library",
        "parent_id",
        "generation",
        "_item",
        "_loader",
    )

    def __init__(
        self,
        item_id: str,
        kind: str,
        library: str,
        parent_id: str,
        item: Optional[Dict[str, Any]],
        generation: int,
        loader: Optional[Callable[[str], Dict[str, Any]]] = None,
    ):
        self.item_id = item_id
        self.kind = kind
        self.library = library
        self.parent_id = parent_id
        self.generation = generation
        self._item = item
        self._loader = loader

    @property
    def item(self) -> Dict[str, Any]:
        if self._item is not None:
            return self._item
        if self._loader is None:
            return {}
        return self._loader(self.item_id)

    @property
    def loaded(self) -> bool:
        return self._item is not None

    def _key(self):
        return (self.item_id, self.kind, self.library, self.parent_id, self.generation)

    def __eq__(self, other):
        if not isinstance(other, Record):
            return NotImplemented
        return self._key() == other._key() and self.item == other.item

    def __hash__(self):
        return hash(self._key())

    def __repr__(self):
        return "Record(%r, %r, generation=%r)" % (
            self.item_id,
            self.kind,
            self.generation,
        )


class PayloadWindow:
    """Payloads read on demand, a bounded number held at a time.

    ``walk`` yields records in order and reads each chunk's payloads with
    one query ahead of it, so a pass over thousands of records holds a few
    hundred payloads, never the catalogue.
    """

    CHUNK = 200

    def __init__(self, store: "Store", size: int = 512):
        self.store = store
        self.size = size
        self._held: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self.reads = 0

    def __call__(self, item_id: str) -> Dict[str, Any]:
        payload = self._held.get(item_id)
        if payload is None:
            self.reads += 1
            payload = self.store.payload(item_id)
            self._keep(item_id, payload)
        else:
            self._held.move_to_end(item_id)
        return payload

    def _keep(self, item_id: str, payload: Dict[str, Any]):
        self._held[item_id] = payload
        while len(self._held) > self.size:
            self._held.popitem(last=False)

    def prefetch(self, item_ids: Iterable[str]):
        wanted = [i for i in item_ids if i not in self._held]
        if wanted:
            self.reads += 1
            for item_id, payload in self.store.payloads(wanted).items():
                self._keep(item_id, payload)

    def walk(self, records: Iterable[Record]) -> Iterator[Record]:
        pending = list(records)
        for start in range(0, len(pending), self.CHUNK):
            chunk = pending[start : start + self.CHUNK]
            unloaded = [r for r in chunk if not r.loaded]
            for record in unloaded:
                # A lazy record from ``records(payloads=False)`` reads one
                # row at a time; walked, it reads through this window.
                record._loader = self
            self.prefetch(r.item_id for r in unloaded)
            for record in chunk:
                yield record


@dataclass(frozen=True)
class Mapping:
    kodi_id: Optional[int]
    applied: Dict[str, Any]
    kind: str


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
            CREATE TABLE IF NOT EXISTS api_expected(
                namespace TEXT NOT NULL, item_id TEXT NOT NULL, generation INTEGER NOT NULL,
                expires REAL NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(namespace,item_id));
            CREATE TABLE IF NOT EXISTS api_local_userdata(
                namespace TEXT NOT NULL, item_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(namespace,item_id));
            CREATE TABLE IF NOT EXISTS api_setup(
                id INTEGER PRIMARY KEY CHECK(id=1), namespace TEXT NOT NULL);
        """)

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
            live = {e.item_id: e for e in self._entries(cursor, "removed IS NULL", ())}
            incoming = {m.item_id for m in media}
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

    def _generation(self, cursor, pinned):
        row = cursor.execute(
            "SELECT current, pinned FROM api_root WHERE namespace=?",
            (self.namespace,),
        ).fetchone()
        if not row:
            raise ValueError("no committed directory")
        current, pin = row
        return pin if pinned and pin is not None else current

    def entries(self, pinned=True) -> Dict[str, Entry]:
        """Membership at the pinned generation (or the current one)."""
        with Database() as db:
            self._prepare(db.cursor)
            generation = self._generation(db.cursor, pinned)
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

    def tombstones(self) -> Dict[str, Entry]:
        """The last known placement of every item with a pending removal."""
        with Database() as db:
            self._prepare(db.cursor)
            rows = db.cursor.execute(
                """SELECT e.item_id, e.kind, e.library, e.parent_id FROM api_item i
                JOIN api_entry e ON e.namespace=i.namespace AND e.item_id=i.item_id
                WHERE i.namespace=? AND i.operation='remove' AND i.status='pending'
                AND e.added=(SELECT MAX(added) FROM api_entry x
                    WHERE x.namespace=e.namespace AND x.item_id=e.item_id)""",
                (self.namespace,),
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
        loading a payload: a root listing of 22,000 songs needs their
        1,500 albums, not their text."""
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
                # subquery scans the item table, and once per item it turned
                # a whole-library removal into minutes of SQLite.
                self._collect(db.cursor, root[1] if root[1] is not None else root[0])
        return count

    def invalidate(self, item_ids: Iterable[str]):
        """Put applied upserts back to pending; their desired state is unchanged.

        Used when Kodi replaced the rows behind them -- a show refresh
        re-creates every episode -- so identities and userdata need a pass.
        """
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

    def invalidate_where(self, kinds, except_library=None):
        with Database() as db:
            self._prepare(db.cursor)
            kinds = tuple(kinds)
            params: List[Any] = [self.namespace] + list(kinds)
            query = (
                "UPDATE api_item SET status='pending' WHERE namespace=? AND operation='upsert'"
                " AND item_id IN (SELECT item_id FROM api_entry e WHERE e.namespace=api_item.namespace"
                " AND e.removed IS NULL AND e.kind IN (%s)" % ",".join("?" * len(kinds))
            )
            if except_library is not None:
                query += " AND e.library<>?"
                params.append(except_library)
            db.cursor.execute(query + ")", params)

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
