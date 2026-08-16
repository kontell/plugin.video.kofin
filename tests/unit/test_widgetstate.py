"""L2 units for the widget fingerprint gate (widget-refresh-plan D2/D6):
pristine schema-fixture databases, direct row edits, and the contract that
digests move exactly when widget-rendered state does — parameterized over
every gated schema like the writer suite."""

import sqlite3

import pytest

from kofin.sync import db as sync_db
from kofin.sync import kofindb
from kofin.sync import widgetstate
from tests.unit import kodifixtures
from tests.unit.fakes import FakeAddon, FakeWindow

VIDEO_LEGS = [
    pytest.param(kodifixtures.VIDEO_VERSION, id="omega"),
    pytest.param(kodifixtures.PIERS_VIDEO_VERSION, id="piers"),
    pytest.param(kodifixtures.PIERS_VIDEO_VERSION_147, id="piers147"),
]
MUSIC_LEGS = [
    pytest.param(kodifixtures.MUSIC_VERSION, id="omega"),
    pytest.param(kodifixtures.PIERS_MUSIC_VERSION, id="piers"),
]


@pytest.fixture(autouse=True)
def widget_env(monkeypatch, tmp_path):
    FakeAddon.store = {}
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    monkeypatch.setattr("xbmcvfs.exists", lambda path: True)
    monkeypatch.setattr("xbmcvfs.translatePath", lambda path: str(tmp_path))
    sync_db.reset_overrides()
    sync_db.set_path_override("kofin", str(tmp_path / "kofin.db"))
    yield
    sync_db.reset_overrides()


def make_video_db(tmp_path, version):
    path = str(tmp_path / ("MyVideos%d.db" % version))
    kodifixtures.create_video_db(path, version)
    sync_db.set_path_override("video", path)
    return path


def make_music_db(tmp_path, version):
    path = str(tmp_path / ("MyMusic%d.db" % version))
    kodifixtures.create_music_db(path, version)
    sync_db.set_path_override("music", path)
    return path


def execute(path, sql, params=()):
    conn = sqlite3.connect(path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def seed_movie(
    path,
    movie_id,
    title,
    date_added,
    playcount=0,
    resume=0.0,
    total=3600.0,
    idset=None,
):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO files (idFile, idPath, strFilename, playCount, "
            "lastPlayed, dateAdded) VALUES (?, 1, ?, ?, NULL, ?)",
            (movie_id, "movie%d.mkv" % movie_id, playcount, date_added),
        )
        conn.execute(
            "INSERT INTO movie (idMovie, idFile, c00, idSet) VALUES (?, ?, ?, ?)",
            (movie_id, movie_id, title, idset),
        )
        if resume:
            conn.execute(
                "INSERT INTO bookmark (idFile, timeInSeconds, "
                "totalTimeInSeconds, type) VALUES (?, ?, ?, 1)",
                (movie_id, resume, total),
            )
        conn.commit()
    finally:
        conn.close()


def seed_album(path, album_id, name, song_lastplayed):
    """An album with one song; ``song_lastplayed`` may be None (never played)."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO album (idAlbum, strAlbum) VALUES (?, ?)", (album_id, name)
        )
        conn.execute(
            "INSERT INTO song (idSong, idAlbum, iTimesPlayed, lastplayed) "
            "VALUES (?, ?, ?, ?)",
            (album_id * 100, album_id, 1 if song_lastplayed else 0, song_lastplayed),
        )
        conn.commit()
    finally:
        conn.close()


def seed_reference(jellyfin_id, checksum, jellyfin_type="Movie"):
    """INSERT OR REPLACE a kofin.db reference row (re-seeding re-stamps)."""
    with sync_db.Database("kofin") as opened:
        kofindb.JellyfinDatabase(opened.cursor).add_reference(
            jellyfin_id,
            1,
            None,
            None,
            jellyfin_type,
            "movie",
            None,
            checksum,
            "lib1",
            None,
        )


def moved_after(before, db_file):
    return widgetstate.moved_sections(before, widgetstate.fingerprint(db_file))


# --- video ---------------------------------------------------------------------


@pytest.mark.parametrize("version", VIDEO_LEGS)
def test_video_fingerprint_deterministic(tmp_path, version):
    path = make_video_db(tmp_path, version)
    seed_movie(path, 1, "Alpha", "2026-01-01 10:00:00", playcount=1, resume=120.0)
    seed_reference("jf1", "etag1")

    assert widgetstate.fingerprint("video") == widgetstate.fingerprint("video")


@pytest.mark.parametrize("version", VIDEO_LEGS)
def test_identical_userdata_rewrite_holds(tmp_path, version):
    """The headline suppression: this client's own playback echo rewrites the
    values Kodi already holds — same watched flag, a lastPlayed touch, the
    checksum re-stamped to the same Etag. No section may move."""
    path = make_video_db(tmp_path, version)
    seed_movie(path, 1, "Alpha", "2026-01-01 10:00:00", playcount=2)
    seed_reference("jf1", "etag1")
    before = widgetstate.fingerprint("video")

    execute(path, "UPDATE files SET playCount = 2, lastPlayed = '2026-08-04 11:00:00'")
    seed_reference("jf1", "etag1")

    assert moved_after(before, "video") == set()


@pytest.mark.parametrize("version", VIDEO_LEGS)
def test_watched_flip_moves_userdata(tmp_path, version):
    path = make_video_db(tmp_path, version)
    seed_movie(path, 1, "Alpha", "2026-01-01 10:00:00", playcount=0)
    before = widgetstate.fingerprint("video")

    execute(path, "UPDATE files SET playCount = 1")

    assert moved_after(before, "video") == {"userdata"}


@pytest.mark.parametrize("version", VIDEO_LEGS)
def test_resume_point_moves_inprogress(tmp_path, version):
    path = make_video_db(tmp_path, version)
    seed_movie(path, 1, "Alpha", "2026-01-01 10:00:00")
    before = widgetstate.fingerprint("video")

    execute(
        path,
        "INSERT INTO bookmark (idFile, timeInSeconds, totalTimeInSeconds, type) "
        "VALUES (1, 600.0, 3600.0, 1)",
    )

    assert moved_after(before, "video") == {"userdata", "inprogress"}


@pytest.mark.parametrize("version", VIDEO_LEGS)
def test_sub_percent_resume_creep_holds(tmp_path, version):
    """The progress bar renders whole percent: a resume tick smaller than
    that is not visible state and must not refresh."""
    path = make_video_db(tmp_path, version)
    seed_movie(path, 1, "Alpha", "2026-01-01 10:00:00", resume=600.0)
    before = widgetstate.fingerprint("video")

    execute(path, "UPDATE bookmark SET timeInSeconds = 610.0")  # 16.6% -> 16.9%

    assert moved_after(before, "video") == set()


@pytest.mark.parametrize("version", VIDEO_LEGS)
def test_new_movie_moves_recency(tmp_path, version):
    path = make_video_db(tmp_path, version)
    seed_movie(path, 1, "Alpha", "2026-01-01 10:00:00")
    before = widgetstate.fingerprint("video")

    seed_movie(path, 2, "Beta", "2026-02-01 10:00:00")

    assert {"recency", "userdata"} <= moved_after(before, "video")


@pytest.mark.parametrize("version", VIDEO_LEGS)
def test_metadata_etag_moves_reference(tmp_path, version):
    make_video_db(tmp_path, version)
    seed_reference("jf1", "etag1")
    before = widgetstate.fingerprint("video")

    seed_reference("jf1", "etag2")

    assert moved_after(before, "video") == {"reference"}


@pytest.mark.parametrize("version", VIDEO_LEGS)
def test_boxset_membership_moves(tmp_path, version):
    """Membership can drift while the set's Etag stands still
    (boxsets-robustness-plan): the movie row's idSet is the rendered link."""
    path = make_video_db(tmp_path, version)
    seed_movie(path, 1, "Alpha", "2026-01-01 10:00:00")
    before = widgetstate.fingerprint("video")

    execute(path, "UPDATE movie SET idSet = 5")

    assert moved_after(before, "video") == {"userdata"}


@pytest.mark.parametrize("version", VIDEO_LEGS)
def test_default_rating_pointer_moves_ratings(tmp_path, version):
    """The repoint pass rewrites c05 and nothing else — no checksum, no
    userdata — so ratings is the only section that can carry it to the
    widgets."""
    path = make_video_db(tmp_path, version)
    seed_movie(path, 1, "Alpha", "2026-01-01 10:00:00")
    execute(
        path,
        "INSERT INTO rating (rating_id, media_id, media_type, rating_type, rating) "
        "VALUES (1, 1, 'movie', 'default', 7.1), (2, 1, 'movie', 'critic', 8.9)",
    )
    execute(path, "UPDATE movie SET c05 = '1'")
    before = widgetstate.fingerprint("video")

    execute(path, "UPDATE movie SET c05 = '2'")

    assert moved_after(before, "video") == {"ratings"}


@pytest.mark.parametrize("version", VIDEO_LEGS)
def test_non_default_rating_edit_holds(tmp_path, version):
    """Only the rating Kodi renders is hashed: a row nothing points at moving
    is not visible state."""
    path = make_video_db(tmp_path, version)
    seed_movie(path, 1, "Alpha", "2026-01-01 10:00:00")
    execute(
        path,
        "INSERT INTO rating (rating_id, media_id, media_type, rating_type, rating) "
        "VALUES (1, 1, 'movie', 'default', 7.1), (2, 1, 'movie', 'critic', 8.9)",
    )
    execute(path, "UPDATE movie SET c05 = '1'")
    before = widgetstate.fingerprint("video")

    execute(path, "UPDATE rating SET rating = 9.4 WHERE rating_id = 2")

    assert moved_after(before, "video") == set()


# --- music ---------------------------------------------------------------------


@pytest.mark.parametrize("version", MUSIC_LEGS)
def test_same_album_replay_holds(tmp_path, version):
    """Replaying the front album bumps song lastplayed values without
    reordering the top albums — the phase-3 live gate's zero-refresh case."""
    path = make_music_db(tmp_path, version)
    seed_album(path, 1, "Front", "2026-08-01 10:00:00")
    seed_album(path, 2, "Back", "2026-07-01 10:00:00")
    before = widgetstate.fingerprint("music")

    execute(
        path, "UPDATE song SET lastplayed = '2026-08-04 11:00:00' WHERE idAlbum = 1"
    )

    assert moved_after(before, "music") == set()


@pytest.mark.parametrize("version", MUSIC_LEGS)
def test_cross_album_play_reorders(tmp_path, version):
    path = make_music_db(tmp_path, version)
    seed_album(path, 1, "Front", "2026-08-01 10:00:00")
    seed_album(path, 2, "Back", "2026-07-01 10:00:00")
    before = widgetstate.fingerprint("music")

    execute(
        path, "UPDATE song SET lastplayed = '2026-08-04 11:00:00' WHERE idAlbum = 2"
    )

    assert moved_after(before, "music") == {"recency"}


@pytest.mark.parametrize("version", MUSIC_LEGS)
def test_new_album_moves_recency(tmp_path, version):
    path = make_music_db(tmp_path, version)
    seed_album(path, 1, "Front", None)
    before = widgetstate.fingerprint("music")

    seed_album(path, 2, "Fresh", None)

    assert moved_after(before, "music") == {"recency"}


# --- the gate on Library.refresh_libraries -------------------------------------


@pytest.mark.parametrize("version", VIDEO_LEGS)
def test_refresh_suppressed_until_state_moves(tmp_path, version, monkeypatch):
    from kofin.sync.library import Library
    from tests.unit.test_sync_library import FakeApi, FakePlayer

    calls = []
    monkeypatch.setattr("xbmc.executebuiltin", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(
        "xbmc.getCondVisibility", lambda cond: cond.startswith("Library.HasContent")
    )

    path = make_video_db(tmp_path, version)
    seed_movie(path, 1, "Alpha", "2026-01-01 10:00:00")
    seed_reference("jf1", "etag1")

    api = FakeApi()
    manager = Library(api, FakePlayer(), lambda: api)

    manager.refresh_libraries({"video"})  # fingerprint unknown: fires once
    assert calls == ["UpdateLibrary(video)"]

    manager.refresh_libraries({"video"})  # nothing moved: suppressed
    assert calls == ["UpdateLibrary(video)"]

    execute(path, "UPDATE files SET playCount = 1")
    manager.refresh_libraries({"video"})  # a real change: fires again
    assert calls == ["UpdateLibrary(video)", "UpdateLibrary(video)"]


# --- container scoping (D6) ----------------------------------------------------


def test_container_kinds_by_path_family():
    assert widgetstate.container_kinds("videodb://movies/titles/") == {"video"}
    assert widgetstate.container_kinds("library://video/movies/") == {"video"}
    assert widgetstate.container_kinds("musicdb://recentlyplayedalbums") == {"music"}
    assert widgetstate.container_kinds("library://music/") == {"music"}
    assert widgetstate.container_kinds(
        "special://profile/playlists/video/inprogress.xsp"
    ) == {"video"}
    assert widgetstate.container_kinds("plugin://plugin.video.kofin/?mode=browse") == {
        "video",
        "music",
    }
    assert widgetstate.container_kinds("") == {"video", "music"}


def test_container_refresh_scoped_to_moved_kind():
    assert widgetstate.container_wants_refresh("videodb://movies/", {"music"}) is False
    assert widgetstate.container_wants_refresh("videodb://movies/", {"video"}) is True
    assert (
        widgetstate.container_wants_refresh("plugin://plugin.video.kofin/", {"music"})
        is True
    )


@pytest.mark.parametrize("version", VIDEO_LEGS)
def test_force_reload_does_not_bypass_the_gate(tmp_path, version, monkeypatch):
    """The end of a full sync forces the *reload*, not a refresh for nothing.

    A resumed sync that changed nothing must still do nothing: that is the
    case where an unconditional rebuild would tear down the window of a user
    who is browsing, to show them exactly what they were already looking at.
    """
    from kofin.sync.library import Library
    from tests.unit.test_sync_library import FakeApi, FakePlayer

    calls = []
    monkeypatch.setattr("xbmc.executebuiltin", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(
        "xbmc.getCondVisibility", lambda cond: cond.startswith("Library.HasContent")
    )

    path = make_video_db(tmp_path, version)
    seed_movie(path, 1, "Alpha", "2026-01-01 10:00:00")
    seed_reference("jf1", "etag1")

    api = FakeApi()
    manager = Library(api, FakePlayer(), lambda: api)

    manager.refresh_libraries({"video"}, force_reload=True)
    assert calls == ["UpdateLibrary(video)", "ReloadSkin()"]

    calls.clear()
    manager.refresh_libraries({"video"}, force_reload=True)  # nothing moved
    assert calls == []
