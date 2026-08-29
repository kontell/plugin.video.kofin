"""The kofin.db row factory (audit fixes plan H1)."""

import sqlite3

from kofin.sync import kofindb


def _rows(sql):
    connection = sqlite3.connect(":memory:")
    cursor = connection.cursor()
    cursor.row_factory = kofindb.sqlite_namedtuple_factory
    cursor.execute(sql)
    return cursor.fetchall()


def test_rows_keep_named_field_access():
    (row,) = _rows("SELECT 'abc' AS jellyfin_id, 7 AS kodi_id")
    assert row.jellyfin_id == "abc"
    assert row.kodi_id == 7
    assert row == ("abc", 7)  # still a tuple for the positional callers


def test_the_row_class_is_built_once_per_column_list():
    """One class per distinct SELECT, not one per row: every read through
    JellyfinDatabase paid a namedtuple() class definition per row before."""
    first, second = _rows("SELECT 1 AS a, 'x' AS b UNION ALL SELECT 2, 'y'")
    assert type(first) is type(second)

    again = _rows("SELECT 3 AS a, 'z' AS b")[0]
    assert type(again) is type(first)


def test_different_column_lists_never_share_a_class():
    one = _rows("SELECT 1 AS a, 2 AS b")[0]
    other = _rows("SELECT 1 AS b, 2 AS a")[0]
    assert type(one) is not type(other)
    assert one.a == 1 and other.a == 2
