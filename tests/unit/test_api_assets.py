"""Movie versions and extras in the API build: files in the movie's folder.

Kodi groups nothing from a plugin source itself (22.0b2): a plugin item's
tag takes the NFO branch of RetrieveInfoForMovie, which never reaches the
similar-video grouping, so a version file imports as a movie of its own
until the user groups it in the Versions Manager; an extras folder is a
phantom disc unless Kodi's directory cache knows it. The pass lists the
folder by name, binds it with folder names on, warms the cache and turns
"Ignore video extras" off; everything else is the user's.
"""

from unittest.mock import MagicMock

from kofin.sync.backends.api import metadata, paths
from tests.unit.apifixtures import (  # noqa: F401
    LIB,
    backend,
    kodi,
    methods,
    movie,
    store,
)

KEY = "c" * 32


def assets_movie(**values):
    item = movie(
        MediaSources=[
            {"Id": "s1", "Name": "Director's Cut", "Container": "mkv"},
            {"Id": "s2", "Name": "Theatrical Cut", "Container": "mkv"},
        ],
        SpecialFeatureCount=2,
        SpecialFeatures=[
            {
                "Id": "x1",
                "Name": "Deleted Scene",
                "Container": "mkv",
                "RunTimeTicks": 60000000,
            },
            {"Id": "x2", "Name": "Making Of", "Container": "mp4"},
        ],
    )
    item.update(values)
    return item


def folder_of(store):
    return paths.movie_dir(store.namespace, LIB, "a")


def movie_rows(kodi):
    return sorted(kodi.rows["Movie"].values(), key=lambda r: r["movieid"])


# -- URLs and tokens --------------------------------------------------------------


def test_version_and_extras_urls_round_trip():
    canonical = paths.playback_url(KEY, "Movie", LIB, "a")
    version = paths.version_url(KEY, LIB, "a", "s2")
    assert paths.without_source(version) == canonical
    assert paths.without_source(canonical) == canonical
    assert paths.is_version_url(version) and not paths.is_version_url(canonical)
    assert paths.parse_item(version)[1] == "a"
    # An extra is a file named by its title, with a video extension Kodi's
    # listing mask accepts, less what a URL path or Kodi's parsing misreads.
    feature = {"Id": "x1", "Name": "What? Part 1/2", "Container": "mov,mp4,m4a"}
    ((stem, _),) = paths.extra_stems([feature])
    url = paths.extra_url(KEY, LIB, "a", stem, feature)
    assert url == paths.extras_dir(KEY, LIB, "a") + "What- Part 1-2.mov"
    location = paths.parse(url)
    assert location is not None
    assert (location.movie, location.extras, location.extra, location.probe) == (
        "a",
        True,
        "What- Part 1-2",
        False,
    )
    # Three locations: the directory, a file in it, a disc-probe folder below.
    directory = paths.parse(paths.extras_dir(KEY, LIB, "a"))
    assert (directory.extras, directory.extra, directory.probe) == (True, None, False)
    probe = paths.parse(paths.extras_dir(KEY, LIB, "a") + "BDMV/")
    assert (probe.movie, probe.extras, probe.extra, probe.probe) == (
        "a",
        True,
        None,
        True,
    )
    assert paths.describe(probe.key and paths.extras_dir(KEY, LIB, "a") + "BDMV/") == (
        "movies movie a extras probe"
    )
    assert paths.extra_name({"Name": "..."}) == "Extra"
    assert paths.video_container_of({"Container": "weird"}) == "mkv"
    assert paths.describe(url) == "movies movie a extras"


def test_extras_with_one_title_get_stems_of_their_own_in_id_order():
    """The stem is the extra's whole identity (Kodi names it by the path), so
    two features titled alike, or alike once sanitized, cannot share one."""
    features = [
        {"Id": "x3", "Name": "Trailer"},
        {"Id": "x1", "Name": "Trailer"},
        {"Id": "x2", "Name": "A/B"},
        {"Id": "x4", "Name": "A-B"},
        {"Id": "x5", "Name": "trailer (2)"},
    ]
    assert [(stem, f["Id"]) for stem, f in paths.extra_stems(features)] == [
        ("Trailer", "x1"),
        ("A-B", "x2"),
        ("Trailer (2)", "x3"),
        ("A-B (2)", "x4"),
        ("trailer (2) (2)", "x5"),
    ]
    # The token tells the two Trailers apart, and the listing's order does not move it.
    one = metadata.assets_token(movie(SpecialFeatures=features))
    assert one == metadata.assets_token(movie(SpecialFeatures=list(reversed(features))))
    assert one != metadata.assets_token(movie(SpecialFeatures=features[1:]))


def test_assets_token_names_versions_and_extras_and_is_empty_without_them():
    assert metadata.assets_token(movie()) == ""
    token = metadata.assets_token(assets_movie())
    renamed = assets_movie()
    renamed["SpecialFeatures"][0]["Name"] = "Deleted Scenes"
    resourced = assets_movie()
    resourced["MediaSources"][1]["Name"] = "Extended Cut"
    assert token
    assert (
        len({token, metadata.assets_token(renamed), metadata.assets_token(resourced)})
        == 3
    )
    # One source is the movie itself, not a version.
    assert metadata.version_sources(movie(MediaSources=[{"Id": "s1"}])) == []
    # The count travels only when it says something.
    assert "SpecialFeatureCount" not in metadata.compact(movie(SpecialFeatureCount=0))
    assert metadata.compact(movie(SpecialFeatureCount=2))["SpecialFeatureCount"] == 2


def test_an_extra_listitem_carries_title_duration_and_streams(monkeypatch):
    tag = MagicMock()
    li = MagicMock()
    li.getVideoInfoTag.return_value = tag
    monkeypatch.setattr(metadata.xbmcgui, "ListItem", lambda *a, **k: li)
    feature = {
        "Id": "x1",
        "Name": "Deleted Scene",
        "Overview": " Cut for time ",
        "RunTimeTicks": 65 * 10_000_000,
        "MediaStreams": [
            {"Type": "Video", "Codec": "h264", "Width": 1280, "Height": 720}
        ],
    }
    assert metadata.extra_listitem(feature, "http://server") is li
    tag.setMediaType.assert_called_once_with("video")
    tag.setTitle.assert_called_once_with("Deleted Scene")
    tag.setPlot.assert_called_once_with("Cut for time")
    tag.setDuration.assert_called_once_with(65)
    assert tag.addVideoStream.call_count == 1


def test_special_features_travel_in_the_movie_payload():
    from kofin.sync.backends.api.library import attach_extras

    class Api:
        calls = []

        def special_features(self, item_id):
            self.calls.append(item_id)
            return [
                {
                    "Id": "x1",
                    "Name": "Deleted Scene",
                    "Container": "mkv",
                    "ImageBlurHashes": {"Primary": {}},
                    "MediaSources": [{"MediaStreams": [{"Type": "Video"}]}],
                },
                {"Name": "no id"},
            ]

    items = [movie(SpecialFeatureCount=1), movie("b")]
    attach_extras(Api(), items)
    assert Api.calls == ["a"]
    assert items[0]["SpecialFeatures"] == [
        {
            "Id": "x1",
            "Name": "Deleted Scene",
            "Container": "mkv",
            "MediaStreams": [{"Type": "Video"}],
        }
    ]
    assert "SpecialFeatures" not in items[1]


# -- the listing ------------------------------------------------------------------


def test_the_movie_folder_lists_versions_and_the_extras_folder(store, monkeypatch):
    from kofin.plugin.router import Request
    from kofin.sync.backends.api import provider

    store.publish([assets_movie()], library=LIB)
    rendered, content, titles = [], [], []

    class Tag:
        def setVideoAssetTitle(self, name):
            titles.append(name)

    class Item:
        def __init__(self, label):
            self.label = label

        def getVideoInfoTag(self):
            return Tag()

    monkeypatch.setattr(
        provider.metadata, "listitem", lambda item, *a, **k: Item(item["Id"])
    )
    monkeypatch.setattr(
        provider.metadata, "extra_listitem", lambda item, server: Item(item["Name"])
    )
    monkeypatch.setattr(
        provider.xbmcplugin,
        "addDirectoryItems",
        lambda h, items, n: rendered.append(list(items)),
    )
    monkeypatch.setattr(
        provider.xbmcplugin, "setContent", lambda h, c: content.append(c)
    )
    monkeypatch.setattr(provider.xbmcplugin, "endOfDirectory", lambda *a, **k: None)
    key = store.namespace
    folder = folder_of(store)
    canonical = paths.playback_url(key, "Movie", LIB, "a")
    provider.serve(Request(folder, 1, {}))
    assert [(e[0], e[2]) for e in rendered[-1]] == [
        (canonical, False),
        (paths.version_url(key, LIB, "a", "s2"), False),
        (folder + "extras/", True),
    ]
    assert titles == ["Director's Cut", "Theatrical Cut"]
    # The root lists the movie's own file alone, with its source's name...
    provider.serve(Request(paths.library_dir(key, LIB, "movies"), 1, {}))
    assert [e[0] for e in rendered[-1]] == [canonical]
    assert titles[-1] == "Director's Cut"
    # ...and so does a refresh, which takes the first item.
    provider.serve(Request(folder, 1, {"kodi_action": "refresh_info", "id": "a"}))
    assert [e[0] for e in rendered[-1]] == [canonical]
    # The extras folder: a file per feature, named by title with its extension.
    provider.serve(Request(folder + "extras/", 1, {}))
    assert [e[0] for e in rendered[-1]] == [
        folder + "extras/Deleted Scene.mkv",
        folder + "extras/Making Of.mp4",
    ]
    assert [e[1].label for e in rendered[-1]] == ["Deleted Scene", "Making Of"]
    assert content[-1] == "videos"
    # The disc folders Kodi probes for below it are empty listings.
    provider.serve(Request(folder + "extras/VIDEO_TS/", 1, {}))
    assert rendered[-1] == []


def test_an_extra_exists_while_its_movie_names_it_and_resolves_to_its_id(store):
    from kofin.sync.backends.api import provider

    store.publish([assets_movie()], library=LIB)
    folder = folder_of(store)
    deleted = paths.parse(folder + "extras/Deleted Scene.mkv")
    assert provider.exists(store, deleted, "") is True
    assert provider.exists(store, paths.parse(folder + "extras/Gone.mkv"), "") is False
    assert provider.exists(store, paths.parse(folder + "extras/BDMV/"), "") is False
    assert provider.resolve_extra(deleted) == "x1"
    assert provider.resolve_extra(paths.parse(folder + "extras/Gone.mkv")) == ""
    # A version file exists while the movie still lists its source; the
    # movie's own file while the movie does.
    assert provider.exists(store, paths.parse(folder), "a", "s2") is True
    assert provider.exists(store, paths.parse(folder), "a", "s9") is False
    assert provider.exists(store, paths.parse(folder), "a", "") is True
    store.publish([movie()], library=LIB)
    assert provider.exists(store, paths.parse(folder), "a", "s2") is False
    assert provider.exists(store, deleted, "") is False
    store.publish([], library=LIB)
    assert provider.exists(store, deleted, "") is False
    assert provider.exists(store, paths.parse(folder), "a", "s2") is False


def test_two_extras_titled_alike_are_listed_played_and_kept_apart(store, monkeypatch):
    """The listing, the existence check and the play route all read the one
    stems list, so the second Trailer is its own file and plays as itself."""
    from kofin.plugin import router
    from kofin.sync.backends.api import provider

    item = assets_movie(SpecialFeatureCount=2)
    item["SpecialFeatures"] = [
        {"Id": "x2", "Name": "Trailer", "Container": "mkv"},
        {"Id": "x1", "Name": "Trailer", "Container": "mkv"},
    ]
    store.publish([item], library=LIB)
    folder = folder_of(store)
    second = paths.parse(folder + "extras/Trailer (2).mkv")
    assert provider.exists(store, second, "") is True
    assert provider.resolve_extra(second) == "x2"
    assert provider.resolve_extra(paths.parse(folder + "extras/Trailer.mkv")) == "x1"
    listed = []
    monkeypatch.setattr(
        provider.xbmcplugin,
        "addDirectoryItems",
        lambda h, items, n=0: listed.extend(url for url, _, _ in items),
    )
    monkeypatch.setattr(provider.xbmcplugin, "setContent", lambda h, c: None)
    monkeypatch.setattr(provider.xbmcplugin, "endOfDirectory", lambda *a, **k: None)
    provider.serve(router.Request(folder + "extras/", 1, {}))
    assert listed == [folder + "extras/Trailer.mkv", folder + "extras/Trailer (2).mkv"]
    # A file the catalogue no longer names is a failed play, never a listing.
    resolved = []
    monkeypatch.setattr(router.buildconfig, "BACKEND", "api")
    monkeypatch.setattr(
        "xbmcplugin.setResolvedUrl", lambda h, ok, item: resolved.append((h, ok))
    )
    monkeypatch.setattr(
        "xbmcplugin.addDirectoryItems", lambda *a, **k: listed.append("listing")
    )
    router.dispatch([folder + "extras/Trailer (3).mkv", 7, ""])
    assert resolved == [(7, False)] and "listing" not in listed


# -- the pass ---------------------------------------------------------------------


def test_versions_and_extras_scan_the_movie_folder_after_the_root(store, backend, kodi):
    backend.setup()
    store.publish([assets_movie()], library=LIB)
    backend.reconcile()
    key = store.namespace
    root = paths.library_dir(key, LIB, "movies")
    folder = folder_of(store)
    # The root walk imports the movie; its folder is then scanned by name,
    # bound with folder names on, which the extras need.
    assert kodi.scanned == [root, folder]
    assert kodi.bindings[folder] == "movies" and kodi.dirnames[folder] is True
    # Kodi's directory cache was warmed for the extras folder and the disc
    # folders probed below it, and "Ignore video extras" turned off, first.
    extras = folder + paths.EXTRAS
    assert kodi.listed == [extras, extras + "VIDEO_TS/", extras + "BDMV/"]
    assert kodi.settings["videolibrary.ignorevideoextras"] is False
    assert not kodi.phantoms
    rows = movie_rows(kodi)
    assert [r["file"] for r in rows] == [
        paths.playback_url(key, "Movie", LIB, "a"),
        paths.version_url(key, LIB, "a", "s2"),
    ]
    assert kodi.extras[rows[0]["movieid"]] == ["Deleted Scene", "Making Of"]
    # The movie's own file owns the identity; the version's row is Kodi's
    # until the user groups it, and nothing is patched on it.
    assert store.mapping("a").kodi_id == rows[0]["movieid"]
    assert store.mapping("a").applied["assets"] == metadata.assets_token(assets_movie())
    assert not [
        p
        for p in methods(kodi, "VideoLibrary.SetMovieDetails")
        if p["movieid"] == rows[1]["movieid"]
    ]
    assert not store.pending()
    backend.reconcile()
    assert kodi.scanned == [root, folder]


def test_new_extras_rescan_the_folder_once_and_a_plain_change_does_not(
    store, backend, kodi
):
    backend.setup()
    store.publish([assets_movie()], library=LIB)
    backend.reconcile()
    folder = folder_of(store)
    item = assets_movie(SpecialFeatureCount=3)
    item["SpecialFeatures"].append(
        {"Id": "x3", "Name": "Trailer Reel", "Container": "mkv"}
    )
    store.publish([item], library=LIB)
    backend.reconcile()
    assert kodi.scanned.count(folder) == 2
    assert kodi.listed.count(folder + paths.EXTRAS) == 2
    owner = store.mapping("a").kodi_id
    assert kodi.extras[owner] == ["Deleted Scene", "Making Of", "Trailer Reel"]
    assert store.mapping("a").applied["assets"] == metadata.assets_token(item)
    store.publish([dict(item, Overview="Changed")], library=LIB)
    backend.reconcile()
    assert kodi.scanned.count(folder) == 2
    assert kodi.rows["Movie"][owner]["plot"] == "Changed"
    assert not store.pending()
    # The last version and extra gone: the folder is scanned once more, so
    # the empty token acknowledged is one a scan presented, and no more.
    store.publish([movie()], library=LIB)
    backend.reconcile()
    assert kodi.scanned.count(folder) == 3
    assert store.mapping("a").applied["assets"] == ""
    store.publish([movie(Overview="Again")], library=LIB)
    backend.reconcile()
    assert kodi.scanned.count(folder) == 3
    assert not store.pending()


def test_every_publish_path_attaches_a_movie_s_features():
    """An enumeration page, a catch-up page and a single re-fetch all carry
    the features: a payload without them reads as a movie without extras."""
    from kofin.sync.backends.api.library import fetch_kind

    class Api:
        user_id = "u"
        calls = []

        def items(self, params):
            return {
                "TotalRecordCount": 1,
                "Items": [movie(SpecialFeatureCount=1)],
            }

        def special_features(self, item_id):
            self.calls.append(item_id)
            return [{"Id": "x1", "Name": "Deleted Scene", "Container": "mkv"}]

    (found,) = fetch_kind(Api(), LIB, "Movie")
    assert Api.calls == ["a"]
    assert [f["Id"] for f in found["SpecialFeatures"]] == ["x1"]


def test_a_version_the_user_made_default_still_owns_the_row(store, backend, kodi):
    backend.setup()
    store.publish([assets_movie()], library=LIB)
    backend.reconcile()
    owner, version = movie_rows(kodi)
    # The user grouped the two in Kodi's Versions Manager and made the
    # Theatrical Cut the default: one row, listed by the version's file.
    del kodi.rows["Movie"][version["movieid"]]
    owner["file"] = version["file"]
    store.publish([assets_movie(Overview="Changed")], library=LIB)
    backend.reconcile()
    assert store.state("a").status == "applied"
    assert store.mapping("a").kodi_id == owner["movieid"]
    assert kodi.rows["Movie"][owner["movieid"]]["plot"] == "Changed"
    assert kodi.scanned.count(folder_of(store)) == 1
    assert not store.pending()


def test_removing_the_movie_takes_its_ungrouped_version_rows_too(store, backend, kodi):
    backend.setup()
    store.publish([assets_movie(), movie("b")], library=LIB)
    backend.reconcile()
    assert len(kodi.rows["Movie"]) == 3
    store.publish([movie("b")], library=LIB)
    backend.reconcile()
    assert [r["file"] for r in kodi.rows["Movie"].values()] == [
        paths.playback_url(store.namespace, "Movie", LIB, "b")
    ]
    assert len(methods(kodi, "VideoLibrary.RemoveMovie")) == 2
    assert kodi.extras == {}
    assert not store.pending()


def test_a_movie_without_versions_or_extras_never_scans_its_folder(
    store, backend, kodi
):
    backend.setup()
    store.publish([movie()], library=LIB)
    backend.reconcile()
    root = paths.library_dir(store.namespace, LIB, "movies")
    store.publish([movie(Overview="Changed")], library=LIB)
    backend.reconcile()
    assert kodi.scanned == [root]
    assert kodi.listed == []
    assert kodi.settings["videolibrary.ignorevideoextras"] is True
    assert store.mapping("a").applied["assets"] == ""
    assert not store.pending()


def test_two_rows_by_the_movie_s_own_file_are_still_a_duplicate(store, backend, kodi):
    from kofin.sync.backends.api.readback import Readback

    backend.setup()
    store.publish([movie()], library=LIB)
    backend.reconcile()
    (row,) = movie_rows(kodi)
    kodi.rows["Movie"][900] = dict(row, movieid=900)
    readback = Readback(store.namespace)
    try:
        readback.scope("Movie", LIB)
    except RuntimeError as error:
        assert "duplicate owned Movie identity" in str(error)
    else:
        raise AssertionError("two rows by the movie's own file were accepted")
