"""Private desired/applied state for API operations.

This is the phase-1 outbox foundation, not a published scanner snapshot. A
native API accepting a request does not call confirm(); the adapter must first
read back the owned item. Failed and superseded work cannot advance applied.
Namespaces include server and user identity; no native IDs are adopted here.
"""

import json
from dataclasses import dataclass
from typing import Optional

from kofin.sync.private import Database
from kofin.sync.model import MediaItem


class BackendMismatch(Exception):
    """A branch switch requires a fresh native library and cleared mappings."""


def claim_backend(cursor, backend: str) -> None:
    if backend not in ("sql", "api"):
        raise ValueError("unknown backend")
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS backend_state (id INTEGER PRIMARY KEY CHECK(id=1), backend TEXT NOT NULL, version INTEGER NOT NULL)"
    )
    row = cursor.execute(
        "SELECT backend, version FROM backend_state WHERE id=1"
    ).fetchone()
    if row is not None:
        if row != (backend, 1):
            raise BackendMismatch(
                "sync state belongs to another backend/version; start with fresh sync state"
            )
        return
    # Pre-marker installations are SQL mappings. Generic private reads remain
    # available; only an adapter claiming these mappings is refused.
    legacy = cursor.execute("SELECT 1 FROM jellyfin LIMIT 1").fetchone()
    if legacy and backend != "sql":
        raise BackendMismatch("legacy SQL mappings require a fresh library")
    cursor.execute("INSERT INTO backend_state VALUES (1, ?, 1)", (backend,))


@dataclass(frozen=True)
class ItemState:
    desired: int
    applied: int
    operation: str
    status: str
    error: str


class Catalogue:
    def __init__(self, namespace: str):
        if not namespace:
            raise ValueError("server/user namespace is required")
        self.namespace = namespace

    def _prepare(self, cursor):
        claim_backend(cursor, "api")
        cursor.execute("""CREATE TABLE IF NOT EXISTS api_item(
            namespace TEXT NOT NULL, item_id TEXT NOT NULL, kind TEXT NOT NULL,
            payload TEXT NOT NULL, desired INTEGER NOT NULL, applied INTEGER NOT NULL,
            operation TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL,
            PRIMARY KEY(namespace, item_id))""")

    def stage(self, item: MediaItem, operation: str = "upsert") -> int:
        if operation not in ("upsert", "remove"):
            raise ValueError("unknown operation")
        payload = json.dumps(item.payload, sort_keys=True, separators=(",", ":"))
        with Database() as db:
            self._prepare(db.cursor)
            row = db.cursor.execute(
                "SELECT payload, desired, applied, operation FROM api_item WHERE namespace=? AND item_id=?",
                (self.namespace, item.item_id),
            ).fetchone()
            if row and row[0] == payload and row[3] == operation:
                return int(row[1])
            generation = int(row[1]) + 1 if row else 1
            applied = int(row[2]) if row else 0
            db.cursor.execute(
                """INSERT OR REPLACE INTO api_item VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '')""",
                (
                    self.namespace,
                    item.item_id,
                    item.kind,
                    payload,
                    generation,
                    applied,
                    operation,
                ),
            )
            return generation

    def state(self, item_id: str) -> Optional[ItemState]:
        with Database() as db:
            self._prepare(db.cursor)
            row = db.cursor.execute(
                "SELECT desired, applied, operation, status, error FROM api_item WHERE namespace=? AND item_id=?",
                (self.namespace, item_id),
            ).fetchone()
            return ItemState(*row) if row else None

    def confirm(self, item_id: str, generation: int) -> bool:
        """Acknowledge readback for exactly the current desired generation."""
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute(
                """UPDATE api_item SET applied=desired, status='applied', error=''
                WHERE namespace=? AND item_id=? AND desired=?""",
                (self.namespace, item_id, generation),
            )
            return bool(db.cursor.rowcount)

    def failed(self, item_id: str, generation: int, reason: str) -> None:
        with Database() as db:
            self._prepare(db.cursor)
            db.cursor.execute(
                """UPDATE api_item SET status='pending', error=?
                WHERE namespace=? AND item_id=? AND desired=?""",
                (reason, self.namespace, item_id, generation),
            )

    def pending(self):
        """Replayable desired DTOs and tombstones, ordered deterministically."""
        with Database() as db:
            self._prepare(db.cursor)
            rows = db.cursor.execute(
                "SELECT payload, desired, operation FROM api_item WHERE namespace=? AND status='pending' ORDER BY item_id",
                (self.namespace,),
            ).fetchall()
        return [
            (MediaItem.from_dto(json.loads(payload)), generation, operation)
            for payload, generation, operation in rows
        ]
