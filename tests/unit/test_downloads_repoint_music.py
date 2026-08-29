"""L2 repoint suite, music leg (plan W3.2): songs against pristine MyMusic.

Same invariants as the video suite, on the music shape: full sync ->
repoint -> restore must reproduce the post-sync music database dump exactly
(datetime-masked via ``music_dump`` — Kodi's own schema triggers stamp
DATETIME('now')), the mid-states pin the one-row mechanics (``song.idPath``
moved, extension rule satisfied, the album path row's lifecycle), and the
writer-pass test pins the W1.8 contract from the music side.
"""

import pytest

from kofin.downloads import repoint, store
from kofin.sync import db as sync_db
from kofin.sync import musicsources, schema
from kofin.sync.kodidb.kodi import Kodi
from tests.unit import kodifixtures
from tests.unit.fakes import FakeAddon, FakeWindow
from tests.unit.sync_dtos import SONG, dto
from tests.unit.test_sync_writers import (
    FakeMonitor,
    music_dump,
    music_query,
    kofin_query,
    write_music_tree,
)
from tests.unit.test_sync_writers import api as writers_api  # noqa: F401

ROOT = "/dl"
REL = "Music/The Band/Greatest Hits/01 Opening Track.opus"


@pytest.fixture(
    autouse=True,
    params=[kodifixtures.MUSIC_VERSION, kodifixtures.PIERS_MUSIC_VERSION],
    ids=["omega", "piers"],
)
def music_env(request, monkeypatch, tmp_path):
    FakeAddon.store = {}
    FakeWindow.store = {"kofin.online": "true"}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    monkeypatch.setattr("kofin.sync.shims._monitor", FakeMonitor())
    monkeypatch.setattr("xbmcvfs.exists", lambda path: True)
    monkeypatch.setattr("xbmcvfs.translatePath", lambda path: str(tmp_path))

    Kodi.reset_people_cache()
    sync_db.reset_overrides()
    schema.reset_cache()

    sync_db.set_path_override("kofin", str(tmp_path / "kofin.db"))
    sync_db.set_path_override(
        "music",
        kodifixtures.create_music_db(
            str(tmp_path / ("MyMusic%d.db" % request.param)), request.param
        ),
    )
    monkeypatch.setattr("kofin.downloads.repoint.downloads_root", lambda: ROOT)
    yield request.param
    sync_db.reset_overrides()
    Kodi.reset_people_cache()


def downloaded(rel_path=REL, container="opus"):
    store.queue(store.Download(jellyfin_id="song1", media_type="song", queued_at=100))
    store.claim()
    store.record_details("song1", "song", "album1", 0, "", store.QUALITY_TRANSCODE)
    store.finish("song1", rel_path, container, 999)
    row = store.get("song1")
    assert row is not None
    return row


def song_row():
    return music_query(
        "SELECT s.idPath, s.strFileName, p.strPath FROM song s "
        "JOIN path p ON p.idPath = s.idPath WHERE s.strTitle = 'Opening Track'"
    )[0]


def listed():
    """songview inner-joins path: a song on a deleted row is in no listing."""
    return music_query(
        "SELECT COUNT(*) FROM songview WHERE strTitle = 'Opening Track'"
    )[0][0]


def mapped_path_id():
    return kofin_query(
        "SELECT kodi_pathid FROM jellyfin "
        "WHERE jellyfin_id = 'song1' AND media_type = 'song'"
    )[0][0]


def startup_prune():
    """What every service start runs (library.py check_version)."""
    with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
        return musicsources.prune_orphan_paths(kdb.cursor, mdb.cursor)


def delete_path_row(path_id):
    """Kodi's own Clean library: any path row no song references goes."""
    with sync_db.Database("music") as mdb:
        mdb.cursor.execute("DELETE FROM path WHERE idPath = ?", (path_id,))


def test_repoint_moves_the_song_row(writers_api):
    write_music_tree(writers_api)
    row = downloaded()

    assert repoint.repoint(row, ROOT) is True

    _path_id, filename, str_path = song_row()
    assert filename == "01 Opening Track.opus"  # the musicdb extension rule
    assert str_path == "/dl/Music/The Band/Greatest Hits/"
    # The writer-built filename was captured verbatim for the restore.
    assert store.get("song1").restore_filename == "stream.flac?static=true"

    # Idempotent: a second pass reuses the same path row.
    assert repoint.repoint(store.get("song1"), ROOT) is True
    rows = music_query("SELECT COUNT(*) FROM path WHERE strPath LIKE '/dl/%'")
    assert rows[0][0] == 1


def test_restore_is_byte_identical(writers_api):
    write_music_tree(writers_api)
    baseline = music_dump(str(sync_db._path_overrides["music"]))

    row = downloaded()
    repoint.repoint(row, ROOT)
    assert music_dump(str(sync_db._path_overrides["music"])) != baseline

    assert repoint.restore(store.get("song1"), ROOT) is True
    assert music_dump(str(sync_db._path_overrides["music"])) == baseline


def test_repoint_captures_the_server_path(writers_api):
    """The row the restore needs is not guaranteed to survive (below), so the
    string is captured beside the filename -- and only from a song still on
    its server row, never from one already on its local directory."""
    write_music_tree(writers_api)
    original = song_row()[2]

    assert repoint.repoint(downloaded(), ROOT) is True
    assert store.get("song1").restore_path == original

    assert repoint.repoint(store.get("song1"), ROOT) is True
    assert store.get("song1").restore_path == original


def test_the_startup_prune_spares_a_downloaded_songs_server_row(writers_api):
    """The Bravia sequence (2026-08-28): download, service restart, delete.
    The server row has no song on it while the download lives, and the sweep
    used to take it -- then the restore put the song back onto a deleted id
    and every listing dropped it. Now the sweep reads the mapping, and the
    restore is byte-identical the way the plain one is."""
    write_music_tree(writers_api)
    baseline = music_dump(str(sync_db._path_overrides["music"]))
    assert repoint.repoint(downloaded(), ROOT) is True

    assert startup_prune() == 0

    assert repoint.restore(store.get("song1"), ROOT) is True
    assert music_dump(str(sync_db._path_overrides["music"])) == baseline
    assert listed() == 1


def test_restore_recreates_a_server_row_kodi_cleaned(writers_api):
    """Kodi's own Clean library takes any path row no song references -- a
    downloaded song's server row included -- and the mapping's id alone
    cannot bring it back. The captured string can, and the mapping follows
    the new id so the writers, the next repoint and delete_song agree."""
    write_music_tree(writers_api)
    original = song_row()[2]
    stored = mapped_path_id()
    assert repoint.repoint(downloaded(), ROOT) is True
    delete_path_row(stored)

    assert repoint.restore(store.get("song1"), ROOT) is True

    path_id, filename, str_path = song_row()  # the join finds a row again
    assert str_path == original
    assert filename == "stream.flac?static=true"
    assert path_id != stored
    assert mapped_path_id() == path_id
    assert listed() == 1
    assert music_query("SELECT COUNT(*) FROM path WHERE strPath LIKE '/dl/%'") == [(0,)]


def test_restore_of_a_legacy_row_refuses_rather_than_pointing_at_nothing(
    writers_api,
):
    """A row captured before restore_path existed: put back by id while the
    row is there, refused once it is not -- the song stays listed on its
    local row instead of vanishing onto a deleted id."""
    write_music_tree(writers_api)
    baseline = music_dump(str(sync_db._path_overrides["music"]))
    stored = mapped_path_id()

    assert repoint.repoint(downloaded(), ROOT) is True
    with sync_db.Database("kofin") as kdb:
        store.set_restore_path_on(kdb.cursor, "song1", "")
    assert repoint.restore(store.get("song1"), ROOT) is True
    assert music_dump(str(sync_db._path_overrides["music"])) == baseline

    assert repoint.repoint(store.get("song1"), ROOT) is True
    with sync_db.Database("kofin") as kdb:
        store.set_restore_path_on(kdb.cursor, "song1", "")
    delete_path_row(stored)
    assert repoint.restore(store.get("song1"), ROOT) is False
    path_id, _filename, str_path = song_row()
    assert str_path == "/dl/Music/The Band/Greatest Hits/"
    assert path_id != stored
    assert listed() == 1


def test_restore_refuses_without_a_captured_filename(writers_api):
    write_music_tree(writers_api)
    row = downloaded()
    assert repoint.restore(row, ROOT) is False  # nothing captured yet
    _path_id, filename, _str_path = song_row()
    assert filename == "stream.flac?static=true"  # untouched


def test_a_writer_pass_reasserts_the_music_repoint(writers_api):
    write_music_tree(writers_api)
    row = downloaded()
    repoint.repoint(row, ROOT)

    # The server changed the item: the writer rebuilds the row in writer
    # shape inside its own transaction, and the reassert hook must put the
    # local location back before that commits.
    changed = dto(SONG)
    changed["Etag"] = "etag-song1-v2"
    write_music_tree(writers_api, song=changed)

    _path_id, filename, str_path = song_row()
    assert filename == "01 Opening Track.opus"
    assert str_path == "/dl/Music/The Band/Greatest Hits/"
    # And the capture is fresh from this very pass, not the first one's.
    assert store.get("song1").restore_filename == "stream.flac?static=true"


def test_siblings_share_the_album_path_row_until_the_last_leaves(writers_api):
    write_music_tree(writers_api)
    second = dto(SONG)
    second.update(
        {
            "Id": "song2",
            "Name": "Second Track",
            "Etag": "etag-song2-v1",
            "IndexNumber": 2,
            "Path": "/media/music/The Band/Greatest Hits/02 - Second Track.flac",
        }
    )
    write_music_tree(writers_api, song=second)

    row1 = downloaded()
    store.queue(store.Download(jellyfin_id="song2", media_type="song", queued_at=101))
    store.claim()
    store.record_details("song2", "song", "album1", 0, "", store.QUALITY_TRANSCODE)
    store.finish(
        "song2", "Music/The Band/Greatest Hits/02 Second Track.opus", "opus", 999
    )
    repoint.repoint(row1, ROOT)
    repoint.repoint(store.get("song2"), ROOT)

    assert repoint.restore(store.get("song1"), ROOT) is True
    remaining = music_query("SELECT COUNT(*) FROM path WHERE strPath LIKE '/dl/%'")
    assert remaining[0][0] == 1  # song2 still holds the album row

    assert repoint.restore(store.get("song2"), ROOT) is True
    gone = music_query("SELECT COUNT(*) FROM path WHERE strPath LIKE '/dl/%'")
    assert gone[0][0] == 0


def test_song_mapping_shape_matches_the_writers(writers_api):
    """The mapping row the writers leave is what the repoint reads: song id
    plus path id, no file id — pinned so a writer-side column change cannot
    silently strand the music repoint."""
    write_music_tree(writers_api)
    rows = kofin_query(
        "SELECT kodi_id, kodi_pathid FROM jellyfin "
        "WHERE jellyfin_id = 'song1' AND media_type = 'song'"
    )
    assert rows and rows[0][0] is not None and rows[0][1] is not None
