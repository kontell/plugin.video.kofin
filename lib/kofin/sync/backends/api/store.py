"""Atomic movie publication and recovery state, in Kofin's database only."""

import hashlib
import json
import time

from kofin.sync.catalogue import Catalogue
from kofin.sync.model import MediaItem
from kofin.sync.private import Database


def namespace(server_id, user_id):
    if not server_id or not user_id:
        raise ValueError("server and user identity required")
    return hashlib.sha256((server_id + "/" + user_id).encode()).hexdigest()[:32]


def directory(key):
    return "plugin://plugin.video.kofin/native/%s/" % key


def playback_url(key, item_id):
    from urllib.parse import urlencode

    return directory(key) + "?" + urlencode({"mode": "play", "id": item_id})


def identity(key, item_id):
    return key + ":" + item_id


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class MovieStore(Catalogue):
    def _prepare(self, cursor):
        super()._prepare(cursor)
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS api_directory(
                namespace TEXT PRIMARY KEY, current INTEGER NOT NULL DEFAULT 0,
                pinned INTEGER, server TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS api_snapshot(
                namespace TEXT NOT NULL, generation INTEGER NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(namespace,generation));
            CREATE TABLE IF NOT EXISTS api_movie(
                namespace TEXT NOT NULL, item_id TEXT NOT NULL, library TEXT NOT NULL,
                kodi_id INTEGER, applied_payload TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(namespace,item_id));
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

    def initialize(self, server):
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute(
                "INSERT OR IGNORE INTO api_directory(namespace,server) VALUES (?,?)",
                (self.namespace, server),
            )
            db.cursor.execute(
                "UPDATE api_directory SET server=? WHERE namespace=?",
                (server, self.namespace),
            )

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

    def publish(
        self, items, library=None, removed=(), membership=None, complete_libraries=None
    ):
        """A validated complete library, or a batch of explicit item changes.

        The caller must finish fetching/validating before entering this method.
        A single transaction commits desired work and its scanner directory.
        """
        movies = [MediaItem.from_dto(item) for item in items]
        if any(m.kind != "Movie" or not m.payload.get("Name") for m in movies):
            raise ValueError("complete movie metadata required")
        if len({m.item_id for m in movies}) != len(movies):
            raise ValueError("duplicate movie identities")
        with Database() as db:
            self._prepare(db.cursor)
            # executescript in _prepare commits implicitly; start only after DDL.
            db.cursor.execute("BEGIN IMMEDIATE")
            current = self._records(db.cursor)
            deleting = set(removed)
            if library is not None:
                incoming = {m.item_id for m in movies}
                deleting.update(
                    item_id
                    for item_id, record in current.items()
                    if record["library"] == library and item_id not in incoming
                )
            if complete_libraries is not None:
                incoming = {m.item_id for m in movies}
                deleting.update(
                    item_id
                    for item_id, record in current.items()
                    if record["library"] in complete_libraries
                    and item_id not in incoming
                )
            for movie in movies:
                old = current.get(movie.item_id)
                item_library = (
                    membership.get(movie.item_id)
                    if isinstance(membership, dict)
                    else membership
                ) or (library if library is not None else (old or {}).get("library"))
                if item_library is None:
                    raise ValueError("new movies require selected library membership")
                generation = self.stage_in(db.cursor, movie)
                if (
                    old
                    and old["library"] != item_library
                    and generation == old["generation"]
                ):
                    generation += 1
                    db.cursor.execute(
                        "UPDATE api_item SET desired=?,status='pending' WHERE namespace=? AND item_id=?",
                        (generation, self.namespace, movie.item_id),
                    )
                db.cursor.execute(
                    """INSERT INTO api_movie(namespace,item_id,library) VALUES (?,?,?)
                    ON CONFLICT(namespace,item_id) DO UPDATE SET library=excluded.library""",
                    (self.namespace, movie.item_id, item_library),
                )
                current[movie.item_id] = dict(
                    item=movie.payload, library=item_library, generation=generation
                )
                deleting.discard(movie.item_id)
            for item_id in deleting:
                old = current.pop(item_id, None)
                if old:
                    self.stage_in(db.cursor, MediaItem.from_dto(old["item"]), "remove")
            return self._publish(db.cursor, current)

    def _records(self, cursor):
        row = cursor.execute(
            """SELECT payload FROM api_snapshot JOIN api_directory USING(namespace)
            WHERE namespace=? AND generation=current""",
            (self.namespace,),
        ).fetchone()
        return json.loads(row[0]) if row else {}

    def _publish(self, cursor, records):
        row = cursor.execute(
            "SELECT current FROM api_directory WHERE namespace=?", (self.namespace,)
        ).fetchone()
        if not row:
            raise ValueError("directory not initialized")
        payload = encode(records)
        old = cursor.execute(
            "SELECT payload FROM api_snapshot WHERE namespace=? AND generation=?",
            (self.namespace, row[0]),
        ).fetchone()
        if old and old[0] == payload:
            return row[0]
        generation = row[0] + 1
        cursor.execute(
            "INSERT INTO api_snapshot VALUES (?,?,?)",
            (self.namespace, generation, payload),
        )
        cursor.execute(
            "UPDATE api_directory SET current=? WHERE namespace=?",
            (generation, self.namespace),
        )
        # Keep previous usable generation and any generation an active scan uses.
        cursor.execute(
            """DELETE FROM api_snapshot WHERE namespace=? AND generation<?
            AND generation != COALESCE((SELECT pinned FROM api_directory WHERE namespace=?),-1)""",
            (self.namespace, generation - 1, self.namespace),
        )
        return generation

    def snapshot(self, pinned=True):
        with Database() as db:
            self._prepare(db.cursor)
            row = db.cursor.execute(
                """SELECT s.payload,d.server,s.generation FROM api_directory d
                JOIN api_snapshot s ON s.namespace=d.namespace AND s.generation=
                CASE WHEN ? THEN COALESCE(d.pinned,d.current) ELSE d.current END
                WHERE d.namespace=?""",
                (pinned, self.namespace),
            ).fetchone()
        if not row:
            raise ValueError("no committed movie directory")
        return json.loads(row[0]), row[1], row[2]

    def pin(self):
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute(
                "UPDATE api_directory SET pinned=COALESCE(pinned,current) WHERE namespace=? AND current>0",
                (self.namespace,),
            )
        return self.snapshot()

    def unpin(self):
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute(
                "UPDATE api_directory SET pinned=NULL WHERE namespace=?",
                (self.namespace,),
            )

    def mapping(self, item_id):
        with Database() as db:
            self._prepare(db.cursor)
            row = db.cursor.execute(
                "SELECT kodi_id,applied_payload,library FROM api_movie WHERE namespace=? AND item_id=?",
                (self.namespace, item_id),
            ).fetchone()
        return row

    def remember(self, item_id, generation, kodi_id, payload):
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute("BEGIN IMMEDIATE")
            db.cursor.execute(
                """UPDATE api_item SET applied=desired,status='applied',error=''
                WHERE namespace=? AND item_id=? AND desired=?""",
                (self.namespace, item_id, generation),
            )
            if not db.cursor.rowcount:
                return False
            db.cursor.execute(
                "UPDATE api_movie SET kodi_id=?,applied_payload=? WHERE namespace=? AND item_id=?",
                (kodi_id, encode(payload), self.namespace, item_id),
            )
            return True

    def expect(self, item_id, generation, userdata):
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute(
                "INSERT OR REPLACE INTO api_expected VALUES (?,?,?,?,?)",
                (
                    self.namespace,
                    item_id,
                    generation,
                    time.time() + 120,
                    encode(userdata),
                ),
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
        return bool(row and json.loads(row[0]).get(field) == value)

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
