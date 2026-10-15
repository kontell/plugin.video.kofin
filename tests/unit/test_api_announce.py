"""New content in the API build: what a catch-up brings is announced once the
pass has filed it, and nothing else is."""

import time
from types import SimpleNamespace

import pytest

from kofin.sync.backends.api import announce
from tests.unit.apifixtures import (  # noqa: F401
    LIB,
    LIB2,
    SHOW,
    Feed,
    Server,
    episode,
    movie,
    show_bundle,
    store,
    worker,
)
from tests.unit.test_new_content import load_strings


@pytest.fixture
def toasts(monkeypatch):
    strings = load_strings()
    monkeypatch.setattr(
        announce.newcontent.settings, "localized", lambda string_id: strings[string_id]
    )
    shown = []
    monkeypatch.setattr(
        announce.toast, "show", lambda message, **_: shown.append(message)
    )
    switch = {"notifyNewContent": True}
    monkeypatch.setattr(announce.settings, "get_bool", lambda key: switch.get(key))
    queued = []
    monkeypatch.setattr(
        announce.downloads_auto,
        "queue_new_content",
        lambda api, entries: queued.append([e.item_id for e in entries]),
    )
    return SimpleNamespace(shown=shown, switch=switch, queued=queued)


def _caught_up(store, monkeypatch, known, records, libraries=None):
    """A worker whose catalogue holds ``known`` and whose feed brings
    ``records``."""
    from kofin.sync.changefeed import ChangeRecord, unix_to_watermark

    server = Server(libraries or {LIB: known + [movie("b"), movie("c")]})
    store.publish(known, library=LIB)
    store.set_watermark(watermark=unix_to_watermark(500), enumerated=time.time())
    feed = Feed(
        [ChangeRecord(i, status, library_ids=[lib]) for i, status, lib in records]
    )
    w = worker(store, server, [LIB, LIB2], monkeypatch, feed=feed)
    w.player = SimpleNamespace(isPlayingVideo=lambda: False, current_item=lambda: None)
    return w, server


def _file(store, *item_ids):
    """What the pass does for a row it filed: acknowledge its generation."""
    for number, item_id in enumerate(item_ids):
        state = store.state(item_id)
        kind = store.entry(item_id).kind
        assert store.remember(item_id, state.desired, number + 1, {}, kind)


def test_a_new_item_is_announced_once_the_pass_has_filed_it(store, monkeypatch, toasts):
    w, _ = _caught_up(store, monkeypatch, [movie()], [("b", "Added", LIB)])
    w.catch_up()
    w.announce_new_content()
    assert toasts.shown == [] and toasts.queued == []

    _file(store, "b")
    w.announce_new_content()
    assert toasts.shown == ["Fixture b movie added to library"]
    assert toasts.queued == [["b"]]
    # Once.
    w.announce_new_content()
    assert len(toasts.shown) == 1


def test_an_item_the_catalogue_held_is_not_new(store, monkeypatch, toasts):
    w, server = _caught_up(store, monkeypatch, [movie()], [("a", "Updated", LIB)])
    server.libraries[LIB][0]["Overview"] = "Edited"
    w.catch_up()
    _file(store, "a")
    w.announce_new_content()
    assert toasts.shown == [] and toasts.queued == []


def test_items_landing_together_are_one_toast_and_a_new_show_names_itself(
    store, monkeypatch, toasts
):
    bundle = show_bundle(episodes=3)
    w, _ = _caught_up(
        store,
        monkeypatch,
        [movie()],
        [(item["Id"], "Added", LIB2) for item in bundle]
        + [("b", "Added", LIB), ("c", "Added", LIB)],
        libraries={LIB: [movie(), movie("b"), movie("c")], LIB2: bundle},
    )
    w.catch_up()
    _file(store, "b", "c", *[item["Id"] for item in bundle])
    w.announce_new_content()
    assert toasts.shown == [
        "2 movies added to library",
        "Show a1 show added to library",
    ]


def test_episodes_of_a_known_show_are_counted_under_it(store, monkeypatch, toasts):
    bundle = show_bundle(episodes=1)
    w, _ = _caught_up(
        store,
        monkeypatch,
        [],
        [("e9", "Added", LIB2)],
        libraries={LIB2: bundle + [episode("e9", number=9)]},
    )
    store.publish(bundle, library=LIB2)
    w.catch_up()
    _file(store, "e9")
    w.announce_new_content()
    assert toasts.shown == ["1 episode of Show added to library"]


def test_a_played_item_and_a_song_are_not_announced(store, monkeypatch, toasts):
    from tests.unit.apifixtures import movie as make

    w, server = _caught_up(store, monkeypatch, [movie()], [("b", "Added", LIB)])
    server.libraries[LIB][1] = make("b", UserData={"Played": True})
    w.catch_up()
    _file(store, "b")
    w.announce_new_content()
    assert toasts.shown == [] and toasts.queued == []

    arrivals = announce.Arrivals()
    arrivals.published(
        [{"Id": "s", "Type": "Audio", "Name": "Track", "UserData": {}}], set()
    )
    assert arrivals.arriving == {}


def test_a_complete_listing_announces_nothing(store, monkeypatch, toasts):
    from kofin.sync.backends.api import library

    server = Server({LIB: [movie(), movie("b")]})
    store.publish([movie()], library=LIB)
    w = worker(store, server, [LIB], monkeypatch)
    w.player = SimpleNamespace(isPlayingVideo=lambda: False, current_item=lambda: None)
    monkeypatch.setattr(library.private, "get_sync", lambda: {"Whitelist": [LIB]})
    w.full_sync()
    _file(store, "a", "b")
    w.announce_new_content()
    assert toasts.shown == [] and toasts.queued == []


def test_an_item_removed_before_it_was_filed_is_dropped(store, monkeypatch, toasts):
    w, _ = _caught_up(store, monkeypatch, [movie()], [("b", "Added", LIB)])
    w.catch_up()
    w.command("removed", ["b"])
    w.announce_new_content()
    assert w.arrivals.arriving == {}
    assert toasts.shown == []


def test_the_toast_waits_for_a_video_to_end_and_the_download_does_not(
    store, monkeypatch, toasts
):
    w, _ = _caught_up(store, monkeypatch, [movie()], [("b", "Added", LIB)])
    playing = [True]
    w.player = SimpleNamespace(
        isPlayingVideo=lambda: playing[0], current_item=lambda: None
    )
    monkeypatch.setattr(announce.xbmc, "getCondVisibility", lambda _: False)
    w.catch_up()
    _file(store, "b")
    w.announce_new_content()
    assert toasts.shown == [] and toasts.queued == [["b"]]

    playing[0] = False
    w.announce_new_content()
    assert toasts.shown == ["Fixture b movie added to library"]
    assert toasts.queued == [["b"]]


def test_turning_the_toasts_off_drops_what_is_held(store, monkeypatch, toasts):
    w, _ = _caught_up(store, monkeypatch, [movie()], [("b", "Added", LIB)])
    toasts.switch["notifyNewContent"] = False
    w.catch_up()
    _file(store, "b")
    w.announce_new_content()
    toasts.switch["notifyNewContent"] = True
    w.announce_new_content()
    assert toasts.shown == [] and toasts.queued == [["b"]]


def test_the_catch_up_by_save_date_announces_too(store, monkeypatch, toasts):
    server = Server(
        {
            LIB: [
                movie(DateLastSaved="2026-01-01T00:00:00Z"),
                movie("b", DateLastSaved="2026-10-09T00:00:00Z"),
            ]
        }
    )
    store.publish([movie()], library=LIB)
    store.set_watermark(watermark="2026-10-08T00:00:00Z", enumerated=time.time())
    w = worker(store, server, [LIB], monkeypatch)
    w.player = None
    w.catch_up()
    _file(store, "b")
    w.announce_new_content()
    assert toasts.shown == ["Fixture b movie added to library"]
