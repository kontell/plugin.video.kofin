import pytest

import xbmcplugin

from kofin.plugin import browse
from kofin.plugin.browse import _genre_types, _guess_content, _node_content, node_query
from kofin.plugin.router import Request
from tests.unit.fakes import FakeAddon, FakeWindow


def test_node_query_movies_all():
    query = node_query("movies", "all", "v1")
    assert query["IncludeItemTypes"] == "Movie"
    assert query["ParentId"] == "v1"
    assert query["Recursive"] is True
    assert query["SortBy"] == "SortName"


def test_node_query_recent_limits_and_sorts():
    query = node_query("tvshows", "recentepisodes", "v1")
    assert query["IncludeItemTypes"] == "Episode"
    assert query["SortBy"] == "DateCreated"
    assert query["SortOrder"] == "Descending"
    assert query["Limit"] == 25


def test_node_query_genre_filter():
    query = node_query("movies", "genre-g42", "v1")
    assert query["GenreIds"] == "g42"
    assert query["IncludeItemTypes"] == "Movie"


def test_node_query_special_routes_return_none():
    assert node_query("tvshows", "nextup", "v1") is None
    assert node_query("music", "artists", "v1") is None
    assert node_query("movies", "genres", "v1") is None


def test_node_query_music_albums():
    query = node_query("music", "albums", "v1")
    assert query["IncludeItemTypes"] == "MusicAlbum"
    assert query["SortBy"] == "AlbumArtist,SortName"


def test_content_helpers():
    assert _node_content("tvshows", "nextup") == "episodes"
    assert _node_content("movies", "sets") == "movies"
    assert _node_content("music", "albums") == "albums"
    assert _genre_types("musicvideos") == "MusicVideo"
    assert _guess_content([{"Type": "Photo"}]) == "images"
    assert _guess_content([{"Type": "Unknown"}]) == "videos"


# --- TV extras (phase 3: plugin browse over SpecialFeatures) -----------------


class ExtrasApi:
    server = "http://server:8096"

    def __init__(self, features=None, series_count=0, view_series=None, fail=False):
        self.features = features or []
        self.series_count = series_count
        self.view_series = view_series or []
        self.fail = fail
        self.items_params = []

    def special_features(self, item_id):
        if self.fail:
            from kofin.core.http import JellyfinError

            raise JellyfinError("down")
        return self.features

    def item(self, item_id):
        if self.fail:
            from kofin.core.http import JellyfinError

            raise JellyfinError("down")
        return {"Id": item_id, "SpecialFeatureCount": self.series_count}

    def items(self, params):
        self.items_params.append(params)
        if self.fail:
            from kofin.core.http import JellyfinError

            raise JellyfinError("down")
        return {"Items": self.view_series}


FEATURE = {
    "Id": "extra1",
    "Name": "Blooper Reel",
    "Type": "Video",
    "ImageTags": {},
}

SERIES_DTO = {"Id": "series1", "Name": "The Show", "Type": "Series", "ImageTags": {}}


@pytest.fixture(autouse=True)
def kodi_env(monkeypatch):
    FakeAddon.store = {}
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)


@pytest.fixture
def directory(monkeypatch):
    """Capture what the handlers hand to xbmcplugin."""
    captured = {"entries": [], "content": None, "succeeded": None}

    def add_items(handle, entries, count):
        captured["entries"].extend(entries)
        return True

    monkeypatch.setattr(xbmcplugin, "addDirectoryItems", add_items)
    monkeypatch.setattr(
        xbmcplugin,
        "setContent",
        lambda handle, content: captured.__setitem__("content", content),
    )
    monkeypatch.setattr(
        xbmcplugin,
        "endOfDirectory",
        lambda handle, succeeded=True, **kw: captured.__setitem__(
            "succeeded", succeeded
        ),
    )
    monkeypatch.setattr(xbmcplugin, "addSortMethod", lambda handle, method: None)
    return captured


def test_extras_listing_routes_to_play(monkeypatch, directory):
    api = ExtrasApi(features=[FEATURE])
    monkeypatch.setattr(browse, "_api", lambda: api)

    browse.extras(Request("plugin://x", 1, {"mode": "extras", "id": "series1"}))

    assert directory["succeeded"] is True
    assert directory["content"] == "videos"
    paths = [path for path, _li, _folder in directory["entries"]]
    assert len(paths) == 1
    assert "mode=play" in paths[0] and "id=extra1" in paths[0]
    assert directory["entries"][0][2] is False  # playable, not a folder


def test_extras_listing_failure_fails_directory(monkeypatch, directory):
    api = ExtrasApi(fail=True)
    monkeypatch.setattr(browse, "_api", lambda: api)

    browse.extras(Request("plugin://x", 1, {"mode": "extras", "id": "series1"}))

    assert directory["succeeded"] is False


def test_extras_node_lists_series_with_specials(directory):
    api = ExtrasApi(view_series=[SERIES_DTO])

    browse._extras_node(Request("plugin://x", 1, {}), api, "view1")

    assert directory["content"] == "tvshows"
    paths = [path for path, _li, folder in directory["entries"]]
    assert len(paths) == 1
    assert "mode=extras" in paths[0] and "id=series1" in paths[0]
    assert directory["entries"][0][2] is True  # opens the extras listing
    assert api.items_params[0]["HasSpecialFeature"] is True
    assert api.items_params[0]["IncludeItemTypes"] == "Series"


def test_view_has_specials_probe():
    assert browse._view_has_specials(ExtrasApi(view_series=[SERIES_DTO]), "v1") is True
    assert browse._view_has_specials(ExtrasApi(), "v1") is False
    assert browse._view_has_specials(ExtrasApi(fail=True), "v1") is False


def test_node_menu_includes_extras_when_view_has_specials(directory):
    api = ExtrasApi(view_series=[SERIES_DTO])
    browse._node_menu(Request("plugin://x", 1, {}), api, "tvshows", "view1")
    extras_paths = [
        path for path, _li, _f in directory["entries"] if "folder=extras" in path
    ]
    assert len(extras_paths) == 1

    directory["entries"].clear()
    api = ExtrasApi()  # no specials anywhere: node hidden
    browse._node_menu(Request("plugin://x", 1, {}), api, "tvshows", "view1")
    assert all("folder=extras" not in path for path, _li, _f in directory["entries"])


def test_series_drilldown_appends_extras_entry(directory):
    api = ExtrasApi(series_count=2)
    browse._append_extras_entry(Request("plugin://x", 1, {}), api, "series1")
    assert len(directory["entries"]) == 1
    path = directory["entries"][0][0]
    assert "mode=extras" in path and "id=series1" in path

    directory["entries"].clear()
    browse._append_extras_entry(
        Request("plugin://x", 1, {}), ExtrasApi(series_count=0), "series1"
    )
    assert directory["entries"] == []

    browse._append_extras_entry(
        Request("plugin://x", 1, {}), ExtrasApi(fail=True), "series1"
    )
    assert directory["entries"] == []
