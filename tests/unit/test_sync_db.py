import sqlite3

import pytest

from kofin.sync import db as sync_db
from kofin.sync import kofindb, queries_map
from tests.unit import kodifixtures


@pytest.fixture(autouse=True)
def clean_overrides():
    sync_db.reset_overrides()
    yield
    sync_db.reset_overrides()


@pytest.fixture
def kofin_path(tmp_path):
    path = str(tmp_path / "kofin.db")
    sync_db.set_path_override("kofin", path)
    return path


def test_kofin_db_created_with_fork_schema(kofin_path):
    with sync_db.Database("kofin") as opened:
        opened.cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in opened.cursor.fetchall()]
    assert "jellyfin" in tables
    assert "view" in tables
    assert "version" in tables


def test_kofin_db_has_fork_indexes(kofin_path):
    with sync_db.Database("kofin") as opened:
        opened.cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        )
        indexes = {row[0] for row in opened.cursor.fetchall()}
    assert indexes == {
        "idx_jellyfin_kodi",
        "idx_jellyfin_parent",
        "idx_jellyfin_media_folder",
        "idx_jellyfin_parent_id",
    }


def test_mapping_reference_round_trip(kofin_path):
    with sync_db.Database("kofin") as opened:
        mapping = kofindb.JellyfinDatabase(opened.cursor)
        mapping.add_reference(
            "item1",
            12,
            34,
            56,
            "Movie",
            "movie",
            None,
            "etag|plugin",
            "lib1",
            "parent1",
        )
        row = mapping.get_item_by_id("item1")
        assert row.kodi_id == 12
        assert row.kodi_fileid == 34
        assert row.kodi_pathid == 56
        assert row.media_type == "movie"
        assert row.jellyfin_type == "Movie"
        assert row.media_folder == "lib1"
        assert row.jellyfin_parent_id == "parent1"
        assert row.checksum == "etag|plugin"

        mapping.update_reference("etag2|plugin", "item1")
        assert mapping.get_item_by_id("item1").checksum == "etag2|plugin"

        assert mapping.get_item_by_kodi_id(12, "movie") == "item1"
        assert mapping.get_media_by_id("item1") == "Movie"

        mapping.remove_item("item1")
        assert mapping.get_item_by_id("item1") is None


def test_view_round_trip(kofin_path):
    with sync_db.Database("kofin") as opened:
        mapping = kofindb.JellyfinDatabase(opened.cursor)
        mapping.add_view("v1", "Movies", "movies")
        mapping.add_view("v2", "Tunes", "music")

        assert mapping.get_view_name("v1") == "Movies"
        assert mapping.get_view("v2").media_type == "music"
        assert len(mapping.get_views()) == 2
        assert [v.view_id for v in mapping.get_views_by_media("music")] == ["v2"]

        mapping.remove_view("v1")
        assert mapping.get_view("v1") is None


def test_version_row_is_single(kofin_path):
    with sync_db.Database("kofin") as opened:
        mapping = kofindb.JellyfinDatabase(opened.cursor)
        mapping.add_version("1")
        mapping.add_version("2")
        assert mapping.get_version().idVersion == "2"


def test_unsupported_kodi_schema_refused(monkeypatch, tmp_path):
    monkeypatch.setattr("xbmcvfs.listdir", lambda path: ([], ["MyVideos146.db"]))
    monkeypatch.setattr("xbmcvfs.translatePath", lambda path: str(tmp_path))

    from kofin.sync import schema

    schema.reset_cache()
    try:
        with pytest.raises(schema.SchemaUnsupported):
            with sync_db.Database("video"):
                pass
    finally:
        schema.reset_cache()


def test_fixture_databases_open_and_carry_versions(tmp_path):
    video = kodifixtures.create_video_db(str(tmp_path / "MyVideos131.db"))
    music = kodifixtures.create_music_db(str(tmp_path / "MyMusic83.db"))

    with sqlite3.connect(video) as conn:
        assert conn.execute("SELECT idVersion FROM version").fetchone()[0] == 131
        count = conn.execute("SELECT COUNT(*) FROM videoversiontype").fetchone()[0]
        assert count > 300

    with sqlite3.connect(music) as conn:
        assert conn.execute("SELECT idVersion FROM version").fetchone()[0] == 83
        assert (
            conn.execute("SELECT strRole FROM role WHERE idRole=1").fetchone()[0]
            == "Artist"
        )


def test_sync_json_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr("xbmcvfs.exists", lambda path: True)
    monkeypatch.setattr("xbmcvfs.translatePath", lambda path: str(tmp_path))

    sync = sync_db.get_sync()
    assert sync["Libraries"] == []
    assert sync["Whitelist"] == []

    sync["Whitelist"].append("lib1")
    sync["RestorePoints"]["lib1/movies"] = {"params": {"StartIndex": 50}}
    sync_db.save_sync(sync)

    loaded = sync_db.get_sync()
    assert loaded["Whitelist"] == ["lib1"]
    assert loaded["RestorePoints"]["lib1/movies"]["params"]["StartIndex"] == 50
    assert "Date" in loaded
