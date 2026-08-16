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


def test_node_query_fields_follow_boundedness():
    """MediaStreams rides only bounded listings: on a whole-library node it
    multiplied the payload 3.6x and the server time 3x (perf plan W2.1), and
    music never reads stream details at all (_fill_music)."""
    unbounded = ["all", "unwatched", "favorites", "sets", "genre-g1"]
    for node in unbounded:
        assert "MediaStreams" not in node_query("movies", node, "v1")["Fields"], node
    for node in ("recent", "inprogress", "random"):
        assert "MediaStreams" in node_query("movies", node, "v1")["Fields"], node
    for node in ("recentepisodes", "inprogressepisodes", "random"):
        assert "MediaStreams" in node_query("tvshows", node, "v1")["Fields"], node
    # Bounded but music: still no stream details.
    assert "MediaStreams" not in node_query("music", "recentalbums", "v1")["Fields"]


def test_season_episodes_keep_stream_details():
    """One season is exactly the bounded listing where per-row codec flags
    matter; the drill-down must ask for them."""
    captured = {}

    class EpisodesApi:
        server = "http://s:8096"

        def episodes(self, series_id, season_id, fields):
            captured["fields"] = fields
            return {"Items": []}

    browse._list_items(
        EpisodesApi(),
        "season",
        "season1",
        "v1",
        Request("plugin://x", 1, {"series": "show1"}),
    )
    assert "MediaStreams" in captured["fields"]


def test_generic_children_stay_slim():
    """A folder/boxset/playlist drill-down is unbounded (a playlist can hold a
    thousand rows), so it takes the slim field list."""
    captured = {}

    class ChildrenApi:
        server = "http://s:8096"

        def items(self, params):
            captured["params"] = params
            return {"Items": []}

    browse._list_items(
        ChildrenApi(), "boxset", "set1", "v1", Request("plugin://x", 1, {})
    )
    assert "MediaStreams" not in captured["params"]["Fields"]


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

# Carries the count because kofin now asks for it and filters on it itself:
# the server's HasSpecialFeature filter does not work for series (see
# browse.specials_only), so a DTO that arrived without it is not evidence of
# anything.
SERIES_DTO = {
    "Id": "series1",
    "Name": "The Show",
    "Type": "Series",
    "ImageTags": {},
    "SpecialFeatureCount": 2,
}


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
    assert "SpecialFeatureCount" in api.items_params[0]["Fields"]
    assert "HasSpecialFeature" not in api.items_params[0]  # does not work for TV
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
    # A 25-row listing keeps stream details (BROWSE_FIELDS_STREAMS): bounded
    # payload, and resume rows are where codec/HDR flags are most looked at.
    assert api.resume_calls == [(browse.BROWSE_FIELDS_STREAMS, 25)]


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
    """The two ways in that are not a place come first, then the libraries."""
    api = ResumeApi(views=[{"Id": "v1", "Name": "Movies", "CollectionType": "movies"}])
    monkeypatch.setattr(browse, "_api", lambda: api)

    browse.root(Request("plugin://x", 1, {}))

    paths = [path for path, _li, _folder in directory["entries"]]
    assert "mode=continuewatching" in paths[0]
    assert directory["entries"][0][2] is True  # a folder to open
    assert "mode=search" in paths[1]
    assert "view=v1" in paths[2]  # the libraries follow them


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


def test_whos_watching_label_reads_the_published_names(monkeypatch):
    """The root renders the label from the property the service maintains
    (connect-time restore, picker confirm) — no /Sessions round trip per
    root render, which also hung the offline root for the call's whole
    retry ladder (perf plan W1.4)."""
    from kofin.core import state

    monkeypatch.setattr(
        browse.settings, "localized", lambda sid: "with %s" if sid == 30046 else "base"
    )
    assert browse._who_is_watching_label() == "base"
    state.set_watching_names(["Bob", "Dan"])
    assert browse._who_is_watching_label() == "with Bob, Dan"


def test_root_lists_whos_watching_unless_the_shortlist_is_empty(monkeypatch, directory):
    """Nothing on the Advanced-tab shortlist switches the feature off, and the
    root entry is the half of that the user sees (plugin/adduser.py). Empty is
    the pre-sentinel spelling of "everyone" and still has to list it."""
    from kofin.plugin import adduser

    api = ResumeApi(views=[])
    monkeypatch.setattr(browse, "_api", lambda: api)

    listed = {}
    for stored in (adduser.SHORTLIST_ALL, "u2", "", adduser.SHORTLIST_NOBODY):
        FakeAddon.store["whoIsWatchingShortlist"] = stored
        directory["entries"].clear()
        browse.root(Request("plugin://x", 1, {}))
        listed[stored] = any(
            "mode=adduser" in path for path, _li, _folder in directory["entries"]
        )

    assert listed == {
        adduser.SHORTLIST_ALL: True,
        "u2": True,
        "": True,
        adduser.SHORTLIST_NOBODY: False,
    }


def test_add_items_reads_the_resume_offset_once_per_listing(monkeypatch, directory):
    """One settings read for the whole page, however many rows it has — the
    per-row read built a fresh Addon each time and dominated large listings
    (perf plan W1.1)."""
    reads = []
    monkeypatch.setattr(
        browse.settings, "resume_offset", lambda: reads.append(1) or 0.0
    )
    items = [
        {
            "Id": "m%d" % index,
            "Type": "Movie",
            "Name": "Movie %d" % index,
            "RunTimeTicks": 600 * 10_000_000,
            "UserData": {"PlaybackPositionTicks": 300 * 10_000_000},
        }
        for index in range(5)
    ]
    browse._add_items(Request("plugin://x", 1, {}), ExtrasApi(), items, "v1", "movies")

    assert len(directory["entries"]) == 5
    assert len(reads) == 1


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


def test_backdrop_fills_a_row_that_has_none(recording_art):
    li = recording_art("x")
    browse.apply_backdrop(li)
    assert li.getArt("fanart") == browse._addon_media(browse.BACKDROP_IMAGE)


def test_media_rows_never_take_the_addon_backdrop(recording_art, directory):
    """A DVR recording is the case this is about: Primary thumbnail, no
    BackdropImageTags, so art_for sets no fanart and every row in the library
    drew the addon's own artwork as if it were the recording's."""
    browse._add_items(
        Request("plugin://x", 1, {}),
        ExtrasApi(),
        [
            {
                "Type": "Movie",
                "Id": "r1",
                "Name": "Nations Champ Rugby",
                "ImageTags": {"Primary": "p1"},
            }
        ],
        "v1",
        "",
    )
    li = directory["entries"][0][1]
    assert li.getArt("fanart") == ""


def test_media_rows_keep_their_own_backdrop(recording_art, directory):
    browse._add_items(
        Request("plugin://x", 1, {}),
        ExtrasApi(),
        [{"Type": "Movie", "Id": "m1", "Name": "Heat", "BackdropImageTags": ["b1"]}],
        "v1",
        "movies",
    )
    li = directory["entries"][0][1]
    assert li.getArt("fanart").endswith("/Items/m1/Images/Backdrop/0?tag=b1")


def test_structural_rows_in_a_listing_still_take_the_backdrop(recording_art, directory):
    """The other half of the rule: a genre stands for a query, has no artwork
    of its own, and would otherwise leave the background empty."""
    browse._add_items(
        Request("plugin://x", 1, {}),
        ExtrasApi(),
        [{"Type": "Genre", "Id": "g1", "Name": "Drama"}],
        "v1",
        "movies",
    )
    li = directory["entries"][0][1]
    assert li.getArt("fanart") == browse._addon_media(browse.BACKDROP_IMAGE)


def test_series_without_specials_are_filtered_here_not_by_the_server():
    """HasSpecialFeature=true matches no series at all on 10.11, even one whose
    folder holds two extras that /SpecialFeatures returns and whose
    SpecialFeatureCount reads 2 — verified before and after a full library
    rescan. Trusting it meant the Extras node could never appear for TV."""
    items = [
        {"Id": "a", "SpecialFeatureCount": 2},
        {"Id": "b", "SpecialFeatureCount": 0},
        {"Id": "c"},  # field absent entirely
    ]
    assert [item["Id"] for item in browse.specials_only(items)] == ["a"]


def test_the_specials_probe_asks_for_counts_not_artwork(directory):
    """It runs on every TV node menu, so it fetches the counts and nothing
    else: 36 KB and 38 ms against a 78-series view."""
    api = ExtrasApi(view_series=[SERIES_DTO])
    browse._view_has_specials(api, "v1")
    params = api.items_params[0]
    assert params["Fields"] == "SpecialFeatureCount"
    assert params["EnableImages"] is False
    assert params["EnableUserData"] is False
    assert "HasSpecialFeature" not in params


class SearchApi:
    """Records what search asked the server for."""

    server = "http://server:8096"

    def __init__(self, items=None, persons=None, fail=False):
        self._items = items if items is not None else []
        self._persons = persons if persons is not None else []
        self.queries = []
        self.person_calls = []
        self.fail = fail

    def items(self, params):
        self.queries.append(params)
        if self.fail:
            from kofin.core.http import JellyfinError

            raise JellyfinError("down")
        return {"Items": self._items}

    def persons(self, term, limit=100):
        self.person_calls.append((term, limit))
        return {"Items": self._persons}


def test_search_with_no_type_is_a_menu_and_asks_nothing(monkeypatch, directory):
    """The menu shape opens no dialog, which is what makes it safe as a node
    or a widget: Kodi runs a node's <path> through CDirectory::GetDirectory
    and a modal fights that fetch."""
    monkeypatch.setattr(browse, "_api", lambda: SearchApi())
    monkeypatch.setattr(
        browse.xbmcgui, "Dialog", lambda: pytest.fail("the menu must not prompt")
    )

    browse.search(Request("plugin://x", 1, {}))

    paths = [path for path, _li, folder in directory["entries"]]
    assert len(paths) == len(browse.SEARCH_KINDS)
    for kind in browse.SEARCH_KINDS:
        assert any("type=%s" % kind in path for path in paths), kind
    assert all(folder for _path, _li, folder in directory["entries"])
    assert directory["succeeded"] is True


def test_search_with_a_query_does_not_prompt(monkeypatch, directory):
    """The term is a parameter, so a skin's own search box can address the
    route directly and a node can carry a fixed search."""
    api = SearchApi(items=[{"Id": "m1", "Name": "Dune", "Type": "Movie"}])
    monkeypatch.setattr(browse, "_api", lambda: api)
    monkeypatch.setattr(
        browse.xbmcgui, "Dialog", lambda: pytest.fail("a given query must not prompt")
    )

    browse.search(Request("plugin://x", 1, {"type": "movies", "query": "dune"}))

    assert api.queries[0]["searchTerm"] == "dune"
    assert api.queries[0]["IncludeItemTypes"] == "Movie"
    assert api.queries[0]["Recursive"] is True
    assert api.queries[0]["Limit"] == browse.SEARCH_LIMIT
    assert directory["content"] == "movies"
    assert directory["succeeded"] is True


def test_search_prompts_when_the_query_is_missing(monkeypatch, directory):
    api = SearchApi(items=[])
    monkeypatch.setattr(browse, "_api", lambda: api)
    monkeypatch.setattr(
        browse.xbmcgui,
        "Dialog",
        lambda: type("D", (), {"input": lambda *a, **k: "rat"})(),
    )

    browse.search(Request("plugin://x", 1, {"type": "episodes"}))

    assert api.queries[0]["searchTerm"] == "rat"
    assert api.queries[0]["IncludeItemTypes"] == "Episode"


def test_search_cancelled_at_the_keyboard_fails_the_fetch(monkeypatch, directory):
    """An empty listing would strand the viewer in a results screen they
    never asked for; a failed fetch returns them to where they were."""
    api = SearchApi()
    monkeypatch.setattr(browse, "_api", lambda: api)
    monkeypatch.setattr(
        browse.xbmcgui, "Dialog", lambda: type("D", (), {"input": lambda *a, **k: ""})()
    )

    browse.search(Request("plugin://x", 1, {"type": "movies"}))

    assert api.queries == []
    assert directory["succeeded"] is False


def test_search_people_lead_to_their_own_filmography(monkeypatch, directory):
    api = SearchApi(persons=[{"Id": "p1", "Name": "Nic Cage", "Type": "Person"}])
    monkeypatch.setattr(browse, "_api", lambda: api)

    browse.search(Request("plugin://x", 1, {"type": "people", "query": "cage"}))

    assert api.person_calls == [("cage", browse.SEARCH_LIMIT)]
    path, _li, folder = directory["entries"][0]
    assert "person=p1" in path
    assert folder is True


def test_search_person_lists_what_they_are_in(monkeypatch, directory):
    api = SearchApi(items=[{"Id": "m1", "Name": "Con Air", "Type": "Movie"}])
    monkeypatch.setattr(browse, "_api", lambda: api)

    browse.search(Request("plugin://x", 1, {"person": "p1"}))

    assert api.queries[0]["PersonIds"] == "p1"
    assert directory["content"] == "videos"


def test_search_failure_fails_the_fetch(monkeypatch, directory):
    monkeypatch.setattr(browse, "_api", lambda: SearchApi(fail=True))

    browse.search(Request("plugin://x", 1, {"type": "movies", "query": "dune"}))

    assert directory["succeeded"] is False


# --- browse depth: alphabet, years, tags, play history ----------------------


def _folder_of(path):
    """The folder key a listing row points at."""
    import urllib.parse

    return urllib.parse.parse_qs(urllib.parse.urlparse(path).query)["folder"][0]


def test_node_query_alphabet():
    query = node_query("movies", "alpha-D", "v1")
    assert query["NameStartsWith"] == "D"
    assert query["IncludeItemTypes"] == "Movie"
    assert "NameLessThan" not in query


def test_node_query_alphabet_hash_bucket():
    """ "#" has no NameStartsWith character; everything before A is what it
    means, and NameLessThan is what Jellyfin answers that with."""
    query = node_query("movies", "alpha-#", "v1")
    assert query["NameLessThan"] == "A"
    assert "NameStartsWith" not in query


def test_node_query_alphabet_over_music_lists_artists():
    """The music tree is albums, but an alphabet over music is how you find a
    performer -- so this leg switches the item type as well."""
    query = node_query("music", "alpha-B", "v1")
    assert query["IncludeItemTypes"] == "MusicArtist"
    assert browse._node_content("music", "alpha-B") == "artists"


def test_node_query_year_and_tag():
    assert node_query("movies", "year-1984", "v1")["Years"] == "1984"
    assert node_query("movies", "tag-heist", "v1")["Tags"] == "heist"


def test_node_query_play_history_is_songs():
    """Filters=IsPlayed against MusicAlbum returns nothing, so both history
    nodes list songs (verified live: 7,176 played of 20,802)."""
    for node, sort in (("lastplayed", "DatePlayed"), ("topsongs", "PlayCount")):
        query = node_query("music", node, "v1")
        assert query["IncludeItemTypes"] == "Audio", node
        assert query["Filters"] == "IsPlayed", node
        assert query["SortBy"] == sort, node
        assert query["SortOrder"] == "Descending", node
        assert query["Limit"] == browse.PLAY_HISTORY_LIMIT, node
        assert browse._node_content("music", node) == "songs", node


def test_node_label_picks_the_right_string_table(monkeypatch):
    """NODES mixes the addon's ids with Kodi's own. Asking the addon for a core
    id returns an empty string, which renders as a nameless row."""
    monkeypatch.setattr(browse.settings, "localized", lambda i: "addon-%d" % i)
    import xbmc

    monkeypatch.setattr(xbmc, "getLocalizedString", lambda i: "core-%d" % i)

    assert browse.node_label(30030) == "addon-30030"  # All
    assert browse.node_label(652) == "core-652"  # Years
    assert browse.node_label(20459) == "core-20459"  # Tags
    assert browse.node_label(38043) == "core-38043"  # Album artists


def test_alphabet_menu_asks_the_server_nothing(monkeypatch, directory):
    browse._alpha_menu(Request("plugin://x", 1, {}), "movies", "v1")

    # Kodistubs' ListItem forgets its label, so the paths are the evidence.
    keys = [_folder_of(path) for path, _li, _f in directory["entries"]]
    assert keys[0] == "alpha-#"
    assert keys[1:] == ["alpha-%s" % c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
    assert all(folder for _p, _li, folder in directory["entries"])


class FiltersApi:
    server = "http://server:8096"

    def __init__(self, years=None, tags=None):
        self.payload = {"Years": years or [], "Tags": tags or []}

    def filters(self, parent_id, item_type=""):
        return self.payload


def test_years_menu_is_newest_first(monkeypatch, directory):
    api = FiltersApi(years=[1970, 2020, 1999])

    browse._filter_menu(Request("plugin://x", 1, {}), api, "movies", "v1", "years")

    assert [_folder_of(p) for p, _li, _f in directory["entries"]] == [
        "year-2020",
        "year-1999",
        "year-1970",
    ]


def test_a_short_tag_list_stays_flat(monkeypatch, directory):
    api = FiltersApi(tags=["heist", "noir"])

    browse._filter_menu(Request("plugin://x", 1, {}), api, "movies", "v1", "tags")

    assert [_folder_of(p) for p, _li, _f in directory["entries"]] == [
        "tag-heist",
        "tag-noir",
    ]


def test_a_long_tag_list_becomes_initials(monkeypatch, directory):
    """Measured on a real library: 7,794 tags, mostly scraped keywords. A flat
    menu of that is not a menu, and it costs a ListItem per row."""
    tags = ["heist", "noir"] + ["tag%03d" % n for n in range(browse.TAG_MENU_MAX)]
    api = FiltersApi(tags=tags)

    browse._filter_menu(Request("plugin://x", 1, {}), api, "movies", "v1", "tags")

    # Only the initials that have tags behind them.
    assert [_folder_of(p) for p, _li, _f in directory["entries"]] == [
        "tags-H",
        "tags-N",
        "tags-T",
    ]


def test_playlists_get_their_own_glyph_and_empty_content():
    """A playlist is a place, not a piece of media. With a media content type
    the skin draws watched-status overlays and ignores setArt(icon), which is
    what a playlist listing looked like -- thirteen identical squares."""
    assert _guess_content([{"Type": "Playlist"}, {"Type": "Playlist"}]) == ""
    # A mixed listing still describes its media.
    assert _guess_content([{"Type": "Playlist"}, {"Type": "Movie"}]) == "movies"
