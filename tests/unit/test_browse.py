import pytest

import xbmcplugin

from kofin.plugin import browse
from kofin.plugin.browse import (
    _collection_type,
    _genre_types,
    _guess_content,
    _node_content,
    node_icon,
    node_query,
)
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


def test_collection_type_infers_recordings_when_jellyfin_omits_enum():
    assert (
        _collection_type({"Name": "Recordings", "CollectionType": ""}) == "recordings"
    )
    assert _collection_type({"Name": "Recordings"}) == "recordings"
    assert _collection_type({"Name": "Movies", "CollectionType": "movies"}) == "movies"
    assert node_icon("recordings") == "DefaultPVRRecordings.png"


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


@pytest.fixture
def recording_art(monkeypatch):
    """A ListItem that remembers setArt -- Kodistubs' accepts it and forgets.

    Patched in all three namespaces the listing code reaches ListItem through,
    so an item built by browse or by listitems records either way.
    """
    import xbmcgui

    class RecordingListItem(xbmcgui.ListItem):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._art = {}

        def setArt(self, dictionary):
            self._art.update(dictionary)

        def getArt(self, key):
            return self._art.get(key, "")

    monkeypatch.setattr(xbmcgui, "ListItem", RecordingListItem)
    monkeypatch.setattr(browse.xbmcgui, "ListItem", RecordingListItem)
    monkeypatch.setattr(browse.listitems.xbmcgui, "ListItem", RecordingListItem)
    return RecordingListItem


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


# --- Continue watching (the server's own resume list) -------------------------


class ResumeApi:
    server = "http://server:8096"

    def __init__(self, items=None, fail=False, views=None):
        self.items = items if items is not None else []
        self.fail = fail
        self._views = views or []
        self.resume_calls = []

    def resume(self, fields="", limit=25):
        self.resume_calls.append((fields, limit))
        if self.fail:
            from kofin.core.http import JellyfinError

            raise JellyfinError("down")
        return {"Items": self.items}

    def views(self):
        return {"Items": self._views}

    def device_sessions(self, device_id):
        return []


MOVIE_DTO = {
    "Id": "m1",
    "Name": "Rio Bravo",
    "Type": "Movie",
    "ImageTags": {},
    "RunTimeTicks": 84000000000,
    "UserData": {"PlaybackPositionTicks": 12243410000},
}

EPISODE_DTO = {
    "Id": "e1",
    "Name": "Mole Hunt",
    "Type": "Episode",
    "SeriesName": "Archer",
    "ImageTags": {},
}


def test_continue_watching_lists_the_server_order(monkeypatch, directory):
    """Movies and episodes in one listing, in the order the server sent them:
    most recently played first is what the listing is for."""
    api = ResumeApi(items=[MOVIE_DTO, EPISODE_DTO])
    monkeypatch.setattr(browse, "_api", lambda: api)

    browse.continue_watching(Request("plugin://x", 1, {"mode": "continuewatching"}))

    assert directory["succeeded"] is True
    assert directory["content"] == "videos"
    paths = [path for path, _li, _folder in directory["entries"]]
    assert ["id=m1" in paths[0], "id=e1" in paths[1]] == [True, True]
    assert all("mode=play" in path for path in paths)
    assert [folder for _path, _li, folder in directory["entries"]] == [False, False]
    assert api.resume_calls == [(browse.BROWSE_FIELDS, 25)]


def test_continue_watching_failure_fails_directory(monkeypatch, directory):
    monkeypatch.setattr(browse, "_api", lambda: ResumeApi(fail=True))

    browse.continue_watching(Request("plugin://x", 1, {"mode": "continuewatching"}))

    assert directory["succeeded"] is False


def test_continue_watching_empty_is_still_a_listing(monkeypatch, directory):
    """Nothing in progress is an empty listing, not a failed one -- the entry is
    offered without asking the server first, so arriving at nothing is normal."""
    monkeypatch.setattr(browse, "_api", lambda: ResumeApi(items=[]))

    browse.continue_watching(Request("plugin://x", 1, {"mode": "continuewatching"}))

    assert directory["succeeded"] is True
    assert directory["entries"] == []


def test_root_leads_with_continue_watching(monkeypatch, directory):
    api = ResumeApi(views=[{"Id": "v1", "Name": "Movies", "CollectionType": "movies"}])
    monkeypatch.setattr(browse, "_api", lambda: api)

    browse.root(Request("plugin://x", 1, {}))

    paths = [path for path, _li, _folder in directory["entries"]]
    assert "mode=continuewatching" in paths[0]
    assert directory["entries"][0][2] is True  # a folder to open
    assert "view=v1" in paths[1]  # the libraries follow it


def test_addon_media_joins_under_resources_media(monkeypatch):
    monkeypatch.setattr(browse.settings, "addon_path", lambda: "/tmp/kofin")
    assert browse._addon_media("person-search.png") == (
        "/tmp/kofin/resources/media/person-search.png"
    )


def test_addon_media_empty_when_path_unknown(monkeypatch):
    monkeypatch.setattr(browse.settings, "addon_path", lambda: "")
    assert browse._addon_media("person-search.png") == ""


def test_root_art_icon_and_thumb(monkeypatch, directory, recording_art):
    """Root rows set list icon + focus art: continue watching mirrors stock
    art on both; libraries put stock Default*.png on icon/thumb (Contuary
    ListItem.Icon prefers thumb) and server Primary on poster; action rows
    use addon media for both."""
    FakeAddon.store["syncPlayEnabled"] = "true"
    monkeypatch.setattr(
        "kofin.plugin.syncplay.external_player_configured", lambda: False
    )

    api = ResumeApi(
        views=[
            {
                "Id": "v1",
                "Name": "Movies",
                "CollectionType": "movies",
                "ImageTags": {"Primary": "p1"},
            }
        ]
    )
    monkeypatch.setattr(browse, "_api", lambda: api)

    browse.root(Request("plugin://x", 1, {}))

    by_mode = {}
    for path, li, _folder in directory["entries"]:
        if "mode=" in path:
            mode = path.split("mode=")[1].split("&")[0]
        else:
            mode = ""
        by_mode[mode] = li

    resume = by_mode["continuewatching"]
    assert resume.getArt("icon") == "DefaultInProgressShows.png"
    assert resume.getArt("thumb") == "DefaultInProgressShows.png"

    view = by_mode["browse"]
    assert view.getArt("icon") == "DefaultMovies.png"
    assert view.getArt("thumb") == "DefaultMovies.png"
    assert "Images/Primary" in view.getArt("poster")

    watching = by_mode["adduser"]
    assert watching.getArt("icon").endswith("person-search.png")
    assert watching.getArt("thumb") == watching.getArt("icon")

    syncplay = by_mode["syncplay"]
    assert syncplay.getArt("icon").endswith("syncplay-groups.png")
    assert syncplay.getArt("thumb") == syncplay.getArt("icon")


# --- sub-section rows get the root's icon treatment (PR #39) -----------------


def _by_folder(entries):
    return {path.split("folder=")[1].split("&")[0]: li for path, li, _f in entries}


@pytest.mark.parametrize(
    "media, node, icon",
    [
        ("movies", "all", "DefaultMovies.png"),
        ("movies", "sets", "DefaultSets.png"),
        ("movies", "genres", "DefaultGenre.png"),
        ("tvshows", "recentepisodes", "DefaultRecentlyAddedEpisodes.png"),
        ("tvshows", "nextup", "DefaultInProgressShows.png"),
        ("music", "artists", "DefaultMusicArtists.png"),
        ("music", "albums", "DefaultMusicAlbums.png"),
        ("musicvideos", "all", "DefaultMusicVideos.png"),
    ],
)
def test_node_menu_rows_carry_stock_art_on_both_keys(
    recording_art, directory, media, node, icon
):
    """Contuary binds the list glyph to ListItem.Icon, which prefers thumb --
    so an icon-only row draws nothing. Same rule the root already follows."""
    browse._node_menu(Request("plugin://x", 1, {}), ExtrasApi(), media, "v1")
    li = _by_folder(directory["entries"])[node]
    assert li.getArt("icon") == icon
    assert li.getArt("thumb") == icon


def test_node_menu_leaves_content_empty(recording_art, directory):
    """With "files" the skin switches to ListWatchedIconVar and ignores
    setArt(icon) outright, which is what left these menus glyphless."""
    browse._node_menu(Request("plugin://x", 1, {}), ExtrasApi(), "movies", "v1")
    assert directory["content"] == ""


def test_extras_entry_carries_art_on_both_keys(recording_art, directory):
    browse._append_extras_entry(
        Request("plugin://x", 1, {}), ExtrasApi(series_count=2), "series1"
    )
    li = directory["entries"][0][1]
    assert li.getArt("icon") == "DefaultVideo.png"
    assert li.getArt("thumb") == "DefaultVideo.png"


class GenresApi(ExtrasApi):
    def genres(self, parent_id, include_types=None):
        return {"Items": [{"Type": "Genre", "Id": "g1", "Name": "Drama"}]}


def test_genres_listing_leaves_content_empty():
    items, content = browse._list_items(
        GenresApi(), "movies", "genres", "v1", Request("plugin://x", 1, {})
    )
    assert content == ""
    assert items[0]["Name"] == "Drama"


def test_genre_rows_carry_stock_art_on_both_keys(recording_art, directory):
    """A genre has no server artwork to fall back on, so the stock glyph is
    the whole row -- it has to land on both keys like every other structural
    entry."""
    browse._add_items(
        Request("plugin://x", 1, {}),
        ExtrasApi(),
        [{"Type": "Genre", "Id": "g1", "Name": "Drama"}],
        "v1",
        "movies",
    )
    li = directory["entries"][0][1]
    assert li.getArt("icon") == "DefaultGenre.png"
    assert li.getArt("thumb") == "DefaultGenre.png"


# --- addon backdrop fallback -------------------------------------------------


def test_every_root_row_gets_a_backdrop_including_library_rows(
    monkeypatch, directory, recording_art
):
    """The library rows are the ones this is really about. They carry a
    Primary image, so they *look* covered -- but a Jellyfin UserView has no
    BackdropImageTags, so art_for never sets fanart for one and they bypass
    structural_art entirely. Live, that left the background empty on exactly
    the rows the addon root is mostly made of."""
    FakeAddon.store["syncPlayEnabled"] = "true"
    monkeypatch.setattr(
        "kofin.plugin.syncplay.external_player_configured", lambda: False
    )
    api = ResumeApi(
        views=[
            {
                "Id": "v1",
                "Name": "Movies",
                "CollectionType": "movies",
                "ImageTags": {"Primary": "p1"},
            }
        ]
    )
    monkeypatch.setattr(browse, "_api", lambda: api)

    browse.root(Request("plugin://x", 1, {}))

    backdrop = browse._addon_media(browse.BACKDROP_IMAGE)
    assert backdrop  # the fixture's addon path resolves
    for path, li, _folder in directory["entries"]:
        assert li.getArt("fanart") == backdrop, path


def test_backdrop_never_displaces_real_server_artwork(recording_art):
    """A fallback, not a decoration."""
    own = "https://s/Items/x/Images/Backdrop/0?tag=t"
    assert browse.with_backdrop({"fanart": own})["fanart"] == own

    li = recording_art("x")
    li.setArt({"fanart": own})
    browse.apply_backdrop(li)
    assert li.getArt("fanart") == own


def test_backdrop_fills_a_media_row_that_has_none(recording_art):
    li = recording_art("x")
    browse.apply_backdrop(li)
    assert li.getArt("fanart") == browse._addon_media(browse.BACKDROP_IMAGE)
