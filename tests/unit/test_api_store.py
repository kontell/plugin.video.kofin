"""The API store: generations as intervals, one payload copy, tombstones."""

import json

import pytest

from kofin.sync import private
from kofin.sync.backends.api.store import Store
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


def test_publication_pin_tombstone_restart_and_old_ack(store):
    store.publish([movie()], library=LIB)
    first = store.pin()
    store.publish([movie(Overview="New")], library=LIB)
    assert store.pin() == first
    assert store.records(pinned=False)["a"].item["Overview"] == "New"
    assert store.records()["a"].generation == 2
    assert not store.remember("a", 1, 10, {})
    reopened = Store(store.namespace)
    assert reopened.state("a").applied == 0
    reopened.unpin()
    assert reopened.records()["a"].generation == 2
    reopened.publish([], library=LIB)
    assert reopened.records() == {}
    assert reopened.state("a").operation == "remove"
    assert reopened.tombstones()["a"].library == LIB
    assert len(reopened.pending()) == 1
    assert reopened.forget("a", 3)
    assert json.loads(reopened.item("a") or "null") is None
    assert not reopened.pending()


def test_invalid_publication_is_atomic(store):
    store.publish([movie()], library=LIB)
    before = store.records(), store.generation()
    with pytest.raises(ValueError):
        store.publish([movie("b"), movie("c", Type="Photo")], library=LIB)
    with pytest.raises(ValueError):
        store.publish([movie("b")])
    with pytest.raises(ValueError):
        store.publish([episode("e1", SeriesId=None)], library=LIB)
    assert (store.records(), store.generation()) == before
    assert store.state("b") is None


def test_closed_intervals_are_collected_once_unreachable(store):
    store.publish([movie(), movie("b")], library=LIB)
    pinned = store.pin()
    store.publish([movie()], library=LIB)
    assert set(store.records()) == {"a", "b"}
    assert set(store.records(pinned=False)) == {"a"}
    with private.Database() as db:
        rows = db.cursor.execute("SELECT COUNT(*) FROM api_entry").fetchone()[0]
    assert rows == 2
    store.unpin()
    store.publish([movie(Overview="x")], library=LIB)
    with private.Database() as db:
        rows = db.cursor.execute("SELECT COUNT(*) FROM api_entry").fetchone()[0]
    # The pending removal keeps its last placement until it is applied.
    assert rows == 2
    assert store.tombstones()["b"].library == LIB
    assert store.forget("b", 2)
    with private.Database() as db:
        rows = db.cursor.execute("SELECT COUNT(*) FROM api_entry").fetchone()[0]
    assert rows == 1
    assert pinned == 1


def test_library_move_is_pending_without_metadata_change(store):
    store.publish([movie()], library=LIB)
    store.remember("a", 1, 7, {})
    store.publish([movie()], library=LIB2)
    assert store.state("a").desired == 2 and store.state("a").applied == 1
    assert store.entry("a").library == LIB2
    assert store.publish([movie()], library=LIB2) == store.generation()


def test_pending_work_carries_payloads_only_for_removals(store):
    """A pass holds one record per pending item; loading every payload with
    them was 200 MB of Python for 6,600 video items and wedged a 1 GB
    device. Only a tombstone, which names what it removes, travels with
    the work; an upsert's payload is read on demand."""
    store.publish([movie(), movie("b")], library=LIB)
    store.publish([movie("b")], library=LIB)
    work = {item_id: rest for item_id, *rest in store.pending_work()}
    assert work["b"][1] == "upsert" and work["b"][2] == {}
    assert work["a"][1] == "remove" and work["a"][2].get("Type") == "Movie"
    lazy = store.records(payloads=False)["b"]
    assert not lazy.loaded
    assert lazy.item["Id"] == "b"
    assert lazy == store.records()["b"]


def test_payload_window_holds_a_bounded_number_of_payloads(store):
    from kofin.sync.backends.api.records import PayloadWindow, Record

    store.publish([movie("m%03d" % i) for i in range(30)], library=LIB)
    window = PayloadWindow(store, size=8)
    records = [
        Record(e.item_id, e.kind, e.library, e.parent_id, None, 1, loader=window)
        for e in store.entries().values()
    ]
    window.CHUNK = 5
    seen = [r.item["Id"] for r in window.walk(records)]
    assert sorted(seen) == sorted(r.item_id for r in records)
    assert len(window._held) <= 8
    # Six chunks of five: one query each, and no single-row read behind them.
    assert window.reads == 6
    assert window("m000")["Id"] == "m000"
    assert window.reads == 7
