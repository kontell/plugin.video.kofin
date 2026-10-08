"""Public movie lifecycle and recovery, with no native database connection."""

import copy
from types import SimpleNamespace

import pytest

from kofin.sync import private
from kofin.sync.backends.api import metadata
from kofin.sync.backends.api.library import fetch_movies
from kofin.sync.backends.api.movies import Movies
from kofin.sync.backends.api.store import MovieStore, identity, namespace, playback_url
from kofin.sync.catalogue import BackendMismatch


def movie(item_id="a", **values):
    item = dict(
        Id=item_id,
        Type="Movie",
        Name="Fixture " + item_id,
        Overview="Original",
        RunTimeTicks=1200000000,
        UserData={"Played": False, "PlaybackPositionTicks": 120000000},
        MediaStreams=[
            {"Type": "Video", "Codec": "h264", "Width": 1920, "Height": 1080}
        ],
        People=[{"Name": "Fixture actor", "Type": "Actor", "Role": "Original"}],
    )
    item.update(values)
    return item


@pytest.fixture
def store(tmp_path, monkeypatch):
    private.reset_overrides()
    private.set_path_override("kofin", str(tmp_path / "kofin.db"))
    monkeypatch.setattr(private, "addon_data_path", lambda: str(tmp_path))
    store = MovieStore(namespace("server", "user"))
    store.initialize("http://fixture.invalid")
    yield store
    private.reset_overrides()


def test_publication_pin_tombstone_restart_and_old_ack(store):
    store.publish([movie()], library="one")
    original = store.pin()
    store.publish([movie(Overview="New")], library="one")
    assert store.snapshot() == original
    assert store.snapshot(False)[0]["a"]["item"]["Overview"] == "New"
    assert not store.remember("a", 1, 10, movie())
    reopened = MovieStore(store.namespace)
    assert reopened.state("a").applied == 0
    assert reopened.snapshot() == original
    reopened.unpin()
    assert reopened.snapshot()[0]["a"]["generation"] == 2
    reopened.publish([], library="one")
    assert reopened.snapshot()[0] == {}
    assert reopened.state("a").operation == "remove"
    assert len(reopened.pending()) == 1


def test_invalid_publication_is_atomic(store):
    store.publish([movie()], library="one")
    before = store.snapshot()
    with pytest.raises(ValueError):
        store.publish([movie("b"), movie("c", Type="Episode")], library="one")
    assert store.snapshot() == before
    assert store.state("b") is None
    with pytest.raises(ValueError):
        store.publish([movie("b")])
    assert store.snapshot() == before
    assert store.state("b") is None


def test_sorttitle_whitespace_does_not_keep_native_work_pending(
    store, native, monkeypatch
):
    backend, kodi = native
    original_rpc = kodi.rpc

    def trimming_setter(method, params=None):
        if method == "VideoLibrary.SetMovieDetails":
            params = dict(params, sorttitle=params["sorttitle"].strip(" \t\n\r\v\f"))
        return original_rpc(method, params)

    from kofin.sync.backends.api import movies

    monkeypatch.setattr(movies, "rpc", trimming_setter)
    store.publish([movie(SortName="  Fixture sort title \t")], library="one")
    backend.reconcile()
    assert not store.pending()
    row = kodi.rows[store.mapping("a")[0]]
    assert row["sorttitle"] == "Fixture sort title"
    backend.reconcile(repair=True)
    assert not store.pending()


def test_native_metadata_normalizes_tags_studios_and_release_year():
    item = movie(
        Name=" Fixture title ",
        SortName=" Fixture sort \t",
        Tags=[" Tag ", "Tag", " "],
        Studios=[{"Name": " Studio one / Studio two "}],
        ProductionYear=1999,
        PremiereDate="2000-01-02T00:00:00Z",
    )
    result = metadata.details(item, "", "key", "library")
    assert result["title"] == "Fixture title"
    assert result["sorttitle"] == "Fixture sort"
    assert result["tag"] == ["Tag", "kofin.library.library"]
    assert result["studio"] == ["Studio one", "Studio two"]
    assert result["year"] == 2000
    assert item["ProductionYear"] == 1999
    result = metadata.details(item, "", "key", "library", separator=" | ")
    assert result["studio"] == ["Studio one / Studio two"]


def test_native_array_separator_uses_public_tag_api(monkeypatch):
    values = []
    tag = SimpleNamespace(
        setGenres=lambda genres: values.extend(genres),
        getGenre=lambda: " | ".join(values),
    )
    monkeypatch.setattr(
        metadata.xbmcgui,
        "ListItem",
        lambda **kw: SimpleNamespace(getVideoInfoTag=lambda: tag),
    )
    assert metadata.item_separator() == " | "


def test_selected_libraries_publish_only_after_all_fetches_succeed(store, monkeypatch):
    from kofin.sync.backends.api import library

    store.publish([movie()], library="one")
    before = store.snapshot()
    worker = library.Library.__new__(library.Library)
    worker.store = store
    worker.api = SimpleNamespace(
        views=lambda: {
            "Items": [
                {"Id": "one", "CollectionType": "movies"},
                {"Id": "two", "CollectionType": "movies"},
            ]
        }
    )
    worker._stop_event = SimpleNamespace(is_set=lambda: False)
    monkeypatch.setattr(library.settings, "get_list", lambda _: ["one", "two"])

    def fetch(api, selected, abort):
        if selected == "two":
            raise OSError("offline")
        return []

    monkeypatch.setattr(library, "fetch_movies", fetch)
    with pytest.raises(OSError):
        worker.full_sync()
    assert store.snapshot() == before
    monkeypatch.setattr(
        library,
        "fetch_movies",
        lambda api, selected, abort: [movie()] if selected == "two" else [],
    )
    worker.full_sync()
    assert store.snapshot()[0]["a"]["library"] == "two"
    worker.command("RemoveLibrary", {"Id": "one"})
    assert "a" in store.snapshot()[0]


def test_pin_survives_multiple_publications_and_library_move_preserves_url(store):
    store.publish([movie()], library="one")
    old = store.pin()
    for index in range(4):
        store.publish([movie(Overview=str(index))], library="one")
    store.publish([movie(Overview="3")], library="two")
    store.publish([], library="one")
    assert store.snapshot() == old
    assert store.snapshot(False)[0]["a"]["library"] == "two"
    assert "one" not in playback_url(store.namespace, "a")


@pytest.mark.parametrize(
    "pages",
    [
        [
            {"Items": [movie()], "TotalRecordCount": 2},
            {"Items": [], "TotalRecordCount": 2},
        ],
        [
            {"Items": [movie()], "TotalRecordCount": 2},
            {"Items": [movie()], "TotalRecordCount": 2},
        ],
        [
            {"Items": [movie()], "TotalRecordCount": 2},
            {"Items": [movie("b")], "TotalRecordCount": 3},
        ],
        [{"Items": []}],
        [{"Items": [movie()], "TotalRecordCount": 0}],
    ],
)
def test_partial_enumeration_never_changes_store(store, pages):
    store.publish([movie()], library="one")
    before = store.snapshot()
    replies = iter(pages)
    api = SimpleNamespace(user_id="u", items=lambda params: next(replies))
    with pytest.raises(ValueError):
        store.publish(fetch_movies(api, "one"), library="one")
    assert store.snapshot() == before


def test_failed_second_page_preserves_native_desired_state(store):
    store.publish([movie()], library="one")

    def page(params):
        if params["StartIndex"]:
            raise OSError("server offline")
        return {"Items": [movie("b")], "TotalRecordCount": 2}

    with pytest.raises(OSError):
        store.publish(
            fetch_movies(SimpleNamespace(user_id="u", items=page), "one"), library="one"
        )
    assert set(store.snapshot()[0]) == {"a"}


def test_movie_pagination_uses_supported_sort_keys():
    seen = []

    def page(params):
        seen.append(params)
        return {"Items": [movie()], "TotalRecordCount": 1}

    assert fetch_movies(SimpleNamespace(user_id="u", items=page), "one") == [movie()]
    assert seen[0]["SortBy"] == "DateCreated,SortName"
    assert seen[0]["SortOrder"] == "Ascending,Ascending"


@pytest.fixture
def worker(store, monkeypatch):
    from kofin.core.settings import Credentials
    from kofin.sync.backends.api.library import Library
    from tests.unit.fakes import FakeApi

    monkeypatch.setattr(
        Credentials, "load", lambda: Credentials(server_id="server", user_id="user")
    )
    api = FakeApi(server="http://fixture.invalid", update_user_data={})
    return Library(api, None, lambda: api)


@pytest.mark.parametrize("failure", ["deleted_item", "no_snapshot", "local_delivery"])
def test_failed_event_or_local_delivery_cannot_starve_enumeration(
    store, worker, monkeypatch, failure
):
    from kofin.sync.backends.api import library

    clock = [0.0]
    scans = []
    if failure != "no_snapshot":
        store.publish([movie(), movie("b")], library="one")
    worker._refresh_due = 300
    worker.updated(["a"])
    worker.removed(["b"])

    def missing(*args):
        raise OSError("item no longer available")

    worker.api.item = missing
    if failure == "local_delivery":
        monkeypatch.setattr(worker, "flush_local", missing)

    def enumerate_movies():
        scans.append(clock[0])
        # The later removal command still ran despite the failed update.
        if failure != "no_snapshot":
            assert store.state("b").operation == "remove"
        store.publish([movie("c")], library="one")

    monkeypatch.setattr(worker, "full_sync", enumerate_movies)
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        library, "Movies", lambda *args: SimpleNamespace(setup=lambda: None)
    )
    monkeypatch.setattr(worker, "apply", lambda movies: None)
    monkeypatch.setattr(library, "status", lambda value: None)

    def wait(seconds):
        clock[0] += seconds
        assert clock[0] < 60, "worker failed to reach recovery"
        return bool(scans)

    worker._stop_event = SimpleNamespace(is_set=lambda: False, wait=wait)
    monkeypatch.setattr(
        library.xbmc,
        "Monitor",
        lambda: SimpleNamespace(abortRequested=lambda: False, waitForAbort=wait),
    )
    worker.run()
    assert len(scans) == 1
    assert scans[0] < 36
    if failure != "no_snapshot":
        assert scans[0] >= 30
    assert set(store.snapshot()[0]) == {"c"}


def test_incremental_single_item_preserves_rich_metadata(store, worker):
    original = movie(
        ProviderIds={"Imdb": "tt123"},
        Genres=["Drama"],
        Studios=[{"Name": "Studio"}],
        Tags=["Tag"],
        MediaSources=[{"Id": "source"}],
    )
    store.publish([original], library="one")
    updated = dict(original, Overview="Changed", UserData={"Played": True})
    # GET /Items/{id} returns the full DTO without a Fields query parameter.
    worker.api.item = lambda item_id: updated
    worker.command("changed", ["a"])
    assert store.snapshot()[0]["a"]["item"] == updated
    store.local("a", {"playcount": 1})
    worker.flush_local()
    assert store.snapshot()[0]["a"]["item"] == updated
    assert not store.local_pending()


class Kodi:
    def __init__(self, store):
        self.store = store
        self.rows = {}
        self.next_id = 1
        self.calls = []
        self.fail = ""
        self.accept_without_apply = False
        self.monitor = None

    def imported(self, item_id, record):
        row = metadata.details(
            record["item"],
            "http://fixture.invalid",
            self.store.namespace,
            record["library"],
        )
        row.update(
            movieid=self.next_id, file=playback_url(self.store.namespace, item_id)
        )
        self.next_id += 1
        return row

    def rpc(self, method, params=None):
        params = params or {}
        self.calls.append((method, copy.deepcopy(params)))
        if method == self.fail:
            raise RuntimeError("injected native failure")
        if method == "VideoLibrary.GetMovies":
            rows = list(self.rows.values())
            return {"movies": copy.deepcopy(rows), "limits": {"total": len(rows)}}
        if (
            method.startswith(("VideoLibrary.Get", "AudioLibrary.Get"))
            and "Details" not in method
        ):
            return {"limits": {"total": 0}}
        if method == "VideoLibrary.GetMovieDetails":
            return {"moviedetails": copy.deepcopy(self.rows[params["movieid"]])}
        if method == "VideoLibrary.Scan":
            records, _, _ = self.store.snapshot()
            for item_id, record in records.items():
                if not any(
                    r["file"] == playback_url(self.store.namespace, item_id)
                    for r in self.rows.values()
                ):
                    row = self.imported(item_id, record)
                    self.rows[row["movieid"]] = row
            self.monitor.finished += 1
        if method == "VideoLibrary.RefreshMovie":
            old = self.rows.pop(params["movieid"])
            item_id = old["uniqueid"]["kofin"].split(":")[1]
            row = self.imported(item_id, self.store.snapshot()[0][item_id])
            row["playcount"] = 0
            row["resume"] = {"position": 0, "total": 120}
            self.rows[row["movieid"]] = row
        if method == "VideoLibrary.SetMovieDetails" and not self.accept_without_apply:
            self.rows[params["movieid"]].update(copy.deepcopy(params))
        if method == "VideoLibrary.RemoveMovie":
            del self.rows[params["movieid"]]
        return "OK"


@pytest.fixture
def native(store, monkeypatch):
    from kofin.sync.backends.api import movies

    kodi = Kodi(store)
    monkeypatch.setattr(movies, "rpc", kodi.rpc)
    monkeypatch.setattr(movies.xbmc, "getCondVisibility", lambda _: False)
    monkeypatch.setattr(
        movies,
        "Monitor",
        lambda: SimpleNamespace(finished=0, waitForAbort=lambda _: False),
    )
    backend = Movies(store)
    kodi.monitor = backend.monitor
    return backend, kodi


def test_complete_lifecycle_and_foreign_item_survives(store, native):
    backend, kodi = native
    backend.setup()
    store.publish([movie()], library="one")
    backend.reconcile()
    first = store.mapping("a")[0]
    assert store.state("a").applied == 1
    store.publish(
        [
            movie(
                Overview="Changed",
                People=[{"Name": "New actor", "Type": "Actor"}],
                UserData={
                    "Played": True,
                    "PlayCount": 4,
                    "PlaybackPositionTicks": 300000000,
                },
            )
        ],
        library="one",
    )
    backend.reconcile()
    second = store.mapping("a")[0]
    assert second != first
    assert kodi.rows[second]["playcount"] == 4
    assert kodi.rows[second]["resume"]["position"] == 30
    kodi.rows[900] = {
        "movieid": 900,
        "file": "/foreign/movie.mkv",
        "uniqueid": {"imdb": "foreign"},
    }
    store.publish([], library="one")
    backend.reconcile()
    assert set(kodi.rows) == {900}
    assert not store.pending()


def test_repair_updates_changed_id_and_preserves_local_art(store, native):
    backend, kodi = native
    store.publish([movie()], library="one")
    backend.reconcile()
    row = kodi.rows.pop(store.mapping("a")[0])
    row["movieid"] = 400
    row["art"]["local"] = "image://https%3A%2F%2Ffixture.invalid%2Flocal.png/"
    kodi.rows[400] = row
    backend.reconcile(repair=True)
    assert store.mapping("a")[0] == 400
    store.publish([movie(Overview="Changed")])
    backend.reconcile()
    assert kodi.rows[400]["art"]["local"] == "https://fixture.invalid/local.png"


def test_patch_failure_and_restart_does_not_duplicate_import(store, native):
    backend, kodi = native
    store.publish([movie()], library="one")
    kodi.fail = "VideoLibrary.SetMovieDetails"
    with pytest.raises(RuntimeError):
        backend.reconcile()
    assert store.state("a").applied == 0
    assert len(kodi.rows) == 1
    kodi.fail = ""
    backend.store = MovieStore(store.namespace)
    backend.reconcile()
    assert store.state("a").applied == 1
    assert len(kodi.rows) == 1


def test_rpc_acceptance_without_patch_cannot_confirm(store, native):
    backend, kodi = native
    store.publish([movie()], library="one")
    backend.reconcile()
    store.publish([movie(Overview="New")], library="one")
    kodi.accept_without_apply = True
    with pytest.raises(RuntimeError, match="readback"):
        backend.reconcile()
    assert store.state("a").applied == 1
    assert store.state("a").desired == 2


def test_stale_native_id_never_removes_foreign_content(store, native):
    backend, kodi = native
    store.publish([movie()], library="one")
    backend.reconcile()
    native_id = store.mapping("a")[0]
    kodi.rows[native_id].update(file="/foreign.mkv", uniqueid={})
    store.publish([], library="one")
    backend.reconcile()
    assert native_id in kodi.rows
    assert not any(m == "VideoLibrary.RemoveMovie" for m, _ in kodi.calls)


def test_first_run_gate_refuses_existing_library_and_other_namespace(store, native):
    backend, kodi = native
    kodi.rows[4] = {"movieid": 4, "file": "/foreign.mkv", "uniqueid": {}}
    with pytest.raises(BackendMismatch, match="fresh"):
        backend.setup()
    assert not store.prepared()
    kodi.rows.clear()
    backend.setup()
    other = Movies(MovieStore(namespace("another", "user")))
    with pytest.raises(BackendMismatch, match="another"):
        other.setup()


def test_expected_userdata_scoped_by_generation_and_expiry(store, monkeypatch):
    store.publish([movie()], library="one")
    store.expect("a", 1, {"playcount": 3})
    assert store.is_echo("a", "playcount", 3)
    assert not store.is_echo("a", "playcount", 0)
    store.publish([movie(Overview="second")], library="one")
    assert not store.is_echo("a", "playcount", 3)
    store.expect("a", 2, {"playcount": 3})
    monkeypatch.setattr("kofin.sync.backends.api.store.time.time", lambda: 10**12)
    assert not store.is_echo("a", "playcount", 3)


def test_local_userdata_ack_cannot_lose_newer_edit(store):
    store.local("a", {"playcount": 1})
    sent = store.local_pending()[0][1]
    store.local("a", {"playcount": 0, "position": 0})
    store.local_done("a", sent)
    assert store.local_pending() == [("a", {"playcount": 0, "position": 0})]


def test_resume_echo_tolerance_retains_generation_and_expiry_guards(store, monkeypatch):
    from kofin.service import kodiuserdata
    from kofin.sync.backends.api import movies

    store.publish([movie()], library="one")
    store.expect("a", 1, {"resume": {"position": 12.75, "total": 999}})
    monkeypatch.setattr(movies, "current_store", lambda: store)
    monkeypatch.setattr(movies, "mapped_item", lambda *args: "a")
    monkeypatch.setattr(kodiuserdata.kodirpc, "resume_seconds", lambda *args: 12.0)
    watcher = kodiuserdata.KodiUserData(SimpleNamespace())
    watcher._apply_api(kodiuserdata.UPDATE_RESUME, 1, "movie", 0)
    assert not store.local_pending()
    assert not store.is_echo("a", "resume", {"position": 11.75})
    monkeypatch.setattr(kodiuserdata.kodirpc, "resume_seconds", lambda *args: 0)
    watcher._apply_api(kodiuserdata.UPDATE_RESUME, 1, "movie", 0)
    assert store.local_pending() == [("a", {"position": 0})]
    store.publish([movie(Overview="new generation")])
    assert not store.is_echo("a", "resume", {"position": 12.75})
    store.expect("a", 2, {"resume": {"position": 12.75}})
    monkeypatch.setattr("kofin.sync.backends.api.store.time.time", lambda: 10**12)
    assert not store.is_echo("a", "resume", {"position": 12.75})


def test_library_path_requires_imported_mapping(store, monkeypatch):
    from kofin.service import libraryclaim
    from kofin.sync.backends.api import movies

    monkeypatch.setattr(libraryclaim.buildconfig, "BACKEND", "api")
    monkeypatch.setattr(movies, "current_store", lambda: store)
    assert libraryclaim.library_video_path("a", "movie") is None
    store.publish([movie()], library="one")
    assert libraryclaim.library_video_path("a", "movie") is None
    store.remember("a", 1, 10, movie())
    assert libraryclaim.library_video_path("a", "movie") == playback_url(
        store.namespace, "a"
    )
    assert libraryclaim.library_video_path("a", "episode") is None
    store.publish([], library="one")
    store.remember("a", 2, None, {})
    assert libraryclaim.library_video_path("a", "movie") is None


@pytest.mark.parametrize("error", [OSError("locked"), BackendMismatch("wrong backend")])
def test_library_path_lookup_failure_does_not_break_play_next(monkeypatch, error):
    from kofin.service import libraryclaim
    from kofin.sync.backends.api import movies

    def unavailable():
        raise error

    monkeypatch.setattr(libraryclaim.buildconfig, "BACKEND", "api")
    monkeypatch.setattr(movies, "current_store", unavailable)
    assert libraryclaim.library_video_path("a", "movie") is None


def test_provider_refresh_and_existence_are_offline(store, monkeypatch):
    from kofin.sync.backends.api import provider
    from kofin.plugin.router import Request

    store.publish([movie()], library="one")
    rendered = []
    resolved = []
    monkeypatch.setattr(provider.metadata, "build", lambda *args: args[0]["Name"])
    monkeypatch.setattr(
        provider.xbmcplugin,
        "addDirectoryItems",
        lambda h, items, n: rendered.extend(items),
    )
    monkeypatch.setattr(
        provider.xbmcplugin, "setResolvedUrl", lambda h, ok, li: resolved.append(ok)
    )
    base = playback_url(store.namespace, "a").split("?")[0]
    provider.serve(Request(base, 1, {"id": "a", "kodi_action": "refresh_info"}))
    assert rendered[0][0] == playback_url(store.namespace, "a")
    provider.serve(Request(base, 1, {"id": "a", "kodi_action": "check_exists"}))
    store.publish([], removed=["a"])
    provider.serve(Request(base, 1, {"id": "a", "kodi_action": "check_exists"}))
    assert resolved == [True, False]


def test_namespace_is_server_and_user_scoped():
    assert namespace("server", "a") != namespace("server", "b")
    assert namespace("server", "a") != namespace("other", "a")
    assert identity(namespace("server", "a"), "movie").endswith(":movie")


def test_import_tag_refresh_marker_uses_unmodified_source_dto(monkeypatch):
    from unittest.mock import Mock

    tag = Mock()
    li = Mock()
    li.getVideoInfoTag.return_value = tag
    monkeypatch.setattr(metadata.listitems, "build", lambda *args, **kwargs: li)
    item = movie()
    original = copy.deepcopy(item)
    metadata.build(item, "http://fixture.invalid", "namespace", "library")
    ids = tag.setUniqueIDs.call_args.args[0]
    assert ids["kofinrefresh"] == metadata.refresh_token(original)
    assert item == original


def test_scalar_delta_does_not_enumerate_native_catalogue(store, native):
    backend, kodi = native
    store.publish([movie(), movie("b")], library="one")
    backend.reconcile()
    store.publish([movie(Overview="One changed plot")])
    kodi.calls.clear()
    backend.reconcile()
    assert not any(m == "VideoLibrary.GetMovies" for m, _ in kodi.calls)
    assert len([m for m, _ in kodi.calls if m == "VideoLibrary.SetMovieDetails"]) == 1


def test_library_move_is_pending_without_metadata_change(store, native):
    backend, kodi = native
    store.publish([movie()], library="one")
    backend.reconcile()
    first = store.mapping("a")[0]
    store.publish([movie()], library="two")
    assert store.state("a").desired == 2 and store.state("a").applied == 1
    backend.reconcile()
    assert store.mapping("a")[0] == first
    assert "kofin.library.two" in kodi.rows[first]["tag"]
    assert "kofin.library.one" not in kodi.rows[first]["tag"]


def test_real_local_edit_is_preserved_during_server_refresh(store, native):
    backend, kodi = native
    store.publish([movie()], library="one")
    backend.reconcile()
    native_id = store.mapping("a")[0]
    kodi.rows[native_id]["playcount"] = 3
    store.publish([movie(People=[{"Type": "Actor", "Name": "Another actor"}])])
    backend.reconcile()
    assert store.local_pending() == [("a", {"playcount": 3})]
    assert store.state("a").applied == 1
    assert kodi.rows[native_id]["playcount"] == 3


def test_snapshot_pin_survives_interrupted_scan(store, native, monkeypatch):
    backend, kodi = native
    store.publish([movie()], library="one")
    original = store.snapshot()
    scan = backend.scan

    def interrupted(records):
        backend._async_pending = True
        raise InterruptedError("restart")

    monkeypatch.setattr(backend, "scan", interrupted)
    with pytest.raises(InterruptedError):
        backend.reconcile()
    store.publish([movie(Overview="Later generation")], library="one")
    assert store.snapshot() == original
    # Replaying the older pinned generation cannot acknowledge the newer one.
    monkeypatch.setattr(backend, "scan", scan)
    backend.reconcile()
    assert store.state("a").applied == 0
    backend.reconcile()
    assert store.state("a").applied == 2
    assert len(kodi.rows) == 1


def test_native_readback_failure_retains_pending_metadata(store, native):
    backend, kodi = native
    store.publish([movie()], library="one")
    backend.reconcile()
    store.publish([movie(Genres=["New genre"])])
    kodi.accept_without_apply = True
    with pytest.raises(RuntimeError, match="readback"):
        backend.reconcile()
    assert store.state("a").applied == 1


def test_local_delivery_is_idempotent_and_newer_edit_survives(store, monkeypatch):
    from kofin.core.settings import Credentials
    from kofin.sync.backends.api.library import Library
    from tests.unit.fakes import FakeApi

    monkeypatch.setattr(
        Credentials, "load", lambda: Credentials(server_id="server", user_id="user")
    )
    store.publish([movie()], library="one")
    store.local("a", {"playcount": 3, "position": 0})
    sent = []

    def update(item_id, payload):
        sent.append(payload)
        store.local(item_id, {"playcount": 0})

    api = FakeApi(
        update_user_data=update, item=movie(UserData={"Played": True, "PlayCount": 3})
    )
    Library(api, None, lambda: api).flush_local()
    assert sent == [{"Played": True, "PlayCount": 3, "PlaybackPositionTicks": 0}]
    assert store.local_pending() == [("a", {"playcount": 0, "position": 0})]
