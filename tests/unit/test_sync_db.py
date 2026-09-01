import sqlite3

import pytest

from kofin.sync import db as sync_db
from kofin.sync import kofindb
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
        # kofin additions beyond the fork schema (downloads plan W1.3).
        "idx_download_series",
        "idx_download_state",
        # ...and the request the completion toast asks about once per
        # finished track (D6).
        "idx_download_request",
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
    monkeypatch.setattr("xbmcvfs.listdir", lambda path: ([], ["MyVideos999.db"]))
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


def test_save_sync_never_leaves_a_half_written_record(monkeypatch, tmp_path):
    """The write is temp+replace: a failure at any point leaves the previous
    record whole, never a truncated one (audit finding #23 — a reader inside
    the old in-place write window carried on with an empty whitelist)."""
    monkeypatch.setattr("xbmcvfs.exists", lambda path: True)
    monkeypatch.setattr("xbmcvfs.translatePath", lambda path: str(tmp_path))

    sync_db.save_sync({"Whitelist": ["v1"]})
    original = (tmp_path / "sync.json").read_bytes()

    def crash(source, destination):
        raise OSError("simulated crash at the rename")

    monkeypatch.setattr(sync_db.os, "replace", crash)
    with pytest.raises(OSError):
        sync_db.save_sync({"Whitelist": ["v2"]})

    assert (tmp_path / "sync.json").read_bytes() == original
    assert sync_db.get_sync()["Whitelist"] == ["v1"]


def test_get_sync_missing_or_empty_file_answers_defaults(monkeypatch, tmp_path):
    """Absent is a fresh install; empty is the truncate-then-crash a
    pre-atomic save could leave. Neither is worth refusing to sync over."""
    monkeypatch.setattr("xbmcvfs.exists", lambda path: True)
    monkeypatch.setattr("xbmcvfs.translatePath", lambda path: str(tmp_path))

    assert sync_db.get_sync()["Whitelist"] == []
    (tmp_path / "sync.json").write_bytes(b"  \n")
    assert sync_db.get_sync()["Whitelist"] == []


def test_exit_rolls_back_the_exception_path(tmp_path):
    """A mid-write failure must not persist half a multi-table write: the
    fork's unconditional commit stranded partial items (audit finding #15)."""
    path = str(tmp_path / "t.db")
    with sync_db.Database(path) as db:
        db.cursor.execute("CREATE TABLE t(x)")
        db.cursor.execute("INSERT INTO t VALUES (1)")

    with pytest.raises(RuntimeError):
        with sync_db.Database(path) as db:
            db.cursor.execute("INSERT INTO t VALUES (2)")
            raise RuntimeError("mid-item failure")

    with sync_db.Database(path) as db:
        count = db.cursor.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    assert count == 1


def test_exit_rollback_reaches_only_the_last_commit(tmp_path):
    """The writer passes commit per page; a failure loses at most the page in
    flight, and the restore point re-runs exactly that page on resume."""
    path = str(tmp_path / "t.db")
    with sync_db.Database(path) as db:
        db.cursor.execute("CREATE TABLE t(x)")

    with pytest.raises(RuntimeError):
        with sync_db.Database(path) as db:
            db.cursor.execute("INSERT INTO t VALUES (1)")
            db.conn.commit()  # the per-page commit
            db.cursor.execute("INSERT INTO t VALUES (2)")
            raise RuntimeError("failure after the page commit")

    with sync_db.Database(path) as db:
        rows = db.cursor.execute("SELECT x FROM t").fetchall()
    assert rows == [(1,)]


def test_enter_closes_the_connection_when_setup_fails(tmp_path, monkeypatch):
    """__exit__ never runs when __enter__ raises, so a failed setup must close
    its own handle or the WAL lock leaks for the rest of the process."""

    class TrackingConnection:
        def __init__(self, conn):
            self._conn = conn
            self.closed = False

        def close(self):
            self.closed = True
            self._conn.close()

        def __getattr__(self, name):
            return getattr(self._conn, name)

    holder = {}
    real_connect = sync_db.sqlite3.connect

    def tracking_connect(*args, **kwargs):
        holder["conn"] = TrackingConnection(real_connect(*args, **kwargs))
        return holder["conn"]

    def refuse(cursor):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sync_db.sqlite3, "connect", tracking_connect)
    monkeypatch.setattr(sync_db, "kofin_tables", refuse)
    sync_db.set_path_override("kofin", str(tmp_path / "k.db"))
    try:
        with pytest.raises(sqlite3.OperationalError):
            with sync_db.Database("kofin"):
                pass
    finally:
        sync_db.reset_overrides()

    assert holder["conn"].closed


def test_get_sync_screams_on_corrupt_content(monkeypatch, tmp_path):
    """Content that exists but does not parse must raise, not default: an
    empty whitelist born from a bad read makes find_library answer {} and the
    writers skip items silently while the watermark advances."""
    monkeypatch.setattr("xbmcvfs.exists", lambda path: True)
    monkeypatch.setattr("xbmcvfs.translatePath", lambda path: str(tmp_path))

    (tmp_path / "sync.json").write_bytes(b'{"Whitelist": ["v1"], "Restor')
    with pytest.raises(sync_db.SyncStateCorrupt):
        sync_db.get_sync()

    (tmp_path / "sync.json").write_bytes(b'["not", "an", "object"]')
    with pytest.raises(sync_db.SyncStateCorrupt):
        sync_db.get_sync()
