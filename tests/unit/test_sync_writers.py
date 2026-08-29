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

from kofin.core.http import HttpError
from kofin.sync import db as sync_db
from kofin.sync import schema
from kofin.sync.kodidb import Music as MusicKodiDb
from kofin.sync.kodidb.kodi import Kodi
from kofin.sync.workers import UpdateWorker
from kofin.sync.newcontent import Entry
from kofin.sync.shims import LibraryOrphanException
from kofin.sync.writers import Movies, MusicVideos, TVShows, Music
from kofin.sync.hooks import pipeline_hooks

HOOKS = pipeline_hooks()
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
        self.local_trailers_by_id = {}
        self.ancestors_by_id = {}

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
        if path.startswith("/Shows/") and path.endswith("/Episodes"):
            return {"TotalRecordCount": 0, "Items": []}
        if path.endswith("/LocalTrailers"):
            trailers = self.local_trailers_by_id.get(path.split("/")[2], [])
            if isinstance(trailers, Exception):
                raise trailers
            return trailers
        if path == "/Items":
            assert params.get("userId") == self.user_id, "item query lost its user"
            children = self.boxset_children.get(params.get("ParentId"), [])
            if params.get("Limit") == 1 and params.get("EnableTotalRecordCount"):
                return {"TotalRecordCount": len(children), "Items": []}
            start = params.get("StartIndex", 0)
            limit = params.get("Limit", 50)
            return {"Items": children[start : start + limit]}
        raise AssertionError("unexpected GET %s %s" % (path, params))

    def items(self, params):
        merged = {"userId": self.user_id}
        merged.update(params or {})
        return self.get("/Items", merged)

    def ancestors(self, item_id):
        return self.ancestors_by_id.get(item_id, [])


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
        (kodifixtures.PIERS_VIDEO_VERSION_148, kodifixtures.PIERS_MUSIC_VERSION),
    ],
    ids=["omega", "piers", "piers147", "piers148"],
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
        Movies(api, kdb, vdb, library=LIBRARY, hooks=HOOKS).movie(payload or dto(MOVIE))


def write_boxset(api, payload=None):
    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        return Movies(api, kdb, vdb, library=LIBRARY, hooks=HOOKS).boxset(
            payload or dto(BOXSET)
        )


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

    # A row per provider the server actually sent, never one per hardcoded
    # provider name — and no empty-valued row at all (issue #146).
    unique = video_query(
        "SELECT value, type FROM uniqueid WHERE media_type='movie' ORDER BY uniqueid_id"
    )
    assert unique == [("tt0000001", "imdb"), ("42", "tmdb")]
    # c09 is the default-uniqueid pointer movie_view joins on, the same shape
    # c05 has for ratings.
    assert row["c09"] == str(
        video_query("SELECT uniqueid_id FROM uniqueid WHERE type='imdb'")[0][0]
    )

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


# --- uniqueid: a row per provider the item has (issue #146) -------------------


def _uniqueids(media_type="movie"):
    return video_query(
        "SELECT type, value FROM uniqueid WHERE media_type=? ORDER BY uniqueid_id",
        (media_type,),
    )


def _uniqueid_pointer(column="c09", table="movie"):
    """(the media row's pointer, the uniqueid_id it names or None)."""
    pointer = video_query("SELECT %s FROM %s" % (column, table))[0][0]
    named = video_query("SELECT type FROM uniqueid WHERE uniqueid_id=?", (pointer,))
    return pointer, (named[0][0] if named else None)


def test_movie_keeps_the_id_it_has_when_the_preferred_provider_is_absent(api):
    """The defect in one test: the fork wrote an empty row typed 'imdb' for a
    film carrying only a TMDB id, and discarded the TMDB id. 15,796 such rows
    on the bench corpus (issue #146, upstream jellyfin-kodi#920)."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})

    payload = dto(MOVIE)
    payload["ProviderIds"] = {"Tmdb": "42"}
    write_movie(api, payload)

    assert _uniqueids() == [("tmdb", "42")]
    # No empty-valued row, and the pointer names the id the film does have
    # rather than dangling at a row holding nothing.
    assert video_query(
        "SELECT COUNT(*) FROM uniqueid WHERE value IS NULL OR value=''"
    ) == [(0,)]
    assert _uniqueid_pointer()[1] == "tmdb"


def test_movie_with_no_provider_ids_writes_no_uniqueid_row(api):
    """The honest answer for an item the server has no external id for: no
    row, and a NULL pointer. The fork wrote a row holding nothing, which is
    what made every such item look identifiable and resolve to nothing."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})

    payload = dto(MOVIE)
    payload["ProviderIds"] = {}
    write_movie(api, payload)

    assert _uniqueids() == []
    assert video_query("SELECT c09 FROM movie") == [(None,)]


def test_movie_uniqueid_empty_values_are_skipped_not_stored(api):
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})

    payload = dto(MOVIE)
    payload["ProviderIds"] = {"Imdb": "", "Tmdb": "42"}
    write_movie(api, payload)

    assert _uniqueids() == [("tmdb", "42")]


def test_movie_uniqueid_dropped_provider_is_deleted_and_the_pointer_moves(api):
    """The ratings bug in the neighbouring table: a provider the server stops
    sending must not leave the media row pointing at a deleted uniqueid_id, or
    the view's LEFT JOIN renders the film with no external id at all."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)
    assert [t for t, _ in _uniqueids()] == ["imdb", "tmdb"]

    changed = dto(MOVIE)
    changed["Etag"] = "etag-movie1-v2"
    changed["ProviderIds"] = {"Tmdb": "42"}
    write_movie(api, changed)

    assert _uniqueids() == [("tmdb", "42")]
    pointer, named = _uniqueid_pointer()
    assert named == "tmdb"
    assert video_query("SELECT COUNT(*) FROM uniqueid") == [(1,)]


def test_movie_uniqueid_rows_are_updated_not_multiplied(api):
    """The update path allocated nothing new before and must not now: two
    passes, two rows."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)

    changed = dto(MOVIE)
    changed["Etag"] = "etag-movie1-v2"
    changed["ProviderIds"] = {"Imdb": "tt0000001", "Tmdb": "99"}
    write_movie(api, changed)

    assert _uniqueids() == [("imdb", "tt0000001"), ("tmdb", "99")]


def test_movie_uniqueid_provider_order_is_priority_then_server_order(api):
    """Deterministic uniqueid_id allocation is what keeps the idempotency
    dumps byte-identical, so the order is pinned rather than incidental."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})

    payload = dto(MOVIE)
    payload["ProviderIds"] = {"Zap2It": "z1", "Tmdb": "42", "Imdb": "tt1"}
    write_movie(api, payload)

    assert [t for t, _ in _uniqueids()] == ["imdb", "tmdb", "zap2it"]


def test_unknown_providers_are_written_rather_than_filtered(api):
    """An allowlist here is the defect this fixes, so a provider kofin has
    never heard of still gets its row."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})

    payload = dto(MOVIE)
    payload["ProviderIds"] = {"SomeFutureDb": "abc"}
    write_movie(api, payload)

    assert _uniqueids() == [("somefuturedb", "abc")]


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


def test_movie_survives_the_addon_unregister_window(api, monkeypatch):
    """A writer's settings read must not take the library pass down with it
    (issue #143).

    ``Movies.__init__`` reads preferCriticRating once per instance, and Kodi
    unloads the add-on before replacing its files on any update — so
    ``xbmcaddon.Addon()`` raised RuntimeError straight through the writer,
    ``full_sync.movies`` and ``process_library``, and only ``add_library``
    caught it. The library stayed queued and the resume backoff walked it
    again, so nothing was lost except the pass; the walk simply must not
    abort. Degrading the read means the film is written with the community
    rating as its pointer, which the next writer or a settings flip repoints.
    """

    class UnregisteredAddon:
        def __init__(self, addon_id: str = ""):
            raise RuntimeError("Unknown addon id 'plugin.video.kofin'.")

    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    monkeypatch.setattr("xbmcaddon.Addon", UnregisteredAddon)

    write_movie(api)

    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    assert video_query("SELECT c00 FROM movie") == [("The Example",)]
    pointer, community_id = _pointer("default")
    assert pointer == community_id


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
        Movies(api, kdb, vdb, library=LIBRARY, hooks=HOOKS).remove("movie1")

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


def test_movie_extras_fetch_failure_leaves_the_stored_set_alone(api):
    """A failed listing is not an empty listing: the extras already in the
    database stay, exactly as when the fetch raised inside extras()."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    api.special_features_by_id = {"movie1": FEATURES}
    write_movie(api, movie_with_extras())
    assert len(extras_rows()) == 2

    api.special_features_by_id = {"movie1": RuntimeError("special features down")}
    write_movie(api, movie_with_extras(etag="etag-movie1-v2"))

    assert len(extras_rows()) == 2


def test_movie_gone_at_its_extras_fetch_is_not_written(api):
    """P2.5c. A movie deleted between the page and the write 404s on the
    one child fetch a movie makes, its special features -- and the fork's
    extras() swallowed that with every other fetch error, after the row was
    in. The listing is fetched first now and the 404 propagates, so the
    walk skips the movie (test_sync_walk) and nothing at all is written."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    api.special_features_by_id = {
        "movie1": HttpError(404, "GET /Items/movie1/SpecialFeatures -> 404")
    }

    with pytest.raises(HttpError):
        write_movie(api, movie_with_extras())

    assert video_query("SELECT COUNT(*) FROM movie") == [(0,)]
    assert video_query("SELECT COUNT(*) FROM files") == [(0,)]
    assert video_query("SELECT COUNT(*) FROM path") == [(0,)]
    assert kofin_query("SELECT COUNT(*) FROM jellyfin") == [(0,)]


def test_movie_gone_at_its_trailer_fetch_is_not_written(api):
    """The same 404 on the local-trailers fetch, the other child request a
    movie can make."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    api.local_trailers_by_id = {
        "movie1": HttpError(404, "GET /Items/movie1/LocalTrailers -> 404")
    }
    payload = dto(MOVIE)
    payload["LocalTrailerCount"] = 1

    with pytest.raises(HttpError):
        write_movie(api, payload)

    assert video_query("SELECT COUNT(*) FROM movie") == [(0,)]
    assert kofin_query("SELECT COUNT(*) FROM jellyfin") == [(0,)]


def test_movie_trailer_fetch_failure_never_gates_sync(api):
    """Anything but a 404 is still a trailer-less movie, as in the fork."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    api.local_trailers_by_id = {"movie1": RuntimeError("trailers down")}
    payload = dto(MOVIE)
    payload["LocalTrailerCount"] = 1

    write_movie(api, payload)

    assert video_query("SELECT c19 FROM movie") == [(None,)]
    assert kofin_query("SELECT COUNT(*) FROM jellyfin") == [(1,)]


def test_movie_trailer_status_other_than_404_never_gates_sync(api):
    """The status a child fetch answers with is the endpoint's: only a 404
    says gone. A 500 on the trailers is absorbed like any other failure."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    api.local_trailers_by_id = {
        "movie1": HttpError(500, "GET /Items/movie1/LocalTrailers -> 500")
    }
    payload = dto(MOVIE)
    payload["LocalTrailerCount"] = 1

    write_movie(api, payload)

    assert video_query("SELECT COUNT(*) FROM movie") == [(1,)]


def test_a_short_youtube_trailer_link_is_written_without_a_traceback(api):
    """Jellyfin stores RemoteTrailers verbatim; a youtu.be or /shorts/ link
    has no '=' and the old rsplit raised IndexError — caught as a
    trailer-less film, with a full LOG.exception per movie (audit R3)."""
    from kofin.sync.writers import movies as movies_module

    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    tracebacks = []
    api.local_trailers_by_id = {}
    payload = dto(MOVIE)
    payload["LocalTrailerCount"] = 0
    payload["RemoteTrailers"] = [{"Url": "https://youtu.be/dQw4w9WgXcQ"}]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            movies_module.LOG, "exception", lambda *a, **k: tracebacks.append(a)
        )
        write_movie(api, payload)

    assert video_query("SELECT c19 FROM movie") == [
        ("plugin://plugin.video.youtube/play/?video_id=dQw4w9WgXcQ",)
    ]
    assert tracebacks == []


def test_a_trailer_that_is_not_youtube_is_no_trailer(api):
    from kofin.sync.writers import movies as movies_module

    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    tracebacks = []
    api.local_trailers_by_id = {}
    payload = dto(MOVIE)
    payload["LocalTrailerCount"] = 0
    payload["RemoteTrailers"] = [{"Url": "https://example.com/trailer.mp4"}]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            movies_module.LOG, "exception", lambda *a, **k: tracebacks.append(a)
        )
        write_movie(api, payload)

    assert video_query("SELECT c19 FROM movie") == [(None,)]
    assert tracebacks == []


def test_series_gone_at_its_trailer_fetch_is_not_written(api):
    """TVShows.trailer() mirrors Movies.trailer(), the 404 included."""
    register_views({"Id": "lib-shows", "Name": "Shows", "Media": "tvshows"})
    api.local_trailers_by_id = {
        "series1": HttpError(404, "GET /Items/series1/LocalTrailers -> 404")
    }
    payload = dto(SERIES)
    payload["LocalTrailerCount"] = 1

    with pytest.raises(HttpError):
        with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
            TVShows(api, kdb, vdb, library=TV_LIBRARY, hooks=HOOKS).tvshow(payload)

    assert video_query("SELECT COUNT(*) FROM tvshow") == [(0,)]
    assert kofin_query("SELECT COUNT(*) FROM jellyfin") == [(0,)]


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
        Movies(api, kdb, vdb, library=LIBRARY, hooks=HOOKS).remove("movie1")

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
        Movies(api, kdb, vdb, library=LIBRARY, hooks=HOOKS).boxset(dto(BOXSET))

    sets = video_query("SELECT idSet, strSet FROM sets")
    assert sets == [(1, "Example Collection")]
    linked = video_query("SELECT idMovie FROM movie WHERE idSet = 1")
    assert linked == [(1,)]  # movie1 only
    assert kofin_query("SELECT parent_id FROM jellyfin WHERE jellyfin_id='movie1'") == [
        (1,)
    ]

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        Movies(api, kdb, vdb, library=LIBRARY, hooks=HOOKS).remove("set1")

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
        Movies(api, kdb, vdb, library=LIBRARY, hooks=HOOKS).remove("movie1")
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
        Movies(api, kdb, vdb, library=LIBRARY, hooks=HOOKS).remove("set1")
    assert kofin_query("SELECT COUNT(*) FROM boxset_state") == [(0,)]

    api.boxset_children = {"set1": [dto(MOVIE)]}
    assert write_boxset(api) == BOXSET_WRITTEN
    assert kofin_query("SELECT COUNT(*) FROM boxset_state") == [(1,)]

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        Movies(api, kdb, vdb, library=LIBRARY, hooks=HOOKS).boxsets_reset()
    assert kofin_query("SELECT COUNT(*) FROM boxset_state") == [(0,)]
    assert video_query("SELECT COUNT(*) FROM sets") == [(0,)]


def make_fullsync(api):
    """A FullSync wired for direct method calls (no context manager, no
    Kodi): only the database lock is real."""
    from kofin.sync.full_sync import FullSync
    from tests.unit.synchost import FakeHost

    sync = FullSync(FakeHost(), server=api)
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
        fullsync.release()

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
        fullsync.release()

    assert kofin_query("SELECT COUNT(*) FROM jellyfin WHERE media_type = 'set'") == [
        (1,)
    ]
    assert video_query("SELECT COUNT(*) FROM sets") == [(1,)]


def boxset2(**overrides):
    """A second collection payload beside sync_dtos.BOXSET."""
    payload = dict(BOXSET)
    payload.update({"Id": "set2", "Name": "Second Collection", "Etag": "etag-set2-v1"})
    payload.update(overrides)
    return payload


def walk_boxsets(api, sets):
    """The real boxsets walk: paging, outcome tally, sweep, restamp."""
    api.boxset_children[LIBRARY["Id"]] = [dict(payload) for payload in sets]
    fullsync = make_fullsync(api)
    try:
        fullsync.boxsets(dict(LIBRARY))
    finally:
        fullsync.release()


def drifted_boxsets():
    """The startup drift probe's predicate (library.probe_boxset_drift),
    computed straight from the databases."""
    states = dict(kofin_query("SELECT jellyfin_id, linked_count FROM boxset_state"))
    counts = dict(
        video_query(
            "SELECT idSet, COUNT(*) FROM movie WHERE idSet IS NOT NULL GROUP BY idSet"
        )
    )
    set_rows = {row[0] for row in video_query("SELECT idSet FROM sets")}

    return [
        jellyfin_id
        for jellyfin_id, kodi_id in kofin_query(
            "SELECT jellyfin_id, kodi_id FROM jellyfin WHERE media_type = 'set'"
        )
        if kodi_id not in set_rows
        or states.get(jellyfin_id) is None
        or states[jellyfin_id] != counts.get(kodi_id, 0)
    ]


def test_boxset_shared_member_walk_converges(api):
    """V7 (docs/healing-loops-plan.md): movie1 belongs to both sets, and
    movie.idSet is single-valued, so the walk's last set owns it. The
    walk-end restamp measures after the stealing stops: stored equals
    reality for both sets, the drift probe stays silent, and a second walk
    is a byte-identical no-op instead of a heal-every-boot loop."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)
    write_movie(api, dto(MOVIE_2))
    api.boxset_children = {
        "set1": [dto(MOVIE), dto(MOVIE_2)],
        "set2": [dto(MOVIE)],
    }

    walk_boxsets(api, [dto(BOXSET), boxset2()])

    set2_id = kofin_query("SELECT kodi_id FROM jellyfin WHERE jellyfin_id = 'set2'")[0][
        0
    ]
    assert video_query("SELECT idSet FROM movie WHERE idMovie = 1") == [(set2_id,)]
    assert drifted_boxsets() == []

    first = dump(str(sync_db._path_overrides["video"]))
    first_map = dump(str(sync_db._path_overrides["kofin"]))
    walk_boxsets(api, [dto(BOXSET), boxset2()])
    assert dump(str(sync_db._path_overrides["video"])) == first
    assert dump(str(sync_db._path_overrides["kofin"])) == first_map


def test_boxset_incremental_steal_walk_reconverges(api):
    """An Etag-driven pass on one overlapped set steals the shared member
    mid-cycle and drifts the other set's stamp. The next walk heals the
    victim and the restamp closes both stamps: one walk per disturbance,
    then quiet, instead of a permanent probe->walk cycle."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)
    write_movie(api, dto(MOVIE_2))
    api.boxset_children = {
        "set1": [dto(MOVIE), dto(MOVIE_2)],
        "set2": [dto(MOVIE)],
    }
    walk_boxsets(api, [dto(BOXSET), boxset2()])
    assert drifted_boxsets() == []

    touched = dto(BOXSET)
    touched["Etag"] = "etag-set1-v2"
    assert write_boxset(api, dict(touched)) == BOXSET_WRITTEN
    assert drifted_boxsets() == ["set2"]

    walk_boxsets(api, [dict(touched), boxset2()])
    assert drifted_boxsets() == []

    first = dump(str(sync_db._path_overrides["video"]))
    first_map = dump(str(sync_db._path_overrides["kofin"]))
    walk_boxsets(api, [dict(touched), boxset2()])
    assert dump(str(sync_db._path_overrides["video"])) == first
    assert dump(str(sync_db._path_overrides["kofin"])) == first_map


def test_boxset_guarded_set_keeps_missing_state(api, boxset_log):
    """A set guarded on its first post-upgrade pass keeps stored=None: the
    probe keeps flagging it, which is the designed retry-every-start for a
    server answering suspiciously. The walk-end restamp must not grade that
    answer healthy."""
    linked_boxset(api)
    kofin_exec("DELETE FROM boxset_state")

    guarded = dto(BOXSET)
    guarded["ChildCount"] = 2
    api.boxset_children["set1"] = []
    walk_boxsets(api, [guarded])

    assert kofin_query("SELECT COUNT(*) FROM boxset_state") == [(0,)]
    assert video_query("SELECT COUNT(*) FROM movie WHERE idSet = 1") == [(2,)]
    assert drifted_boxsets() == ["set1"]


# --- the version-type row per film (audit A4-1, fixes plan H2) -----------------


def _user_version_types():
    """USER-owned VERSION type rows — what Kodi's "Manage versions" lists."""
    return video_query(
        "SELECT name FROM videoversiontype WHERE owner = ? AND itemType = ?"
        " ORDER BY name",
        (schema.VIDEO_ASSET_OWNER_USER, version_item_type()),
    )


def test_movie_named_as_its_file_is_the_standard_edition(api):
    """Jellyfin names a single-file movie's MediaSource after the file
    (``Video.GetMediaSourceName``: the stem, unless local alternate versions
    exist). That is no label, and it must not mint a type row per film —
    1,799 of them for 1,784 movies on one profile before this."""
    payload = dto(MOVIE)
    payload["MediaSources"] = [
        _version_source(
            "movie1",
            "The Example",
            "/media/movies/The Example (2020)/The Example.mkv",
            72000000000,
        )
    ]
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api, payload)

    rows = version_rows()
    assert len(rows) == 1
    assert rows[0][2] == 40400
    assert _user_version_types() == []


def test_the_stem_rule_ignores_case_and_windows_separators(api):
    """Jellyfin reports the server's own path; a Windows server hands back
    backslashes, and the name comparison is Kodi's COLLATE NOCASE anyway."""
    payload = dto(MOVIE)
    payload["MediaSources"] = [
        _version_source(
            "movie1",
            "the example",
            "D:\\Movies\\The Example (2020)\\The Example.MKV",
            72000000000,
        )
    ]
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api, payload)

    assert version_rows()[0][2] == 40400
    assert _user_version_types() == []


def test_a_rewrite_to_the_standard_edition_sweeps_the_stale_type(api):
    """The Repair path for every profile written before H2: the film's
    primary moves from its file-named type to 40400, and the row it leaves
    behind must go with it — nothing else ever removes one."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    labelled = dto(MOVIE)
    labelled["MediaSources"] = [
        _version_source(
            "movie1",
            "IMAX Exclusive",
            "/media/movies/The Example (2020)/The Example.mkv",
            72000000000,
        )
    ]
    write_movie(api, labelled)
    assert version_rows()[0][2] != 40400
    assert _user_version_types() == [("IMAX Exclusive",)]

    renamed = dto(MOVIE)
    renamed["Etag"] = "etag-movie1-v2"
    renamed["MediaSources"] = [
        _version_source(
            "movie1",
            "The Example",
            "/media/movies/The Example (2020)/The Example.mkv",
            72000000000,
        )
    ]
    write_movie(api, renamed)

    assert version_rows()[0][2] == 40400
    assert _user_version_types() == []


def test_movie_removal_leaves_no_orphan_version_type(api):
    """A film removed with a custom version label takes its type row with
    it; the seeded builtins (owner 0) are never touched."""
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    payload = dto(MOVIE)
    payload["MediaSources"] = [
        _version_source(
            "movie1",
            "IMAX Exclusive",
            "/media/movies/The Example (2020)/The Example.mkv",
            72000000000,
        )
    ]
    write_movie(api, payload)
    seeded = video_query("SELECT COUNT(*) FROM videoversiontype WHERE owner = 0")
    assert _user_version_types() == [("IMAX Exclusive",)]

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        Movies(api, kdb, vdb, library=LIBRARY, hooks=HOOKS).remove("movie1")

    assert video_query("SELECT COUNT(*) FROM videoversion") == [(0,)]
    assert _user_version_types() == []
    assert (
        video_query("SELECT COUNT(*) FROM videoversiontype WHERE owner = 0") == seeded
    )


# --- tv shows ------------------------------------------------------------------


def write_series_tree(api):
    register_views({"Id": "lib-shows", "Name": "Shows", "Media": "tvshows"})
    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        shows = TVShows(api, kdb, vdb, library=TV_LIBRARY, hooks=HOOKS)
        shows.tvshow(dto(SERIES))
        shows.episode(dto(EPISODE))


def test_series_and_episode_uniqueids_come_from_the_item(api):
    """The TV leg of issue #146: both hardcoded 'tvdb', both unconditional.
    tvshow.c12 and episode.c20 are the pointers their views join on."""
    write_series_tree(api)

    assert _uniqueids("tvshow") == [("tvdb", "5555")]
    assert _uniqueids("episode") == [("tvdb", "9999")]
    assert _uniqueid_pointer("c12", "tvshow")[1] == "tvdb"
    assert _uniqueid_pointer("c20", "episode")[1] == "tvdb"


def test_episode_with_only_a_tmdb_id_keeps_it(api):
    register_views({"Id": "lib-shows", "Name": "Shows", "Media": "tvshows"})
    episode = dto(EPISODE)
    episode["ProviderIds"] = {"Tmdb": "777"}

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        shows = TVShows(api, kdb, vdb, library=TV_LIBRARY, hooks=HOOKS)
        shows.tvshow(dto(SERIES))
        shows.episode(episode)

    assert _uniqueids("episode") == [("tmdb", "777")]
    assert video_query(
        "SELECT COUNT(*) FROM uniqueid WHERE value IS NULL OR value=''"
    ) == [(0,)]


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
        TVShows(api, kdb, vdb, library=TV_LIBRARY, hooks=HOOKS).tvshow(dto(SERIES))

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
        TVShows(api, kdb, vdb, library=TV_LIBRARY, hooks=HOOKS).season(dto(SEASON_1))

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
            TVShows(api, kdb, vdb, library=TV_LIBRARY, hooks=HOOKS).season(orphan)

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


def test_an_item_the_writer_refused_is_not_announced(api):
    """The writers refuse by returning early, and the notify call sat after
    them unconditionally -- so an item that produced no Kodi row and no
    kofin.db reference was still announced as new content, and still handed to
    downloads_auto.queue_new_content. The library filter hides the commonest
    cause; it does not fix this."""
    seed_movie_library(api)
    # Visible to the user, in a library this box does not sync.
    api.ancestors = lambda item_id: [{"Id": "lib-other", "Name": "Bench-Movies"}]

    work = queue.Queue()
    work.put(dto(MOVIE_2))
    notify = queue.Queue()

    UpdateWorker(
        work, notify, threading.Lock(), "video", api, notify_enabled=True
    ).run()

    # Refused: nothing written, and therefore nothing to announce.
    assert video_query("SELECT COUNT(*) FROM movie") == [(0,)]
    assert kofin_query("SELECT COUNT(*) FROM jellyfin") == [(0,)]
    assert notify.qsize() == 0


def test_a_refusal_does_not_silence_the_items_around_it(api):
    """Per item, not per drain: the refusal must not take its neighbours'
    announcements down with it.

    The two need distinct parents to be in distinct libraries at all --
    find_library keys its memo on the parent precisely because siblings share
    a library, so a shared folder would (correctly) refuse both."""
    seed_movie_library(api)
    whitelisted = {"Id": "lib-movies", "Name": "Movies"}
    elsewhere = {"Id": "lib-other", "Name": "Bench-Movies"}
    api.ancestors = lambda item_id: [elsewhere if item_id == "movie1" else whitelisted]

    work = queue.Queue()
    # Unwatched, so the only reason it could go unannounced is the refusal.
    work.put(
        dto(dict(MOVIE, ParentId="folder-bench", UserData={"Played": False}))
    )  # refused
    work.put(dto(MOVIE_2))  # written
    notify = queue.Queue()

    UpdateWorker(
        work, notify, threading.Lock(), "video", api, notify_enabled=True
    ).run()

    assert video_query("SELECT c00 FROM movie") == [("Second Feature",)]
    reported = []
    while not notify.empty():
        reported.append(notify.get())

    assert reported == [Entry("Movie", "movie2", "Second Feature")]


def test_a_virtual_episode_is_not_announced(api):
    """A refusal that has nothing to do with library scope, and so survives
    the change feed's filter: Jellyfin's placeholder for an episode that has
    no file. It is skipped by the writer and must not be announced."""
    register_views({"Id": "lib-shows", "Name": "Shows", "Media": "tvshows"})
    sync = sync_db.get_sync()
    sync["Whitelist"] = ["lib-shows"]
    sync_db.save_sync(sync)
    api.ancestors = lambda item_id: [{"Id": "lib-shows", "Name": "Shows"}]

    work = queue.Queue()
    work.put(dto(dict(EPISODE, LocationType="Virtual")))
    notify = queue.Queue()

    UpdateWorker(
        work, notify, threading.Lock(), "video", api, notify_enabled=True
    ).run()

    assert video_query("SELECT COUNT(*) FROM episode") == [(0,)]
    assert notify.qsize() == 0


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


# --- the other three workers (P2.0b) -------------------------------------------
#
# Only UpdateWorker was ever constructed by a test before phase 2; the other
# three drain loops ran only inside Kodi. Each is driven here exactly as
# Library.start_writers / worker_sort drives it, against the L2 fixtures.


def _writer_queues():
    from kofin.sync.library import MUSIC_QUEUES

    return {
        item_type: queue.Queue()
        for item_type in (
            "Movie",
            "BoxSet",
            "MusicVideo",
            "Series",
            "Season",
            "Episode",
        )
        + tuple(MUSIC_QUEUES)
    }


def test_userdata_worker_applies_video_userdata(api):
    """A favourite flip and a resume point, through the drain loop."""
    from kofin.sync.workers import UserDataWorker

    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)
    write_series_tree(api)

    movie = dto(MOVIE)
    movie["UserData"] = {
        "Played": False,
        "PlayCount": 0,
        "IsFavorite": True,
        "PlaybackPositionTicks": 12000000000,
    }
    episode = dto(EPISODE)
    episode["UserData"]["PlaybackPositionTicks"] = 0  # resume cleared
    work = queue.Queue()
    work.put(movie)
    work.put(episode)

    worker = UserDataWorker(work, threading.Lock(), "video", api)
    worker.run()

    assert worker.is_done is True
    assert work.qsize() == 0
    favourites = video_query(
        "SELECT COUNT(*) FROM tag_link JOIN tag ON tag.tag_id = tag_link.tag_id"
        " WHERE tag.name = 'Favorite movies' AND tag_link.media_type = 'movie'"
    )
    assert favourites == [(1,)]
    movie_file = video_query("SELECT idFile FROM movie")[0][0]
    # No jumpback configured in the fake store, so the point lands as sent.
    assert video_query(
        "SELECT timeInSeconds FROM bookmark WHERE idFile = ?", (movie_file,)
    ) == [(1200.0,)]
    # The cleared resume took the episode's shadow row with it.
    assert resume_shadow_rows() == []


def test_userdata_worker_flags_what_it_cannot_apply(api):
    from kofin.sync.workers import UserDataWorker

    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)
    flagged = []
    broken = dto(MOVIE)
    broken["UserData"]["PlaybackPositionTicks"] = "not a number"  # the writer raises
    work = queue.Queue()
    work.put(broken)
    work.put(dto(MOVIE))  # and the drain lives on

    worker = UserDataWorker(
        work,
        threading.Lock(),
        "video",
        api,
        unapplied=lambda item_id, reason: flagged.append((item_id, reason)),
    )
    worker.run()

    assert [item_id for item_id, _ in flagged] == ["movie1"]
    assert flagged[0][1].startswith("userdata Movie:")
    assert worker.is_done is True


def test_userdata_worker_applies_music_userdata(api, frozen_music_clock):
    from kofin.sync.workers import UserDataWorker

    write_music_tree(api)
    song = dto(SONG)
    song["UserData"] = dict(song["UserData"], PlayCount=3, Played=True)
    work = queue.Queue()
    work.put(song)

    worker = UserDataWorker(work, threading.Lock(), "music", api)
    worker.run()

    assert worker.is_done is True
    assert music_query("SELECT iTimesPlayed FROM song") == [(3,)]


def test_sort_worker_routes_ids_to_their_media_queue(api):
    """The removed feed carries bare ids; the sorter resolves each to the
    media type its writer queue is keyed on, children via the parent."""
    from kofin.sync.workers import SortWorker

    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)
    write_series_tree(api)
    output = _writer_queues()
    work = queue.Queue()
    for item_id in ("movie1", "episode1", "series1", "never-synced"):
        work.put(item_id)

    worker = SortWorker(work, output)
    worker.run()

    assert worker.is_done is True
    routed = {
        item_type: [entry["Id"] for entry in drain(output[item_type])]
        for item_type in output
    }
    assert routed["Movie"] == ["movie1"]
    assert routed["Episode"] == ["episode1"]
    assert routed["Series"] == ["series1"]
    assert not any(routed[t] for t in routed if t not in ("Movie", "Episode", "Series"))


def test_sort_worker_expands_a_parent_it_never_synced(api):
    """A series removed server-side arrives as its own id; with its own row
    already gone the sorter still finds the children referencing it
    (jellyfin_parent_id, the episode's SeriesId) and routes those."""
    from kofin.sync.workers import SortWorker

    write_series_tree(api)
    kofin_exec("DELETE FROM jellyfin WHERE jellyfin_id = 'series1'")
    output = _writer_queues()
    work = queue.Queue()
    work.put("series1")

    SortWorker(work, output).run()

    routed = [entry["Id"] for entry in drain(output["Episode"])]
    assert routed == ["episode1"]
    assert drain(output["Series"]) == []


def test_removed_worker_removes_every_kind_and_leaves_no_orphans(api):
    from kofin.sync.workers import RemovedWorker

    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)
    write_series_tree(api)  # EPISODE resumes: the shadow row is in play
    work = queue.Queue()
    work.put({"Id": "movie1", "Type": "Movie"})
    work.put({"Id": "episode1", "Type": "Episode"})
    flagged = []

    worker = RemovedWorker(
        work,
        threading.Lock(),
        "video",
        api,
        unapplied=lambda item_id, reason: flagged.append((item_id, reason)),
    )
    worker.run()

    assert worker.is_done is True and flagged == []
    assert video_query("SELECT COUNT(*) FROM movie") == [(0,)]
    assert video_query("SELECT COUNT(*) FROM episode") == [(0,)]
    assert video_query("SELECT COUNT(*) FROM tvshow") == [(0,)]  # the cascade
    assert kofin_query("SELECT COUNT(*) FROM jellyfin") == [(0,)]
    for label, sql in ORPHAN_RULES:
        assert video_query(sql) == [(0,)], "orphans in %s" % label


def test_removed_worker_leaves_an_unknown_kind_in_place(api):
    from kofin.sync.workers import RemovedWorker

    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)
    work = queue.Queue()
    work.put({"Id": "movie1", "Type": "Photo"})

    RemovedWorker(work, threading.Lock(), "video", api).run()

    assert video_query("SELECT COUNT(*) FROM movie") == [(1,)]
    assert work.qsize() == 0


def drain(q):
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            return items


# --- library removal through the moved module (P2.2, sync/removal.py) ----------


class _Dialog:
    def update(self, *args, **kwargs):
        pass


def test_removal_takes_every_row_of_the_library_and_drops_it_from_the_whitelist(api):
    from kofin.sync import removal
    from tests.unit.synchost import FakeHost

    register_views(
        {"Id": LIBRARY["Id"], "Name": "Movies", "Media": "movies"},
        {"Id": TV_LIBRARY["Id"], "Name": "Shows", "Media": "tvshows"},
    )
    write_movie(api)
    write_series_tree(api)  # a second library: must survive
    sync = {"Whitelist": [LIBRARY["Id"], TV_LIBRARY["Id"]], "Libraries": []}

    removal.remove_library(FakeHost(), api, sync, LIBRARY["Id"], _Dialog())

    assert video_query("SELECT COUNT(*) FROM movie") == [(0,)]
    assert video_query("SELECT COUNT(*) FROM episode") == [(1,)]
    assert kofin_query(
        "SELECT COUNT(*) FROM jellyfin WHERE media_folder = ?", (LIBRARY["Id"],)
    ) == [(0,)]
    assert sync["Whitelist"] == [TV_LIBRARY["Id"]]
    # The surviving episode keeps its resume shadow (an unlinked files row
    # by design while the episode exists), so the two unlinked rules are
    # the removed library's to satisfy, not this test's.
    for label, sql in ORPHAN_RULES:
        if "unlinked" in label:
            continue
        assert video_query(sql) == [(0,)], "orphans in %s" % label
    assert video_query("SELECT COUNT(*) FROM files") == [(2,)]  # episode + shadow


def test_removal_of_an_unknown_library_is_a_no_op(api):
    from kofin.sync import removal
    from tests.unit.synchost import FakeHost

    write_movie(api)
    sync = {"Whitelist": ["lib-movies"], "Libraries": []}

    removal.remove_library(FakeHost(), api, sync, "never-synced", _Dialog())

    assert video_query("SELECT COUNT(*) FROM movie") == [(1,)]
    assert sync["Whitelist"] == ["lib-movies"]


ORPHAN_RULES = [
    (
        "videoversiontype USER VERSION rows nothing references (A4-1)",
        "SELECT COUNT(*) FROM videoversiontype WHERE owner = 2"
        " AND itemType = (SELECT itemType FROM videoversiontype WHERE id = 40400)"
        " AND id NOT IN (SELECT idType FROM videoversion)",
    ),
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
    # P2.5b: a files row belongs to a movie, an episode, a music video or a
    # version/extra asset; a bookmark belongs to one of the first three. The
    # resume shadow (writers/tvshows.py remove_episode) is neither once its
    # episode is gone.
    (
        "files unlinked",
        "SELECT COUNT(*) FROM files WHERE idFile NOT IN (SELECT idFile FROM movie)"
        " AND idFile NOT IN (SELECT idFile FROM episode)"
        " AND idFile NOT IN (SELECT idFile FROM musicvideo)"
        " AND idFile NOT IN (SELECT idFile FROM videoversion)",
    ),
    (
        "bookmark unlinked",
        "SELECT COUNT(*) FROM bookmark WHERE idFile NOT IN (SELECT idFile FROM movie)"
        " AND idFile NOT IN (SELECT idFile FROM episode)"
        " AND idFile NOT IN (SELECT idFile FROM musicvideo)",
    ),
]


ROOT_PATH = "plugin://plugin.video.kofin/"


def resume_shadow_rows():
    """The files rows under the add-on's root path: the second row an
    episode with a resume point gets, its bookmark repeated on it."""
    return video_query(
        "SELECT files.idFile, (SELECT COUNT(*) FROM bookmark WHERE bookmark.idFile = files.idFile)"
        " FROM files JOIN path ON path.idPath = files.idPath WHERE path.strPath = ?",
        (ROOT_PATH,),
    )


def test_episode_removal_takes_its_resume_shadow(api):
    """P2.5b. An episode with a resume point carries a second files row under
    the add-on root path with its own bookmark, and delete_episode drops the
    episode's own file only: after one Shows round trip the Omega rig held
    the shadow and its bookmark for every removed episode (S-P1.3b). The
    removal takes both now, and the zero-orphan rules cover files and
    bookmark from here on."""
    write_series_tree(api)  # EPISODE resumes at 300 s

    assert resume_shadow_rows() == [
        (video_query("SELECT MAX(idFile) FROM files")[0][0], 1)
    ]
    assert video_query("SELECT COUNT(*) FROM bookmark") == [(2,)]

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        TVShows(api, kdb, vdb, library=TV_LIBRARY, hooks=HOOKS).remove("episode1")

    assert video_query("SELECT COUNT(*) FROM episode") == [(0,)]
    assert resume_shadow_rows() == []
    assert video_query("SELECT COUNT(*) FROM bookmark") == [(0,)]
    for label, sql in ORPHAN_RULES:
        assert video_query(sql) == [(0,)], "orphans in %s" % label


def test_episode_without_a_resume_point_removes_cleanly_too(api):
    """The shadow is only there when there was a resume point; the delete
    must not depend on it."""
    register_views({"Id": "lib-shows", "Name": "Shows", "Media": "tvshows"})
    payload = dto(EPISODE)
    payload["UserData"]["PlaybackPositionTicks"] = 0
    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        shows = TVShows(api, kdb, vdb, library=TV_LIBRARY, hooks=HOOKS)
        shows.tvshow(dto(SERIES))
        shows.episode(payload)

    assert resume_shadow_rows() == []

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        TVShows(api, kdb, vdb, library=TV_LIBRARY, hooks=HOOKS).remove("episode1")

    assert video_query("SELECT COUNT(*) FROM episode") == [(0,)]
    for label, sql in ORPHAN_RULES:
        assert video_query(sql) == [(0,)], "orphans in %s" % label


def test_series_removal_leaves_no_orphans(api):
    register_views({"Id": "lib-movies", "Name": "Movies", "Media": "movies"})
    write_movie(api)  # unrelated content must survive
    write_series_tree(api)

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        TVShows(api, kdb, vdb, library=TV_LIBRARY, hooks=HOOKS).remove("series1")

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
        TVShows(api, kdb, vdb, library=TV_LIBRARY, hooks=HOOKS).remove("bb-s1")

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
        TVShows(api, kdb, vdb, library=TV_LIBRARY, hooks=HOOKS).season(alias, show_id=1)

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
        TVShows(api, kdb, vdb, library=TV_LIBRARY, hooks=HOOKS).remove("episode1")

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
        TVShows(api, kdb, vdb, library=TV_LIBRARY, hooks=HOOKS).remove("episode1")

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


# --- series pooling attribution (healing-loops-plan F2) -----------------------


TV_LIBRARY_B = {"Id": "lib-shows-b", "Name": "Shows B"}


def series2(**overrides):
    """The same show contributed by a second library (series pooling)."""
    payload = dict(SERIES)
    payload.update({"Id": "series2", "Etag": "etag-series2-v1"})
    payload.update(overrides)
    return payload


def pool_season():
    """A season the server lists under series1 but attributes to series2 —
    the signal that triggers the pool arm."""
    payload = dto(SEASON_1)
    payload.update({"Id": "season2", "SeriesId": "series2"})
    return payload


def write_show(api, payload, library):
    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        TVShows(api, kdb, vdb, library=library, hooks=HOOKS).tvshow(payload)


def show_folder(jellyfin_id):
    return kofin_query(
        "SELECT media_folder FROM jellyfin WHERE jellyfin_id = ?", (jellyfin_id,)
    )


def register_pool_views():
    register_views(
        {"Id": "lib-shows", "Name": "Shows", "Media": "tvshows"},
        {"Id": "lib-shows-b", "Name": "Shows B", "Media": "tvshows"},
    )


def test_series_pool_a_then_b_rehomes_on_contact(api):
    """The pooling library writes a NULL placeholder, not an attribution;
    the series' own library adopts it on first contact. Stamped with the
    pooling library's id (the fork's shape) the row was counted by that
    library's prune and divergence probe forever — a heal that never
    converged."""
    register_pool_views()
    api.seasons_by_series = {"series1": [dto(SEASON_1), pool_season()]}

    write_show(api, dto(SERIES), TV_LIBRARY)
    assert show_folder("series2") == [(None,)]
    show_row = kofin_query("SELECT kodi_id FROM jellyfin WHERE jellyfin_id = 'series1'")
    assert (
        kofin_query("SELECT kodi_id FROM jellyfin WHERE jellyfin_id = 'series2'")
        == show_row
    )

    write_show(api, dto(series2()), TV_LIBRARY_B)
    assert show_folder("series2") == [("lib-shows-b",)]
    assert show_folder("series1") == [("lib-shows",)]


def test_series_pool_b_then_a_keeps_attribution(api):
    """Synced in the other order the pooled series is already tracked, the
    pool arm never fires, and both attributions are right from the start."""
    register_pool_views()
    api.seasons_by_series = {
        "series1": [dto(SEASON_1), pool_season()],
        "series2": [],
    }

    write_show(api, dto(series2()), TV_LIBRARY_B)
    write_show(api, dto(SERIES), TV_LIBRARY)

    assert show_folder("series2") == [("lib-shows-b",)]
    assert show_folder("series1") == [("lib-shows",)]


def test_series_pool_placeholder_adopted_incrementally(api):
    """A placeholder touched by a realtime update (no library context)
    resolves its home through one Ancestors call and re-homes."""
    register_pool_views()
    api.seasons_by_series = {"series1": [dto(SEASON_1), pool_season()]}
    write_show(api, dto(SERIES), TV_LIBRARY)
    assert show_folder("series2") == [(None,)]

    sync = sync_db.get_sync()
    sync["Whitelist"] = ["lib-shows", "lib-shows-b"]
    sync_db.save_sync(sync)
    api.ancestors_by_id = {"series2": [{"Id": "lib-shows-b", "Name": "Shows B"}]}

    write_show(api, dto(series2()), None)
    assert show_folder("series2") == [("lib-shows-b",)]


def test_series_pool_placeholder_outside_whitelist_stays_dormant(api):
    """No synced library owns the pooled series: the placeholder stays NULL
    and the realtime update writes nothing — there is no library to path the
    row under, and a NULL row is unreachable by every prune and probe."""
    register_pool_views()
    api.seasons_by_series = {"series1": [dto(SEASON_1), pool_season()]}
    write_show(api, dto(SERIES), TV_LIBRARY)

    sync = sync_db.get_sync()
    sync["Whitelist"] = ["lib-shows"]
    sync_db.save_sync(sync)
    api.ancestors_by_id = {}

    before = dump(str(sync_db._path_overrides["video"]))
    write_show(api, dto(series2()), None)
    assert show_folder("series2") == [(None,)]
    assert dump(str(sync_db._path_overrides["video"])) == before


def test_series_pool_placeholder_dies_with_the_show(api):
    """Pool placeholders alias the show's Kodi row and die with it; a
    sibling still live on the server comes back as a missing id on its own
    library's next pass, never as a dangling reference."""
    register_pool_views()
    api.seasons_by_series = {"series1": [dto(SEASON_1), pool_season()]}
    write_show(api, dto(SERIES), TV_LIBRARY)
    assert kofin_query("SELECT COUNT(*) FROM jellyfin WHERE media_type = 'tvshow'") == [
        (2,)
    ]

    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        TVShows(api, kdb, vdb, library=TV_LIBRARY, hooks=HOOKS).remove("series1")

    assert kofin_query("SELECT COUNT(*) FROM jellyfin WHERE media_type = 'tvshow'") == [
        (0,)
    ]
    assert video_query("SELECT COUNT(*) FROM tvshow") == [(0,)]


def test_prune_rehome_spared_references(api):
    from kofin.sync import prune

    """A legacy misattributed row heals on the next UpdateLibrary instead of
    sparing and warning forever: re-homed to its whitelisted ancestor view,
    or to the NULL placeholder state when no synced library owns it. Season
    rows are exempt — they carry no media_folder by design."""
    register_pool_views()
    api.seasons_by_series = {"series1": [dto(SEASON_1), pool_season()]}
    write_show(api, dto(SERIES), TV_LIBRARY)
    kofin_exec(
        "UPDATE jellyfin SET media_folder = 'lib-shows' "
        "WHERE jellyfin_id = 'series2'"
    )

    sync = sync_db.get_sync()
    sync["Whitelist"] = ["lib-shows", "lib-shows-b"]
    sync_db.save_sync(sync)
    api.ancestors_by_id = {"series2": [{"Id": "lib-shows-b", "Name": "Shows B"}]}

    fullsync = make_fullsync(api)
    try:
        prune.rehome_spared(fullsync.server, {"series2"})
        assert show_folder("series2") == [("lib-shows-b",)]

        api.ancestors_by_id = {}
        prune.rehome_spared(fullsync.server, {"series2"})
        assert show_folder("series2") == [(None,)]

        season_folder = kofin_query(
            "SELECT media_folder FROM jellyfin WHERE jellyfin_id = 'season1'"
        )
        prune.rehome_spared(fullsync.server, {"season1"})
        assert (
            kofin_query(
                "SELECT media_folder FROM jellyfin WHERE jellyfin_id = 'season1'"
            )
            == season_folder
        )
    finally:
        fullsync.release()


# --- music videos ----------------------------------------------------------------


def test_musicvideo_write_and_idempotency(api):
    register_views({"Id": "lib-mv", "Name": "Clips", "Media": "musicvideos"})
    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        MusicVideos(api, kdb, vdb, library=MV_LIBRARY, hooks=HOOKS).musicvideo(
            dto(MUSICVIDEO)
        )

    row = video_query("SELECT c00, c09, c10, premiered FROM musicvideo")[0]
    assert row[0] == "Hit Single"
    assert row[1] == "Hits"
    assert row[2] == "The Band"
    assert row[3].startswith("2018-06-0")

    files = video_query("SELECT strFilename FROM files")[0]
    assert "dbid=1" in files[0] and "id=mvideo1" in files[0]

    first = dump(str(sync_db._path_overrides["video"]))
    with sync_db.Database("kofin") as kdb, sync_db.Database("video") as vdb:
        MusicVideos(api, kdb, vdb, library=MV_LIBRARY, hooks=HOOKS).musicvideo(
            dto(MUSICVIDEO)
        )
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
        music = Music(api, kdb, mdb, library=MUSIC_LIBRARY, hooks=HOOKS)
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


def test_music_song_with_no_artist_at_all_gets_the_blank_credit(
    api, frozen_music_clock
):
    # Both lists empty (a file with no artist tags at all): the fork wrote no
    # song_artist row, and Kodi's song listings inner-join songartistview, so
    # the song was not artist-less — it was invisible. The guarantee files it
    # under Kodi's own [Missing Tag] blank artist, like Kodi's scanner would.
    write_music_tree(api, song=dict(SONG, ArtistItems=[], AlbumArtists=[]))

    song_id = music_query("SELECT idSong FROM song")[0][0]
    assert music_query(
        "SELECT idArtist, idSong, iOrder, strArtist FROM song_artist"
    ) == [(1, song_id, 0, "[Missing Tag]")]
    # The join Kodi's song listings run: the song is reachable.
    assert music_query(
        "SELECT s.strTitle FROM song s JOIN song_artist sa ON sa.idSong = s.idSong"
    ) == [("Opening Track",)]


def test_music_blank_credit_is_idempotent(api, frozen_music_clock):
    song = dict(SONG, ArtistItems=[], AlbumArtists=[])
    write_music_tree(api, song=song)
    first = music_dump(str(sync_db._path_overrides["music"]))

    write_music_tree(api, song=song)
    assert music_dump(str(sync_db._path_overrides["music"])) == first


def test_music_blank_credit_yields_to_a_real_one(api, frozen_music_clock):
    write_music_tree(api, song=dict(SONG, ArtistItems=[], AlbumArtists=[]))

    # The tags arrive later (new Etag, real ArtistItems): the placeholder
    # credit must go, or the song keeps showing under [Missing Tag] too.
    with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
        Music(api, kdb, mdb, library=MUSIC_LIBRARY, hooks=HOOKS).song(
            dict(dto(SONG), Etag="etag-song1-v2")
        )

    artist_id = music_query("SELECT idArtist FROM artist WHERE strArtist='The Band'")[
        0
    ][0]
    song_id = music_query("SELECT idSong FROM song")[0][0]
    assert music_query("SELECT idArtist, idSong FROM song_artist") == [
        (artist_id, song_id)
    ]


def test_music_artist_removal_spares_compilation_appearances(api, frozen_music_clock):
    # The Bravia incident: an artist with one album of their own plus one
    # track on a compilation. Removing the artist fires Kodi's
    # tgrDeleteArtist trigger, which strips the compilation track's only
    # song_artist row — and the track's Etag never moves, so nothing ever
    # wrote it back. The compilation is also parented to one arbitrary
    # contributor in kofin.db (artist_discography's last write wins), so an
    # unlucky removal used to take the whole album with it.
    va_artist = dict(
        dto(ARTIST),
        Id="artist-va",
        Name="Various Artists",
        Etag="etag-va-v1",
        ProviderIds={"MusicBrainzArtist": "mbid-artist-va"},
    )
    va_album = dict(
        dto(ALBUM),
        Id="album-va",
        Name="Now That Is Music",
        Etag="etag-album-va-v1",
        ProviderIds={"MusicBrainzAlbum": "mbid-album-va"},
        AlbumArtists=[{"Name": "Various Artists", "Id": "artist-va"}],
        # The Band contributed a track, so it lands last in ArtistItems and
        # kofin.db parents the compilation to it — the hazard under test.
        ArtistItems=[
            {"Name": "Various Artists", "Id": "artist-va"},
            {"Name": "The Band", "Id": "artist1"},
        ],
    )
    va_song = dict(
        dto(SONG),
        Id="song-va",
        Name="Compilation Track",
        Etag="etag-song-va-v1",
        Album="Now That Is Music",
        AlbumId="album-va",
        ParentId="album-va",
        ProviderIds={"MusicBrainzTrackId": "mbid-track-va"},
        ArtistItems=[{"Name": "The Band", "Id": "artist1"}],
        AlbumArtists=[{"Name": "Various Artists", "Id": "artist-va"}],
    )
    api.items_by_id["artist-va"] = va_artist
    write_music_tree(api)

    with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
        music = Music(api, kdb, mdb, library=MUSIC_LIBRARY, hooks=HOOKS)
        music.artist(dto(va_artist))
        music.album(dto(va_album))
        music.song(dto(va_song))

    # The compilation really is parented to the removed artist in kofin.db.
    band_kodi_id = kofin_query(
        "SELECT kodi_id FROM jellyfin WHERE jellyfin_id='artist1'"
    )[0][0]
    assert kofin_query(
        "SELECT parent_id FROM jellyfin WHERE jellyfin_id='album-va'"
    ) == [(band_kodi_id,)]

    with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
        Music(api, kdb, mdb, library=MUSIC_LIBRARY, hooks=HOOKS).remove("artist1")

    # The artist and its own album/song are gone...
    assert music_query("SELECT COUNT(*) FROM artist WHERE strArtist='The Band'") == [
        (0,)
    ]
    assert music_query("SELECT COUNT(*) FROM album WHERE strAlbum='Greatest Hits'") == [
        (0,)
    ]
    assert kofin_query(
        "SELECT COUNT(*) FROM jellyfin WHERE jellyfin_id IN ('artist1','album1','song1')"
    ) == [(0,)]

    # ...the compilation and its track are not.
    assert music_query(
        "SELECT COUNT(*) FROM album WHERE strAlbum='Now That Is Music'"
    ) == [(1,)]
    assert music_query(
        "SELECT strTitle FROM song WHERE strTitle='Compilation Track'"
    ) == [("Compilation Track",)]
    assert kofin_query(
        "SELECT parent_id FROM jellyfin WHERE jellyfin_id='album-va'"
    ) == [(None,)]

    # The track lost its only credit to the trigger and was given the
    # substitute [Missing Tag] credit, so it stays reachable through Kodi's
    # join...
    song_id = music_query("SELECT idSong FROM song WHERE strTitle='Compilation Track'")[
        0
    ][0]
    assert music_query("SELECT idArtist, idSong FROM song_artist WHERE idRole = 1") == [
        (1, song_id)
    ]

    # ...and its checksum is cleared so the next walk restores true credits.
    assert kofin_query("SELECT checksum FROM jellyfin WHERE jellyfin_id='song-va'") == [
        (None,)
    ]

    # Removal hygiene held for what was actually removed.
    assert music_query(
        "SELECT COUNT(*) FROM song_artist WHERE idSong NOT IN (SELECT idSong FROM song)"
    ) == [(0,)]
    assert music_query(
        "SELECT COUNT(*) FROM album_artist WHERE idAlbum NOT IN (SELECT idAlbum FROM album)"
    ) == [(0,)]

    # The heal: the server re-creates the artist (the track still credits
    # it), and the cleared checksum makes the next update walk rewrite the
    # song — which re-creates the artist reference and restores the true
    # credit, replacing the substitute.
    with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
        Music(api, kdb, mdb, library=MUSIC_LIBRARY, hooks=HOOKS).song(dto(va_song))

    band_id = music_query("SELECT idArtist FROM artist WHERE strArtist='The Band'")[0][
        0
    ]
    assert kofin_query("SELECT COUNT(*) FROM jellyfin WHERE jellyfin_id='artist1'") == [
        (1,)
    ]
    assert music_query(
        "SELECT idArtist FROM song_artist WHERE idSong = ? AND idRole = 1",
        (song_id,),
    ) == [(band_id,)]
    assert kofin_query("SELECT checksum FROM jellyfin WHERE jellyfin_id='song-va'") == [
        ("etag-song-va-v1|plugin",)
    ]


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


def test_music_update_heals_a_song_whose_path_row_is_gone(api, frozen_music_clock):
    """A song can be left pointing at a deleted path row (a download's server
    row swept while the download lived, then a restore by id -- 61 songs on a
    Bravia, 2026-08-28). songview inner-joins path, so the song is in no
    listing; and the fork's update rewrote the missing row in place, a silent
    no-op, so no repair could bring it back. The update now re-resolves the
    row by string and the mapping follows it."""
    write_music_tree(api)
    original = music_query(
        "SELECT p.strPath FROM song s JOIN path p ON p.idPath = s.idPath"
    )[0][0]
    with sync_db.Database("music") as mdb:
        mdb.cursor.execute("DELETE FROM path WHERE idPath = (SELECT idPath FROM song)")
    assert music_query("SELECT COUNT(*) FROM songview") == [(0,)]

    register_views({"Id": "lib-music", "Name": "Tunes", "Media": "music"})
    with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
        music = Music(api, kdb, mdb, library=MUSIC_LIBRARY, hooks=HOOKS)
        music.song(dto(dict(SONG, Etag="etag-song1-v2")))

    rows = music_query(
        "SELECT s.idPath, p.strPath FROM song s JOIN path p ON p.idPath = s.idPath"
    )
    assert rows == [(rows[0][0], original)]
    assert music_query("SELECT COUNT(*) FROM songview") == [(1,)]
    assert kofin_query(
        "SELECT kodi_pathid FROM jellyfin WHERE jellyfin_id = 'song1'"
    ) == [(rows[0][0],)]


def test_music_rewrite_with_a_bumped_etag_is_byte_identical(api, frozen_music_clock):
    """The plain idempotency test above never reaches the writers -- an
    unchanged Etag short-circuits in check_unchanged. This is the pass that
    actually re-runs them, and it used to be impossible to assert: discography
    has no unique index, so its INSERT OR REPLACE appended instead of
    replacing and every rewrite grew the table."""
    write_music_tree(api)
    first = music_dump(str(sync_db._path_overrides["music"]))

    register_views({"Id": "lib-music", "Name": "Tunes", "Media": "music"})
    with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
        music = Music(api, kdb, mdb, library=MUSIC_LIBRARY, hooks=HOOKS)
        music.album(dto(dict(ALBUM, Etag="etag-album1-v2")))
        music.song(dto(dict(SONG, Etag="etag-song1-v2")))

    assert music_dump(str(sync_db._path_overrides["music"])) == first


def test_music_discography_keeps_one_row_per_album(api, frozen_music_clock):
    """discography carries no unique index (Kodi gives it only
    idxDiscography_1 on idArtist), so nothing in the schema stops a rewrite
    appending. The growth was per *track*, not per pass: the song leg fires
    once per song, so a 12-track album left 13 rows after a single walk and
    another 13 on every Etag change -- 251 rows for AC/DC's 22 albums on a
    real library. The album's own year must be the one that survives; the
    song leg writes 0 and must not stamp it over the top."""
    register_views({"Id": "lib-music", "Name": "Tunes", "Media": "music"})

    for walk in (1, 2, 3):
        with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
            music = Music(api, kdb, mdb, library=MUSIC_LIBRARY, hooks=HOOKS)
            music.artist(dto(dict(ARTIST, Etag="etag-artist1-v%d" % walk)))
            music.album(dto(dict(ALBUM, Etag="etag-album1-v%d" % walk)))
            for track in range(1, 13):
                music.song(
                    dto(
                        dict(
                            SONG,
                            Id="song%d" % track,
                            Name="Track %d" % track,
                            IndexNumber=track,
                            Etag="etag-song%d-v%d" % (track, walk),
                        )
                    )
                )

        assert music_query("SELECT idArtist, strAlbum, strYear FROM discography") == [
            (2, "Greatest Hits", "2017")
        ], ("grew on walk %d" % walk)


def test_music_discography_year_follows_the_album(api, frozen_music_clock):
    """A year correction on the server re-points the row rather than adding
    one -- the album leg owns the row and clears before it writes."""
    write_music_tree(api)

    with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
        music = Music(api, kdb, mdb, library=MUSIC_LIBRARY, hooks=HOOKS)
        music.album(dto(dict(ALBUM, Etag="etag-album1-v2", ProductionYear=1971)))

    assert music_query("SELECT strAlbum, strYear FROM discography") == [
        ("Greatest Hits", "1971")
    ]


def test_music_discography_song_leg_covers_an_unlisted_album_artist(
    api, frozen_music_clock
):
    """The song leg is not redundant with the album leg: the album writer
    walks ArtistItems and the song writer walks AlbumArtists, which are
    different lists. An album artist absent from ArtistItems gets its only
    discography row from the song leg, so that leg has to keep writing --
    it just must not overwrite a row the album leg already owns."""
    api.items_by_id["artist2"] = dto(
        dict(
            ARTIST,
            Id="artist2",
            Name="The Guest",
            ProviderIds={"MusicBrainzArtist": "mbid-artist-2"},
        )
    )
    register_views({"Id": "lib-music", "Name": "Tunes", "Media": "music"})

    with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
        music = Music(api, kdb, mdb, library=MUSIC_LIBRARY, hooks=HOOKS)
        music.artist(dto(ARTIST))
        music.album(dto(ALBUM))  # ArtistItems names artist1 only
        music.song(
            dto(
                dict(
                    SONG,
                    AlbumArtists=[
                        {"Name": "The Band", "Id": "artist1"},
                        {"Name": "The Guest", "Id": "artist2"},
                    ],
                )
            )
        )

    assert music_query(
        "SELECT a.strArtist, d.strAlbum, d.strYear FROM discography d "
        "JOIN artist a ON a.idArtist = d.idArtist ORDER BY a.strArtist"
    ) == [
        ("The Band", "Greatest Hits", "2017"),
        ("The Guest", "Greatest Hits", "0"),
    ]


def test_music_discography_keys_on_the_albums_own_title(api, frozen_music_clock):
    """Jellyfin reports a song's Album tag separately from the album item's
    name and the two disagree in the wild -- a track tagged "The Terminator"
    on an album named "The Terminator: Original Soundtrack". discography is
    keyed by title, so keying the song leg on the tag wrote a second row that
    matched no album in GetArtistDiscography's fold and rendered on its own
    as "0 - The Terminator" (seen live on a real library)."""
    register_views({"Id": "lib-music", "Name": "Tunes", "Media": "music"})

    with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
        music = Music(api, kdb, mdb, library=MUSIC_LIBRARY, hooks=HOOKS)
        music.artist(dto(ARTIST))
        music.album(dto(ALBUM))  # named "Greatest Hits"
        music.song(dto(dict(SONG, Album="Greatest Hits (Remastered)")))

    assert music_query("SELECT strAlbum, strYear FROM discography") == [
        ("Greatest Hits", "2017")
    ]


def test_music_discography_falls_back_to_the_song_tag(api, frozen_music_clock):
    """No album row to read a title from -- the tag is all there is."""
    register_views({"Id": "lib-music", "Name": "Tunes", "Media": "music"})

    with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
        music = Music(api, kdb, mdb, library=MUSIC_LIBRARY, hooks=HOOKS)
        music.artist(dto(ARTIST))
        obj = music.objects.map(dto(dict(SONG, Album="Tag Only Title")), "Song")
        obj["AlbumId"] = 9999  # no album row carries this id
        obj["LibraryId"] = "lib-music"
        obj["LibraryName"] = "Tunes"
        music.song_artist_discography(obj)

    assert music_query("SELECT strAlbum, strYear FROM discography") == [
        ("Tag Only Title", "0")
    ]


def test_music_album_removal_takes_its_discography(api, frozen_music_clock):
    """Kodi cascades an album delete into song/album_artist/album_source/art
    but never into discography, so the rows used to outlive the album -- and
    an unmatched pair is exactly where GetArtistDiscography stops folding
    duplicates away, rendering "0 - <album>" beside the real entry."""
    write_music_tree(api)
    assert music_query("SELECT COUNT(*) FROM discography") == [(1,)]

    with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
        music = Music(api, kdb, mdb, library=MUSIC_LIBRARY, hooks=HOOKS)
        music.remove("song1")
        music.remove("album1")

    assert music_query("SELECT COUNT(*) FROM album") == [(0,)]
    assert music_query("SELECT COUNT(*) FROM discography") == [(0,)]
    # The artist itself is untouched -- only its rows for that album go.
    assert music_query("SELECT COUNT(*) FROM artist WHERE strArtist='The Band'") == [
        (1,)
    ]


def test_music_album_removal_spares_another_artists_same_title(api, frozen_music_clock):
    """Album titles repeat across artists, so the delete is scoped by
    album_artist. A title-only delete would take the other artist's row --
    and Kodi's own scraped rows for albums that were never in the library."""
    write_music_tree(api)

    artist_id = music_query("SELECT idArtist FROM artist WHERE strArtist='The Band'")[
        0
    ][0]
    with sync_db.Database("music") as mdb:
        mdb.cursor.execute(
            "INSERT INTO artist (idArtist, strArtist) VALUES (?, ?)",
            (artist_id + 50, "Another Band"),
        )
        mdb.cursor.execute(
            "INSERT INTO discography (idArtist, strAlbum, strYear) VALUES (?, ?, ?)",
            (artist_id + 50, "Greatest Hits", "1999"),
        )

    with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
        music = Music(api, kdb, mdb, library=MUSIC_LIBRARY, hooks=HOOKS)
        music.remove("song1")
        music.remove("album1")

    assert music_query("SELECT idArtist, strAlbum, strYear FROM discography") == [
        (artist_id + 50, "Greatest Hits", "1999")
    ]


def test_music_artist_removal_no_orphans(api, frozen_music_clock):
    write_music_tree(api)

    with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
        Music(api, kdb, mdb, library=MUSIC_LIBRARY, hooks=HOOKS).remove("artist1")

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


# --- the per-library music source ---------------------------------------------


def source_rows():
    return music_query("SELECT idSource, strName, strMultipath FROM source")


def album_source_rows():
    return sorted(music_query("SELECT idSource, idAlbum FROM album_source"))


def test_music_write_links_the_library_source(api, frozen_music_clock):
    """MyMusic has no tag table, so a per-library music node filters on the
    library's own source row instead — and album_source is the one link that
    a downloaded song's repointing leaves alone."""
    write_music_tree(api)

    assert source_rows() == [(1, "Tunes", "plugin://plugin.video.kofin/lib-music/")]

    album_id = music_query("SELECT idAlbum FROM album WHERE strAlbum='Greatest Hits'")[
        0
    ][0]
    assert album_source_rows() == [(1, album_id)]

    # Deliberately never written: nothing in the source rule reads it, and its
    # idPath is a per-source ordinal that delete_path_if_unused and
    # prune_orphan_paths both read as a path.idPath.
    assert music_query("SELECT COUNT(*) FROM source_path") == [(0,)]


def test_music_source_link_survives_a_rewrite(api, frozen_music_clock):
    """Etag bumped so check_unchanged does not short-circuit: the plain
    idempotency test never reaches the hook at all, and this is the pass that
    would show a duplicate link or a second source row.

    Scoped to the two source tables because that is what this test is about;
    the whole-dump assertion for the same rewrite lives in
    ``test_music_rewrite_with_a_bumped_etag_is_byte_identical``.
    """
    write_music_tree(api)
    before_sources = source_rows()
    before_links = album_source_rows()

    register_views({"Id": "lib-music", "Name": "Tunes", "Media": "music"})
    with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
        music = Music(api, kdb, mdb, library=MUSIC_LIBRARY, hooks=HOOKS)
        music.album(dto(dict(ALBUM, Etag="etag-album1-v2")))
        music.song(dto(dict(SONG, Etag="etag-song1-v2")))

    assert source_rows() == before_sources
    assert album_source_rows() == before_links


def test_music_single_reaches_its_library_source(api, frozen_music_clock):
    """A single's album is created by song_add and never passes through the
    album writer, so the album leg alone would leave every single out of its
    library's nodes."""
    register_views({"Id": "lib-music", "Name": "Tunes", "Media": "music"})
    with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
        music = Music(api, kdb, mdb, library=MUSIC_LIBRARY, hooks=HOOKS)
        music.artist(dto(ARTIST))
        music.song(dto(dict(SONG, AlbumId=None, Album=None)))

    album_id = music_query("SELECT idAlbum FROM song")[0][0]
    assert album_source_rows() == [(1, album_id)]


def test_music_source_removal_takes_its_links(api, frozen_music_clock):
    """The source row is the whole cleanup — tgrDeleteSource drops the links,
    and tgrDeleteAlbum has already dropped each album's own."""
    write_music_tree(api)

    with sync_db.Database("music") as mdb:
        MusicKodiDb(mdb.cursor).delete_source_for("lib-music")

    assert source_rows() == []
    assert album_source_rows() == []


def test_music_sources_are_reasserted_after_a_kodi_scan(api, frozen_music_clock):
    """Kodi's own scanner runs DELETE FROM source whenever the table
    disagrees with sources.xml — which, with an empty one, it does the moment
    kofin writes a row. Without the reconcile every per-library music node
    comes back from a user's scan matching nothing."""
    from kofin.sync import musicsources

    write_music_tree(api)
    album_id = music_query("SELECT idAlbum FROM album WHERE strAlbum='Greatest Hits'")[
        0
    ][0]

    with sync_db.Database("music") as mdb:
        mdb.cursor.execute("DELETE FROM source")

    assert source_rows() == []
    assert album_source_rows() == []  # tgrDeleteSource took them too

    with sync_db.Database("kofin") as kdb, sync_db.Database("music") as mdb:
        musicsources.reassert(
            kdb.cursor,
            mdb.cursor,
            [{"Id": "lib-music", "Name": "Tunes"}],
        )

    assert source_rows() == [(1, "Tunes", "plugin://plugin.video.kofin/lib-music/")]
    assert album_source_rows() == [(1, album_id)]
