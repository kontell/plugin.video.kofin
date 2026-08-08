"""L2 repoint suite: the downloads repoint against pristine Kodi schemas.

The invariant that enforces plan W1.7's column list is byte-identical
restore: full sync -> repoint -> restore must reproduce the post-sync
database dump exactly, on every gated schema. Mid-state assertions pin the
Emby-shape facts individually (file row moved, mirror columns, path stamps,
parent links, prune behavior), and the writer-pass test pins the W1.8
interaction contract from this side: a resync rebuilds the row in writer
shape and the re-assert recaptures before repointing again.
"""

import sqlite3

import pytest

from kofin.downloads import repoint, store
from kofin.sync import db as sync_db
from kofin.sync import schema
from kofin.sync.kodidb.kodi import Kodi
from kofin.sync.writers import Movies, TVShows
from tests.unit import kodifixtures
from tests.unit.fakes import FakeAddon, FakeWindow
from tests.unit.sync_dtos import EPISODE, LIBRARY, MOVIE, SERIES, TV_LIBRARY, dto
from tests.unit.test_sync_writers import FakeApi, FakeMonitor

ROOT = "/dl"


@pytest.fixture(
    autouse=True,
    params=[
        kodifixtures.VIDEO_VERSION,
        kodifixtures.PIERS_VIDEO_VERSION,
        kodifixtures.PIERS_VIDEO_VERSION_147,
    ],
    ids=["omega", "piers", "piers147"],
)
def sync_env(request, monkeypatch, tmp_path):
    FakeAddon.store = {
        "enableCoverArt": "true",
        "compressArt": "false",
        "maxArtResolution": "0",
    }
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
        "video",
        kodifixtures.create_video_db(
            str(tmp_path / ("MyVideos%d.db" % request.param)), request.param
        ),
    )
    # The writers' re-assert resolves the root itself (settings-driven); pin
    # it to the same literal every manual repoint/restore in here uses, or
    # the two sides repoint under different roots.
    monkeypatch.setattr("kofin.downloads.repoint.downloads_root", lambda: ROOT)
    yield request.param
    sync_db.reset_overrides()
    Kodi.reset_people_cache()


@pytest.fixture
def api():
    fake = FakeApi()
    fake.items_by_id = {"movie1": dto(MOVIE), "series1": dto(SERIES)}
    return fake


def register_views(*views):
    with sync_db.Database("kofin") as opened:
        from kofin.sync import kofindb

        mapping = kofindb.JellyfinDatabase(opened.cursor)
        for view in views:
            mapping.add_view(view["Id"], view["Name"], view["Media"])


def write_movie(api, payload=None):
    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        Movies(api, kdb, vdb, library=LIBRARY).movie(payload or dto(MOVIE))


def write_series_tree(api, extra_episodes=()):
    register_views({"Id": "lib-shows", "Name": "Shows", "Media": "tvshows"})
    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        shows = TVShows(api, kdb, vdb, library=TV_LIBRARY)
        shows.tvshow(dto(SERIES))
        shows.episode(dto(EPISODE))
        for episode in extra_episodes:
            shows.episode(episode)


def dump():
    conn = sqlite3.connect(str(sync_db._path_overrides["video"]))
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


def video_query(sql, args=()):
    conn = sqlite3.connect(str(sync_db._path_overrides["video"]))
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def done_download(item_id, media_type, rel_path, series_id=""):
    store.queue(
        store.Download(
            jellyfin_id=item_id,
            media_type=media_type,
            series_id=series_id,
            queued_at=100,
        )
    )
    claimed = store.claim()
    assert claimed is not None and claimed.jellyfin_id == item_id
    store.finish(item_id, rel_path, rel_path.rsplit(".", 1)[-1], 1000)
    return store.get(item_id)


def mapping_row(item_id):
    conn = sqlite3.connect(str(sync_db._path_overrides["kofin"]))
    try:
        return conn.execute(
            "SELECT kodi_id, kodi_fileid, kodi_pathid FROM jellyfin "
            "WHERE jellyfin_id = ?",
            (item_id,),
        ).fetchone()
    finally:
        conn.close()


def path_row(str_path):
    rows = video_query(
        "SELECT idPath, strContent, strScraper, noUpdate, useFolderNames, "
        "idParentPath FROM path WHERE strPath = ?",
        (str_path,),
    )
    return rows[0] if rows else None


def test_movie_repoint_moves_the_row_and_restore_is_byte_identical(api):
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)
    baseline = dump()
    _kodi_id, file_id, plugin_path_id = mapping_row("movie1")
    (original_filename,) = video_query(
        "SELECT strFilename FROM files WHERE idFile = ?", (file_id,)
    )[0]
    assert original_filename.startswith("plugin://")

    download = done_download("movie1", "movie", "Movies/The Movie (2019)/movie.mkv")
    assert repoint.repoint(download, ROOT) is True

    moved = video_query(
        "SELECT idPath, strFilename FROM files WHERE idFile = ?", (file_id,)
    )[0]
    title = path_row("/dl/Movies/The Movie (2019)/")
    kind = path_row("/dl/Movies/")
    assert moved[1] == "movie.mkv"
    assert moved[0] == title[0]
    # The writers' movie stamps, on the title directory (V5 / info dialog).
    assert title[1:5] == ("movies", "metadata.local", 1, None)
    assert title[5] == kind[0]
    assert kind[1] is None  # the type directory stays bare
    # The capture is the writers' row, verbatim.
    assert store.get("movie1").restore_filename == original_filename

    assert repoint.restore(store.get("movie1"), ROOT) is True
    assert dump() == baseline


def test_episode_repoint_mirrors_the_episode_columns_and_restores(api):
    write_series_tree(api)
    baseline = dump()
    kodi_id, file_id, plugin_path_id = mapping_row("episode1")
    original_c18, original_c19 = video_query(
        "SELECT c18, c19 FROM episode WHERE idEpisode = ?", (kodi_id,)
    )[0]
    assert original_c18.startswith("plugin://")
    assert int(original_c19) == plugin_path_id

    download = done_download(
        "episode1", "episode", "TV/The Show/Season 01/S01E01.mkv", series_id="series1"
    )
    assert repoint.repoint(download, ROOT) is True

    season = path_row("/dl/TV/The Show/Season 01/")
    show = path_row("/dl/TV/The Show/")
    kind = path_row("/dl/TV/")
    moved = video_query(
        "SELECT idPath, strFilename FROM files WHERE idFile = ?", (file_id,)
    )[0]
    mirrored = video_query(
        "SELECT c18, c19 FROM episode WHERE idEpisode = ?", (kodi_id,)
    )[0]
    assert moved == (season[0], "S01E01.mkv")
    assert mirrored[0] == "/dl/TV/The Show/Season 01/S01E01.mkv"
    assert int(mirrored[1]) == season[0]
    # The writers' show stamps on the show directory; season and type bare.
    assert show[1:5] == ("tvshows", "metadata.local", 1, 1)
    assert season[1] is None and season[5] == show[0]
    assert show[5] == kind[0] and kind[1] is None

    assert repoint.restore(store.get("episode1"), ROOT) is True
    assert dump() == baseline


def test_repoint_is_idempotent(api):
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)
    download = done_download("movie1", "movie", "Movies/The Movie (2019)/movie.mkv")

    assert repoint.repoint(download, ROOT) is True
    first = dump()
    assert repoint.repoint(store.get("movie1"), ROOT) is True

    assert dump() == first
    assert (
        video_query(
            "SELECT COUNT(*) FROM path WHERE strPath = ?",
            ("/dl/Movies/The Movie (2019)/",),
        )[0][0]
        == 1
    )


def test_shared_show_rows_survive_until_the_last_restore(api):
    second = dto(EPISODE)
    second.update(
        Id="episode2",
        Name="Second",
        IndexNumber=2,
        Path="/media/shows/The Show/Season 1/S01E02.mkv",
        Etag="etag-episode2-v1",
        ProviderIds={"Tvdb": "9998"},
    )
    write_series_tree(api, extra_episodes=[second])
    baseline = dump()

    first_dl = done_download(
        "episode1", "episode", "TV/The Show/Season 01/S01E01.mkv", "series1"
    )
    second_dl = done_download(
        "episode2", "episode", "TV/The Show/Season 01/S01E02.mkv", "series1"
    )
    assert repoint.repoint(first_dl, ROOT) is True
    assert repoint.repoint(second_dl, ROOT) is True

    assert repoint.restore(store.get("episode1"), ROOT) is True
    # The sibling still lives there: every shared directory row survives.
    assert path_row("/dl/TV/The Show/Season 01/") is not None
    assert path_row("/dl/TV/The Show/") is not None
    assert path_row("/dl/TV/") is not None

    assert repoint.restore(store.get("episode2"), ROOT) is True
    assert path_row("/dl/TV/The Show/Season 01/") is None
    assert path_row("/dl/TV/") is None
    assert dump() == baseline


def parallel_writer_state(tmp_path, version, payloads):
    """The dump of a fresh sandbox after the given writes and nothing else —
    the never-downloaded expectation the interleaved sequence must land on.
    Swaps the overrides to a second pair of databases and back; the people
    cache resets on both edges because its name->id map is per-database."""
    saved_video = sync_db._path_overrides["video"]
    saved_kofin = sync_db._path_overrides["kofin"]
    Kodi.reset_people_cache()
    sync_db.set_path_override("kofin", str(tmp_path / "kofin-expected.db"))
    sync_db.set_path_override(
        "video",
        kodifixtures.create_video_db(
            str(tmp_path / ("expected-MyVideos%d.db" % version)), version
        ),
    )
    try:
        register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
        api = FakeApi()
        for payload in payloads:
            write_movie(api, payload)
        return dump()
    finally:
        sync_db.set_path_override("video", saved_video)
        sync_db.set_path_override("kofin", saved_kofin)
        Kodi.reset_people_cache()


def test_a_changed_resync_reasserts_itself_and_remove_leaves_zero_trace(
    api, sync_env, tmp_path
):
    """W1.8 as built: the writer's own post-pass hook re-points a downloaded
    item before the page commits — no manual re-assert — recapturing the
    fresh URL, and the injected tag survives add_tags' wholesale replace.
    An unchanged resync never gets that far (checksum skip, repoint intact).
    The full remove flow (restore + row + tag) then lands byte-identical
    with a parallel never-downloaded sandbox: zero trace, tag rows included."""
    changed = dto(MOVIE)
    changed["Etag"] = "etag-movie1-v2"
    expected = parallel_writer_state(tmp_path, sync_env, [dto(MOVIE), dto(changed)])

    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)
    _kodi_id, file_id, _path_id = mapping_row("movie1")

    download = done_download("movie1", "movie", "Movies/The Movie (2019)/movie.mkv")
    assert repoint.repoint(download, ROOT) is True
    repoint.stamp_tag(download)

    write_movie(api)  # unchanged payload: checksum short-circuit, no revert
    (untouched,) = video_query(
        "SELECT strFilename FROM files WHERE idFile = ?", (file_id,)
    )[0]
    assert untouched == "movie.mkv"

    write_movie(api, dto(changed))  # a real update pass
    (name,) = video_query("SELECT strFilename FROM files WHERE idFile = ?", (file_id,))[
        0
    ]
    assert name == "movie.mkv"  # the writer re-asserted the repoint itself
    assert store.get("movie1").restore_filename.startswith("plugin://")
    assert _tag_links("movie") == 1  # injected through the wholesale replace

    row = store.get("movie1")  # the remove flow, as the manager runs it
    assert repoint.restore(row, ROOT) is True
    store.remove("movie1")
    repoint.unstamp_tag(row)
    assert dump() == expected


def _tag_links(media_type):
    from kofin.downloads import TAG

    return video_query(
        "SELECT COUNT(*) FROM tag_link JOIN tag ON tag.tag_id = tag_link.tag_id "
        "WHERE tag.name = ? AND tag_link.media_type = ?",
        (TAG, media_type),
    )[0][0]


def test_the_show_carries_the_tag_while_any_episode_is_downloaded(api):
    """Episodes have no tag surface in Kodi's nodes, so a downloaded episode
    tags its show; the show writer keeps it through rewrites via series_id,
    and the last sibling's removal drops link and tag row both."""
    write_series_tree(api)
    download = done_download(
        "episode1", "episode", "TV/The Show/Season 01/S01E01.mkv", "series1"
    )
    assert repoint.repoint(download, ROOT) is True
    repoint.stamp_tag(download)
    assert _tag_links("tvshow") == 1

    changed = dto(SERIES)
    changed["Etag"] = "etag-series1-v2"
    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        TVShows(api, kdb, vdb, library=TV_LIBRARY).tvshow(changed)
    assert _tag_links("tvshow") == 1  # survived the wholesale replace

    row = store.get("episode1")
    assert repoint.restore(row, ROOT) is True
    store.remove("episode1")
    repoint.unstamp_tag(row)
    assert _tag_links("tvshow") == 0
    from kofin.downloads import TAG

    assert (
        video_query("SELECT COUNT(*) FROM tag WHERE name = ?", (TAG,))[0][0] == 0
    )  # the orphaned tag row went with the last link


def test_restore_refuses_without_a_captured_filename(api):
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)
    download = done_download("movie1", "movie", "Movies/The Movie (2019)/movie.mkv")
    before = dump()

    assert repoint.restore(download, ROOT) is False  # never repointed
    assert dump() == before


def test_repoint_skips_unmapped_items():
    orphan = store.Download(
        jellyfin_id="ghost", media_type="movie", rel_path="Movies/G/g.mkv"
    )
    assert repoint.repoint(orphan, ROOT) is False
    assert repoint.restore(orphan, ROOT) is False
