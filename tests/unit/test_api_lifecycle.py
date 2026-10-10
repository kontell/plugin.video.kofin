"""Native lifecycle through the fake Kodi: import, patch, refresh, remove."""

import pytest

from kofin.sync import private
from kofin.sync.backends.api import metadata, native, paths
from kofin.sync.backends.api.native import Native
from kofin.sync.backends.api.store import Store, namespace
from kofin.sync.catalogue import BackendMismatch
from tests.unit.apifixtures import (  # noqa: F401
    LIB,
    LIB2,
    SHOW,
    SHOW2,
    backend,
    boxset,
    episode,
    kodi,
    methods,
    movie,
    musicvideo,
    season,
    series,
    show_bundle,
    store,
)


def test_complete_movie_lifecycle_and_foreign_item_survives(store, backend, kodi):
    backend.setup()
    store.publish([movie()], library=LIB)
    backend.reconcile()
    first = store.mapping("a").kodi_id
    assert store.state("a").applied == 1
    # A first import scans the root, which lists every movie as a file under
    # its own folder's URL; no folder is bound until one is scanned by name.
    assert kodi.bindings == {
        paths.library_dir(store.namespace, LIB, "movies"): "movies"
    }
    assert kodi.scanned == [paths.library_dir(store.namespace, LIB, "movies")]
    assert kodi.rows["Movie"][first]["file"].startswith(
        paths.movie_dir(store.namespace, LIB, "a")
    )
    # A scanner row built from the same policy needs no patch at all.
    assert not methods(kodi, "VideoLibrary.SetMovieDetails")
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
        library=LIB,
    )
    backend.reconcile()
    second = store.mapping("a").kodi_id
    assert second != first
    assert kodi.rows["Movie"][second]["playcount"] == 4
    assert kodi.rows["Movie"][second]["resume"]["position"] == 30
    kodi.rows["Movie"][900] = {
        "movieid": 900,
        "file": "/foreign/movie.mkv",
        "uniqueid": {"imdb": "foreign"},
    }
    store.publish([], library=LIB)
    backend.reconcile()
    assert set(kodi.rows["Movie"]) == {900}
    assert not store.pending()
    assert kodi.bindings == {}


def test_show_import_files_seasons_and_episodes_under_the_show(store, backend, kodi):
    items = show_bundle(seasons=(1, 2)) + [
        season(SHOW[:4] + "s0", SHOW, 0, Name="Specials")
    ]
    items[1]["Name"] = "Book One"
    store.publish(items, library=LIB)
    backend.reconcile()
    assert not store.pending()
    assert kodi.bindings == {
        paths.library_dir(store.namespace, LIB, "tvshows"): "tvshows",
        paths.show_dir(store.namespace, LIB, SHOW): "tvshows",
    }
    # Kodi finds a plugin folder's scraper only through the folder's own
    # binding, so every show is bound before the root is scanned.
    assert [p for m, p in kodi.calls if m == "VideoLibrary.SetSourceContent"][1] == {
        "path": paths.show_dir(store.namespace, LIB, SHOW),
        "content": "tvshows",
        "scraperid": "metadata.local",
        "containssingleitem": True,
        "usedirectorynames": False,
        "refresh": False,
    }
    assert kodi.scanned == [paths.library_dir(store.namespace, LIB, "tvshows")]
    show = kodi.owned("Series")[SHOW]
    assert show["file"] == paths.show_dir(store.namespace, LIB, SHOW)
    assert "Favorite tvshows" in show["tag"]
    episodes = kodi.owned("Episode")
    assert len(episodes) == 2
    assert all(r["tvshowid"] == show["tvshowid"] for r in episodes.values())
    assert episodes["ea11"]["file"] == paths.playback_url(
        store.namespace, "Episode", LIB, "ea11", SHOW
    )
    seasons = {r["season"]: r for r in kodi.rows["Season"].values()}
    assert seasons[1]["title"] == "Book One"
    assert seasons[2]["title"] == "Season 2"
    assert store.mapping(SHOW[:4] + "s1").kodi_id == seasons[1]["seasonid"]
    # An empty specials season is invisible to GetSeasons: applied, no row.
    assert store.mapping(SHOW[:4] + "s0").kodi_id is None
    # addSeason named it at import; the defaults are Kodi's own labels.
    assert not methods(kodi, "VideoLibrary.SetSeasonDetails")
    assert not methods(kodi, "VideoLibrary.SetEpisodeDetails")
    store.publish([season(SHOW[:4] + "s1", SHOW, 1, Name="Book Uno")])
    backend.reconcile()
    assert [p["title"] for p in methods(kodi, "VideoLibrary.SetSeasonDetails")] == [
        "Book Uno"
    ]
    assert seasons[1]["title"] == "Book Uno"
    store.publish([season(SHOW[:4] + "s1", SHOW, 1)])
    backend.reconcile()
    # Withdrawn custom name: cleared once, then left to Kodi's label.
    assert [p["title"] for p in methods(kodi, "VideoLibrary.SetSeasonDetails")] == [
        "Book Uno",
        "",
    ]
    assert seasons[1]["title"] == "Season 1"
    assert not store.pending()


def test_episode_refresh_changes_id_and_restores_userdata(store, backend, kodi):
    store.publish(show_bundle(), library=LIB)
    backend.reconcile()
    old = store.mapping("ea11").kodi_id
    kodi.rows["Episode"][old]["playcount"] = 3
    store.publish([episode("ea11", People=[{"Name": "Other", "Type": "GuestStar"}])])
    backend.reconcile()
    # A real local edit beats the refresh: delivered first, never overwritten.
    assert store.local_pending() == [("ea11", {"playcount": 3})]
    # Delivered to the server and re-fetched, as flush_local does.
    store.local_done("ea11", {"playcount": 3})
    store.publish(
        [
            episode(
                "ea11",
                People=[{"Name": "Other", "Type": "GuestStar"}],
                UserData={"Played": True, "PlayCount": 3},
            )
        ]
    )
    backend.reconcile()
    new = store.mapping("ea11").kodi_id
    assert kodi.rows["Episode"][new]["playcount"] == 3
    assert new != old
    assert methods(kodi, "VideoLibrary.RefreshEpisode") == [
        {"episodeid": old, "ignorenfo": False}
    ]
    assert kodi.rows["Episode"][new]["uniqueid"][
        "kofinrefresh"
    ] == metadata.refresh_token(store.item("ea11"))
    assert not store.pending()


def test_show_cast_change_refreshes_the_show_and_every_episode_follows(
    store, backend, kodi
):
    store.publish(show_bundle(episodes=3), library=LIB)
    backend.reconcile()
    old_show = store.mapping(SHOW).kodi_id
    old_episodes = {i: store.mapping(i).kodi_id for i in ("ea11", "ea12", "ea13")}
    kodi.rows["Episode"][old_episodes["ea12"]]["playcount"] = 1
    store.publish([series(People=[{"Name": "Recast", "Type": "Actor"}])])
    backend.reconcile()
    assert methods(kodi, "VideoLibrary.RefreshTVShow") == [
        {"tvshowid": old_show, "ignorenfo": False, "refreshepisodes": True}
    ]
    assert store.mapping(SHOW).kodi_id != old_show
    for item_id, before in old_episodes.items():
        assert store.mapping(item_id).kodi_id != before
    # The local watched mark set before the refresh is carried as a local
    # edit rather than lost under the re-created row.
    assert store.local_pending() == [("ea12", {"playcount": 1})]
    assert not [i for i in store.pending() if i[0].item_id != "ea12"]


def test_removing_one_of_two_libraries_clears_only_that_one_in_one_call(
    store, backend, kodi
):
    membership = {}
    items = []
    for library, show_id in ((LIB, SHOW), (LIB2, SHOW2)):
        for item in show_bundle(show_id) + [movie("m" + library[0])]:
            items.append(item)
            membership[item["Id"]] = library
    store.publish(items, membership=membership, complete_libraries=[LIB, LIB2])
    backend.reconcile()
    assert len(kodi.owned("Series")) == 2 and len(kodi.owned("Movie")) == 2
    kodi.calls.clear()
    kodi.builtins.clear()
    store.publish([], library=LIB)
    backend.reconcile()
    clears = methods(kodi, "VideoLibrary.SetSourceContent")
    assert clears == [
        {
            "path": paths.library_root(store.namespace, LIB),
            "content": "none",
            "clearmode": "remove",
            "refresh": False,
        }
    ]
    assert not methods(kodi, "VideoLibrary.RemoveTVShow")
    assert not methods(kodi, "VideoLibrary.RemoveMovie")
    assert set(kodi.owned("Series")) == {SHOW2}
    assert set(kodi.owned("Movie")) == {"m2"}
    assert kodi.builtins == ["UpdateLibrary(video)"]
    assert not store.pending()
    assert set(kodi.bindings) == {
        paths.library_dir(store.namespace, LIB2, "tvshows"),
        paths.show_dir(store.namespace, LIB2, SHOW2),
        paths.library_dir(store.namespace, LIB2, "movies"),
    }
    assert set(store.bindings()) == set(kodi.bindings)
    # The survivor was re-read, not re-imported.
    assert store.mapping(SHOW2).kodi_id in kodi.rows["Series"]
    assert not any(m == "VideoLibrary.Scan" for m, _ in kodi.calls)


def test_episode_and_show_removals_confirm_with_scoped_readbacks(store, backend, kodi):
    store.publish(
        show_bundle(episodes=3) + show_bundle(SHOW2, episodes=1, prefix="f"),
        library=LIB,
    )
    backend.reconcile()
    kodi.calls.clear()
    store.publish([], removed=["ea13"])
    backend.reconcile()
    assert len(methods(kodi, "VideoLibrary.RemoveEpisode")) == 1
    # One scoped listing finds the row by the id it has now, one confirms
    # the removal; no details read by a stored id.
    assert len(methods(kodi, "VideoLibrary.GetEpisodes")) == 2
    assert not methods(kodi, "VideoLibrary.GetEpisodeDetails")
    assert set(kodi.owned("Episode")) == {"ea11", "ea12", "fb21"}
    kodi.calls.clear()
    store.publish([], removed=[SHOW2, "fb21", SHOW2[:4] + "s1"])
    backend.reconcile()
    assert len(methods(kodi, "VideoLibrary.RemoveTVShow")) == 1
    assert not methods(kodi, "VideoLibrary.RemoveEpisode")
    assert set(kodi.owned("Series")) == {SHOW}
    assert paths.show_dir(store.namespace, LIB, SHOW2) not in kodi.bindings
    assert paths.show_dir(store.namespace, LIB, SHOW2) not in store.bindings()
    assert not kodi.rows["Season"] or all(
        r["tvshowid"] == kodi.owned("Series")[SHOW]["tvshowid"]
        for r in kodi.rows["Season"].values()
    )
    assert not store.pending()


def test_unnumbered_special_and_empty_season_apply_without_a_row(store, backend, kodi):
    items = show_bundle() + [
        episode("ea19", season_number=0, number=0),
        season(SHOW[:4] + "s0", SHOW, 0, Name="Specials"),
        season(SHOW[:4] + "s7", SHOW, 7, Name="Unaired"),
    ]
    store.publish(items, library=LIB)
    backend.reconcile()
    assert "ea19" not in kodi.owned("Episode")
    assert not store.pending()
    assert store.mapping("ea19").kodi_id is None
    # Nor is the unnumbered special a reason to scan again.
    kodi.scanned.clear()
    store.publish([episode("ea19", season_number=0, number=0, Overview="edit")])
    backend.reconcile()
    assert kodi.scanned == [] and not store.pending()
    # The specials season holds only the unnumbered episode; season 7
    # holds nothing. Neither is visible to GetSeasons, so neither is a row.
    assert store.mapping(SHOW[:4] + "s0").kodi_id is None
    assert store.mapping(SHOW[:4] + "s7").kodi_id is None
    assert store.mapping(SHOW[:4] + "s1").kodi_id is not None


def test_collections_file_sets_at_import_and_patch_set_details(store, backend, kodi):
    store.publish(
        [
            movie(),
            movie("b"),
            movie("c"),
            boxset("box", "Trilogy", ["a", "b"], Overview="Three films"),
        ],
        library=LIB,
    )
    backend.reconcile()
    assert not store.pending()
    assert kodi.owned("Movie")["a"]["set"] == "Trilogy"
    assert kodi.owned("Movie")["c"]["set"] == ""
    assert not methods(kodi, "VideoLibrary.SetMovieDetails")
    sets = {r["title"]: r for r in kodi.rows["BoxSet"].values()}
    assert sets["Trilogy"]["plot"] == "Three films"
    assert store.mapping("box").kodi_id == sets["Trilogy"]["setid"]
    kodi.calls.clear()
    store.publish([boxset("box", "Trilogy", ["b", "c"], Overview="Three films")])
    store.invalidate({"a", "c"})
    backend.reconcile()
    assert kodi.owned("Movie")["a"]["set"] == ""
    assert kodi.owned("Movie")["c"]["set"] == "Trilogy"
    assert len(methods(kodi, "VideoLibrary.SetMovieDetails")) == 2
    kodi.calls.clear()
    store.publish([], library=LIB)
    backend.reconcile()
    assert methods(kodi, "VideoLibrary.Clean") == [
        {"showdialogs": False, "directory": paths.library_root(store.namespace, LIB)}
    ]
    assert kodi.rows["BoxSet"] == {}


def test_patches_travel_in_arrays_of_twenty_five(store, backend, kodi, monkeypatch):
    store.publish([movie("m%02d" % i) for i in range(30)], library=LIB)
    backend.reconcile()
    batches = []
    original = kodi.batch

    def counting(requests):
        batches.append(len(requests))
        return original(requests)

    from kofin.sync.backends.api import patch

    monkeypatch.setattr(patch, "rpc_batch", counting)
    store.publish(
        [movie("m%02d" % i, Overview="edited") for i in range(30)], library=LIB
    )
    backend.reconcile()
    # 30 setters as 25 + 5, each followed by its confirmation reads.
    assert batches == [25, 25, 5, 5]
    assert not store.pending()


def test_repair_updates_changed_id_and_preserves_local_art(store, backend, kodi):
    store.publish([movie()], library=LIB)
    backend.reconcile()
    row = kodi.rows["Movie"].pop(store.mapping("a").kodi_id)
    row["movieid"] = 400
    row["art"]["local"] = "image://https%3A%2F%2Ffixture.invalid%2Flocal.png/"
    kodi.rows["Movie"][400] = row
    backend.reconcile(repair=True)
    assert store.mapping("a").kodi_id == 400
    store.publish([movie(Overview="Changed")])
    backend.reconcile()
    assert (
        kodi.rows["Movie"][400]["art"]["local"] == "https://fixture.invalid/local.png"
    )
    assert kodi.rows["Movie"][400]["plot"] == "Changed"


def test_patch_failure_and_restart_does_not_duplicate_import(store, backend, kodi):
    store.publish([movie()], library=LIB)
    backend.reconcile()
    store.publish([movie(Overview="New")], library=LIB)
    kodi.fail = "VideoLibrary.SetMovieDetails"
    with pytest.raises(RuntimeError):
        backend.reconcile()
    assert store.state("a").applied == 1
    assert len(kodi.rows["Movie"]) == 1
    kodi.fail = ""
    backend.store = Store(store.namespace)
    backend.reconcile()
    assert store.state("a").applied == 2
    assert len(kodi.rows["Movie"]) == 1


def test_rpc_acceptance_without_patch_cannot_confirm(store, backend, kodi):
    store.publish([movie()], library=LIB)
    backend.reconcile()
    store.publish([movie(Overview="New")], library=LIB)
    kodi.accept_without_apply = True
    with pytest.raises(RuntimeError, match="readback differs"):
        backend.reconcile()
    assert store.state("a").applied == 1
    assert store.state("a").desired == 2


def test_stale_native_id_never_removes_foreign_content(store, backend, kodi):
    store.publish([movie(), movie("b")], library=LIB)
    backend.reconcile()
    native_id = store.mapping("a").kodi_id
    kodi.rows["Movie"][native_id].update(file="/foreign.mkv", uniqueid={})
    store.publish([], removed=["a"])
    # The id now names someone else's row: our item is gone from the scope,
    # so the removal is acknowledged and the foreign row is never touched.
    backend.reconcile()
    assert native_id in kodi.rows["Movie"]
    assert not methods(kodi, "VideoLibrary.RemoveMovie")
    assert not store.pending()


def test_unconfirmed_removal_stays_pending(store, backend, kodi):
    store.publish([movie(), movie("b")], library=LIB)
    backend.reconcile()
    store.publish([], removed=["a"])
    kodi.accept_without_apply = True
    with pytest.raises(RuntimeError, match="removal not confirmed"):
        backend.reconcile()
    assert store.state("a").status == "pending"
    kodi.accept_without_apply = False
    backend.reconcile()
    assert not store.pending()
    assert len(kodi.rows["Movie"]) == 1


def test_first_run_gate_refuses_existing_library_and_other_namespace(
    store, backend, kodi
):
    kodi.rows["Movie"][4] = {"movieid": 4, "file": "/foreign.mkv", "uniqueid": {}}
    with pytest.raises(BackendMismatch, match="fresh"):
        backend.setup()
    assert not store.prepared()
    kodi.rows["Movie"].clear()
    backend.setup()
    other = Native(Store(namespace("another", "user")))
    with pytest.raises(BackendMismatch, match="another"):
        other.setup()


def test_expected_userdata_scoped_by_generation_and_expiry(store, monkeypatch):
    store.publish([movie()], library=LIB)
    store.expect("a", 1, {"playcount": 3})
    assert store.is_echo("a", "playcount", 3)
    assert not store.is_echo("a", "playcount", 0)
    store.publish([movie(Overview="second")], library=LIB)
    assert not store.is_echo("a", "playcount", 3)
    store.expect("a", 2, {"playcount": 3})
    monkeypatch.setattr("kofin.sync.backends.api.store.time.time", lambda: 10**12)
    assert not store.is_echo("a", "playcount", 3)


def test_real_local_edit_is_preserved_during_server_refresh(store, backend, kodi):
    store.publish([movie()], library=LIB)
    backend.reconcile()
    native_id = store.mapping("a").kodi_id
    kodi.rows["Movie"][native_id]["playcount"] = 3
    store.publish([movie(People=[{"Type": "Actor", "Name": "Another actor"}])])
    backend.reconcile()
    assert store.local_pending() == [("a", {"playcount": 3})]
    assert store.state("a").applied == 1
    assert kodi.rows["Movie"][native_id]["playcount"] == 3


def test_snapshot_pin_survives_interrupted_scan(store, backend, kodi, monkeypatch):
    store.publish([movie()], library=LIB)
    scan = backend.scan

    def interrupted(directories, **_):
        backend._async_pending = True
        raise InterruptedError("restart")

    monkeypatch.setattr(backend, "scan", interrupted)
    with pytest.raises(InterruptedError):
        backend.reconcile()
    store.publish([movie(Overview="Later generation"), movie("b")], library=LIB)
    # The pinned membership is what the interrupted scan was listing; the
    # payload it serves is the newest, so no stale generation is applied.
    assert set(store.records()) == {"a"}
    assert set(store.records(pinned=False)) == {"a", "b"}
    monkeypatch.setattr(backend, "scan", scan)
    backend.reconcile()
    assert store.state("a").applied == 2
    # The pass that resumed the pinned listing does not add to it; the
    # next one, unpinned, imports the newcomer.
    assert store.state("b").applied == 0
    backend.reconcile()
    assert store.state("b").applied == 1
    assert len(kodi.rows["Movie"]) == 2


@pytest.mark.parametrize("finishes", [True, False])
def test_scan_wait_outlasts_a_long_scan_but_not_an_idle_one(
    store, backend, kodi, monkeypatch, finishes
):
    clock = [0.0]
    scanning = [False]
    remaining = [0]
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(native.xbmc, "getCondVisibility", lambda _: scanning[0])
    original_rpc = kodi.rpc

    def slow_scan(method, params=None):
        if method == "VideoLibrary.Scan":
            scanning[0], remaining[0] = True, 10
            return "OK"
        return original_rpc(method, params)

    def tick(_):
        clock[0] += 30
        if scanning[0]:
            remaining[0] -= 1
            if not remaining[0]:
                scanning[0] = False
                if finishes:
                    original_rpc(
                        "VideoLibrary.Scan",
                        {
                            "directory": paths.library_dir(
                                store.namespace, LIB, "movies"
                            )
                        },
                    )
        return False

    monkeypatch.setattr(native, "rpc", slow_scan)
    backend.monitor.waitForAbort = tick
    store.publish([movie()], library=LIB)
    directory = paths.library_dir(store.namespace, LIB, "movies")
    if finishes:
        backend.scan([directory])
        assert clock[0] >= 300
        assert len(kodi.rows["Movie"]) == 1
    else:
        with pytest.raises(TimeoutError):
            backend.scan([directory])
        assert 300 + 60 <= clock[0] <= 300 + 60 + 30


def test_acknowledgements_and_expectations_are_written_in_batches(
    store, backend, kodi, monkeypatch
):
    opens = []
    original = private.Database.__enter__

    def counting(self):
        opens.append(1)
        return original(self)

    monkeypatch.setattr(private.Database, "__enter__", counting)
    store.publish([movie("m%03d" % i) for i in range(120)], library=LIB)
    opens.clear()
    backend.reconcile()
    assert not store.pending()
    # Well under one open per item: the pass reads the catalogue a few
    # times and commits acknowledgements by the batch.
    assert len(opens) < 40


# -- review regressions ------------------------------------------------------------


def test_whole_library_clear_survives_a_movie_that_never_imported(store, backend, kodi):
    """A member with no native row (never scanned, never acknowledged) must not
    wedge the removal of its library."""
    store.publish([movie(), movie("b"), boxset("box", "Pair", ["a", "b"])], library=LIB)
    backend.reconcile()
    store.publish([movie("c")], library=LIB)
    # "c" is published but the library is deselected before any pass imports it.
    store.publish([], library=LIB)
    kodi.calls.clear()
    backend.reconcile()
    assert not store.pending()
    assert kodi.rows["Movie"] == {}
    assert kodi.rows["BoxSet"] == {}
    assert [p["clearmode"] for p in methods(kodi, "VideoLibrary.SetSourceContent")] == [
        "remove"
    ]
    # The clean that drops empty sets runs for every cleared movie library.
    assert methods(kodi, "VideoLibrary.Clean") == [
        {"showdialogs": False, "directory": paths.library_root(store.namespace, LIB)}
    ]


def test_episodes_of_a_show_whose_removal_fails_stay_pending(store, backend, kodi):
    store.publish(
        show_bundle(episodes=2) + show_bundle(SHOW2, episodes=1, prefix="f"),
        library=LIB,
    )
    backend.reconcile()
    store.publish([], removed=[SHOW2, "fb21", SHOW2[:4] + "s1"])
    kodi.fail = "VideoLibrary.RemoveTVShow"
    with pytest.raises(RuntimeError):
        backend.reconcile()
    # Nothing is acknowledged on the strength of a call that did not happen.
    assert {i.item_id for i, _, _ in store.pending()} == {
        SHOW2,
        "fb21",
        SHOW2[:4] + "s1",
    }
    assert "fb21" in kodi.owned("Episode")
    kodi.fail = ""
    kodi.accept_without_apply = True
    with pytest.raises(RuntimeError, match="removal not confirmed"):
        backend.reconcile()
    assert {i.item_id for i, _, _ in store.pending()} == {
        SHOW2,
        "fb21",
        SHOW2[:4] + "s1",
    }
    kodi.accept_without_apply = False
    backend.reconcile()
    assert not store.pending()
    assert set(kodi.owned("Series")) == {SHOW}


def test_collections_without_a_kodi_set_apply_without_a_row(store, backend, kodi):
    # "outside": its movies are in no selected library. "loser": the movie
    # belongs to two collections and Kodi can hold one set.
    store.publish(
        [
            movie(),
            boxset("outside", "Elsewhere", ["x", "y"]),
            boxset("winner", "Alpha", ["a"]),
            boxset("loser", "Zeta", ["a"]),
        ],
        library=LIB,
    )
    backend.reconcile()
    assert not store.pending()
    assert store.mapping("outside").kodi_id is None
    assert store.mapping("loser").kodi_id is None
    assert store.mapping("winner").kodi_id is not None
    assert kodi.owned("Movie")["a"]["set"] == "Alpha"
    # A set a synced movie should have filed, whose row is missing, is a retry.
    kodi.rows["BoxSet"].clear()
    store.publish([boxset("winner", "Alpha", ["a"], Overview="edited")])
    with pytest.raises(RuntimeError, match="no native row yet"):
        backend.reconcile()


def test_removing_a_collection_unfiles_its_movies_and_drops_the_empty_set(
    store, backend, kodi
):
    store.publish([movie(), movie("b"), boxset("box", "Pair", ["a", "b"])], library=LIB)
    backend.reconcile()
    assert kodi.owned("Movie")["a"]["set"] == "Pair"
    kodi.calls.clear()
    store.publish([], removed=["box"])
    # The coordinator invalidates the members on the removal path; the pass
    # does it again from the tombstone, so a direct removal is covered too.
    backend.reconcile()
    assert not store.pending()
    assert kodi.owned("Movie")["a"]["set"] == ""
    assert kodi.owned("Movie")["b"]["set"] == ""
    assert kodi.rows["BoxSet"] == {}
    assert len(methods(kodi, "VideoLibrary.SetMovieDetails")) == 2
    assert methods(kodi, "VideoLibrary.Clean") == [
        {"showdialogs": False, "directory": paths.root(store.namespace)}
    ]
    assert store.state("box").status == "applied"


def test_a_collection_removal_waits_while_a_member_still_files_it(store, backend, kodi):
    store.publish([movie(), boxset("box", "Pair", ["a"])], library=LIB)
    backend.reconcile()
    store.publish([], removed=["box"])
    kodi.fail = "VideoLibrary.SetMovieDetails"
    with pytest.raises(RuntimeError):
        backend.reconcile()
    assert store.state("box").status == "pending"
    assert store.state("box").error.startswith("RuntimeError: collection still has")
    assert kodi.rows["BoxSet"] != {}
    assert not methods(kodi, "VideoLibrary.Clean")
    kodi.fail = ""
    backend.reconcile()
    assert not store.pending()
    assert kodi.rows["BoxSet"] == {}


def test_refresh_confirmation_relists_the_scope_at_most_once_a_second(
    store, backend, kodi, monkeypatch
):
    from kofin.sync.backends.api import patch

    clock = [0.0]
    monkeypatch.setattr(patch.time, "monotonic", lambda: clock[0])
    original_rpc = kodi.rpc
    lists = []

    def slow_refresh(method, params=None):
        if method == "VideoLibrary.GetMovies":
            lists.append(clock[0])
        if method == "VideoLibrary.RefreshMovie":
            # Kodi takes three seconds to re-create the row.
            kodi.pending_refresh = (params["movieid"], clock[0] + 3.0)
            return "OK"
        due = getattr(kodi, "pending_refresh", None)
        if due and clock[0] >= due[1]:
            kodi.pending_refresh = None
            original_rpc("VideoLibrary.RefreshMovie", {"movieid": due[0]})
        return original_rpc(method, params)

    def tick(_):
        clock[0] += 0.1
        return False

    kodi.rpc = slow_refresh
    backend.monitor.waitForAbort = tick
    store.publish([movie()], library=LIB)
    backend.reconcile()
    lists.clear()
    store.publish([movie(People=[{"Name": "Recast", "Type": "Actor"}])])
    backend.reconcile()
    assert not store.pending()
    # Thirty polls of 100 ms; the scope was listed a handful of times, not thirty.
    assert len(lists) <= 6


def test_a_tombstone_whose_row_kodi_already_deleted_is_forgotten(store, backend, kodi):
    """A Clean that answered False for the tombstone, or a reissued id, must
    not keep the removal pending on a details read that can never succeed."""
    store.publish([movie(), movie("b"), movie("c")], library=LIB)
    backend.reconcile()
    gone = store.mapping("a").kodi_id
    reissued = store.mapping("b").kodi_id
    store.publish([], removed=["a", "b"])
    del kodi.rows["Movie"][gone]
    kodi.rows["Movie"][reissued] = {
        "movieid": reissued,
        "file": "/foreign/other.mkv",
        "uniqueid": {"imdb": "foreign"},
        "set": "",
    }
    kodi.calls.clear()
    backend.reconcile()
    assert not store.pending()
    assert not methods(kodi, "VideoLibrary.RemoveMovie")
    assert not methods(kodi, "VideoLibrary.GetMovieDetails")
    assert reissued in kodi.rows["Movie"]
    assert set(kodi.owned("Movie")) == {"c"}


def test_a_vanished_show_takes_its_pending_episodes_with_it(store, backend, kodi):
    store.publish(
        show_bundle(episodes=3) + show_bundle(SHOW2, episodes=1, prefix="f"),
        library=LIB,
    )
    backend.reconcile()
    store.publish([], removed=[SHOW, "ea11", "ea12", "ea13", SHOW[:4] + "s1"])
    # Kodi already dropped the show and, with it, every episode row.
    kodi._remove_show(store.mapping(SHOW).kodi_id)
    kodi.calls.clear()
    backend.reconcile()
    assert not store.pending()
    assert not methods(kodi, "VideoLibrary.RemoveTVShow")
    assert not methods(kodi, "VideoLibrary.RemoveEpisode")
    assert set(kodi.owned("Series")) == {SHOW2}
    assert set(kodi.owned("Episode")) == {"fb21"}


def test_scans_are_issued_one_at_a_time(store, backend, kodi, monkeypatch):
    """Kodi's UpdateLibrary builtin, which VideoLibrary.Scan and
    AudioLibrary.Scan both go through, stops a running scan instead of
    queuing a second one; a pass that selected two libraries at once lost
    one scan on every retry on 22.0b2. Each scan waits for the one before."""
    from kofin.sync.backends.api import native as native_module

    issued = []
    scanning = {"on": False}
    original = kodi.rpc

    def rpc(method, params=None):
        if method == "VideoLibrary.Scan":
            assert not scanning["on"], "a scan was issued while another was running"
            issued.append(params["directory"])
            scanning["on"] = True
            return "OK"
        return original(method, params)

    polls = {"n": 0}

    def visible(flag):
        if flag == "Library.IsScanningVideo" and scanning["on"]:
            # The scan "finishes" on the third poll.
            polls["n"] += 1
            if polls["n"] >= 3:
                polls["n"] = 0
                scanning["on"] = False
                backend.monitor.finished += 1
            return True
        return False

    monkeypatch.setattr(native_module, "rpc", rpc)
    monkeypatch.setattr(native_module.xbmc, "getCondVisibility", visible)
    backend.scan(["plugin://a/", "plugin://b/", "plugin://c/"])
    assert issued == ["plugin://a/", "plugin://b/", "plugin://c/"]
    assert backend.monitor.finished == 3


def test_a_first_import_under_repair_acknowledges_without_a_write(store, backend, kodi):
    """The pass after every complete enumeration runs as a repair; a row the
    scanner just filed from this listing has nothing stale behind it. A
    tablet re-wrote all 6,200 rows of its first import before this."""
    store.publish([movie()], library=LIB)
    backend.reconcile(repair=True)
    assert store.mapping("a") is not None
    assert not any(method == "VideoLibrary.SetMovieDetails" for method, _ in kodi.calls)
    # A later repair of a row whose acknowledged hash moved still writes.
    store.publish([movie(Overview="Changed")])
    kodi.rows["Movie"][store.mapping("a").kodi_id]["plot"] = "Changed"
    backend.reconcile(repair=True)
    assert any(method == "VideoLibrary.SetMovieDetails" for method, _ in kodi.calls)
