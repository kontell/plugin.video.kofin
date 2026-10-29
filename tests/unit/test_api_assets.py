"""Movie versions and extras: files in the movie's folder.

Kodi groups nothing from a plugin source itself: a plugin item's tag takes
the NFO branch of RetrieveInfoForMovie, which never reaches the
similar-video grouping, so a version file is a version of its movie only
because its tag says so (kontell/xbmc #8). An extras folder is a phantom
disc unless Kodi's directory cache knows it. The pass scans the folder by
name, binds it with folder names on and warms the cache. Kodi files the
extras only while its "Ignore video extras on scan" is off, and that setting
is the viewer's: the tests here run with it off.
"""

from unittest.mock import MagicMock

import pytest

from kofin.sync.backends.api import capabilities, metadata, paths
from kofin.sync.backends.api.capabilities import ALL
from tests.unit.apifixtures import (  # noqa: F401
    LIB,
    backend,
    kodi,
    methods,
    movie,
    store,
)

KEY = "c" * 32


@pytest.fixture(autouse=True)
def kodi23(monkeypatch):
    """Every feature on, for the listings and answers served here; the
    Piers side of versions and extras is test_api_piers.py's."""
    monkeypatch.setattr(capabilities, "features", lambda: ALL)


@pytest.fixture(autouse=True)
def extras_allowed(kodi):
    kodi.settings["videolibrary.ignorevideoextras"] = False


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
    assert location.extra_container == "mov"
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
    one = metadata.assets_token(movie(SpecialFeatures=features), features=ALL)
    assert one == metadata.assets_token(movie(SpecialFeatures=list(reversed(features))))
    assert one != metadata.assets_token(
        movie(SpecialFeatures=features[1:]), features=ALL
    )


def test_assets_token_names_versions_and_extras_and_is_empty_without_them():
    assert metadata.assets_token(movie(), features=ALL) == ""
    token = metadata.assets_token(assets_movie(), features=ALL)
    renamed = assets_movie()
    renamed["SpecialFeatures"][0]["Name"] = "Deleted Scenes"
    resourced = assets_movie()
    resourced["MediaSources"][1]["Name"] = "Extended Cut"
    assert token
    assert (
        len(
            {
                token,
                metadata.assets_token(renamed, features=ALL),
                metadata.assets_token(resourced, features=ALL),
            }
        )
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
    rendered, content, versions = [], [], []

    class Item:
        def __init__(self, label):
            self.label = label

    def listitem(item, *a, version=None, **k):
        versions.append(version)
        return Item(item["Id"])

    monkeypatch.setattr(provider.metadata, "listitem", listitem)
    monkeypatch.setattr(
        provider.metadata,
        "extra_listitem",
        lambda item, server, **kwargs: Item(item["Name"]),
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
    # Each file says it is a version of the movie, under the movie's own
    # ids, the movie's own file the default.
    ids = metadata.unique_ids(store.item("a"), key)
    # A further version leaves the refresh token out: Kodi groups a file
    # under a movie only while none of their unique id types differ.
    grouped = {k: v for k, v in ids.items() if k != "kofinrefresh"}
    assert versions == [
        metadata.Version("Director's Cut", True, ids),
        metadata.Version("Theatrical Cut", False, grouped),
    ]
    # The root lists the movie's own file alone, with its source's name...
    provider.serve(Request(paths.library_dir(key, LIB, "movies"), 1, {}))
    assert [e[0] for e in rendered[-1]] == [canonical]
    assert versions[-1] == metadata.Version("Director's Cut", True, ids)
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
    # The container is part of the file name: a feature that moved to mp4
    # leaves its mkv row to Clean, and the play route still knows the stem.
    moved = paths.parse(folder + "extras/Deleted Scene.mp4")
    assert provider.exists(store, moved, "") is False
    assert provider.resolve_extra(moved) == "x1"
    assert provider.exists(store, paths.parse(folder + "extras/BDMV/"), "") is False
    assert provider.resolve_extra(deleted) == "x1"
    assert provider.resolve_extra(paths.parse(folder + "extras/Gone.mkv")) == ""
    # A version file exists while the movie still lists its source; the
    # movie's own file while the movie does.
    assert provider.exists(store, paths.parse(folder), "a", "s2", ALL) is True
    assert provider.exists(store, paths.parse(folder), "a", "s9", ALL) is False
    assert provider.exists(store, paths.parse(folder), "a", "", ALL) is True
    store.publish([movie()], library=LIB)
    assert provider.exists(store, paths.parse(folder), "a", "s2", ALL) is False
    assert provider.exists(store, deleted, "") is False
    store.publish([], library=LIB)
    assert provider.exists(store, deleted, "") is False
    assert provider.exists(store, paths.parse(folder), "a", "s2", ALL) is False


def test_clean_reads_a_version_file_s_source_from_the_query(store, monkeypatch):
    """Kodi asks about a version file with the folder as argv[0] and the
    query, source included, as argv[2]: read from argv[0], every version
    file would exist."""
    from kofin.plugin import router

    store.publish([assets_movie()], library=LIB)
    folder = folder_of(store)
    resolved = []
    monkeypatch.setattr(
        "xbmcplugin.setResolvedUrl", lambda h, ok, item: resolved.append(ok)
    )
    for source in ("s2", "s9"):
        query = "?id=a&kodi_action=check_exists&mediasourceid=%s&mode=play" % source
        router.dispatch([folder, "1", query, "resume:false"])
    assert resolved == [True, False]


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
    # A file the catalogue does not name is a failed play, never a listing.
    resolved = []
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
    # folders probed below it, first.
    extras = folder + paths.EXTRAS
    assert kodi.listed == [extras, extras + "VIDEO_TS/", extras + "BDMV/"]
    assert not kodi.phantoms
    # One movie: its further version file is filed as a version of it, with
    # the source's label.
    (row,) = movie_rows(kodi)
    assert row["file"] == paths.playback_url(key, "Movie", LIB, "a")
    assert kodi.versions[row["movieid"]] == [
        (paths.version_url(key, LIB, "a", "s2"), "Theatrical Cut")
    ]
    assert kodi.extras[row["movieid"]] == ["Deleted Scene", "Making Of"]
    assert store.mapping("a").kodi_id == row["movieid"]
    assert store.mapping("a").applied["assets"] == metadata.assets_token(
        assets_movie(), features=ALL
    )
    assert not store.pending()
    backend.reconcile()
    assert kodi.scanned == [root, folder]


def test_kodi_ignoring_extras_is_left_alone_and_scans_the_folder_once(
    store, backend, kodi
):
    """Kodi's default: the extras folder is dropped from the listing. The
    setting stays the viewer's, the versions are filed, and the folder is
    not scanned again for extras Kodi will not take."""
    kodi.settings["videolibrary.ignorevideoextras"] = True
    backend.setup()
    store.publish([assets_movie()], library=LIB)
    backend.reconcile()
    folder = folder_of(store)
    assert not methods(kodi, "Settings.SetSettingValue")
    assert kodi.settings["videolibrary.ignorevideoextras"] is True
    (row,) = movie_rows(kodi)
    assert kodi.versions[row["movieid"]]
    assert kodi.extras == {} and not kodi.phantoms
    assert not store.pending()
    scanned = list(kodi.scanned)
    assert folder in scanned
    backend.reconcile()
    assert kodi.scanned == scanned


def test_the_extras_folder_of_a_movie_with_extras_is_bound_noupdate(
    store, backend, kodi, monkeypatch
):
    """An extra's URL has no query, so Kodi files it under the add-on's root,
    and a clean asks about a plugin file only through a binding on its
    folder: without one, Clean library deletes every extra unasked. The scan
    that files the extras binds their folder, noupdate so that Update library
    never scans them as movies, and a profile whose extras an earlier build
    filed has them bound once."""
    from kofin.sync.backends.api import native as native_module
    from tests.unit.fakes import FakeAddon

    FakeAddon.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    store.publish([assets_movie(), movie("b")], library=LIB)
    backend.reconcile()
    extras = paths.extras_dir(store.namespace, LIB, "a")
    assert kodi.bindings[extras] == "movies" and kodi.noupdate[extras] is True
    assert paths.extras_dir(store.namespace, LIB, "b") not in kodi.bindings
    # An earlier build's profile: movie folders bound, extras folders not.
    kodi.bindings.pop(extras)
    store.unbind(extras)
    FakeAddon.store[native_module.MOVIE_BINDINGS_SETTING] = (
        native_module.MOVIE_BINDINGS_STATE
    )
    kodi.scanned.clear()
    kodi.calls.clear()
    backend.bind_movie_folders()
    assert [p["path"] for p in methods(kodi, "VideoLibrary.SetSourceContent")] == [
        extras
    ]
    assert kodi.noupdate[extras] is True and kodi.scanned == []
    assert (
        FakeAddon.store[native_module.MOVIE_BINDINGS_SETTING]
        == native_module.EXTRAS_BINDINGS_STATE
    )
    # Once: later calls read no acknowledgement for it.
    kodi.bindings.pop(extras)
    store.unbind(extras)
    backend.bind_movie_folders()
    assert extras not in kodi.bindings


def test_extras_filed_under_an_older_acknowledgement_are_bound_by_the_payload(
    store, backend, kodi, monkeypatch
):
    """A build before the file list acknowledged the assets token alone, and
    a movie whose token never moves is never planned again to gain the
    list. Its extras folder is bound by what its payload lists: the token
    cannot tell extras from versions, and a movie with versions alone gets
    no extras binding."""
    from kofin.sync.backends.api import native as native_module
    from tests.unit.fakes import FakeAddon

    FakeAddon.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    versions_only = movie(
        "c",
        MediaSources=[
            {"Id": "s1", "Name": "Director's Cut", "Container": "mkv"},
            {"Id": "s2", "Name": "Theatrical Cut", "Container": "mkv"},
        ],
    )
    store.publish([assets_movie(), movie("b"), versions_only], library=LIB)
    backend.reconcile()
    a, c = (paths.extras_dir(store.namespace, LIB, i) for i in "ac")
    for item_id in "ac":
        mapping = store.mapping(item_id)
        legacy = {k: v for k, v in mapping.applied.items() if k != "asset_files"}
        assert legacy["assets"]
        record = store.records(kind="Movie", item_ids=[item_id])[item_id]
        assert store.remember(
            item_id, record.generation, mapping.kodi_id, legacy, "Movie"
        )
    kodi.bindings.pop(a)
    store.unbind(a)
    FakeAddon.store[native_module.MOVIE_BINDINGS_SETTING] = (
        native_module.MOVIE_BINDINGS_STATE
    )
    kodi.calls.clear()
    backend.bind_movie_folders()
    assert [p["path"] for p in methods(kodi, "VideoLibrary.SetSourceContent")] == [a]
    assert kodi.noupdate[a] is True and c not in kodi.bindings
    assert (
        FakeAddon.store[native_module.MOVIE_BINDINGS_SETTING]
        == native_module.EXTRAS_BINDINGS_STATE
    )


def test_a_failed_extras_binding_is_tried_again(store, backend, kodi, monkeypatch):
    """The setting that ends the one-off read is stored after the folders
    are bound, so a binding Kodi refused is asked again."""
    from kofin.sync.backends.api import native as native_module
    from tests.unit.fakes import FakeAddon

    FakeAddon.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    store.publish([assets_movie()], library=LIB)
    backend.reconcile()
    extras = paths.extras_dir(store.namespace, LIB, "a")
    kodi.bindings.pop(extras)
    store.unbind(extras)
    FakeAddon.store[native_module.MOVIE_BINDINGS_SETTING] = (
        native_module.MOVIE_BINDINGS_STATE
    )
    bind_folders = backend.bind_folders

    def refuse_extras(content, folders, **kwargs):
        if extras in folders:
            raise RuntimeError("refused")
        return bind_folders(content, folders, **kwargs)

    monkeypatch.setattr(backend, "bind_folders", refuse_extras)
    with pytest.raises(RuntimeError):
        backend.bind_movie_folders()
    assert (
        FakeAddon.store[native_module.MOVIE_BINDINGS_SETTING]
        == native_module.MOVIE_BINDINGS_STATE
    )
    monkeypatch.setattr(backend, "bind_folders", bind_folders)
    backend.bind_movie_folders()
    assert kodi.bindings[extras] == "movies" and kodi.noupdate[extras] is True
    assert (
        FakeAddon.store[native_module.MOVIE_BINDINGS_SETTING]
        == native_module.EXTRAS_BINDINGS_STATE
    )


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
    assert store.mapping("a").applied["assets"] == metadata.assets_token(
        item, features=ALL
    )
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
    (owner,) = movie_rows(kodi)
    # The user made the Theatrical Cut the default in Kodi's Versions
    # Manager: the row is listed by the version's file.
    owner["file"] = paths.version_url(store.namespace, LIB, "a", "s2")
    store.publish([assets_movie(Overview="Changed")], library=LIB)
    backend.reconcile()
    assert store.state("a").status == "applied"
    assert store.mapping("a").kodi_id == owner["movieid"]
    assert kodi.rows["Movie"][owner["movieid"]]["plot"] == "Changed"
    assert kodi.scanned.count(folder_of(store)) == 1
    assert not store.pending()


def test_removing_the_movie_takes_its_versions_and_extras(store, backend, kodi):
    backend.setup()
    store.publish([assets_movie(), movie("b")], library=LIB)
    backend.reconcile()
    assert len(kodi.rows["Movie"]) == 2
    store.publish([movie("b")], library=LIB)
    backend.reconcile()
    assert [r["file"] for r in kodi.rows["Movie"].values()] == [
        paths.playback_url(store.namespace, "Movie", LIB, "b")
    ]
    assert len(methods(kodi, "VideoLibrary.RemoveMovie")) == 1
    assert kodi.extras == {} and kodi.versions == {}
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


# -- what a movie's folder listed ---------------------------------------------------


def test_a_movies_asset_files_are_acknowledged_by_url(store, backend, kodi):
    """The acknowledgement names the files a movie's folder listed beyond
    its own: the version file with its label, and the extras. A movie that
    loses every version and extra keeps an empty entry, so its last
    acknowledgement does not still list them."""
    backend.setup()
    store.publish([assets_movie()], library=LIB)
    backend.reconcile()
    assert store.mapping("a").applied["asset_files"] == {
        "versions": [
            [paths.version_url(store.namespace, LIB, "a", "s2"), "Theatrical Cut"]
        ],
        "extras": [
            paths.extra_url(
                store.namespace, LIB, "a", "Deleted Scene", {"Container": "mkv"}
            ),
            paths.extra_url(
                store.namespace, LIB, "a", "Making Of", {"Container": "mp4"}
            ),
        ],
    }
    store.publish([movie("a", Overview="No assets now")], library=LIB)
    backend.reconcile()
    assert store.mapping("a").applied["asset_files"] == {"versions": [], "extras": []}


def test_a_dropped_version_and_extra_leave_through_a_clean_of_the_folder(
    store, backend, kodi
):
    """The folder's scan adds and never removes. A pass whose listing of a
    movie lost a version file or an extra cleans that movie's folder, and
    Kodi 23 removes what the add-on says has gone (kontell/xbmc #1, #2,
    #10): only below that folder, and once the pass has applied."""
    backend.setup()
    store.publish([assets_movie(), assets_movie(Id="b")], library=LIB)
    backend.reconcile()
    (row,) = [r for r in movie_rows(kodi) if r["file"].endswith("id=a")]
    assert kodi.versions[row["movieid"]] and len(kodi.extras[row["movieid"]]) == 2
    assert kodi.cleaned == []
    item = assets_movie(
        MediaSources=[{"Id": "s1", "Name": "Director's Cut", "Container": "mkv"}],
        SpecialFeatures=[assets_movie()["SpecialFeatures"][1]],
        SpecialFeatureCount=1,
    )
    store.publish([item, assets_movie(Id="b")], library=LIB)
    kodi.calls.clear()
    backend.reconcile()
    assert not store.pending()
    # A version that went is the folder's, not the movie row's: no refresh.
    assert not methods(kodi, "VideoLibrary.RefreshMovie")
    assert kodi.cleaned == [folder_of(store)]
    assert kodi.versions[row["movieid"]] == []
    assert kodi.extras[row["movieid"]] == ["Making Of"]
    # The other movie's assets were not asked about.
    (other,) = [r for r in movie_rows(kodi) if r["file"].endswith("id=b")]
    assert kodi.versions[other["movieid"]] and len(kodi.extras[other["movieid"]]) == 2
    # A change that drops nothing cleans nothing.
    store.publish([dict(item, Overview="Changed"), assets_movie(Id="b")], library=LIB)
    backend.reconcile()
    assert kodi.cleaned == [folder_of(store)]


def test_a_dropped_asset_is_judged_by_the_files_the_folder_listed():
    from kofin.sync.backends.api.patch import assets_dropped

    listed = {"versions": [["v1", ""], ["v2", "Cut"]], "extras": ["x1"]}
    assert not assets_dropped({}, {"asset_files": listed})
    assert not assets_dropped({"asset_files": listed}, {"asset_files": listed})
    fewer = {"versions": [["v1", ""]], "extras": ["x1"]}
    assert assets_dropped({"asset_files": listed}, {"asset_files": fewer})
    assert not assets_dropped({"asset_files": fewer}, {"asset_files": listed})
    # A label that moved is no file that went.
    relabelled = {"versions": [["v1", ""], ["v2", "Other"]], "extras": ["x1"]}
    assert not assets_dropped({"asset_files": listed}, {"asset_files": relabelled})
    # An acknowledgement older than the file list names no files: a token
    # that moved off a non-empty one may have lost some.
    assert assets_dropped({"assets": "t1"}, {"assets": "t2"})
    assert not assets_dropped({"assets": "t1"}, {"assets": "t1"})
    assert not assets_dropped({"assets": ""}, {"assets": "t2"})


def test_assets_lost_under_an_older_acknowledgement_are_still_recorded(
    store, backend, kodi
):
    """A build before the file list acknowledged the assets token alone. A
    movie that had assets then and has none on the first pass of this build
    is known by that token to have had them; one that never had any has no
    entry."""
    backend.setup()
    store.publish([assets_movie()], library=LIB)
    backend.reconcile()
    mapping = store.mapping("a")
    legacy = {k: v for k, v in mapping.applied.items() if k != "asset_files"}
    assert legacy["assets"]
    record = store.records(kind="Movie", item_ids=["a"])["a"]
    assert store.remember("a", record.generation, mapping.kodi_id, legacy, "Movie")
    assert "asset_files" not in store.mapping("a").applied
    store.publish([movie("a", Overview="No assets now")], library=LIB)
    backend.reconcile()
    assert store.mapping("a").applied["asset_files"] == {"versions": [], "extras": []}
    store.publish([movie("b")], library=LIB)
    backend.reconcile()
    assert "asset_files" not in store.mapping("b").applied


def test_the_companions_requests_are_deleted_at_start(store, tmp_path):
    """What the Piers build kept for the companion add-on goes the first time
    this build starts; a companion still installed reads a missing document
    as one that asks for nothing."""
    from kofin.sync.backends.api import forget_companion

    folder = tmp_path / "companion"
    (folder / "chapters" / "x").mkdir(parents=True)
    (folder / "requests.json").write_text("{}")
    (folder / "sets.json").write_text("{}")
    forget_companion()
    assert not folder.exists()
    forget_companion()  # nothing left to delete is no error


def test_a_version_file_lists_its_own_streams_and_runtime(store, monkeypatch):
    from kofin.plugin.router import Request
    from kofin.sync.backends.api import metadata, provider

    item = assets_movie()
    item["MediaSources"][1].update(
        RunTimeTicks=720000000,
        MediaStreams=[
            {"Type": "Video", "Codec": "hevc", "Width": 3840, "Height": 2160}
        ],
    )
    store.publish([item], library=LIB)
    shown = []

    class Item:
        def getVideoInfoTag(self):
            return type("Tag", (), {"setVideoAssetTitle": lambda s, n: None})()

    monkeypatch.setattr(
        provider.metadata, "listitem", lambda it, *a, **k: shown.append(it) or Item()
    )
    monkeypatch.setattr(provider.xbmcplugin, "addDirectoryItems", lambda *a: None)
    monkeypatch.setattr(provider.xbmcplugin, "setContent", lambda *a: None)
    monkeypatch.setattr(provider.xbmcplugin, "endOfDirectory", lambda *a, **k: None)
    provider.serve(Request(folder_of(store), 1, {}))
    canonical, version = shown
    assert canonical["RunTimeTicks"] == 1200000000
    assert version["RunTimeTicks"] == 720000000
    assert version["MediaStreams"][0]["Codec"] == "hevc"
    assert [s["Id"] for s in version["MediaSources"]] == ["s2"]
    # The payload keeps each source's streams only for a movie with several.
    assert "MediaStreams" in metadata.compact(item)["MediaSources"][1]
    assert (
        "MediaStreams"
        not in metadata.compact(
            movie(MediaSources=[{"Id": "s1", "MediaStreams": [{"Type": "Video"}]}])
        )["MediaSources"][0]
    )


def test_a_versions_label_is_acknowledged_never_a_file_stem(store, backend, kodi):
    """The acknowledged file list carries the label the folder listing gave
    the version file, never a raw stem. The assets token keeps the raw name:
    a rename still moves it."""

    def sourced(second):
        item = assets_movie()
        item["MediaSources"][0].update(
            Name="Fixture a (2001)",
            Path="/srv/movies/Fixture a (2001)/Fixture a (2001).mkv",
        )
        item["MediaSources"][1].update(
            Name=second, Path="/srv/movies/alt/%s.mkv" % second
        )
        return item

    backend.setup()
    store.publish([sourced("Fixture a (2001) alt")], library=LIB)
    backend.reconcile()
    assert [n for _url, n in store.mapping("a").applied["asset_files"]["versions"]] == [
        ""
    ]
    token = store.mapping("a").applied["assets"]
    # A real label is kept as it is.
    item = sourced("Theatrical Cut")
    item["MediaSources"][1]["Path"] = "/srv/movies/alt/Fixture a (2001) - tc.mkv"
    store.publish([item], library=LIB)
    backend.reconcile()
    assert [n for _url, n in store.mapping("a").applied["asset_files"]["versions"]] == [
        "Theatrical Cut"
    ]
    assert store.mapping("a").applied["assets"] != token


def test_a_source_named_like_its_file_is_an_untitled_asset(store, monkeypatch):
    """Jellyfin names a source after its file unless a label is known; such a
    name is no version title and would mint a type row per movie in Kodi's
    Versions picker."""
    from kofin.plugin.router import Request
    from kofin.sync.backends.api import provider

    item = assets_movie()
    item["MediaSources"][0].update(
        Name="Fixture a (2001)",
        Path="/srv/movies/Fixture a (2001)/Fixture a (2001).mkv",
    )
    item["MediaSources"][1].update(
        Name="Theatrical Cut",
        Path="/srv/movies/Fixture a (2001)/Fixture a (2001) - Theatrical Cut.mkv",
    )
    store.publish([item], library=LIB)
    titles = []

    def listitem(it, *a, version=None, **k):
        if version is not None and version.label:
            titles.append(version.label)

    monkeypatch.setattr(provider.metadata, "listitem", listitem)
    monkeypatch.setattr(provider.xbmcplugin, "addDirectoryItems", lambda *a: None)
    monkeypatch.setattr(provider.xbmcplugin, "setContent", lambda *a: None)
    monkeypatch.setattr(provider.xbmcplugin, "endOfDirectory", lambda *a, **k: None)
    provider.serve(Request(folder_of(store), 1, {}))
    assert titles == ["Theatrical Cut"]
    assert metadata.version_label({"Name": "Standard Edition"}) == ""
    assert (
        metadata.version_label({"Name": "Film", "Path": "C:\\\\Movies\\\\Film.mkv"})
        == ""
    )
    assert (
        metadata.version_label({"Name": "Director's Cut", "Path": "/x/Film.mkv"})
        == "Director's Cut"
    )
