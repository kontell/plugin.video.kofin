"""L2 writer suite: transplanted writers against pristine Kodi schemas.

Invariants from the phase-2 plan (§5 step 2): full-fidelity writes for every
media type, idempotency (second write of the same payload leaves a
byte-identical database), and removal integrity (deleting a series leaves
zero orphans in any link table).
"""

import datetime
import sqlite3

import pytest

from kofin.sync import db as sync_db
from kofin.sync import schema
from kofin.sync.kodidb.kodi import Kodi
from kofin.sync.writers import Movies, MusicVideos, TVShows, Music
from tests.unit import kodifixtures, sync_dtos
from tests.unit.fakes import FakeAddon, FakeWindow
from tests.unit.sync_dtos import (
    ALBUM,
    ARTIST,
    BOXSET,
    EPISODE,
    LIBRARY,
    MOVIE,
    MOVIE_2,
    MUSICVIDEO,
    MUSIC_LIBRARY,
    MV_LIBRARY,
    SEASON_1,
    SERIES,
    SONG,
    TV_LIBRARY,
    dto,
)


class FakeApi:
    """The slice of kofin.core.api.Api the writers and downloader touch."""

    server = "http://server:8096"
    user_id = "user1"

    def __init__(self):
        self.items_by_id = {}
        self.boxset_children = {}
        self.seasons_by_series = {}
        self.special_features_by_id = {}

    def special_features(self, item_id):
        features = self.special_features_by_id.get(item_id, [])
        if isinstance(features, Exception):
            raise features
        return features

    def item(self, item_id):
        return self.items_by_id[item_id]

    def get(self, path, params=None):
        params = params or {}
        if path.startswith("/Shows/") and path.endswith("/Seasons"):
            series_id = path.split("/")[2]
            return {"Items": self.seasons_by_series.get(series_id, [])}
        if path.endswith("/LocalTrailers"):
            return []
        if path == "/Users/%s/Items" % self.user_id:
            children = self.boxset_children.get(params.get("ParentId"), [])
            if params.get("Limit") == 1 and params.get("EnableTotalRecordCount"):
                return {"TotalRecordCount": len(children), "Items": []}
            start = params.get("StartIndex", 0)
            limit = params.get("Limit", 50)
            return {"Items": children[start : start + limit]}
        raise AssertionError("unexpected GET %s %s" % (path, params))

    def items(self, params):
        return self.get("/Users/%s/Items" % self.user_id, params)

    def ancestors(self, item_id):
        return []


class FakeMonitor:
    def abortRequested(self):
        return False

    def waitForAbort(self, seconds=0):
        return False


@pytest.fixture(
    autouse=True,
    params=[
        (kodifixtures.VIDEO_VERSION, kodifixtures.MUSIC_VERSION),
        (kodifixtures.PIERS_VIDEO_VERSION, kodifixtures.PIERS_MUSIC_VERSION),
    ],
    ids=["omega", "piers"],
)
def sync_env(request, monkeypatch, tmp_path):
    video_version, music_version = request.param
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
            str(tmp_path / ("MyVideos%d.db" % video_version)), video_version
        ),
    )
    sync_db.set_path_override(
        "music",
        kodifixtures.create_music_db(
            str(tmp_path / ("MyMusic%d.db" % music_version)), music_version
        ),
    )
    yield
    sync_db.reset_overrides()
    Kodi.reset_people_cache()


@pytest.fixture
def api():
    fake = FakeApi()
    fake.items_by_id = {
        "movie1": dto(MOVIE),
        "movie2": dto(MOVIE_2),
        "series1": dto(SERIES),
        "artist1": dto(ARTIST),
        "album1": dto(ALBUM),
    }
    fake.seasons_by_series = {"series1": [dto(SEASON_1)]}
    fake.boxset_children = {"set1": [dto(MOVIE)]}
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


def dump(path):
    conn = sqlite3.connect(path)
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


def music_query(sql, args=()):
    conn = sqlite3.connect(str(sync_db._path_overrides["music"]))
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def kofin_query(sql, args=()):
    conn = sqlite3.connect(str(sync_db._path_overrides["kofin"]))
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# --- movies ------------------------------------------------------------------


def test_movie_write_full_fidelity(api):
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)

    movie = video_query("SELECT * FROM movie")[0]
    columns = [d[1] for d in video_query("PRAGMA table_info(movie)")]
    row = dict(zip(columns, movie))
    assert row["c00"] == "The Example"
    assert row["c03"] == "Nothing is real"
    assert row["premiered"] == "2020-05-01"
    assert row["c11"] == "7200.0"  # runtime seconds (cNN columns are TEXT)
    assert row["c14"] == "Drama / Sci-Fi"
    assert row["c12"] == "Not Rated"  # NR normalized
    assert row["c19"] == "plugin://plugin.video.youtube/play/?video_id=trailer123"
    assert row["c21"] == "Ireland"

    path = video_query("SELECT strPath, strContent, strScraper FROM path")[0]
    assert path == (
        "plugin://plugin.video.kofin/lib-movies/",
        "movies",
        "metadata.local",
    )

    files = video_query("SELECT strFilename, playCount, lastPlayed FROM files")[0]
    assert files[0].startswith("plugin://plugin.video.kofin/lib-movies/?filename=")
    assert "dbid=1" in files[0]
    assert "id=movie1" in files[0]
    assert files[1] == 2  # playcount

    rating = video_query("SELECT rating, votes FROM rating WHERE media_type='movie'")[0]
    assert rating == (7.8, 1234)

    unique = video_query(
        "SELECT value, type FROM uniqueid WHERE media_type='movie' ORDER BY uniqueid_id"
    )
    assert ("tt0000001", "imdb") in unique

    genres = {g[0] for g in video_query("SELECT name FROM genre")}
    assert genres == {"Drama", "Sci-Fi"}

    studios = {s[0] for s in video_query("SELECT name FROM studio")}
    assert studios == {"ABC", "Example Studio"}  # abc (us) normalized

    actors = video_query(
        "SELECT a.name, l.role, l.cast_order FROM actor a"
        " JOIN actor_link l ON l.actor_id = a.actor_id ORDER BY l.cast_order"
    )
    assert actors == [("Alice Actor", "The Lead", 1), ("Bob Guest", "Cameo", 2)]
    directors = video_query(
        "SELECT a.name FROM actor a JOIN director_link l ON l.actor_id = a.actor_id"
    )
    assert directors == [("Carol Director",)]
    writers = video_query(
        "SELECT a.name FROM actor a JOIN writer_link l ON l.actor_id = a.actor_id"
    )
    assert writers == [("Dave Writer",)]

    tags = {t[0] for t in video_query("SELECT name FROM tag")}
    assert tags == {"4K", "Movies", "Favorite movies"}

    streams = video_query(
        "SELECT iStreamType, strVideoCodec, iVideoWidth, strHdrType,"
        " strAudioCodec, iAudioChannels, strSubtitleLanguage FROM streamdetails"
    )
    assert (0, "hevc", 3840, "hdr10", None, None, None) in streams
    assert (1, None, None, None, "eac3", 6, None) in streams
    assert (2, None, None, None, None, None, "swe") in streams

    bookmark = video_query("SELECT timeInSeconds, totalTimeInSeconds FROM bookmark")[0]
    assert bookmark == (900.0, 7200.0)

    art = dict(
        (row[0], row[1])
        for row in video_query("SELECT type, url FROM art WHERE media_type='movie'")
    )
    assert art["poster"].startswith("http://server:8096/Items/movie1/Images/Primary")
    assert "fanart" in art and "fanart1" in art
    assert art["clearlogo"].startswith("http://server:8096/Items/movie1/Images/Logo")

    versions = video_query("SELECT idMedia, media_type, idType FROM videoversion")
    assert versions == [(1, "movie", 40400)]

    mapping = kofin_query(
        "SELECT jellyfin_id, kodi_id, media_folder, checksum FROM jellyfin"
    )
    assert mapping == [("movie1", 1, "lib-movies", "etag-movie1-v1|plugin")]


def test_movie_write_is_idempotent(api):
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)
    first = dump(str(sync_db._path_overrides["video"]))
    first_map = dump(str(sync_db._path_overrides["kofin"]))

    write_movie(api)
    assert dump(str(sync_db._path_overrides["video"])) == first
    assert dump(str(sync_db._path_overrides["kofin"])) == first_map


def test_movie_etag_change_updates_row(api):
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)

    changed = dto(MOVIE)
    changed["Etag"] = "etag-movie1-v2"
    changed["Name"] = "The Example (Remastered)"
    write_movie(api, changed)

    assert video_query("SELECT c00 FROM movie") == [("The Example (Remastered)",)]
    assert kofin_query("SELECT checksum FROM jellyfin") == [("etag-movie1-v2|plugin",)]
    # Still exactly one of everything.
    assert video_query("SELECT COUNT(*) FROM movie") == [(1,)]
    assert video_query("SELECT COUNT(*) FROM files") == [(1,)]
    assert video_query("SELECT COUNT(*) FROM rating") == [(1,)]


# --- movie extras (phase 3: native videoversion assets) ----------------------

FEATURES = [
    {
        "Id": "extra1",
        "Name": "Making Of",
        "Type": "Video",
        "ExtraType": "BehindTheScenes",
        "Path": "/media/movies/The Example (2020)/extras/making-of.mkv",
    },
    {
        "Id": "extra2",
        "Name": "Gone Too Soon",
        "Type": "Video",
        "ExtraType": "DeletedScene",
        "Path": "/media/movies/The Example (2020)/extras/deleted.mkv",
    },
]


def movie_with_extras(count=2, etag="etag-movie1-v1"):
    payload = dto(MOVIE)
    payload["SpecialFeatureCount"] = count
    payload["Etag"] = etag
    return payload


def extra_item_type():
    """VideoAssetType::EXTRA for the fixture under test (schema-keyed)."""
    version = video_query("SELECT idVersion FROM version")[0][0]
    return schema.EXTRA_ITEM_TYPE[version]


def version_item_type():
    """VideoAssetType::VERSION, read the way the writer reads it: the
    itemType Kodi stamped on the seeded Standard Edition row."""
    return video_query("SELECT itemType FROM videoversiontype WHERE id = 40400")[0][0]


def extras_rows():
    return video_query(
        "SELECT videoversion.idFile, videoversion.idMedia, videoversion.idType,"
        " videoversiontype.name, videoversiontype.owner, videoversiontype.itemType"
        " FROM videoversion"
        " JOIN videoversiontype ON videoversiontype.id = videoversion.idType"
        " WHERE videoversion.itemType = ? ORDER BY videoversion.idFile",
        (extra_item_type(),),
    )


def test_movie_extras_written_as_native_assets(api):
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    api.special_features_by_id = {"movie1": FEATURES}
    write_movie(api, movie_with_extras())

    rows = extras_rows()
    assert len(rows) == 2
    assert {row[3] for row in rows} == {"Behind the Scenes", "Deleted Scene"}
    for _file_id, id_media, _id_type, _name, owner, vvt_item_type in rows:
        assert id_media == 1  # the movie's idMovie
        assert owner == 2  # VideoAssetTypeOwner::USER
        assert vvt_item_type == extra_item_type()

    filenames = [
        row[0] for row in video_query("SELECT strFilename FROM files ORDER BY idFile")
    ]
    extras_urls = [name for name in filenames if "id=extra" in name]
    assert len(extras_urls) == 2
    for url in extras_urls:
        assert url.startswith("plugin://plugin.video.kofin/lib-movies/?")
        assert "mode=play" in url

    # The main version row is untouched (the A/B guard: extras must not
    # perturb what phase 2 writes).
    versions = video_query(
        "SELECT idMedia, media_type, idType FROM videoversion WHERE itemType = ?",
        (version_item_type(),),
    )
    assert versions == [(1, "movie", 40400)]


def test_movie_extras_idempotent(api):
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    api.special_features_by_id = {"movie1": FEATURES}
    write_movie(api, movie_with_extras())
    first = dump(str(sync_db._path_overrides["video"]))

    # Same payload: the checksum short-circuit leaves the database untouched.
    write_movie(api, movie_with_extras())
    assert dump(str(sync_db._path_overrides["video"])) == first

    # A metadata change re-runs the extras pass; an unchanged feature set
    # must not churn rows or duplicate videoversiontype entries.
    before = extras_rows()
    write_movie(api, movie_with_extras(etag="etag-movie1-v2"))
    assert extras_rows() == before
    type_names = video_query(
        "SELECT name FROM videoversiontype WHERE itemType = ? AND owner = 2",
        (extra_item_type(),),
    )
    assert len(type_names) == 2


def test_movie_extras_pruned_when_feature_disappears(api):
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    api.special_features_by_id = {"movie1": FEATURES}
    write_movie(api, movie_with_extras())
    assert len(extras_rows()) == 2

    api.special_features_by_id = {"movie1": FEATURES[:1]}
    write_movie(api, movie_with_extras(count=1, etag="etag-movie1-v2"))

    rows = extras_rows()
    assert len(rows) == 1
    assert rows[0][3] == "Behind the Scenes"
    gone = video_query(
        "SELECT COUNT(*) FROM files WHERE strFilename LIKE '%id=extra2%'"
    )
    assert gone == [(0,)]


def test_movie_extras_removed_with_movie(api):
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    api.special_features_by_id = {"movie1": FEATURES}
    write_movie(api, movie_with_extras())

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        Movies(api, kdb, vdb, library=LIBRARY).remove("movie1")

    assert video_query("SELECT COUNT(*) FROM movie") == [(0,)]
    assert video_query("SELECT COUNT(*) FROM videoversion") == [(0,)]
    assert video_query("SELECT COUNT(*) FROM files") == [(0,)]
    assert video_query(
        "SELECT COUNT(*) FROM art WHERE media_type = 'videoversion'"
    ) == [(0,)]


def test_movie_extras_fetch_failure_never_gates_sync(api):
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    api.special_features_by_id = {"movie1": RuntimeError("special features down")}
    write_movie(api, movie_with_extras())

    assert video_query("SELECT COUNT(*) FROM movie") == [(1,)]
    assert extras_rows() == []


def test_boxset_links_and_removal(api):
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)
    write_movie(api, dto(MOVIE_2))

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        Movies(api, kdb, vdb, library=LIBRARY).boxset(dto(BOXSET))

    sets = video_query("SELECT idSet, strSet FROM sets")
    assert sets == [(1, "Example Collection")]
    linked = video_query("SELECT idMovie FROM movie WHERE idSet = 1")
    assert linked == [(1,)]  # movie1 only
    assert kofin_query("SELECT parent_id FROM jellyfin WHERE jellyfin_id='movie1'") == [
        (1,)
    ]

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        Movies(api, kdb, vdb, library=LIBRARY).remove("set1")

    assert video_query("SELECT COUNT(*) FROM sets") == [(0,)]
    assert video_query("SELECT idSet FROM movie WHERE idMovie=1") == [(None,)]
    assert kofin_query("SELECT COUNT(*) FROM jellyfin WHERE jellyfin_id='set1'") == [
        (0,)
    ]
    # Movies survive their boxset.
    assert video_query("SELECT COUNT(*) FROM movie") == [(2,)]


# --- tv shows ------------------------------------------------------------------


def write_series_tree(api):
    register_views({"Id": "lib-shows", "Name": "Shows", "Media": "tvshows"})
    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        shows = TVShows(api, kdb, vdb, library=TV_LIBRARY)
        shows.tvshow(dto(SERIES))
        shows.episode(dto(EPISODE))


def test_series_season_episode_write(api):
    write_series_tree(api)

    show = video_query("SELECT c00, c05, c13 FROM tvshow")[0]
    assert show[0] == "The Show"
    assert show[1].startswith("2019-09-01 ")  # Local() shifts into box tz
    assert show[2] == "TV-MA"

    seasons = video_query("SELECT idShow, season, name FROM seasons ORDER BY season")
    assert (1, -1, None) in seasons  # specials placeholder from the fork flow
    assert (1, 1, "Season 1") in seasons

    episode = video_query("SELECT c00, c12, c13, idShow, idSeason FROM episode")[0]
    assert episode[0] == "Pilot"
    assert (episode[1], episode[2]) == ("1", "1")

    show_paths = {p[0] for p in video_query("SELECT strPath FROM path")}
    assert "plugin://plugin.video.kofin/" in show_paths
    assert "plugin://plugin.video.kofin/lib-shows/" in show_paths
    assert "plugin://plugin.video.kofin/lib-shows/series1/" in show_paths

    link = video_query("SELECT idShow, idPath FROM tvshowlinkpath")
    assert len(link) == 1

    mapping = dict(
        (row[0], row[1])
        for row in kofin_query("SELECT jellyfin_id, media_type FROM jellyfin")
    )
    assert mapping["series1"] == "tvshow"
    assert mapping["season1"] == "season"
    assert mapping["episode1"] == "episode"

    # Resume bookmark, both on the episode file and the widget alias file.
    bookmarks = video_query("SELECT timeInSeconds FROM bookmark")
    assert all(b == (300.0,) for b in bookmarks)
    assert len(bookmarks) == 2


ORPHAN_RULES = [
    (
        "genre_link media_id/movie",
        "SELECT COUNT(*) FROM genre_link WHERE media_type='movie' AND media_id NOT IN (SELECT idMovie FROM movie)",
    ),
    (
        "genre_link media_id/tvshow",
        "SELECT COUNT(*) FROM genre_link WHERE media_type='tvshow' AND media_id NOT IN (SELECT idShow FROM tvshow)",
    ),
    (
        "actor_link/movie",
        "SELECT COUNT(*) FROM actor_link WHERE media_type='movie' AND media_id NOT IN (SELECT idMovie FROM movie)",
    ),
    (
        "actor_link/tvshow",
        "SELECT COUNT(*) FROM actor_link WHERE media_type='tvshow' AND media_id NOT IN (SELECT idShow FROM tvshow)",
    ),
    (
        "actor_link/episode",
        "SELECT COUNT(*) FROM actor_link WHERE media_type='episode' AND media_id NOT IN (SELECT idEpisode FROM episode)",
    ),
    (
        "director_link/episode",
        "SELECT COUNT(*) FROM director_link WHERE media_type='episode' AND media_id NOT IN (SELECT idEpisode FROM episode)",
    ),
    (
        "writer_link/episode",
        "SELECT COUNT(*) FROM writer_link WHERE media_type='episode' AND media_id NOT IN (SELECT idEpisode FROM episode)",
    ),
    (
        "studio_link/tvshow",
        "SELECT COUNT(*) FROM studio_link WHERE media_type='tvshow' AND media_id NOT IN (SELECT idShow FROM tvshow)",
    ),
    (
        "tag_link/tvshow",
        "SELECT COUNT(*) FROM tag_link WHERE media_type='tvshow' AND media_id NOT IN (SELECT idShow FROM tvshow)",
    ),
    (
        "rating/tvshow",
        "SELECT COUNT(*) FROM rating WHERE media_type='tvshow' AND media_id NOT IN (SELECT idShow FROM tvshow)",
    ),
    (
        "rating/episode",
        "SELECT COUNT(*) FROM rating WHERE media_type='episode' AND media_id NOT IN (SELECT idEpisode FROM episode)",
    ),
    (
        "uniqueid/tvshow",
        "SELECT COUNT(*) FROM uniqueid WHERE media_type='tvshow' AND media_id NOT IN (SELECT idShow FROM tvshow)",
    ),
    (
        "uniqueid/episode",
        "SELECT COUNT(*) FROM uniqueid WHERE media_type='episode' AND media_id NOT IN (SELECT idEpisode FROM episode)",
    ),
    (
        "art/tvshow",
        "SELECT COUNT(*) FROM art WHERE media_type='tvshow' AND media_id NOT IN (SELECT idShow FROM tvshow)",
    ),
    (
        "art/season",
        "SELECT COUNT(*) FROM art WHERE media_type='season' AND media_id NOT IN (SELECT idSeason FROM seasons)",
    ),
    (
        "art/episode",
        "SELECT COUNT(*) FROM art WHERE media_type='episode' AND media_id NOT IN (SELECT idEpisode FROM episode)",
    ),
    (
        "seasons.idShow",
        "SELECT COUNT(*) FROM seasons WHERE idShow NOT IN (SELECT idShow FROM tvshow)",
    ),
    (
        "episode.idShow",
        "SELECT COUNT(*) FROM episode WHERE idShow NOT IN (SELECT idShow FROM tvshow)",
    ),
    (
        "episode.idSeason",
        "SELECT COUNT(*) FROM episode WHERE idSeason NOT IN (SELECT idSeason FROM seasons)",
    ),
    (
        "bookmark.idFile",
        "SELECT COUNT(*) FROM bookmark WHERE idFile NOT IN (SELECT idFile FROM files)",
    ),
    (
        "streamdetails.idFile",
        "SELECT COUNT(*) FROM streamdetails WHERE idFile NOT IN (SELECT idFile FROM files)",
    ),
    (
        "tvshowlinkpath.idShow",
        "SELECT COUNT(*) FROM tvshowlinkpath WHERE idShow NOT IN (SELECT idShow FROM tvshow)",
    ),
    (
        "tag_link.tag_id",
        "SELECT COUNT(*) FROM tag_link WHERE tag_id NOT IN (SELECT tag_id FROM tag)",
    ),
]


def test_series_removal_leaves_no_orphans(api):
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)  # unrelated content must survive
    write_series_tree(api)

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        TVShows(api, kdb, vdb, library=TV_LIBRARY).remove("series1")

    assert video_query("SELECT COUNT(*) FROM tvshow") == [(0,)]
    assert video_query("SELECT COUNT(*) FROM seasons") == [(0,)]
    assert video_query("SELECT COUNT(*) FROM episode") == [(0,)]

    for label, sql in ORPHAN_RULES:
        assert video_query(sql) == [(0,)], "orphans in %s" % label

    remaining = kofin_query("SELECT jellyfin_id FROM jellyfin ORDER BY jellyfin_id")
    assert remaining == [("movie1",)]

    # The unrelated movie is fully intact.
    assert video_query("SELECT COUNT(*) FROM movie") == [(1,)]
    assert (
        video_query("SELECT COUNT(*) FROM art WHERE media_type='movie' AND media_id=1")[
            0
        ][0]
        > 0
    )


def test_episode_removal_prunes_empty_show(api):
    write_series_tree(api)

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        TVShows(api, kdb, vdb, library=TV_LIBRARY).remove("episode1")

    # Last episode gone -> season and show pruned too (fork semantics).
    assert video_query("SELECT COUNT(*) FROM episode") == [(0,)]
    assert video_query("SELECT COUNT(*) FROM seasons") == [(0,)]
    assert video_query("SELECT COUNT(*) FROM tvshow") == [(0,)]
    assert kofin_query("SELECT COUNT(*) FROM jellyfin") == [(0,)]


# --- music videos ----------------------------------------------------------------


def test_musicvideo_write_and_idempotency(api):
    register_views({"Id": "lib-mv", "Name": "Clips", "Media": "musicvideos"})
    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        MusicVideos(api, kdb, vdb, library=MV_LIBRARY).musicvideo(dto(MUSICVIDEO))

    row = video_query("SELECT c00, c09, c10, premiered FROM musicvideo")[0]
    assert row[0] == "Hit Single"
    assert row[1] == "Hits"
    assert row[2] == "The Band"
    assert row[3].startswith("2018-06-0")

    files = video_query("SELECT strFilename FROM files")[0]
    assert "dbid=1" in files[0] and "id=mvideo1" in files[0]

    first = dump(str(sync_db._path_overrides["video"]))
    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        MusicVideos(api, kdb, vdb, library=MV_LIBRARY).musicvideo(dto(MUSICVIDEO))
    assert dump(str(sync_db._path_overrides["video"])) == first


# --- music -----------------------------------------------------------------------


class _FrozenDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2024, 6, 1, 12, 0, 0)


@pytest.fixture
def frozen_music_clock(monkeypatch):
    frozen = type(
        "datetime",
        (),
        {"datetime": _FrozenDateTime, "date": datetime.date},
    )
    monkeypatch.setattr("kofin.sync.writers.music.datetime", frozen)


def write_music_tree(api):
    register_views({"Id": "lib-music", "Name": "Tunes", "Media": "music"})
    with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
        music = Music(api, kdb, mdb, library=MUSIC_LIBRARY)
        music.artist(dto(ARTIST))
        music.album(dto(ALBUM))
        music.song(dto(SONG))


def test_music_artist_album_song_write(api, frozen_music_clock):
    write_music_tree(api)

    artist = music_query(
        "SELECT idArtist, strArtist, strMusicBrainzArtistID FROM artist WHERE strArtist='The Band'"
    )
    assert len(artist) == 1
    artist_id = artist[0][0]

    album = music_query(
        "SELECT idAlbum, strAlbum, strArtistDisp, strReleaseDate, strGenres FROM album"
    )
    assert len(album) == 1
    assert album[0][1] == "Greatest Hits"
    assert album[0][2] == "The Band"

    song = music_query(
        "SELECT strTitle, iTrack, iDuration, strFileName, idAlbum FROM song"
    )[0]
    assert song[0] == "Opening Track"
    assert song[1] == 1 * 2**16 + 1  # disc * 2^16 + track
    assert song[2] == 180
    assert song[3] == "stream.flac?static=true"
    assert song[4] == album[0][0]

    link = music_query("SELECT idArtist, idAlbum FROM album_artist")
    assert (artist_id, album[0][0]) in link

    song_artists = music_query("SELECT idArtist, idSong FROM song_artist")
    assert len(song_artists) == 1

    path = music_query("SELECT strPath FROM path")[0]
    assert path[0] == "http://server:8096/Audio/song1/"

    mapping = dict(
        (row[0], row[1])
        for row in kofin_query("SELECT jellyfin_id, media_type FROM jellyfin")
    )
    assert mapping == {"artist1": "artist", "album1": "album", "song1": "song"}


def test_music_write_is_idempotent(api, frozen_music_clock):
    write_music_tree(api)
    first = dump(str(sync_db._path_overrides["music"]))
    first_map = dump(str(sync_db._path_overrides["kofin"]))

    write_music_tree(api)
    assert dump(str(sync_db._path_overrides["music"])) == first
    assert dump(str(sync_db._path_overrides["kofin"])) == first_map


def test_music_artist_removal_no_orphans(api, frozen_music_clock):
    write_music_tree(api)

    with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
        Music(api, kdb, mdb, library=MUSIC_LIBRARY).remove("artist1")

    assert music_query("SELECT COUNT(*) FROM song") == [(0,)]
    assert music_query("SELECT COUNT(*) FROM album") == [(0,)]
    assert music_query("SELECT COUNT(*) FROM artist WHERE strArtist='The Band'") == [
        (0,)
    ]
    assert music_query(
        "SELECT COUNT(*) FROM album_artist WHERE idAlbum NOT IN (SELECT idAlbum FROM album)"
    ) == [(0,)]
    assert music_query(
        "SELECT COUNT(*) FROM song_artist WHERE idSong NOT IN (SELECT idSong FROM song)"
    ) == [(0,)]
    assert music_query(
        "SELECT COUNT(*) FROM song_genre WHERE idSong NOT IN (SELECT idSong FROM song)"
    ) == [(0,)]
    assert kofin_query("SELECT COUNT(*) FROM jellyfin") == [(0,)]
