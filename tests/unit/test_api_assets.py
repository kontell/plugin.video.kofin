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

import pytest

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
    assert provider.exists(store, paths.parse(folder), "a", "s2") is True
    assert provider.exists(store, paths.parse(folder), "a", "s9") is False
    assert provider.exists(store, paths.parse(folder), "a", "") is True
    store.publish([movie()], library=LIB)
    assert provider.exists(store, paths.parse(folder), "a", "s2") is False
    assert provider.exists(store, deleted, "") is False
    store.publish([], library=LIB)
    assert provider.exists(store, deleted, "") is False
    assert provider.exists(store, paths.parse(folder), "a", "s2") is False


def test_clean_reads_a_version_file_s_source_from_the_query(store, monkeypatch):
    """Kodi asks about a version file with the folder as argv[0] and the
    query, source included, as argv[2]. Read from argv[0], every version
    file existed and Clean never retired one."""
    from kofin.plugin import router

    store.publish([assets_movie()], library=LIB)
    folder = folder_of(store)
    resolved = []
    monkeypatch.setattr(router.buildconfig, "BACKEND", "api")
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


def test_the_extras_folder_of_a_movie_with_extras_is_bound_noupdate(
    store, backend, kodi, monkeypatch
):
    """An extra's URL has no query, so Kodi files it under the add-on's root,
    and a clean asks about a plugin file only through a binding on its
    folder: without one, Clean library deleted every extra unasked. The scan
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


# -- the companion contract -------------------------------------------------------


def _requests(store):
    import json
    import os

    from kofin.sync import private

    path = os.path.join(private.addon_data_path(), "companion", "requests.json")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def test_ungrouped_versions_are_published_for_the_companion_and_closed_by_readback(
    store, backend, kodi
):
    from kofin.core import contract
    from kofin.sync.backends.api import companion

    from kofin.core import settings
    from tests.unit import contractschema

    settings.set_str("deviceId", "device-1")
    backend.setup()
    store.publish([assets_movie()], library=LIB)
    backend.reconcile()
    owner, version = movie_rows(kodi)
    document = _requests(store)
    assert document["v"] == 2 and document["generation"] == 1
    assert document["namespace"] == store.namespace
    assert document["device"] == "device-1"
    assert document["kinds"] == list(companion.KINDS)
    contractschema.check_document(document, contractschema.load(companion.SCHEMA_PATH))
    by_kind = {r["kind"]: r for r in document["requests"]}
    assert sorted(by_kind) == [companion.GROUP_VERSIONS, companion.RETIRE_ASSETS]
    request = by_kind[companion.GROUP_VERSIONS]
    assert request["kind"] == companion.GROUP_VERSIONS
    assert (request["library"], request["item"]) == (LIB, "a")
    assert request["owner"] == {"movieid": owner["movieid"], "file": owner["file"]}
    assert request["versions"] == [
        {
            "movieid": version["movieid"],
            "file": version["file"],
            "mediasourceid": "s2",
            "name": "Theatrical Cut",
        }
    ]
    (ping,) = kodi.notified
    assert ping["message"] == contract.COMPANION_REQUESTS
    assert ping["sender"] == "plugin.video.kofin"
    assert ping["data"]["v"] == 2
    assert ping["data"]["generation"] == 1 and ping["data"]["count"] == 2
    # The movie's asset rows, as the acknowledged mapping names them: the
    # version file with its name and the extras, for the companion to keep
    # exactly those (retire_assets).
    retire = by_kind[companion.RETIRE_ASSETS]
    assert retire["owner"] == {"movieid": owner["movieid"]}
    assert retire["versions"] == [{"file": version["file"], "name": "Theatrical Cut"}]
    assert [e["file"] for e in retire["extras"]] == [
        paths.extra_url(
            store.namespace, LIB, "a", "Deleted Scene", {"Container": "mkv"}
        ),
        paths.extra_url(store.namespace, LIB, "a", "Making Of", {"Container": "mp4"}),
    ]
    assert store.mapping("a").applied["asset_files"] == {
        "versions": [[version["file"], "Theatrical Cut"]],
        "extras": [e["file"] for e in retire["extras"]],
    }
    assert ping["data"]["path"].endswith("companion/requests.json")
    # The same set again is not rewritten and not announced.
    store.publish([assets_movie(Overview="Changed")], library=LIB)
    backend.reconcile()
    assert len(kodi.notified) == 1 and _requests(store)["generation"] == 1
    # The companion (or the user) grouped the two: the request leaves the
    # document when the readback no longer shows the version's row.
    del kodi.rows["Movie"][version["movieid"]]
    store.publish([assets_movie(Overview="Changed again")], library=LIB)
    backend.reconcile()
    document = _requests(store)
    assert [r["kind"] for r in document["requests"]] == [companion.RETIRE_ASSETS]
    assert document["generation"] == 2
    assert kodi.notified[-1]["data"] == {
        "v": 2,
        "generation": 2,
        "count": 1,
        "path": kodi.notified[-1]["data"]["path"],
    }
    # A movie that loses every version and extra keeps an empty entry, so
    # the companion is told the rows are gone; one that never had any has
    # no entry and no request.
    store.publish([movie("a", Overview="No assets now")], library=LIB)
    backend.reconcile()
    (retire,) = _requests(store)["requests"]
    assert retire["versions"] == [] and retire["extras"] == []
    assert store.mapping("a").applied["asset_files"] == {"versions": [], "extras": []}


def test_assets_lost_under_an_older_acknowledgement_are_still_retired(
    store, backend, kodi
):
    """A build before the file list acknowledged the assets token alone. A
    movie that had assets then and has none on the first pass of this build
    is told to the companion by that token; without it the movie had no
    entry and its version and extra rows waited for Clean."""
    from kofin.sync.backends.api import companion

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
    retire = [
        r for r in _requests(store)["requests"] if r["kind"] == companion.RETIRE_ASSETS
    ]
    assert [(r["item"], r["versions"], r["extras"]) for r in retire] == [("a", [], [])]
    # A movie that never had any still has no entry and no request.
    store.publish([movie("b")], library=LIB)
    backend.reconcile()
    assert "asset_files" not in store.mapping("b").applied


def test_the_companion_is_detected_and_reported_as_optional(store, kodi):
    from kofin.sync.backends.api import companion

    assert companion.present() is None
    kodi.addons[companion.ADDON_ID] = "0.1.0"
    assert companion.present() == "0.1.0"


def test_a_plain_movie_publishes_no_companion_request(store, backend, kodi):
    import os

    from kofin.sync import private

    backend.setup()
    store.publish([movie()], library=LIB)
    backend.reconcile()
    assert kodi.notified == []
    assert not os.path.exists(
        os.path.join(private.addon_data_path(), "companion", "requests.json")
    )


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


def test_the_companion_is_sent_the_label_never_a_file_stem(store, backend, kodi):
    """Both requests that hand the companion a version's name send the label
    the folder listing gave the file. The companion sees a plugin URL for a
    file name and cannot tell a stem from a label itself, so a raw stem
    would still mint a type row per movie (audit A4-1). The assets token
    keeps the raw name: a rename still moves it."""
    from kofin.sync.backends.api import companion

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
    by_kind = {r["kind"]: r for r in _requests(store)["requests"]}
    assert [v["name"] for v in by_kind[companion.GROUP_VERSIONS]["versions"]] == [""]
    assert [v["name"] for v in by_kind[companion.RETIRE_ASSETS]["versions"]] == [""]
    assert [n for _url, n in store.mapping("a").applied["asset_files"]["versions"]] == [
        ""
    ]
    token = store.mapping("a").applied["assets"]
    # A real label is sent as it is.
    item = sourced("Theatrical Cut")
    item["MediaSources"][1]["Path"] = "/srv/movies/alt/Fixture a (2001) - tc.mkv"
    store.publish([item], library=LIB)
    backend.reconcile()
    by_kind = {r["kind"]: r for r in _requests(store)["requests"]}
    assert [v["name"] for v in by_kind[companion.GROUP_VERSIONS]["versions"]] == [
        "Theatrical Cut"
    ]
    assert [v["name"] for v in by_kind[companion.RETIRE_ASSETS]["versions"]] == [
        "Theatrical Cut"
    ]
    assert store.mapping("a").applied["assets"] != token


def test_a_source_named_like_its_file_is_an_untitled_asset(store, monkeypatch):
    """Jellyfin names a source after its file unless a label is known; such a
    name is no version title and would mint a type row per movie in Kodi's
    Versions picker (audit A4-1)."""
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

    class Item:
        def getVideoInfoTag(self):
            return type(
                "Tag", (), {"setVideoAssetTitle": lambda s, n: titles.append(n)}
            )()

    monkeypatch.setattr(provider.metadata, "listitem", lambda it, *a, **k: Item())
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
