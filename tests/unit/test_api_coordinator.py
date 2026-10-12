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
    store.set_watermark(watermark="2026-10-08T00:00:00Z", enumerated=time.time())
    w.command("SyncLibrary", {"Id": LIB})
    assert set(store.records(pinned=False)) == {"a"}
    assert not w._repair and not w._full_due


def test_a_first_selection_is_listed_once(store, monkeypatch):
    """Nothing has been listed whole on a new profile, so the refresh that
    follows the command in the same tick lists every selected library. The
    command listed its library first as well: a first selection enumerated
    everything twice, a minute of it on a small device."""
    server = Server({LIB: [movie()], LIB2: show_bundle()})
    w = worker(store, server, [LIB, LIB2], monkeypatch)
    listings = []
    items = server.items
    server.items = lambda params: listings.append(params.get("ParentId")) or items(
        params
    )
    w.command("SyncLibrary", {"Id": LIB})
    assert listings == [] and w._full_due
    w.refresh()
    assert set(store.records(pinned=False)) >= {"a", SHOW}
    # Each library was asked for once per kind, and the profile is listed.
    assert listings.count(LIB) == 1
    assert store.watermark()[1]


def _named_states(w, monkeypatch):
    """Give the states their string ids for names. With strings to write, the
    node tree would be published for real, into the working directory."""
    from kofin.sync.backends.api import library

    monkeypatch.setattr(library.settings, "localized", lambda i: "L%d" % i)
    monkeypatch.setattr(library.xbmc, "getLocalizedString", lambda i: "K%d" % i)
    w.publish_selection = lambda: None


def test_a_first_listing_is_named_in_the_status_while_it_runs(store, monkeypatch):
    from kofin.sync.backends.api import status

    server = Server({LIB: [movie()], LIB2: show_bundle()})
    w = worker(store, server, [LIB, LIB2], monkeypatch)
    _named_states(w, monkeypatch)
    seen = []
    items = server.items
    server.items = lambda params: seen.append(status.read()) or items(params)

    w.full_sync()

    assert seen[0]["state"] == "L30401"
    assert sorted(seen[0]["listing"]) == sorted(
        view["Name"] for view in server.views()["Items"]
    )
    # Published and waiting for the pass: still updating, nothing being listed.
    assert status.read()["state"] == "L30401" and status.read()["listing"] == []
    assert status.read()["since"] == seen[0]["since"]


def test_a_listing_that_leaves_nothing_to_apply_ends_the_update(store, monkeypatch):
    from kofin.sync.backends.api import status

    w = worker(store, Server({}), [], monkeypatch)
    _named_states(w, monkeypatch)

    w.full_sync([LIB])

    assert status.read()["state"] == "K20177"


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
        library, "Native", lambda *args, **kwargs: SimpleNamespace(setup=lambda: None)
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


class PagedServer:
    """A listing of ``count`` movies served a page at a time, recording what
    was asked and how many requests were in flight at once."""

    user_id = "u"

    def __init__(self, count, log=None, tamper=None):
        import threading

        self.rows = [movie("m%04d" % n) for n in range(count)]
        self.log = log if log is not None else []
        self.tamper = tamper
        self.lock = threading.Lock()
        self.flying = 0
        self.widest = 0
        self.closed = 0

    def items(self, params):
        import time

        with self.lock:
            self.flying += 1
            self.widest = max(self.widest, self.flying)
        try:
            time.sleep(0.01)
            start, limit = params["StartIndex"], params["Limit"]
            reply = {
                "Items": [dict(r) for r in self.rows[start : start + limit]],
                "TotalRecordCount": len(self.rows),
            }
            self.log.append(start)
            return self.tamper(start, reply) if self.tamper else reply
        finally:
            with self.lock:
                self.flying -= 1

    def close(self):
        self.closed += 1


def paged(monkeypatch, count, tamper=None, page=10):
    from kofin.sync.backends.api import library

    monkeypatch.setattr(library, "PAGE", page)
    log = []
    first = PagedServer(count, log, tamper)
    workers = []

    def factory():
        workers.append(PagedServer(count, log, tamper))
        return workers[-1]

    return first, workers, factory, log


def test_pages_after_the_first_are_fetched_side_by_side_in_listing_order(monkeypatch):
    """The rows are the listing's, in its order, whichever page answered
    first; no more than PAGERS requests fly at once and every worker's
    client is closed."""
    from kofin.sync.backends.api import library

    first, workers, factory, log = paged(monkeypatch, 95)
    progress = []
    rows = fetch_kind(
        first,
        LIB,
        "Movie",
        on_page=lambda done, total: progress.append((done, total)),
        api_factory=factory,
    )
    assert [r["Id"] for r in rows] == ["m%04d" % n for n in range(95)]
    assert sorted(log) == list(range(0, 95, 10))
    assert first.log[0] == 0
    assert 1 < len(workers) <= library.PAGERS
    assert max(w.widest for w in workers) == 1
    assert all(w.closed == 1 for w in workers)
    assert progress == [(n, 95) for n in list(range(10, 95, 10)) + [95]]


def test_a_short_page_fails_a_side_by_side_enumeration(monkeypatch):
    def drop_one(start, reply):
        if start == 40:
            reply["Items"].pop()
        return reply

    first, workers, factory, _ = paged(monkeypatch, 95, drop_one)
    with pytest.raises(ValueError, match="short Movie page"):
        fetch_kind(first, LIB, "Movie", api_factory=factory)
    assert all(w.closed == 1 for w in workers)


def test_a_moving_total_fails_a_side_by_side_enumeration(monkeypatch):
    def grow(start, reply):
        if start == 60:
            reply["TotalRecordCount"] += 1
        return reply

    first, _, factory, _ = paged(monkeypatch, 95, grow)
    with pytest.raises(ValueError, match="listing changed"):
        fetch_kind(first, LIB, "Movie", api_factory=factory)


def test_a_duplicate_across_pages_fails_a_side_by_side_enumeration(monkeypatch):
    def repeat(start, reply):
        if start == 30:
            reply["Items"][0]["Id"] = "m0001"
        return reply

    first, _, factory, _ = paged(monkeypatch, 95, repeat)
    with pytest.raises(ValueError, match="invalid or duplicate"):
        fetch_kind(first, LIB, "Movie", api_factory=factory)


def test_a_page_client_of_another_account_fails_the_enumeration(monkeypatch):
    """The factory reads the stored credentials when it is called. A sign-in
    changed mid-enumeration made the later page clients another user's,
    whose pages have the same totals and that user's play state; they are
    never filed under the account the first page was asked as."""
    first, workers, factory, log = paged(monkeypatch, 95)

    def other_account():
        client = factory()
        client.user_id = "someone-else"
        return client

    with pytest.raises(ValueError, match="account changed during Movie"):
        fetch_kind(first, LIB, "Movie", api_factory=other_account)
    assert log == [0]  # no page was asked as the other account
    assert workers and all(w.closed == 1 for w in workers)


def test_a_server_that_caps_its_pages_is_read_one_page_at_a_time(monkeypatch):
    """A first page shorter than was asked for says the offsets cannot be
    known ahead: the listing is followed from where each page ended."""

    def capped(start, reply):
        reply["Items"] = reply["Items"][:7]
        return reply

    first, workers, factory, log = paged(monkeypatch, 40, capped)
    rows = fetch_kind(first, LIB, "Movie", api_factory=factory)
    assert [r["Id"] for r in rows] == ["m%04d" % n for n in range(40)]
    assert not workers
    assert log == list(range(0, 40, 7))


def test_two_pages_need_no_second_client(monkeypatch):
    first, workers, factory, log = paged(monkeypatch, 20)
    assert len(fetch_kind(first, LIB, "Movie", api_factory=factory)) == 20
    assert not workers and log == [0, 10]


def _whitelist(monkeypatch, *libraries):
    from kofin.sync.backends.api import library

    state = {"Whitelist": list(libraries)}
    monkeypatch.setattr(library.private, "get_sync", lambda: state)
    monkeypatch.setattr(
        library.private,
        "save_sync",
        lambda new: state.update(Whitelist=list(new["Whitelist"])),
    )
    return state


def _filed(store, monkeypatch, server, *libraries):
    """Libraries enumerated and with a native row for every item, as a
    profile an earlier build imported has them: no scanner format on record."""
    from kofin.sync import private

    w = worker(store, server, list(libraries), monkeypatch)
    state = _whitelist(monkeypatch)
    w.full_sync()
    for number, (item_id, record) in enumerate(store.records(pinned=False).items()):
        store.remember(item_id, record.generation, number + 1, {}, record.kind)
    with private.Database() as db:
        db.cursor.execute("DELETE FROM api_library")
    w._full_due = w._repair = False
    return w, state


def _pending(store):
    return {item_id: operation for item_id, _, operation, _ in store.pending_work()}


def test_a_repair_rebuilds_a_video_library_filed_under_an_older_scanner_format(
    store, monkeypatch
):
    """Streams have no setter, so a row filed without them gets them only by
    being filed again, and nothing but the viewer's own repair may take a
    library out of Kodi to do it."""
    from kofin.sync.backends.api import metadata

    server = Server({LIB: [movie(), movie("b")], LIB2: show_bundle()})
    w, state = _filed(store, monkeypatch, server, LIB, LIB2)
    assert store.scanner_format(LIB) == 1
    enumerations = []
    full_sync = w.full_sync
    monkeypatch.setattr(w, "full_sync", lambda *a: enumerations.append(a))
    monkeypatch.setattr(w, "catch_up", lambda: enumerations.append("catch-up"))

    w.command("RepairLibrary", {"Id": LIB})
    assert {i: e.library for i, e in store.tombstones().items()} == {"a": LIB, "b": LIB}
    assert state["Whitelist"] == [LIB2]
    assert w._rebuilding == {LIB} and w._full_due
    # Its first pass only removes; the repair is the pass after the listing.
    assert not w._repair
    # Listed while its rows are still in Kodi, it would be compared with them.
    w.refresh()
    assert enumerations == []
    store.forget_many([(i, store.state(i).desired) for i in ("a", "b")])
    assert not store.has_rows(LIB)
    w.refresh()
    assert enumerations == [()] and not w._rebuilding

    monkeypatch.setattr(w, "full_sync", full_sync)
    w.full_sync()
    assert sorted(state["Whitelist"]) == sorted([LIB, LIB2])
    assert store.scanner_format(LIB) == metadata.SCANNER_FORMAT
    pending = _pending(store)
    assert pending["a"] == pending["b"] == "upsert"
    # The shows were not picked and are as they were.
    assert store.scanner_format(LIB2) == 1 and store.has_rows(LIB2)
    # Once rebuilt, a repair of it is the comparison it always was.
    w._full_due = w._repair = False
    w.command("RepairLibrary", {"Id": LIB})
    assert store.tombstones() == {} and w._repair and not w._rebuilding


def test_a_rebuild_outlives_the_service_and_a_listing_asked_for_by_name(
    store, monkeypatch
):
    """The removal is minutes long. Listed before its old rows have left, by
    a restarted service or by a SyncLibrary for it, the library would be
    compared with those rows and the repair would have rebuilt nothing."""
    server = Server({LIB: [movie(), movie("b")], LIB2: show_bundle()})
    w, state = _filed(store, monkeypatch, server, LIB, LIB2)
    w.command("RepairLibrary", {"Id": LIB})
    assert store.rebuilding() == [LIB]

    # Asked for by name while its rows are still in Kodi.
    w.command("SyncLibrary", {"Id": LIB})
    assert _pending(store) == {"a": "remove", "b": "remove"}
    assert state["Whitelist"] == [LIB2]

    # A service started during the removal knows of the rebuild.
    again = worker(store, server, [LIB, LIB2], monkeypatch)
    _whitelist(monkeypatch, LIB2)
    again._rebuilding.update(store.rebuilding())
    enumerations = []
    monkeypatch.setattr(again, "full_sync", lambda *a: enumerations.append(a))
    again.refresh()
    assert enumerations == [] and again._rebuilding == {LIB}
    store.forget_many([(i, store.state(i).desired) for i in ("a", "b")])
    again.refresh()
    assert enumerations and not again._rebuilding


def test_a_repair_with_no_library_named_rebuilds_every_stale_video_library(
    store, monkeypatch
):
    music = "c" * 32
    server = Server(
        {LIB: [movie()], LIB2: show_bundle(), music: []},
        views=[
            {"Id": LIB, "CollectionType": "movies", "Name": "Films"},
            {"Id": LIB2, "CollectionType": "tvshows", "Name": "Shows"},
            {"Id": music, "CollectionType": "music", "Name": "Music"},
        ],
    )
    w, state = _filed(store, monkeypatch, server, LIB, LIB2, music)
    w.command("RepairLibrary", {})
    assert w._rebuilding == {LIB, LIB2}
    # A music library has no such rows and stays where it is.
    assert state["Whitelist"] == [music]


def test_a_library_is_recorded_under_the_format_its_rows_are_filed_with(
    store, monkeypatch
):
    from kofin.sync import private
    from kofin.sync.backends.api import metadata

    server = Server({LIB: [movie()], LIB2: show_bundle()})
    w = worker(store, server, [LIB], monkeypatch)
    state = _whitelist(monkeypatch)
    w.full_sync()
    assert store.scanner_format(LIB) == metadata.SCANNER_FORMAT
    # Deselected and selected again before its rows had left: they are the
    # rows of whatever build filed them, and the record must not say more.
    record = store.records(pinned=False)["a"]
    store.remember("a", record.generation, 1, {}, "Movie")
    with private.Database() as db:
        db.cursor.execute("DELETE FROM api_library")
    state["Whitelist"] = []
    w.full_sync()
    assert state["Whitelist"] == [LIB] and store.scanner_format(LIB) == 1


def _user_data(store, item_id):
    return store.item(item_id).get("UserData") or {}


def test_the_server_user_data_of_the_playing_item_waits_for_its_stop(
    store, monkeypatch
):
    """The server counts a play when it starts. Written to Kodi then, the
    count was added to again by Kodi's own player at the end: one play of a
    song, two counts."""
    from kofin.sync.backends.api import library

    store.publish([movie(UserData={}), movie("b", UserData={})], library=LIB)
    w = worker(store, Server({LIB: [movie(), movie("b")]}), [LIB], monkeypatch)
    playing = [{"Id": "a"}]
    w.player = SimpleNamespace(current_item=lambda: playing[0])
    clock = [1000.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])

    w.command("userdata", [{"ItemId": "a", "Played": True, "PlayCount": 1}])
    w.command("userdata", [{"ItemId": "b", "Played": True, "PlayCount": 1}])
    assert not _user_data(store, "a").get("Played")
    # What is not playing here is merged as it arrives.
    assert _user_data(store, "b")["Played"]
    # Progress reports replace the entry held, and nothing is applied
    # however long the playback lasts.
    w.command("userdata", [{"ItemId": "a", "Played": True, "PlayCount": 1, "X": 1}])
    clock[0] += 3600
    w.release_userdata()
    assert not _user_data(store, "a").get("Played") and list(w._held_userdata) == ["a"]

    # The stop: the server's own entry for it is applied and nothing is held.
    playing[0] = None
    w.command("userdata", [{"ItemId": "a", "Played": True, "PlayCount": 2}])
    assert _user_data(store, "a")["PlayCount"] == 2
    assert w._held_userdata == {}


def test_held_user_data_the_server_never_replaced_is_applied_after_the_stop(
    store, monkeypatch
):
    from kofin.sync.backends.api import library

    store.publish([movie(UserData={})], library=LIB)
    w = worker(store, Server({LIB: [movie()]}), [LIB], monkeypatch)
    playing = [{"Id": "a"}]
    w.player = SimpleNamespace(current_item=lambda: playing[0])
    clock = [1000.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    w.command("userdata", [{"ItemId": "a", "Played": True, "PlayCount": 1}])
    playing[0] = None
    w.release_userdata()
    clock[0] += library.HELD_USERDATA_GRACE - 1
    w.release_userdata()
    assert not _user_data(store, "a").get("Played")
    clock[0] += 2
    w.release_userdata()
    assert _user_data(store, "a")["Played"] and w._held_userdata == {}
    # Played again before the grace was over: held for the new playback.
    w.command("userdata", [{"ItemId": "a", "Played": False, "PlayCount": 2}])
    playing[0] = {"Id": "a"}
    w.command("userdata", [{"ItemId": "a", "Played": False, "PlayCount": 3}])
    playing[0] = None
    w.release_userdata()
    playing[0] = {"Id": "a"}
    clock[0] += library.HELD_USERDATA_GRACE + 5
    w.release_userdata()
    assert _user_data(store, "a")["PlayCount"] == 2


def test_a_watched_mark_goes_to_the_server_without_a_count(store, monkeypatch):
    """Kodi counts its own plays and the server the ones it is told of. The
    count sent with the mark replaced the server's: a song played once went
    up as 2."""
    server = Server({LIB: [movie()]})
    store.publish([movie()], library=LIB)
    w = worker(store, server, [LIB], monkeypatch)
    store.local("a", {"playcount": 4})
    w.flush_local()
    assert server.updated == [("a", {"Played": True})]
    store.local("a", {"playcount": 0, "position": 30.0})
    w.flush_local()
    assert server.updated[-1] == (
        "a",
        {"Played": False, "PlaybackPositionTicks": 300000000},
    )
    assert store.local_pending() == []
