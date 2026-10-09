"""Enumeration, catch-up, events and the loop, with a Jellyfin stand-in."""

import time
from types import SimpleNamespace

import pytest

from kofin.sync.backends.api.library import fetch_kind
from tests.unit.apifixtures import (  # noqa: F401
    LIB,
    LIB2,
    SHOW,
    Feed,
    Server,
    boxset,
    episode,
    movie,
    season,
    series,
    show_bundle,
    store,
    worker,
)


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
        [{"Items": [episode("e", SeriesId=None)], "TotalRecordCount": 1}],
    ],
)
def test_partial_enumeration_never_changes_store(store, pages):
    store.publish([movie()], library=LIB)
    before = store.records(), store.generation()
    replies = iter(pages)
    api = SimpleNamespace(user_id="u", items=lambda params: next(replies))
    kind = pages[0]["Items"][0]["Type"] if pages[0]["Items"] else "Movie"
    with pytest.raises(ValueError):
        store.publish(fetch_kind(api, LIB, kind), library=LIB)
    assert (store.records(), store.generation()) == before


def test_pagination_uses_supported_sort_keys_and_compacts():
    seen = []

    def page(params):
        seen.append(params)
        return {
            "Items": [movie(ImageBlurHashes={"Primary": {}})],
            "TotalRecordCount": 1,
        }

    rows = fetch_kind(SimpleNamespace(user_id="u", items=page), LIB, "Movie")
    assert "ImageBlurHashes" not in rows[0]
    assert seen[0]["SortBy"] == "DateCreated,SortName"
    assert seen[0]["IncludeItemTypes"] == "Movie"


def test_full_enumeration_publishes_every_kind_after_all_fetches(store, monkeypatch):
    server = Server(
        {LIB: [movie(), movie("b")], LIB2: show_bundle()},
        boxsets=[boxset("box", "Pair", ["a", "b"])],
    )
    w = worker(store, server, [LIB, LIB2], monkeypatch)
    failing = server.items

    def flaky(params):
        if (
            params.get("ParentId") == LIB2
            and params.get("IncludeItemTypes") == "Episode"
        ):
            raise OSError("offline")
        return failing(params)

    server.items = flaky
    with pytest.raises(OSError):
        w.full_sync()
    assert store.records(pinned=False) == {}
    server.items = failing
    w.full_sync()
    records = store.records(pinned=False)
    assert {r.kind for r in records.values()} == {
        "Movie",
        "Series",
        "Season",
        "Episode",
        "BoxSet",
    }
    assert records["ea11"].library == LIB2 and records["ea11"].parent_id == SHOW
    assert records["box"].library == ""
    assert store.watermark()[0] and store.watermark()[1]
    assert w._repair
    # A later complete pass that lost a show tombstones it and its children.
    server.libraries[LIB2] = show_bundle(episodes=1)
    w.full_sync()
    assert store.state("ea12").operation == "remove"
    assert store.state("ea11").operation == "upsert"


def test_enumeration_drops_a_library_deselected_meanwhile(store, monkeypatch):
    server = Server({LIB: [movie()], LIB2: show_bundle()})
    w = worker(store, server, [[LIB, LIB2], [LIB2]], monkeypatch)
    w.full_sync()
    assert set(store.records(pinned=False)) == {SHOW, SHOW[:4] + "s1", "ea11", "ea12"}


def test_enumeration_publishes_nothing_when_the_selection_emptied(store, monkeypatch):
    store.publish([movie()], library=LIB)
    before = store.records(pinned=False), store.pending()
    server = Server({LIB: [movie("b")]})
    w = worker(store, server, [[LIB], []], monkeypatch)
    w.full_sync()
    assert (store.records(pinned=False), store.pending()) == before


def test_sync_library_enumerates_only_the_named_library(store, monkeypatch):
    server = Server({LIB: [movie()], LIB2: show_bundle()})
    w = worker(store, server, [LIB, LIB2], monkeypatch)
    w.command("SyncLibrary", {"Id": LIB})
    assert set(store.records(pinned=False)) == {"a"}
    assert not w._repair
    assert store.watermark() == ("", 0.0)


def test_catch_up_applies_feed_records_and_advances_the_watermark(store, monkeypatch):
    from kofin.sync.changefeed import ChangeRecord, unix_to_watermark

    server = Server({LIB: [movie(), movie("b")], LIB2: show_bundle()})
    store.publish([movie(), movie("b")], library=LIB)
    store.set_watermark(watermark=unix_to_watermark(500), enumerated=time.time())
    server.libraries[LIB][0]["Overview"] = "Edited"
    feed = Feed(
        [
            ChangeRecord("a", "Updated", item_type="Movie", library_ids=[LIB]),
            ChangeRecord("b", "Removed"),
            ChangeRecord(
                "ea11", "Added", item_type="Episode", series_id=SHOW, library_ids=[LIB2]
            ),
            ChangeRecord(SHOW, "Added", item_type="Series", library_ids=[LIB2]),
            ChangeRecord("zz", "Added", item_type="Movie", library_ids=["9" * 32]),
        ],
        userdata=[{"ItemId": "a", "Played": True, "PlayCount": 2}],
        server_time=1200,
    )
    w = worker(store, server, [LIB, LIB2], monkeypatch, feed=feed)
    w.catch_up()
    records = store.records(pinned=False)
    assert records["a"].item["Overview"] == "Edited"
    assert records["a"].item["UserData"]["PlayCount"] == 2
    assert store.state("b").operation == "remove"
    assert records["ea11"].library == LIB2 and records[SHOW].library == LIB2
    assert "zz" not in records
    assert store.watermark()[0] == unix_to_watermark(1200)
    assert feed.asked[0][1] == (
        "movies",
        "tvshows",
        "boxsets",
        "musicvideos",
        "music",
    )
    assert set(feed.asked[0][2]) == {LIB, LIB2}


def test_catch_up_retention_overrun_schedules_a_full_pass(store, monkeypatch):
    from kofin.sync.changefeed import unix_to_watermark

    server = Server({LIB: [movie()]})
    store.set_watermark(watermark=unix_to_watermark(500), enumerated=time.time())
    w = worker(
        store,
        server,
        [LIB],
        monkeypatch,
        feed=Feed([], server_time=9000, retention=8000),
    )
    w.catch_up()
    assert w._full_due


def test_catch_up_without_a_companion_uses_the_save_date(store, monkeypatch):
    from kofin.sync.changefeed import unix_to_watermark

    server = Server(
        {
            LIB: [
                movie(DateLastSaved="2026-10-09T00:00:00Z"),
                movie("b", DateLastSaved="2026-01-01T00:00:00Z"),
            ]
        }
    )
    store.set_watermark(watermark="2026-10-08T00:00:00Z", enumerated=time.time())
    w = worker(store, server, [LIB], monkeypatch)
    w.catch_up()
    assert set(store.records(pinned=False)) == {"a"}
    assert store.watermark()[0] > "2026-10-08T00:00:00Z"
    assert unix_to_watermark(0) < store.watermark()[0]


def test_websocket_changes_fetch_known_items_and_defer_unknown_ones(store, monkeypatch):
    server = Server({LIB: [movie(), movie("b")]})
    store.publish([movie()], library=LIB)
    store.set_watermark(watermark="x", enumerated=time.time())
    w = worker(store, server, [LIB], monkeypatch)
    w._catchup_due = 10**9
    server.libraries[LIB][0]["Overview"] = "Fresh"
    w.command("changed", ["a", "b"])
    assert store.records(pinned=False)["a"].item["Overview"] == "Fresh"
    assert "b" not in store.records(pinned=False)
    assert w._catchup_due == 0
    w.command("userdata", [{"ItemId": "a", "Played": True, "PlayCount": 1}])
    assert store.records(pinned=False)["a"].item["UserData"]["PlayCount"] == 1
    w.command("removed", ["a"])
    assert store.state("a").operation == "remove"


def test_failed_event_or_local_delivery_cannot_starve_enumeration(store, monkeypatch):
    from kofin.sync.backends.api import library

    clock = [0.0]
    passes = []
    store.publish([movie(), movie("b")], library=LIB)
    store.set_watermark(watermark="x", enumerated=1.0)
    server = Server({LIB: [movie("c")]})
    w = worker(store, server, [LIB], monkeypatch)
    w._queue.put(("changed", ["a"]))
    w._queue.put(("removed", ["b"]))

    def missing(*args, **kwargs):
        raise OSError("item no longer available")

    server.items = missing
    monkeypatch.setattr(w, "flush_local", missing)

    def enumerate_all(libraries=None):
        passes.append(clock[0])
        assert store.state("b").operation == "remove"
        store.publish([movie("c")], library=LIB)

    monkeypatch.setattr(w, "full_sync", enumerate_all)
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(library.time, "time", lambda: 10**6)
    monkeypatch.setattr(
        library, "Native", lambda *args: SimpleNamespace(setup=lambda: None)
    )
    monkeypatch.setattr(w, "apply", lambda native: None)
    monkeypatch.setattr(library, "status", lambda value: None)
    monkeypatch.setattr(
        library.Credentials, "load", lambda: SimpleNamespace(server_id="s", user_id="u")
    )

    def wait(seconds):
        clock[0] += seconds
        assert clock[0] < 60, "worker failed to reach recovery"
        return bool(passes)

    w._stop_event = SimpleNamespace(is_set=lambda: False, wait=wait)
    w.stop_thread = False
    w.startup_done = False
    monkeypatch.setattr(
        library.xbmc,
        "Monitor",
        lambda: SimpleNamespace(abortRequested=lambda: False, waitForAbort=wait),
    )
    w.run()
    assert len(passes) == 1
    assert set(store.records(pinned=False)) == {"c"}


def test_first_content_reloads_the_skin_once_per_kind_and_waits_for_playback(
    store, monkeypatch
):
    from kofin.sync.backends.api import library

    builtins = []
    monkeypatch.setattr(library.xbmc, "executebuiltin", builtins.append)
    monkeypatch.setattr(library.xbmc, "getCondVisibility", lambda _: True)
    monkeypatch.setattr(library.xbmc, "getLocalizedString", lambda _: "Done")
    monkeypatch.setattr(library, "status", lambda value: None)
    w = worker(store, Server({}), [LIB], monkeypatch)
    store.publish([movie()] + show_bundle(), library=LIB)
    playing = [True]
    w.player = SimpleNamespace(isPlayingVideo=lambda: playing[0])

    def import_movie(repair=False):
        store.remember("a", 1, 10, {}, "Movie")

    w.apply(SimpleNamespace(reconcile=import_movie))
    assert builtins == []
    playing[0] = False
    w.flush_pending_reload()
    assert builtins == ["ReloadSkin()"]
    w.apply(
        SimpleNamespace(
            reconcile=lambda repair=False: store.remember(SHOW, 1, 11, {}, "Series")
        )
    )
    assert builtins == ["ReloadSkin()", "ReloadSkin()"]
    store.publish([movie(Overview="x")] + show_bundle(), library=LIB)
    w.apply(SimpleNamespace(reconcile=lambda repair=False: None))
    assert builtins == ["ReloadSkin()", "ReloadSkin()"]


def test_libraries_selected_while_the_worker_was_off_are_enumerated(store, monkeypatch):
    from kofin.sync.backends.api import library

    server = Server({LIB: [movie()], LIB2: show_bundle()})
    store.set_watermark(watermark="2026-10-08T00:00:00Z", enumerated=time.time())
    w = worker(store, server, [LIB, LIB2], monkeypatch)
    monkeypatch.setattr(library.private, "get_sync", lambda: {"Whitelist": [LIB]})
    caught = []
    monkeypatch.setattr(w, "catch_up", lambda: caught.append(True))
    w.refresh()
    assert set(store.records(pinned=False)) == {SHOW, SHOW[:4] + "s1", "ea11", "ea12"}
    assert not caught
    monkeypatch.setattr(library.private, "get_sync", lambda: {"Whitelist": [LIB, LIB2]})
    w._catchup_due = 0
    w.refresh()
    assert caught == [True]


def test_a_selected_library_the_server_no_longer_lists_is_tried_once(
    store, monkeypatch
):
    from kofin.sync.backends.api import library

    server = Server({LIB: [movie()]})
    store.set_watermark(watermark="2026-10-08T00:00:00Z", enumerated=time.time())
    w = worker(store, server, [LIB, "9" * 32], monkeypatch)
    monkeypatch.setattr(library.private, "get_sync", lambda: {"Whitelist": [LIB]})
    caught = []
    monkeypatch.setattr(w, "catch_up", lambda: caught.append(True))
    views = []
    original = server.views
    server.views = lambda: views.append(True) or original()
    w.refresh()
    w._catchup_due = 0
    w.refresh()
    w.refresh()
    # One enumeration attempt for the vanished library, then the catch-up runs.
    assert len(views) == 1
    assert caught == [True]


def test_a_removed_collection_invalidates_its_members_on_every_path(store, monkeypatch):
    from kofin.sync.changefeed import ChangeRecord, unix_to_watermark

    server = Server({LIB: [movie(), movie("b")]})
    store.publish([movie(), movie("b"), boxset("box", "Pair", ["a", "b"])], library=LIB)
    store.remember("a", 1, 10, {}, "Movie")
    store.remember("b", 1, 11, {}, "Movie")
    store.set_watermark(watermark=unix_to_watermark(500), enumerated=time.time())
    w = worker(store, server, [LIB], monkeypatch)
    w.command("removed", ["box"])
    assert store.state("box").operation == "remove"
    assert {i.item_id for i, _, _ in store.pending()} >= {"a", "b"}
    store.publish([boxset("box", "Pair", ["a", "b"])], library=LIB)
    store.remember("a", 2, 10, {}, "Movie")
    store.remember("b", 2, 11, {}, "Movie")
    store.remember("box", 2, 5, {}, "BoxSet")
    w2 = worker(
        store,
        server,
        [LIB],
        monkeypatch,
        feed=Feed([ChangeRecord("box", "Removed")], server_time=900),
    )
    w2.catch_up()
    assert store.state("box").operation == "remove"
    assert {i.item_id for i, _, _ in store.pending()} >= {"a", "b"}
