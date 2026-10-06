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

import os as os
import sqlite3 as sqlite3
from typing import Dict, Optional


from kofin.core.log import Logger
from kofin.sync import schema
from kofin.sync import private
from kofin.sync.private import (
    ADDON_DATA as ADDON_DATA,
    SyncStateCorrupt as SyncStateCorrupt,
    addon_data_path as addon_data_path,
    get_sync as get_sync,
    save_sync as save_sync,
    get_item as get_item,
    kofin_tables as kofin_tables,
)

LOG = Logger(__name__)


KINDS = ("video", "music", "texture", "kofin")

_path_overrides: Dict[str, str] = {}
_tables_ensured = private._tables_ensured


def set_path_override(kind: str, path: str) -> None:
    """Point a database kind at an explicit file (tests/fixtures only)."""
    _path_overrides[kind] = path
    if kind == "kofin":
        private.set_path_override(kind, path)


def reset_overrides() -> None:
    _path_overrides.clear()
    _tables_ensured.clear()
    private.reset_overrides()


def resolve_path(db_file: str) -> str:
    """Resolve a kind or literal path to the sqlite file to open.

    Kind resolution goes through the schema gate — an unsupported Kodi
    database raises :class:`kofin.sync.schema.SchemaError` here, before
    anything is written.
    """
    if db_file in _path_overrides:
        return _path_overrides[db_file]

    if db_file == "kofin":
        return private.resolve_path()

    if db_file in KINDS:
        return schema.database_path(db_file)

    return db_file  # literal path or :memory:


class Database(private.Database):
    """Legacy SQL adapter: native kinds/literal paths plus private storage.

    Shared/private callers import sync.private instead. This facade is omitted
    from API packages; its commit/rollback behavior is inherited unchanged.
    """

    def __init__(self, db_file: Optional[str] = None, commit_close: bool = True):
        super().__init__(db_file or "video", commit_close)

    def _resolve_path(self) -> str:
        return resolve_path(self.db_file)

    def _prepare(self) -> None:
        if self.db_file in KINDS:
            self.conn.execute("PRAGMA journal_mode=WAL")
        if self.db_file == "kofin" and self.path not in _tables_ensured:
            kofin_tables(self.cursor)
            self.conn.commit()
            _tables_ensured.add(self.path)
