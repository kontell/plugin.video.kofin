"""L2 writer suite: transplanted writers against pristine Kodi schemas.

Invariants from the phase-2 plan (§5 step 2): full-fidelity writes for every
media type, idempotency (second write of the same payload leaves a
byte-identical database), and removal integrity (deleting a series leaves
zero orphans in any link table).
"""

import datetime
import queue
import re
import sqlite3
import threading

import pytest

from kofin.sync import db as sync_db
from kofin.sync import schema
from kofin.sync.kodidb.kodi import Kodi
from kofin.sync.library import UpdateWorker
from kofin.sync.newcontent import Entry
from kofin.sync.shims import LibraryOrphanException
from kofin.sync.writers import Movies, MusicVideos, TVShows, Music
from kofin.sync.writers.movies import (
    BOXSET_GUARDED,
    BOXSET_HEALED,
    BOXSET_UNCHANGED,
    BOXSET_WRITTEN,
)
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
        (kodifixtures.PIERS_VIDEO_VERSION_147, kodifixtures.PIERS_MUSIC_VERSION),
    ],
    ids=["omega", "piers", "piers147"],
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


def write_boxset(api, payload=None):
    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        return Movies(api, kdb, vdb, library=LIBRARY).boxset(payload or dto(BOXSET))


def dump(path):
    conn = sqlite3.connect(path)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


_DATETIME_LITERAL = re.compile(r"'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}'")


def music_dump(path):
    """Music dump with datetime literals masked. Kodi's own schema triggers
    (tgrInsertSong/tgrUpdateSong and the album/artist pair) stamp dateNew and
    dateModified with SQLite's DATETIME('now'), which no Python-side clock
    freeze can reach — a write pair straddling a second boundary flips them."""
    return _DATETIME_LITERAL.sub("'<clock>'", dump(path))


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

    # Community rating first (the fork's single 'default'-typed row), then the
    # server's critic rating rescaled onto Kodi's 0-10 scale: 81 -> 8.1.
    ratings = video_query(
        "SELECT rating_type, rating, votes FROM rating "
        "WHERE media_type='movie' ORDER BY rating_id"
    )
    assert ratings == [("default", 7.8, 1234), ("critic", 8.1, None)]
    # c05 is the default-rating pointer movie_view joins on: community, unless
    # the user asked for critic.
    assert row["c05"] == str(
        video_query("SELECT rating_id FROM rating WHERE rating_type='default'")[0][0]
    )

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
    # Community + critic, still one of each: the second write updates the rows
    # it found rather than adding a set.
    assert video_query("SELECT COUNT(*) FROM rating") == [(2,)]


def _pointer(rating_type):
    """(c05, the rating_id of that type) — the pointer test in one place."""
    rows = video_query(
        "SELECT rating_id FROM rating WHERE media_type='movie' AND rating_type=?",
        (rating_type,),
    )
    return (
        video_query("SELECT c05 FROM movie")[0][0],
        str(rows[0][0]) if rows else None,
    )


def test_movie_prefer_critic_points_at_the_critic_row(api):
    FakeAddon.store["preferCriticRating"] = "true"
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)

    pointer, critic_id = _pointer("critic")
    assert pointer == critic_id
    # Both rows are always written; only which one Kodi calls the default moves.
    assert video_query("SELECT COUNT(*) FROM rating") == [(2,)]


def test_movie_prefer_critic_falls_back_to_community(api):
    """A film the server has no critic rating for keeps its community one."""
    FakeAddon.store["preferCriticRating"] = "true"
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})

    payload = dto(MOVIE)
    del payload["CriticRating"]
    write_movie(api, payload)

    pointer, community_id = _pointer("default")
    assert pointer == community_id
    assert video_query("SELECT rating_type FROM rating") == [("default",)]


def test_movie_dropped_critic_rating_never_dangles(api):
    """The server losing a rating deletes its row — and the pointer with it,
    or movie_view's LEFT JOIN would render the film unrated."""
    FakeAddon.store["preferCriticRating"] = "true"
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)

    dropped = dto(MOVIE)
    dropped["Etag"] = "etag-movie1-v2"
    del dropped["CriticRating"]
    write_movie(api, dropped)

    assert video_query("SELECT rating_type FROM rating") == [("default",)]
    pointer, community_id = _pointer("default")
    assert pointer == community_id
    assert video_query(
        "SELECT rating FROM rating WHERE rating_id = (SELECT c05 FROM movie)"
    ) == [(7.8,)]


def test_movie_repoint_ratings_moves_the_pointer_only(api):
    """The settings flip's apply path: no resync, just the pointer."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)
    before = video_query(
        "SELECT rating_id, rating_type, rating FROM rating ORDER BY rating_id"
    )

    with sync_db.Database("video") as vdb:
        from kofin.sync.kodidb import Movies as KodiDb

        assert KodiDb(vdb.cursor).repoint_ratings([1], "critic") == 1

    assert _pointer("critic")[0] == _pointer("critic")[1]
    assert (
        video_query(
            "SELECT rating_id, rating_type, rating FROM rating ORDER BY rating_id"
        )
        == before
    )

    with sync_db.Database("video") as vdb:
        KodiDb(vdb.cursor).repoint_ratings([1], "default")

    assert _pointer("default")[0] == _pointer("default")[1]


def test_movie_repoint_ratings_leaves_foreign_movies_alone(api):
    """Kodi's own scraped rows carry 'default' ratings too; theirs is not ours
    to move, so only the ids the caller owns are touched."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)
    scraped_pointer = video_query("SELECT c05 FROM movie")[0][0]

    with sync_db.Database("video") as vdb:
        from kofin.sync.kodidb import Movies as KodiDb

        assert KodiDb(vdb.cursor).repoint_ratings([999], "critic") == 0

    assert video_query("SELECT c05 FROM movie")[0][0] == scraped_pointer


# --- movie extras (phase 3: native videoversion assets) ----------------------

# Extra1 is 60s; the parent film is 7200s (MOVIE RunTimeTicks). Duration
# must land on the extra's file_id, not the film's.
FEATURES = [
    {
        "Id": "extra1",
        "Name": "Making Of",
        "Type": "Video",
        "ExtraType": "BehindTheScenes",
        "Path": "/media/movies/The Example (2020)/extras/making-of.mkv",
        "RunTimeTicks": 600_000_000,
        "MediaSources": [
            {
                "Id": "extra1",
                "Container": "mkv",
                "RunTimeTicks": 600_000_000,
                "MediaStreams": [
                    {
                        "Type": "Video",
                        "Codec": "h264",
                        "Width": 1920,
                        "Height": 1080,
                        "AspectRatio": "16:9",
                    },
                    {
                        "Type": "Audio",
                        "Codec": "aac",
                        "Channels": 2,
                        "Language": "eng",
                    },
                ],
            }
        ],
    },
    {
        "Id": "extra2",
        "Name": "Gone Too Soon",
        "Type": "Video",
        "ExtraType": "DeletedScene",
        "Path": "/media/movies/The Example (2020)/extras/deleted.mkv",
        "RunTimeTicks": 120_000_000,
        "MediaSources": [
            {
                "Id": "extra2",
                "Container": "mkv",
                "RunTimeTicks": 120_000_000,
                "MediaStreams": [
                    {
                        "Type": "Video",
                        "Codec": "h264",
                        "Width": 1280,
                        "Height": 720,
                    }
                ],
            }
        ],
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


def test_movie_extras_duration_not_film_duration(api):
    """Extras must carry their own streamdetails duration (video-versions plan
    PR 1). Without it Kodi falls back to the parent film's runtime until the
    extra is played once."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    api.special_features_by_id = {"movie1": FEATURES}
    write_movie(api, movie_with_extras())

    film_file_id = video_query("SELECT idFile FROM movie")[0][0]
    film_duration = video_query(
        "SELECT iVideoDuration FROM streamdetails"
        " WHERE idFile = ? AND iStreamType = 0",
        (film_file_id,),
    )[0][0]
    assert film_duration == 7200  # MOVIE RunTimeTicks / 1e7

    by_feature = {}
    for file_id, _media, _type, name, _owner, _vt in extras_rows():
        duration = video_query(
            "SELECT iVideoDuration FROM streamdetails"
            " WHERE idFile = ? AND iStreamType = 0",
            (file_id,),
        )[0][0]
        by_feature[name] = (file_id, duration)

    assert by_feature["Behind the Scenes"][1] == 60
    assert by_feature["Deleted Scene"][1] == 12
    assert by_feature["Behind the Scenes"][1] != film_duration
    assert by_feature["Deleted Scene"][1] != film_duration

    # Streams land on the extra's file, not the film's.
    for file_id, _ in by_feature.values():
        assert file_id != film_file_id
        codec = video_query(
            "SELECT strVideoCodec FROM streamdetails"
            " WHERE idFile = ? AND iStreamType = 0",
            (file_id,),
        )[0][0]
        assert codec == "h264"


def test_movie_extras_duration_from_runtime_only(api):
    """A feature with RunTimeTicks but no MediaStreams still gets a duration
    row (stub video track) so the UI is correct."""
    bare = [
        {
            "Id": "extra-bare",
            "Name": "Bare Extra",
            "Type": "Video",
            "ExtraType": "Clip",
            "Path": "/media/movies/The Example (2020)/extras/bare.mkv",
            "RunTimeTicks": 90_000_000,
        }
    ]
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    api.special_features_by_id = {"movie1": bare}
    write_movie(api, movie_with_extras(count=1))

    rows = extras_rows()
    assert len(rows) == 1
    file_id = rows[0][0]
    duration = video_query(
        "SELECT iVideoDuration FROM streamdetails"
        " WHERE idFile = ? AND iStreamType = 0",
        (file_id,),
    )[0][0]
    assert duration == 9


# --- movie video versions (MediaSources → native VERSION assets) ------------


def _version_source(source_id, name, path, ticks, width=1920, height=1080):
    return {
        "Id": source_id,
        "Name": name,
        "Path": path,
        "Container": "mkv",
        "RunTimeTicks": ticks,
        "MediaStreams": [
            {
                "Type": "Video",
                "Codec": "h264",
                "Width": width,
                "Height": height,
                "AspectRatio": "16:9",
            }
        ],
    }


def movie_with_versions(etag="etag-movie1-v1"):
    """Primary matches item Id; alternate is Director's Cut (seeded builtin)."""
    payload = dto(MOVIE)
    payload["Etag"] = etag
    payload["MediaSources"] = [
        _version_source(
            "movie1",
            "Theatrical Cut",
            "/media/movies/The Example (2020)/The Example - Theatrical.mkv",
            72000000000,
        ),
        _version_source(
            "source-dc",
            "Director's Cut",
            "/media/movies/The Example (2020)/The Example - Directors Cut.mkv",
            80000000000,
            width=3840,
            height=2160,
        ),
    ]
    return payload


def version_rows():
    """VERSION assets (primary + alternates), ordered by idFile."""
    return video_query(
        "SELECT videoversion.idFile, videoversion.idMedia, videoversion.idType,"
        " videoversiontype.name, videoversiontype.owner, videoversiontype.itemType,"
        " files.strFilename"
        " FROM videoversion"
        " JOIN videoversiontype ON videoversiontype.id = videoversion.idType"
        " JOIN files ON files.idFile = videoversion.idFile"
        " WHERE videoversion.itemType = ? ORDER BY videoversion.idFile",
        (version_item_type(),),
    )


def test_movie_versions_written_as_native_assets(api):
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api, movie_with_versions())

    rows = version_rows()
    assert len(rows) == 2
    film_file_id = video_query("SELECT idFile FROM movie")[0][0]
    primary = next(r for r in rows if r[0] == film_file_id)
    alternate = next(r for r in rows if r[0] != film_file_id)

    assert primary[1] == 1  # idMedia
    assert primary[3] == "Theatrical Cut"
    # Builtin Theatrical Cut is seeded (40406); owner 0 system, not USER.
    assert primary[5] == version_item_type()

    assert alternate[1] == 1
    assert alternate[3] == "Director's Cut"
    assert alternate[5] == version_item_type()
    assert "mediasourceid=source-dc" in alternate[6]
    assert "id=movie1" in alternate[6]
    assert "mode=play" in alternate[6]
    assert alternate[6].startswith("plugin://plugin.video.kofin/lib-movies/?")

    # Alternate has its own duration (8000s), not the film's primary streams alone.
    alt_duration = video_query(
        "SELECT iVideoDuration FROM streamdetails"
        " WHERE idFile = ? AND iStreamType = 0",
        (alternate[0],),
    )[0][0]
    assert alt_duration == 8000


def test_movie_single_named_primary_version(api):
    """One MediaSource named Director's Cut → primary idType is that builtin."""
    payload = dto(MOVIE)
    payload["MediaSources"] = [
        _version_source(
            "movie1",
            "Director's Cut",
            "/media/movies/The Example (2020)/The Example.mkv",
            72000000000,
        )
    ]
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api, payload)

    rows = version_rows()
    assert len(rows) == 1
    film_file_id = video_query("SELECT idFile FROM movie")[0][0]
    assert rows[0][0] == film_file_id
    assert rows[0][3] == "Director's Cut"
    assert rows[0][2] != 40400


def test_movie_versions_idempotent(api):
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api, movie_with_versions())
    first = dump(str(sync_db._path_overrides["video"]))

    write_movie(api, movie_with_versions())
    assert dump(str(sync_db._path_overrides["video"])) == first

    before = version_rows()
    write_movie(api, movie_with_versions(etag="etag-movie1-v2"))
    assert version_rows() == before


def test_movie_versions_pruned_when_source_disappears(api):
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api, movie_with_versions())
    assert len(version_rows()) == 2

    payload = movie_with_versions(etag="etag-movie1-v2")
    payload["MediaSources"] = payload["MediaSources"][:1]  # primary only
    write_movie(api, payload)

    rows = version_rows()
    assert len(rows) == 1
    film_file_id = video_query("SELECT idFile FROM movie")[0][0]
    assert rows[0][0] == film_file_id
    gone = video_query(
        "SELECT COUNT(*) FROM files WHERE strFilename LIKE '%mediasourceid=source-dc%'"
    )
    assert gone == [(0,)]


def test_movie_versions_removed_with_movie(api):
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api, movie_with_versions())
    assert len(version_rows()) == 2

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        Movies(api, kdb, vdb, library=LIBRARY).remove("movie1")

    assert video_query("SELECT COUNT(*) FROM movie") == [(0,)]
    assert video_query("SELECT COUNT(*) FROM videoversion") == [(0,)]
    assert video_query("SELECT COUNT(*) FROM files") == [(0,)]
    assert video_query("SELECT COUNT(*) FROM streamdetails") == [(0,)]


def test_movie_versions_failure_never_gates_sync(api, monkeypatch):
    """A failure inside the versions pass must not roll back the movie row."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})

    def boom(_feature):
        raise RuntimeError("streams down")

    monkeypatch.setattr("kofin.sync.writers.movies.streams_and_runtime", boom)
    write_movie(api, movie_with_versions())

    assert video_query("SELECT COUNT(*) FROM movie") == [(1,)]
    film_file_id = video_query("SELECT idFile FROM movie")[0][0]
    # Primary version row is written in movie_add before versions() runs.
    primary = video_query(
        "SELECT idType FROM videoversion WHERE idFile = ?", (film_file_id,)
    )
    assert primary == [(40406,)]  # Theatrical Cut builtin


def test_movie_novel_version_name_creates_user_type(api):
    payload = dto(MOVIE)
    payload["MediaSources"] = [
        _version_source(
            "movie1",
            "Standard Edition",
            "/media/movies/The Example (2020)/a.mkv",
            72000000000,
        ),
        _version_source(
            "source-imax",
            "IMAX Exclusive",
            "/media/movies/The Example (2020)/b.mkv",
            75000000000,
        ),
    ]
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api, payload)

    rows = version_rows()
    assert len(rows) == 2
    imax = next(r for r in rows if r[3] == "IMAX Exclusive")
    assert imax[4] == 2  # VIDEO_ASSET_OWNER_USER
    # Second write reuses the same type id (no duplicate names).
    write_movie(api, {**payload, "Etag": "etag-movie1-v2"})
    type_names = video_query(
        "SELECT name FROM videoversiontype WHERE name = 'IMAX Exclusive'"
    )
    assert len(type_names) == 1


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


def video_exec(sql, args=()):
    conn = sqlite3.connect(str(sync_db._path_overrides["video"]))
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def kofin_exec(sql, args=()):
    conn = sqlite3.connect(str(sync_db._path_overrides["kofin"]))
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def boxset_log(monkeypatch):
    """Recorded (level, message) tuples from the movies writer's logger."""
    from kofin.sync.writers import movies as writers_movies

    calls = []

    def record(level):
        def _log(msg, *args):
            calls.append((level, msg % args if args else msg))

        return _log

    for level in ("warning", "info"):
        monkeypatch.setattr(writers_movies.LOG, level, record(level))

    return calls


def linked_boxset(api):
    """movie1+movie2 written and linked into set1; returns nothing."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)
    write_movie(api, dto(MOVIE_2))
    api.boxset_children = {"set1": [dto(MOVIE), dto(MOVIE_2)]}
    assert write_boxset(api) == BOXSET_WRITTEN
    assert video_query("SELECT COUNT(*) FROM movie WHERE idSet = 1") == [(2,)]
    assert kofin_query("SELECT linked_count FROM boxset_state") == [(2,)]


def test_boxset_second_write_unchanged_and_idempotent(api):
    linked_boxset(api)
    first = dump(str(sync_db._path_overrides["video"]))
    first_map = dump(str(sync_db._path_overrides["kofin"]))

    assert write_boxset(api) == BOXSET_UNCHANGED
    assert dump(str(sync_db._path_overrides["video"])) == first
    assert dump(str(sync_db._path_overrides["kofin"])) == first_map


def test_boxset_heals_readded_member(api):
    """The V1 drift: a member removed and re-added comes back as a fresh
    movie row with no idSet while the set's Etag never moves. The health
    check must catch the count mismatch and force a relink."""
    linked_boxset(api)

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        Movies(api, kdb, vdb, library=LIBRARY).remove("movie1")
    write_movie(api)

    assert video_query("SELECT COUNT(*) FROM movie WHERE idSet = 1") == [(1,)]

    assert write_boxset(api) == BOXSET_HEALED
    assert video_query("SELECT COUNT(*) FROM movie WHERE idSet = 1") == [(2,)]
    assert kofin_query("SELECT linked_count FROM boxset_state") == [(2,)]
    assert kofin_query("SELECT checksum FROM jellyfin WHERE jellyfin_id = 'set1'") == [
        ("etag-set1-v1|plugin",)
    ]

    assert write_boxset(api) == BOXSET_UNCHANGED


def test_boxset_guard_blocks_suspicious_empty(api, boxset_log):
    """A membership query answering 200-with-zero-items while the DTO says
    the set has children must not mass-unlink, and must not advance the
    checksum (so a changed Etag retries on the next walk)."""
    linked_boxset(api)

    api.boxset_children = {"set1": []}
    payload = dto(BOXSET)
    payload["Etag"] = "etag-set1-v2"
    payload["ChildCount"] = 2

    assert write_boxset(api, payload) == BOXSET_GUARDED
    assert video_query("SELECT COUNT(*) FROM movie WHERE idSet = 1") == [(2,)]
    assert kofin_query("SELECT checksum FROM jellyfin WHERE jellyfin_id = 'set1'") == [
        ("etag-set1-v1|plugin",)
    ]
    assert kofin_query("SELECT linked_count FROM boxset_state") == [(2,)]

    warnings = [msg for level, msg in boxset_log if level == "warning"]
    assert len(warnings) == 1
    assert "0 members" in warnings[0]

    # Unknown children signal (no ChildCount/RecursiveItemCount in the DTO)
    # is just as suspicious: block and warn again on the retry.
    del payload["ChildCount"]
    assert write_boxset(api, dict(payload)) == BOXSET_GUARDED
    assert video_query("SELECT COUNT(*) FROM movie WHERE idSet = 1") == [(2,)]


def test_boxset_confirmed_empty_unlinks_and_springs_back(api, boxset_log):
    """ChildCount 0 confirms a genuinely emptied set: unlink, but leave the
    checksum unstamped so the set is re-verified every walk and springs back
    without any Etag movement (the permission-flap recovery)."""
    linked_boxset(api)

    api.boxset_children = {"set1": []}
    payload = dto(BOXSET)
    payload["Etag"] = "etag-set1-v2"
    payload["ChildCount"] = 0

    assert write_boxset(api, dict(payload)) == BOXSET_WRITTEN
    assert video_query("SELECT COUNT(*) FROM movie WHERE idSet = 1") == [(0,)]
    assert kofin_query("SELECT linked_count FROM boxset_state") == [(0,)]
    assert kofin_query("SELECT checksum FROM jellyfin WHERE jellyfin_id = 'set1'") == [
        (None,)
    ]
    assert not [msg for level, msg in boxset_log if level == "warning"]

    # Members reappear with the same Etag: the NULL checksum forces the
    # pass, which relinks and stamps the checksum again.
    api.boxset_children = {"set1": [dto(MOVIE)]}
    payload["ChildCount"] = 1
    assert write_boxset(api, dict(payload)) == BOXSET_WRITTEN
    assert video_query("SELECT COUNT(*) FROM movie WHERE idSet = 1") == [(1,)]
    assert kofin_query("SELECT checksum FROM jellyfin WHERE jellyfin_id = 'set1'") == [
        ("etag-set1-v2|plugin",)
    ]


def test_boxset_repairs_missing_sets_row(api):
    """A sets row deleted underneath a live reference (Kodi's clean-library
    drops memberless sets) previously took the update leg and UPDATEd
    nothing; now it is recreated and every member relinked."""
    linked_boxset(api)

    video_exec("DELETE FROM sets")
    # A decoy keeps SQLite from handing the recreated set the old rowid, so
    # the assertions can tell "relinked to the new row" from "old id reused".
    video_exec("INSERT INTO sets(idSet, strSet) VALUES (5, 'decoy')")
    assert write_boxset(api) == BOXSET_HEALED

    sets = video_query("SELECT idSet, strSet FROM sets ORDER BY idSet")
    assert sets == [(5, "decoy"), (6, "Example Collection")]
    assert video_query("SELECT COUNT(*) FROM movie WHERE idSet = 6") == [(2,)]
    assert video_query("SELECT COUNT(*) FROM movie WHERE idSet = 1") == [(0,)]
    assert kofin_query("SELECT kodi_id FROM jellyfin WHERE jellyfin_id = 'set1'") == [
        (6,)
    ]
    assert kofin_query(
        "SELECT COUNT(*) FROM jellyfin WHERE parent_id = 6 AND media_type = 'movie'"
    ) == [(2,)]
    assert kofin_query("SELECT linked_count FROM boxset_state") == [(2,)]


def test_boxset_zero_movie_set_never_stamps_and_stays_quiet(api, boxset_log):
    """A set with children but no movie members (mixed collections are
    legitimate) writes cleanly with no warning, keeps a NULL checksum, and
    never enters a heal loop."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    api.boxset_children = {"set1": []}
    payload = dto(BOXSET)
    payload["ChildCount"] = 2

    assert write_boxset(api, dict(payload)) == BOXSET_WRITTEN
    assert kofin_query("SELECT checksum FROM jellyfin WHERE jellyfin_id = 'set1'") == [
        (None,)
    ]
    assert kofin_query("SELECT linked_count FROM boxset_state") == [(0,)]

    assert write_boxset(api, dict(payload)) == BOXSET_WRITTEN
    assert not [msg for level, msg in boxset_log if level == "warning"]


def test_boxset_missing_state_forces_one_heal(api):
    """A stamped set without a boxset_state row (the first pass after
    upgrade) heals exactly once, then goes quiet."""
    linked_boxset(api)
    kofin_exec("DELETE FROM boxset_state")

    assert write_boxset(api) == BOXSET_HEALED
    assert kofin_query("SELECT linked_count FROM boxset_state") == [(2,)]
    assert write_boxset(api) == BOXSET_UNCHANGED


def test_boxset_state_dies_with_the_set(api):
    linked_boxset(api)

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        Movies(api, kdb, vdb, library=LIBRARY).remove("set1")
    assert kofin_query("SELECT COUNT(*) FROM boxset_state") == [(0,)]

    api.boxset_children = {"set1": [dto(MOVIE)]}
    assert write_boxset(api) == BOXSET_WRITTEN
    assert kofin_query("SELECT COUNT(*) FROM boxset_state") == [(1,)]

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        Movies(api, kdb, vdb, library=LIBRARY).boxsets_reset()
    assert kofin_query("SELECT COUNT(*) FROM boxset_state") == [(0,)]
    assert video_query("SELECT COUNT(*) FROM sets") == [(0,)]


def make_fullsync(api):
    """A FullSync wired for direct method calls (no context manager, no
    Kodi): only the database lock is real."""
    from types import SimpleNamespace

    from kofin.sync.full_sync import FullSync

    FullSync._shared_state.clear()
    sync = FullSync(library=SimpleNamespace(database_lock=threading.Lock()), server=api)
    sync.sync = {"Libraries": [], "Whitelist": [], "RestorePoints": {}}
    return sync


def test_boxset_sweep_removes_stale(api):
    """A set deleted server-side with no feed record to say so (tier 2,
    retention gap) is removed by the walk's sweep — reference, sets row,
    state row and links all go."""
    linked_boxset(api)

    ghost = dto(BOXSET)
    ghost["Id"] = "set2"
    ghost["Name"] = "Ghost Collection"
    ghost["Etag"] = "etag-set2-v1"
    api.boxset_children["set2"] = []
    write_boxset(api, ghost)
    assert video_query("SELECT COUNT(*) FROM sets") == [(2,)]

    fullsync = make_fullsync(api)
    try:
        assert fullsync.sweep_stale_boxsets({"set1"}) == 1
    finally:
        type(fullsync)._shared_state.clear()

    assert kofin_query("SELECT jellyfin_id FROM jellyfin WHERE media_type = 'set'") == [
        ("set1",)
    ]
    assert video_query("SELECT strSet FROM sets") == [("Example Collection",)]
    assert kofin_query("SELECT jellyfin_id FROM boxset_state") == [("set1",)]
    assert video_query("SELECT COUNT(*) FROM movie WHERE idSet = 1") == [(2,)]


def test_boxset_sweep_refuses_an_empty_listing(api):
    """An empty walk against existing references is not a deletion order:
    permission and filter failures look exactly like it."""
    linked_boxset(api)

    fullsync = make_fullsync(api)
    try:
        assert fullsync.sweep_stale_boxsets(set()) == 0
    finally:
        type(fullsync)._shared_state.clear()

    assert kofin_query("SELECT COUNT(*) FROM jellyfin WHERE media_type = 'set'") == [
        (1,)
    ]
    assert video_query("SELECT COUNT(*) FROM sets") == [(1,)]


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

    # The content/scraper pair belongs to this library's path, and the addon
    # root must stay bare. The fork stamped the root instead, and on Kodi 21
    # that makes OnItemInfo bail out ("dont lookup on root tvshow folder") for
    # every item in kofin's own plugin listings, whose paths live directly
    # under the root -- no info dialog for movies or episodes, nothing logged.
    content = dict(
        (row[0], (row[1], row[2]))
        for row in video_query("SELECT strPath, strContent, strScraper FROM path")
    )
    assert content["plugin://plugin.video.kofin/lib-shows/"] == (
        "tvshows",
        "metadata.local",
    )
    assert content["plugin://plugin.video.kofin/"] == (None, None)

    # The show's own path row carries the pair as well, useFolderNames and all:
    # a plugin:// path drills up to the addon root in one hop, so the library
    # row above is never consulted and the info dialog loads no cast without
    # this. Asserted after the episode write, which targets the same row and
    # used to blank it. See writers/tvshows.py.
    assert video_query(
        "SELECT strContent, strScraper, useFolderNames, noUpdate FROM path"
        " WHERE strPath = 'plugin://plugin.video.kofin/lib-shows/series1/'"
    ) == [("tvshows", "metadata.local", 1, 1)]

    link = video_query("SELECT idShow, idPath FROM tvshowlinkpath")
    assert len(link) == 1

    mapping = dict(
        (row[0], row[1])
        for row in kofin_query("SELECT jellyfin_id, media_type FROM jellyfin")
    )
    assert mapping["series1"] == "tvshow"
    assert mapping["season1"] == "season"
    assert mapping["episode1"] == "episode"

    # Every reference carries the Etag checksum the update-mode prune diffs
    # against. Seasons stored NULL here, so the prune reported all of them
    # changed on every pass and could never converge.
    checksums = dict(
        (row[0], row[1])
        for row in kofin_query("SELECT jellyfin_id, checksum FROM jellyfin")
    )
    assert checksums["series1"] == "etag-series1-v1|plugin"
    assert checksums["season1"] == "etag-season1-v1|plugin"
    assert checksums["episode1"] == "etag-episode1-v1|plugin"

    # Resume bookmark, both on the episode file and the widget alias file.
    bookmarks = video_query("SELECT timeInSeconds FROM bookmark")
    assert all(b == (300.0,) for b in bookmarks)
    assert len(bookmarks) == 2


def test_virtual_season_is_referenced_like_any_other(api):
    """Jellyfin marks a season virtual when it has no folder of its own --
    what a flat series layout looks like, episodes beside each other in the
    series directory instead of under "Season 01". The episodes are real
    files. get_season creates the Kodi row whatever the Location, so skipping
    only the reference left kofin.db short by exactly the number of
    flat-layout seasons: the prune reported them missing on every pass,
    re-fetched them, and the writer declined again."""
    api.seasons_by_series = {"series1": [dto(dict(SEASON_1, LocationType="Virtual"))]}
    register_views({"Id": "lib-shows", "Name": "Shows", "Media": "tvshows"})

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        TVShows(api, kdb, vdb, library=TV_LIBRARY).tvshow(dto(SERIES))

    # Kodi gets the seasons row either way -- that was never in question.
    assert video_query("SELECT COUNT(*) FROM seasons WHERE season=1") == [(1,)]

    # ...and now kofin.db can account for it, so the prune converges and the
    # row is removable through the mapping.
    reference = kofin_query(
        "SELECT media_type, checksum FROM jellyfin WHERE jellyfin_id='season1'"
    )
    assert reference == [("season", "etag-season1-v1|plugin")]


def test_orphan_season_pulls_its_series(api):
    """A season can reach the writer before its series: the change feed's
    parent prefetch only covers Added records, and the prune enqueues
    Series/Season/Episode together in SortName order. The fork looked the
    series up, missed, and returned False -- not an exception, so the
    UpdateWorker never flagged the item unapplied and no recovery prune was
    scheduled. The season's Kodi row and its kofin.db reference were both
    silently absent while the watermark moved past the change. Seasons now
    self-heal through get_show_id, exactly as orphan episodes do."""
    register_views({"Id": "lib-shows", "Name": "Shows", "Media": "tvshows"})

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        TVShows(api, kdb, vdb, library=TV_LIBRARY).season(dto(SEASON_1))

    # The series was fetched and written on the season's behalf.
    assert video_query("SELECT c00 FROM tvshow") == [("The Show",)]

    seasons = video_query("SELECT idShow, season FROM seasons ORDER BY season")
    assert (1, 1) in seasons

    mapping = dict(
        (row[0], row[1])
        for row in kofin_query("SELECT jellyfin_id, media_type FROM jellyfin")
    )
    assert mapping["series1"] == "tvshow"
    assert mapping["season1"] == "season"

    # And the reference is a normal one -- checksum included, so the prune
    # converges instead of re-fetching this season on every pass.
    assert kofin_query("SELECT checksum FROM jellyfin WHERE jellyfin_id='season1'") == [
        ("etag-season1-v1|plugin",)
    ]


def test_orphan_season_declines_when_series_is_gone(api):
    """The self-heal cannot invent a series the server no longer serves. That
    leg raises rather than returning False, which in this file is what the
    unchanged short-circuit means -- and it writes nothing at all, rather than
    leaving a season row behind with no mapping to remove it by."""
    register_views({"Id": "lib-shows", "Name": "Shows", "Media": "tvshows"})
    orphan = dto(dict(SEASON_1, SeriesId="series-gone"))

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        with pytest.raises(LibraryOrphanException):
            TVShows(api, kdb, vdb, library=TV_LIBRARY).season(orphan)

    assert video_query("SELECT COUNT(*) FROM tvshow") == [(0,)]
    assert video_query("SELECT COUNT(*) FROM seasons") == [(0,)]
    assert kofin_query("SELECT COUNT(*) FROM jellyfin") == [(0,)]


def test_worker_flags_an_unresolvable_child_unapplied(api):
    """The reporting the silent drop never got. The raise lands in the
    UpdateWorker's LibraryException handler, so the item is flagged and the
    cycle schedules a recovery prune -- and the drain carries on, because one
    bad item still must not stop it."""
    register_views({"Id": "lib-shows", "Name": "Shows", "Media": "tvshows"})
    # The worker builds its writers without a library, the way the service
    # does, so they resolve one per item off the whitelist and the ancestor
    # walk. Both legs below go through that.
    sync = sync_db.get_sync()
    sync["Whitelist"] = ["lib-shows"]
    sync_db.save_sync(sync)
    api.ancestors = lambda item_id: [{"Id": "lib-shows", "Name": "Shows"}]

    work = queue.Queue()
    work.put(dto(dict(SEASON_1, SeriesId="series-gone")))
    work.put(dto(SEASON_1))  # resolvable: heals, and proves the drain lived
    flagged = []

    worker = UpdateWorker(
        work,
        queue.Queue(),
        threading.Lock(),
        "video",
        api,
        unapplied=lambda item_id, reason: flagged.append((item_id, reason)),
    )
    worker.run()

    assert [item_id for item_id, _reason in flagged] == ["season1"]
    assert "unresolved series series-gone" in flagged[0][1]
    assert worker.is_done is True

    # The item behind it still landed, series pulled in on its behalf.
    assert video_query("SELECT c00 FROM tvshow") == [("The Show",)]
    assert kofin_query("SELECT COUNT(*) FROM jellyfin WHERE jellyfin_id='season1'") == [
        (1,)
    ]


def seed_movie_library(api):
    """The whitelist and ancestor lookup a worker-built writer resolves its
    library through, the way the service leaves them."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    sync = sync_db.get_sync()
    sync["Whitelist"] = ["lib-movies"]
    sync_db.save_sync(sync)
    api.ancestors = lambda item_id: [{"Id": "lib-movies", "Name": "Movies"}]


def test_added_writer_reports_only_what_should_be_announced(api):
    """The notify payload as the writers actually produce it: a watched
    addition is written like any other and simply never reported, which is
    where "watched gets no notification" is enforced for real items rather
    than for hand-built dicts."""
    seed_movie_library(api)

    work = queue.Queue()
    work.put(dto(MOVIE))  # UserData.Played is True -- watched
    work.put(dto(MOVIE_2))  # unwatched
    notify = queue.Queue()

    worker = UpdateWorker(
        work, notify, threading.Lock(), "video", api, notify_enabled=True
    )
    worker.run()

    # Both were written; only the unwatched one is announceable.
    assert video_query("SELECT COUNT(*) FROM movie") == [(2,)]
    reported = []
    while not notify.empty():
        reported.append(notify.get())

    assert reported == [Entry("Movie", "movie2", "Second Feature")]


def test_metadata_writers_cannot_announce_anything(api):
    """Only the added writers are built with notify_enabled, which is what
    keeps an updated item from arriving as news."""
    seed_movie_library(api)

    work = queue.Queue()
    work.put(dto(MOVIE_2))
    notify = queue.Queue()

    UpdateWorker(work, notify, threading.Lock(), "video", api).run()

    assert video_query("SELECT COUNT(*) FROM movie") == [(1,)]
    assert notify.qsize() == 0


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


def test_season_removal_spares_a_colliding_seasons_episodes(api):
    """Removing a season must not touch another season's episodes.

    Episodes hang off their season's idSeason; a season row's own parent_id is
    its series' idShow. Those are separate Kodi sequences with overlapping
    ranges, so looking episodes up by the season's parent_id matched whichever
    unrelated season carried that number as its idSeason.

    Staged as it actually happened on the Piers box: a stale Breaking Bad
    season (idShow 10) removed The Americans S4 (idSeason 10), taking 13
    episodes with it, while S4's own season row survived because only its
    children matched.
    """
    with sync_db.Database("kofin") as opened:
        from kofin.sync import kofindb

        db = kofindb.JellyfinDatabase(opened.cursor)
        # (id, kodi_id, fileid, pathid, jf_type, media_type, parent, checksum,
        #  media_folder, jf_parent)
        # The series being pruned: idShow 10.
        db.add_reference("bb", 10, None, 1, "Series", "tvshow", None, "c", "lib1", None)
        db.add_reference(
            "bb-s1", 99, None, None, "Season", "season", 10, "c", None, None
        )
        # An unrelated series whose season *idSeason* is also 10.
        db.add_reference("ta", 4, None, 2, "Series", "tvshow", None, "c", "lib1", None)
        db.add_reference(
            "ta-s4", 10, None, None, "Season", "season", 4, "c", None, None
        )
        db.add_reference(
            "ta-e1", 501, 601, 2, "Episode", "episode", 10, "c", None, "ta"
        )
        db.add_reference(
            "ta-e2", 502, 602, 2, "Episode", "episode", 10, "c", None, "ta"
        )

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        TVShows(api, kdb, vdb, library=TV_LIBRARY).remove("bb-s1")

    remaining = {row[0] for row in kofin_query("SELECT jellyfin_id FROM jellyfin")}

    # The season asked for is gone...
    assert "bb-s1" not in remaining
    # ...and the collision victim is untouched, episodes included.
    assert {"ta", "ta-s4", "ta-e1", "ta-e2"} <= remaining


def test_duplicate_season_ids_collapse_to_one_reference(api):
    """Two Jellyfin ids for one season must not leave two references.

    Jellyfin hands out two: the id /Shows/{id}/Seasons reports for a season can
    differ from the one the /Items listing reports for it, and season() is
    reached from both. get_season is keyed on (idShow, season), so both resolve
    to the same idSeason -- and two references to one Kodi row is what makes
    the prune destructive, since the id the listing lacks reads as stale on
    every pass and removing it deletes the row the survivor still points at.
    """
    write_series_tree(api)

    season_rows = video_query("SELECT idSeason FROM seasons WHERE season = 1")
    assert len(season_rows) == 1
    season_kodi_id = season_rows[0][0]

    # The same season arriving under the other id, as the Season pass delivers
    # it after the per-series walk has already written the first.
    alias = dict(dto(SEASON_1))
    alias["Id"] = "season1-alias"

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        TVShows(api, kdb, vdb, library=TV_LIBRARY).season(alias, show_id=1)

    # Still one Kodi row -- get_season found it rather than adding another.
    assert video_query("SELECT COUNT(*) FROM seasons WHERE season = 1") == [(1,)]
    # ...and exactly one reference to it, the id that wrote last.
    refs = kofin_query(
        "SELECT jellyfin_id FROM jellyfin WHERE media_type='season' AND kodi_id=?",
        (season_kodi_id,),
    )
    assert refs == [("season1-alias",)]


def test_episode_removal_prunes_empty_show(api):
    write_series_tree(api)

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        TVShows(api, kdb, vdb, library=TV_LIBRARY).remove("episode1")

    # Last episode gone -> season and show pruned too (fork semantics).
    assert video_query("SELECT COUNT(*) FROM episode") == [(0,)]
    assert video_query("SELECT COUNT(*) FROM seasons") == [(0,)]
    assert video_query("SELECT COUNT(*) FROM tvshow") == [(0,)]
    assert kofin_query("SELECT COUNT(*) FROM jellyfin") == [(0,)]


def test_episode_removal_drops_its_reference_when_the_season_moved(api):
    """An unresolvable season must not leave the episode's reference behind.

    The cascade looks the season up by the episode's *recorded* season
    kodi_id, so it misses whenever the season has been re-created at a fresh
    idSeason since -- a removed-then-re-added season leaves exactly that, the
    episode rows still pointing at the old id. The fork returned on the miss,
    which skipped the reference cleanup at the end of ``remove`` even though
    the Kodi row had already been deleted. What survived was a reference to a
    deleted row, which the prune reads as present because it diffs ids and
    Etags.
    """
    write_series_tree(api)

    episode_kodi_id = video_query("SELECT idEpisode FROM episode")[0][0]
    # Re-create the season under a fresh idSeason, as a remove/re-add would,
    # without rewriting the episode's parent_id.
    with sync_db.Database("kofin") as opened:
        from kofin.sync import kofindb

        db = kofindb.JellyfinDatabase(opened.cursor)
        stale_parent = kofin_query(
            "SELECT parent_id FROM jellyfin WHERE jellyfin_id='episode1'"
        )[0][0]
        db.cursor.execute(
            "UPDATE jellyfin SET kodi_id=? WHERE jellyfin_type='Season'",
            (stale_parent + 500,),
        )

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        TVShows(api, kdb, vdb, library=TV_LIBRARY).remove("episode1")

    # The Kodi row went, so the reference must go with it.
    assert video_query(
        "SELECT COUNT(*) FROM episode WHERE idEpisode=?", (episode_kodi_id,)
    ) == [(0,)]
    assert kofin_query(
        "SELECT COUNT(*) FROM jellyfin WHERE jellyfin_id='episode1'"
    ) == [(0,)]


def _root_content():
    return dict(
        (row[0], (row[1], row[2]))
        for row in video_query("SELECT strPath, strContent, strScraper FROM path")
    )


def _write_pre_migration_shape():
    """Put the tvshows content/scraper back on the addon root, as installs
    synced before the move still have it."""
    conn = sqlite3.connect(str(sync_db._path_overrides["video"]))
    try:
        conn.execute(
            "UPDATE path SET strContent=NULL, strScraper=NULL"
            " WHERE strPath='plugin://plugin.video.kofin/lib-shows/'"
        )
        conn.execute(
            "UPDATE path SET strContent='tvshows', strScraper='metadata.local'"
            " WHERE strPath='plugin://plugin.video.kofin/'"
        )
        conn.commit()
    finally:
        conn.close()


def test_root_content_migration_moves_the_pair_down(api):
    """An install synced before the move keeps a tvshows content row on the
    addon root, which is enough on its own to kill the info dialog in every
    kofin listing. The startup migration moves it onto the library path."""
    from kofin.sync.kodidb import Movies as KodiDb

    write_series_tree(api)
    _write_pre_migration_shape()

    with sync_db.Database("video") as vdb:
        assert KodiDb(vdb.cursor).root_content_migration() is True

    content = _root_content()
    assert content["plugin://plugin.video.kofin/"] == (None, None)
    assert content["plugin://plugin.video.kofin/lib-shows/"] == (
        "tvshows",
        "metadata.local",
    )


def test_root_content_migration_is_a_noop_once_migrated(api):
    from kofin.sync.kodidb import Movies as KodiDb

    write_series_tree(api)
    before = _root_content()

    with sync_db.Database("video") as vdb:
        assert KodiDb(vdb.cursor).root_content_migration() is False

    assert _root_content() == before


def test_root_content_migration_spares_other_content_rows(api):
    """The movies library's own path row is already correct; the migration must
    not touch it on its way past."""
    from kofin.sync.kodidb import Movies as KodiDb

    write_movie(api)
    write_series_tree(api)
    _write_pre_migration_shape()

    with sync_db.Database("video") as vdb:
        KodiDb(vdb.cursor).root_content_migration()

    assert _root_content()["plugin://plugin.video.kofin/lib-movies/"] == (
        "movies",
        "metadata.local",
    )


def _show_path_stamp():
    return video_query(
        "SELECT strContent, strScraper, useFolderNames FROM path"
        " WHERE strPath = 'plugin://plugin.video.kofin/lib-shows/series1/'"
    )


def _strip_show_path_stamp():
    """Bare the show's path row, the shape installs synced before the stamp
    still have."""
    conn = sqlite3.connect(str(sync_db._path_overrides["video"]))
    try:
        conn.execute(
            "UPDATE path SET strContent=NULL, strScraper=NULL, useFolderNames=NULL"
            " WHERE strPath='plugin://plugin.video.kofin/lib-shows/series1/'"
        )
        conn.commit()
    finally:
        conn.close()


def test_show_path_migration_stamps_existing_shows(api):
    """A show synced before the stamp keeps a bare path row, which leaves Kodi
    with no scraper to resolve and so an info dialog with no cast. The startup
    migration stamps it."""
    from kofin.sync.kodidb import Movies as KodiDb

    write_series_tree(api)
    _strip_show_path_stamp()
    assert _show_path_stamp() == [(None, None, None)]

    with sync_db.Database("video") as vdb:
        assert KodiDb(vdb.cursor).show_path_migration() is True

    assert _show_path_stamp() == [("tvshows", "metadata.local", 1)]


def test_show_path_migration_is_a_noop_once_stamped(api):
    """A freshly synced show already has the stamp, so a restart must not
    report work it did not do."""
    from kofin.sync.kodidb import Movies as KodiDb

    write_series_tree(api)
    before = _root_content()

    with sync_db.Database("video") as vdb:
        assert KodiDb(vdb.cursor).show_path_migration() is False

    assert _root_content() == before


def test_show_path_migration_completes_a_half_stamped_row(api):
    """useFolderNames alone missing is still broken -- the info dialog refuses
    to open at all -- so the guard cannot key on the content pair only."""
    from kofin.sync.kodidb import Movies as KodiDb

    write_series_tree(api)
    conn = sqlite3.connect(str(sync_db._path_overrides["video"]))
    try:
        conn.execute(
            "UPDATE path SET useFolderNames=NULL"
            " WHERE strPath='plugin://plugin.video.kofin/lib-shows/series1/'"
        )
        conn.commit()
    finally:
        conn.close()

    with sync_db.Database("video") as vdb:
        assert KodiDb(vdb.cursor).show_path_migration() is True

    assert _show_path_stamp() == [("tvshows", "metadata.local", 1)]


def test_show_path_migration_spares_non_show_paths(api):
    """Only rows a tvshow actually links to; the movies library row and the
    addon root sit in the same table and are none of its business."""
    from kofin.sync.kodidb import Movies as KodiDb

    write_movie(api)
    write_series_tree(api)
    _strip_show_path_stamp()

    with sync_db.Database("video") as vdb:
        KodiDb(vdb.cursor).show_path_migration()

    content = _root_content()
    assert content["plugin://plugin.video.kofin/lib-movies/"] == (
        "movies",
        "metadata.local",
    )
    assert content["plugin://plugin.video.kofin/"] == (None, None)


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


def write_music_tree(api, song=None):
    register_views({"Id": "lib-music", "Name": "Tunes", "Media": "music"})
    with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
        music = Music(api, kdb, mdb, library=MUSIC_LIBRARY)
        music.artist(dto(ARTIST))
        music.album(dto(ALBUM))
        music.song(dto(song or SONG))


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


def test_music_song_without_artist_items_credits_album_artist(api, frozen_music_clock):
    # Jellyfin serves an empty ArtistItems for a song whose own artist tag it
    # could not resolve to an artist entity; AlbumArtists still resolves when
    # only the song-level tag is damaged. Kodi reaches an album's songs
    # through song_artist under an artist path, so with no row here the album
    # opens empty there while still listing its songs on its own.
    write_music_tree(api, song=dict(SONG, ArtistItems=[]))

    artist_id = music_query("SELECT idArtist FROM artist WHERE strArtist='The Band'")[
        0
    ][0]
    song_id = music_query("SELECT idSong FROM song")[0][0]
    assert music_query("SELECT idArtist, idSong, iOrder FROM song_artist") == [
        (artist_id, song_id, 0)
    ]
    # The join Kodi runs for artist -> album -> songs.
    assert music_query(
        "SELECT s.strTitle FROM song s"
        " JOIN song_artist sa ON sa.idSong = s.idSong"
        " WHERE sa.idArtist = ?",
        (artist_id,),
    ) == [("Opening Track",)]


def test_music_song_artist_items_win_over_album_artists(api, frozen_music_clock):
    # The fallback is a last resort: a song that names its own artist is
    # credited to that artist, not to whoever released the album.
    api.items_by_id["artist2"] = dict(dto(ARTIST), Id="artist2", Name="Guest Star")
    write_music_tree(
        api, song=dict(SONG, ArtistItems=[{"Name": "Guest Star", "Id": "artist2"}])
    )

    credited = music_query(
        "SELECT a.strArtist FROM song_artist sa"
        " JOIN artist a ON a.idArtist = sa.idArtist"
    )
    assert credited == [("Guest Star",)]


def test_music_album_artist_fallback_is_idempotent(api, frozen_music_clock):
    song = dict(SONG, ArtistItems=[])
    write_music_tree(api, song=song)
    first = music_dump(str(sync_db._path_overrides["music"]))

    write_music_tree(api, song=song)
    assert music_dump(str(sync_db._path_overrides["music"])) == first


def test_music_transcode_writes_plugin_paths(api, frozen_music_clock):
    # With musicTranscode on the song row addresses the play route instead of
    # the server, so the device profile gets a say in how music is delivered.
    FakeAddon.store["musicTranscode"] = "true"
    write_music_tree(api)

    path = music_query("SELECT strPath FROM path")[0][0]
    song_id, filename = music_query("SELECT idSong, strFileName FROM song")[0]
    assert path == "plugin://plugin.video.kofin/lib-music/song1/"
    assert filename == "stream.flac?mode=play&id=song1&dbid=%d" % song_id
    # The extension is required: Kodi addresses the song as
    # musicdb://songs/<idSong><ext> and CMusicDatabaseFile::TranslateUrl
    # refuses the id when <ext> is missing.
    assert filename.split("?", 1)[0].endswith(".flac")
    # Kodi rebuilds the playable path by appending the filename to the folder's
    # own filename part (URIUtils::AddFileToFolder), which has to give a plugin
    # URL the router can parse.
    assert path + filename == (
        "plugin://plugin.video.kofin/lib-music/song1/"
        "stream.flac?mode=play&id=song1&dbid=%d" % song_id
    )


def test_music_transcode_write_is_idempotent(api, frozen_music_clock):
    FakeAddon.store["musicTranscode"] = "true"
    write_music_tree(api)
    first = music_dump(str(sync_db._path_overrides["music"]))

    write_music_tree(api)
    assert music_dump(str(sync_db._path_overrides["music"])) == first


def test_music_write_is_idempotent(api, frozen_music_clock):
    write_music_tree(api)
    first = music_dump(str(sync_db._path_overrides["music"]))
    first_map = dump(str(sync_db._path_overrides["kofin"]))

    write_music_tree(api)
    assert music_dump(str(sync_db._path_overrides["music"])) == first
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
    # The song's path row goes with it. Kodi's music schema has no cascade, so
    # leaving it behind meant every repair abandoned one row per song.
    assert music_query("SELECT COUNT(*) FROM path") == [(0,)]
