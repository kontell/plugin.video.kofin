"""The route golden (shell refactor phase 2, P2.0).

Every listing route driven over one canned server world, its output —
each row's path, label, folder flag, art, properties and info, plus the
route's ``setContent`` and ``endOfDirectory`` outcome — hashed per route
and the hashes pinned here. P2.1 (the ``listing()`` wrapper and
``structural_rows()``) and P2.2 must not move a byte of it; the 62 tests
in ``test_browse.py`` keep the coverage job, this keeps the identity job.

When a hash moves on purpose, ``pytest -k golden -s`` prints the actual
table to paste in; a move nobody intended is a finding, not a rename.
"""

import hashlib
import json

import pytest
import xbmcgui
import xbmcplugin

from kofin.plugin import browse
from kofin.plugin.router import Request
from tests.unit.fakes import FakeAddon, FakeApi, FakeWindow

# --- one world -----------------------------------------------------------------

V_MOVIES, V_SHOWS, V_MUSIC = "v-movies", "v-shows", "v-music"

MOVIE = {
    "Id": "m1",
    "Name": "Rio Bravo",
    "Type": "Movie",
    "ImageTags": {"Primary": "t1"},
    "BackdropImageTags": ["b1"],
    "RunTimeTicks": 84_000_000_000,
    "ProductionYear": 1959,
    "UserData": {"PlaybackPositionTicks": 12_243_410_000, "Played": False},
    "MediaSources": [
        {"Id": "m1", "MediaStreams": [{"Type": "Video", "Codec": "hevc"}]}
    ],
}
MOVIE2 = {
    "Id": "m2",
    "Name": "The Fly",
    "Type": "Movie",
    "ImageTags": {},
    "RunTimeTicks": 57_000_000_000,
    "ProductionYear": 1986,
    "UserData": {"PlaybackPositionTicks": 0, "Played": True},
}
SERIES = {
    "Id": "series1",
    "Name": "Archer",
    "Type": "Series",
    "ImageTags": {"Primary": "s1"},
    "SpecialFeatureCount": 2,
    "UserData": {"UnplayedItemCount": 3},
}
SEASON = {
    "Id": "season1",
    "Name": "Season 1",
    "Type": "Season",
    "SeriesId": "series1",
    "SeriesName": "Archer",
    "IndexNumber": 1,
    "ImageTags": {},
}
EPISODE = {
    "Id": "e1",
    "Name": "Mole Hunt",
    "Type": "Episode",
    "SeriesId": "series1",
    "SeriesName": "Archer",
    "SeasonId": "season1",
    "ParentIndexNumber": 1,
    "IndexNumber": 1,
    "ImageTags": {},
    "RunTimeTicks": 12_000_000_000,
    "UserData": {"PlaybackPositionTicks": 0, "Played": False},
}
ALBUM = {
    "Id": "a1",
    "Name": "Trainspotting",
    "Type": "MusicAlbum",
    "AlbumArtist": "Various Artists",
    "ProductionYear": 1996,
    "ImageTags": {},
}
SONG = {
    "Id": "song1",
    "Name": "Born Slippy",
    "Type": "Audio",
    "Album": "Trainspotting",
    "AlbumId": "a1",
    "Artists": ["Underworld"],
    "IndexNumber": 3,
    "RunTimeTicks": 5_600_000_000,
    "ImageTags": {},
}
ARTIST = {"Id": "artist1", "Name": "Underworld", "Type": "MusicArtist", "ImageTags": {}}
BOXSET = {"Id": "set1", "Name": "Westerns", "Type": "BoxSet", "ImageTags": {}}
GENRE = {"Id": "g1", "Name": "Drama", "Type": "Genre"}
PERSON = {"Id": "p1", "Name": "John Wayne", "Type": "Person", "ImageTags": {}}
FEATURE = {"Id": "extra1", "Name": "Blooper Reel", "Type": "Video", "ImageTags": {}}
VIEWS = [
    {"Id": V_MOVIES, "Name": "Movies", "CollectionType": "movies", "ImageTags": {}},
    {"Id": V_SHOWS, "Name": "Shows", "CollectionType": "tvshows", "ImageTags": {}},
    {"Id": V_MUSIC, "Name": "Music", "CollectionType": "music", "ImageTags": {}},
    {"Id": "v-tv", "Name": "Live TV", "CollectionType": "livetv", "ImageTags": {}},
]

BY_TYPE = {
    "Movie": [MOVIE, MOVIE2],
    "Series": [SERIES],
    "Season": [SEASON],
    "Episode": [EPISODE],
    "MusicAlbum": [ALBUM],
    "Audio": [SONG],
    "MusicArtist": [ARTIST],
    "BoxSet": [BOXSET],
    "Person": [PERSON],
}


def _items(params):
    kinds = str(params.get("IncludeItemTypes") or "").split(",")
    rows = []
    for kind in kinds:
        rows.extend(BY_TYPE.get(kind.strip(), []))
    if not kinds or kinds == [""]:
        rows = [MOVIE, SERIES, ALBUM]
    return {"Items": rows, "TotalRecordCount": len(rows)}


def world():
    return FakeApi(
        views={"Items": VIEWS},
        resume=lambda fields="", limit=25: {"Items": [MOVIE, EPISODE]},
        device_sessions=lambda device_id: [],
        next_up=lambda view_id, fields="", limit=25: {"Items": [EPISODE]},
        items=_items,
        latest=lambda parent_id, include_types, fields="", limit=25: [ALBUM],
        genres=lambda parent_id, include_types=None: {"Items": [GENRE]},
        artists=lambda view_id: {"Items": [ARTIST]},
        album_artists=lambda view_id: {"Items": [ARTIST]},
        seasons=lambda series_id: {"Items": [SEASON]},
        episodes=lambda series_id, season_id, fields="": {"Items": [EPISODE]},
        filters=lambda parent_id, item_type="": {
            "Years": [1970, 2020, 1999],
            "Tags": ["heist", "noir", "western"],
        },
        special_features=lambda item_id: [FEATURE],
        item=lambda item_id: {"Id": item_id, "SpecialFeatureCount": 2},
        persons=lambda term, limit=100: {"Items": [PERSON]},
    )


# --- what a route hands to Kodi, recorded -------------------------------------


class GoldenListItem(xbmcgui.ListItem):
    """Kodistubs' ListItem forgets everything it is given; this one keeps
    the parts a listing is made of."""

    def __init__(self, label="", label2="", path="", offscreen=False):
        super().__init__(label, label2, path, offscreen)
        self.golden = {
            "label": label,
            "label2": label2,
            "path": path,
            "art": {},
            "properties": {},
            "info": {},
            "context": [],
            "mime": "",
        }

    def setLabel(self, label):
        self.golden["label"] = label

    def setLabel2(self, label):
        self.golden["label2"] = label

    def setPath(self, path):
        self.golden["path"] = path

    def setArt(self, dictionary):
        self.golden["art"].update(dictionary)

    def setProperty(self, key, value):
        self.golden["properties"][key] = value

    def setProperties(self, dictionary):
        self.golden["properties"].update(dictionary)

    def setInfo(self, type, infoLabels):  # noqa: A002 - Kodi's own spelling
        self.golden["info"][type] = dict(infoLabels)

    def addContextMenuItems(self, items, replaceItems=False):
        self.golden["context"].extend(list(entry) for entry in items)

    def setMimeType(self, mimetype):
        self.golden["mime"] = mimetype

    def setIsFolder(self, isFolder):
        self.golden["is_folder"] = bool(isFolder)


@pytest.fixture
def capture(monkeypatch):
    FakeAddon.store = {}
    FakeWindow.store = {"kofin.who.names": "Alice, Bob"}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    for namespace in (xbmcgui, browse.xbmcgui, browse.listitems.xbmcgui):
        monkeypatch.setattr(namespace, "ListItem", GoldenListItem)

    recorded = {"entries": [], "content": None, "succeeded": None, "sort": []}
    monkeypatch.setattr(
        xbmcplugin,
        "addDirectoryItems",
        lambda handle, entries, count: recorded["entries"].extend(entries) or True,
    )
    monkeypatch.setattr(
        xbmcplugin,
        "setContent",
        lambda handle, content: recorded.__setitem__("content", content),
    )
    monkeypatch.setattr(
        xbmcplugin,
        "endOfDirectory",
        lambda handle, succeeded=True, **kw: recorded.__setitem__(
            "succeeded", succeeded
        ),
    )
    monkeypatch.setattr(
        xbmcplugin,
        "addSortMethod",
        lambda handle, method: recorded["sort"].append(method),
    )
    api = world()
    monkeypatch.setattr(browse, "_api", lambda: api)
    return recorded


def _row(entry):
    path, li, folder = entry
    golden = getattr(li, "golden", {"label": "?"})
    return {"path": path, "folder": bool(folder), **golden}


def digest(recorded):
    payload = {
        "rows": [_row(entry) for entry in recorded["entries"]],
        "content": recorded["content"],
        "succeeded": recorded["succeeded"],
        "sort": recorded["sort"],
    }
    text = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# --- the routes ---------------------------------------------------------------


def _browse(**params):
    return ("browse", {"mode": "browse", **params})


ROUTES = {
    "root": ("root", {}),
    "continue_watching": ("continue_watching", {"mode": "continuewatching"}),
    "next_episodes": ("next_episodes", {"mode": "nextepisodes", "id": V_SHOWS}),
    "search_menu": ("search", {"mode": "search"}),
    "search_movies": ("search", {"mode": "search", "type": "movies", "query": "rio"}),
    "search_person": ("search", {"mode": "search", "person": "p1"}),
    "extras": ("extras", {"mode": "extras", "id": "m1"}),
    "movies_menu": _browse(view=V_MOVIES, type="movies"),
    "tvshows_menu": _browse(view=V_SHOWS, type="tvshows"),
    "music_menu": _browse(view=V_MUSIC, type="music"),
    "movies_alpha": _browse(view=V_MOVIES, type="movies", folder="alpha"),
    "movies_years": _browse(view=V_MOVIES, type="movies", folder="years"),
    "movies_tags": _browse(view=V_MOVIES, type="movies", folder="tags"),
    "movies_tags_h": _browse(view=V_MOVIES, type="movies", folder="tags-h"),
    "movies_all": _browse(view=V_MOVIES, type="movies", folder="all"),
    "movies_recent": _browse(view=V_MOVIES, type="movies", folder="recent"),
    "movies_inprogress": _browse(view=V_MOVIES, type="movies", folder="inprogress"),
    "movies_unwatched": _browse(view=V_MOVIES, type="movies", folder="unwatched"),
    "movies_favorites": _browse(view=V_MOVIES, type="movies", folder="favorites"),
    "movies_random": _browse(view=V_MOVIES, type="movies", folder="random"),
    "movies_sets": _browse(view=V_MOVIES, type="movies", folder="sets"),
    "movies_genres": _browse(view=V_MOVIES, type="movies", folder="genres"),
    "movies_genre": _browse(view=V_MOVIES, type="movies", folder="genre-g1"),
    "movies_alpha_a": _browse(view=V_MOVIES, type="movies", folder="alpha-A"),
    "movies_alpha_hash": _browse(view=V_MOVIES, type="movies", folder="alpha-#"),
    "movies_year": _browse(view=V_MOVIES, type="movies", folder="year-2020"),
    "movies_tag": _browse(view=V_MOVIES, type="movies", folder="tag-heist"),
    "movies_children": _browse(view=V_MOVIES, type="movies", folder="children"),
    "tvshows_all": _browse(view=V_SHOWS, type="tvshows", folder="all"),
    "tvshows_recent_episodes": _browse(
        view=V_SHOWS, type="tvshows", folder="recentepisodes"
    ),
    "tvshows_inprogress_episodes": _browse(
        view=V_SHOWS, type="tvshows", folder="inprogressepisodes"
    ),
    "tvshows_nextup": _browse(view=V_SHOWS, type="tvshows", folder="nextup"),
    "tvshows_extras": _browse(view=V_SHOWS, type="tvshows", folder="extras"),
    "series_seasons": _browse(view=V_SHOWS, type="series", folder="series1"),
    "season_episodes": _browse(
        view=V_SHOWS, type="season", folder="season1", series="series1"
    ),
    "music_albums": _browse(view=V_MUSIC, type="music", folder="albums"),
    "music_artists": _browse(view=V_MUSIC, type="music", folder="artists"),
    "music_album_artists": _browse(view=V_MUSIC, type="music", folder="albumartists"),
    "music_recent_albums": _browse(view=V_MUSIC, type="music", folder="recentalbums"),
    "music_favorite_albums": _browse(
        view=V_MUSIC, type="music", folder="favoritealbums"
    ),
    "music_lastplayed": _browse(view=V_MUSIC, type="music", folder="lastplayed"),
    "music_playhistory": _browse(view=V_MUSIC, type="music", folder="playhistory"),
    "music_genres": _browse(view=V_MUSIC, type="music", folder="genres"),
}

# The before build's hashes (P2.0, `8b27bf8`). Regenerate deliberately with
# ``pytest tests/unit/test_browse_golden.py -k table -s``. Identical hashes
# across several movie nodes are expected: the world answers every /Items
# query with the same two films, and what is pinned is the rendering.
EXPECTED = {
    "continue_watching": "e1b8a9bc9a9dbcb2",
    "extras": "bb00cb24bbca4699",
    "movies_all": "70fd54c18e2f24ed",
    "movies_alpha": "51c4068085046736",
    "movies_alpha_a": "70fd54c18e2f24ed",
    "movies_alpha_hash": "70fd54c18e2f24ed",
    "movies_children": "2f236c15be769c51",
    "movies_favorites": "70fd54c18e2f24ed",
    "movies_genre": "70fd54c18e2f24ed",
    "movies_genres": "df4c08cecc066096",
    "movies_inprogress": "70fd54c18e2f24ed",
    "movies_menu": "d1e00532e9031371",
    "movies_random": "70fd54c18e2f24ed",
    "movies_recent": "70fd54c18e2f24ed",
    "movies_sets": "2c34a2e2a4046a47",
    "movies_tag": "70fd54c18e2f24ed",
    "movies_tags": "95110b99fdb6fd55",
    "movies_tags_h": "8e421aeae18d8be6",
    "movies_unwatched": "70fd54c18e2f24ed",
    "movies_year": "70fd54c18e2f24ed",
    "movies_years": "e5bbcabc85827ac2",
    "music_album_artists": "7862a0bb9b419057",
    "music_albums": "1d5673b309336d5f",
    "music_artists": "7862a0bb9b419057",
    "music_favorite_albums": "1d5673b309336d5f",
    "music_genres": "df4c08cecc066096",
    "music_lastplayed": "4314a6e0fb20b388",
    "music_menu": "862e2e2d09afc960",
    "music_playhistory": "2f236c15be769c51",
    "music_recent_albums": "1d5673b309336d5f",
    "next_episodes": "c92e9962cbefdcd0",
    "root": "51cb37ee8dd8351e",
    "search_menu": "1f7daa2c71011f87",
    "search_movies": "cfdff64543ad7601",
    "search_person": "581ee8741dae8a45",
    "season_episodes": "c89bc058e60d8487",
    "series_seasons": "b3d70caf2a36141a",
    "tvshows_all": "9f6409ac3341064b",
    "tvshows_extras": "d369b129ada88895",
    "tvshows_inprogress_episodes": "b7b965409c40f9d2",
    "tvshows_menu": "126a0e312ffb388b",
    "tvshows_nextup": "b7b965409c40f9d2",
    "tvshows_recent_episodes": "b7b965409c40f9d2",
}


def run_route(name):
    handler_name, params = ROUTES[name]
    handler = getattr(browse, handler_name)
    handler(Request("plugin://plugin.video.kofin/", 1, dict(params)))


@pytest.mark.parametrize("name", sorted(ROUTES))
def test_route_golden(name, capture):
    run_route(name)
    actual = digest(capture)
    assert capture["succeeded"] is not None, "%s never closed its handle" % name
    if EXPECTED:
        assert actual == EXPECTED[name], "%s moved: %s -> %s" % (
            name,
            EXPECTED[name],
            actual,
        )


def test_golden_table(capture, monkeypatch):
    """Prints the whole table under ``-s``; asserts it matches EXPECTED."""
    table = {}
    for name in sorted(ROUTES):
        capture["entries"].clear()
        capture["content"] = None
        capture["succeeded"] = None
        capture["sort"].clear()
        run_route(name)
        table[name] = digest(capture)
    print("\nEXPECTED = " + json.dumps(table, indent=4, sort_keys=True))
    if EXPECTED:
        assert table == EXPECTED
