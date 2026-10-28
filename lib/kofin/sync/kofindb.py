# -*- coding: utf-8 -*-
"""Accessors for the kofin.db mapping tables."""

import functools
from collections import namedtuple
from typing import Tuple

from kofin.core.log import Logger
from kofin.sync import queries_map as QU

LOG = Logger(__name__)


@functools.lru_cache(maxsize=64)
def _row_class(fields: Tuple[str, ...]):
    """One namedtuple class per distinct column list.

    sqlite3 calls the row factory once per row, and ``namedtuple()`` builds a
    class, far too costly to repeat per row. Keyed on the exact column names
    so two queries never share a class by accident; 64 covers every SELECT in
    queries_map.
    """
    return namedtuple("Row", fields)


def sqlite_namedtuple_factory(cursor, row):
    return _row_class(tuple(col[0] for col in cursor.description))(*row)


class JellyfinDatabase:

    def __init__(self, cursor):
        self.cursor = cursor
        cursor.row_factory = sqlite_namedtuple_factory

    def get_playlist_state(self, jellyfin_id):
        """(media_type, filename, checksum) or None."""
        self.cursor.execute(QU.get_playlist_state, (jellyfin_id,))
        row = self.cursor.fetchone()
        return row

    def get_playlist_states(self):
        self.cursor.execute(QU.get_playlist_states)
        return self.cursor.fetchall()

    def add_playlist_state(self, *args):
        self.cursor.execute(QU.add_playlist_state, args)

    def remove_playlist_state(self, jellyfin_id):
        self.cursor.execute(QU.delete_playlist_state, (jellyfin_id,))
        self.cursor.execute(
            "DELETE FROM playlist_item WHERE playlist_id = ?", (jellyfin_id,)
        )

    def remove_playlist_states(self):
        self.cursor.execute(QU.delete_playlist_states)
        self.cursor.execute("DELETE FROM playlist_item")

    def replace_playlist_items(self, playlist_id, item_ids):
        self.cursor.execute(
            "DELETE FROM playlist_item WHERE playlist_id = ?", (playlist_id,)
        )
        self.cursor.executemany(
            "INSERT INTO playlist_item(playlist_id, jellyfin_id, position) VALUES (?, ?, ?)",
            (
                (playlist_id, item_id, position)
                for position, item_id in enumerate(item_ids)
            ),
        )

    def get_view(self, *args):

        try:
            self.cursor.execute(QU.get_view, args)

            return self.cursor.fetchone()
        except TypeError:
            return
