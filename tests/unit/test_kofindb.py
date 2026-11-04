"""The kofin.db row factory."""

import sqlite3

from kofin.sync import kofindb, private


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
    """One class per distinct SELECT, not one per row."""
    first, second = _rows("SELECT 1 AS a, 'x' AS b UNION ALL SELECT 2, 'y'")
    assert type(first) is type(second)

    again = _rows("SELECT 3 AS a, 'z' AS b")[0]
    assert type(again) is type(first)


def test_different_column_lists_never_share_a_class():
    one = _rows("SELECT 1 AS a, 2 AS b")[0]
    other = _rows("SELECT 1 AS b, 2 AS a")[0]
    assert type(one) is not type(other)
    assert one.a == 1 and other.a == 2


def _tables():
    cursor = sqlite3.connect(":memory:").cursor()
    private.kofin_tables(cursor)
    return kofindb.JellyfinDatabase(cursor)


def test_views_round_trip():
    tables = _tables()
    tables.remember_view("v1", "Films", "movies")
    tables.remember_view("v2", "Tunes", "music")
    assert [(v.view_id, v.name, v.media_type) for v in tables.views()] == [
        ("v1", "Films", "movies"),
        ("v2", "Tunes", "music"),
    ]
    assert tables.view_names() == {"v1": "Films", "v2": "Tunes"}
    assert tables.view_name("v2") == "Tunes"
    assert tables.view_name("v9") is None
    assert tables.view_media("v2") == "music"
    assert tables.view_media("v9") == ""
    tables.remember_view("v2", "Tunes II", "music")
    assert tables.view_name("v2") == "Tunes II"


def test_playlist_state_lookups():
    tables = _tables()
    tables.add_playlist_state("p1", "Audio", "Mix.m3u8", "e|plugin")
    tables.add_playlist_state("p2", "Video", "Films.m3u8", "")
    tables.add_playlist_state("p3", "Audio", "", "")
    assert tables.playlist_filename("p1") == "Mix.m3u8"
    assert tables.playlist_filename("p3") == ""
    assert tables.playlist_filename("p9") is None
    assert tables.playlist_for_file("Films.m3u8") == "p2"
    assert tables.playlist_for_file("Films.m3u8", side="Audio") == ""
    assert tables.playlist_for_file("Films.m3u8", side="Video") == "p2"
    assert tables.playlist_for_file("Nothing.m3u8") == ""
