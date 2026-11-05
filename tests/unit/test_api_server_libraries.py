"""The selected libraries against the server's: hidden, deleted, unreachable."""

import pytest

from kofin.core.http import HttpError, Unauthorized
from kofin.sync.backends.api import library
from kofin.sync.backends.api.library import Library
from tests.unit.apifixtures import (  # noqa: F401
    LIB,
    LIB2,
    Server,
    movie,
    store,
    worker,
)


@pytest.fixture
def gone(store, monkeypatch):
    """A worker selecting LIB and LIB2 where the server lists LIB alone, with
    what leaving a library does recorded instead of done."""
    server = Server({LIB: [movie()], LIB2: []})
    server.views_list = [v for v in server.views_list if v["Id"] != LIB2]
    w = worker(store, server, [LIB, LIB2], monkeypatch)
    seen = {"dropped": [], "written": [], "shown": [], "recorded": []}
    clock = [1000.0]
    monkeypatch.setattr(library.private, "get_sync", lambda: {"Whitelist": [LIB, LIB2]})
    monkeypatch.setattr(w, "drop_libraries", seen["dropped"].extend)
    monkeypatch.setattr(
        library.settings,
        "set_str",
        lambda key, value: seen["written"].append((key, value)),
    )
    monkeypatch.setattr(library.settings, "localized", lambda i: "%s gone")
    monkeypatch.setattr(
        library.toast, "show", lambda message, **kw: seen["shown"].append(message)
    )
    monkeypatch.setattr(
        library.apistatus,
        "record_missing",
        lambda names, namespace="": seen["recorded"].append(names),
    )
    monkeypatch.setattr(
        Library, "_view_names", staticmethod(lambda: {LIB2: "Old shows"})
    )
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])

    def check(after=0.0):
        clock[0] += after
        w._supported_views()
        w.settle_missing()

    return server, w, seen, check


@pytest.mark.parametrize("spelling", [LIB, "Mixed:" + LIB])
def test_a_library_stored_as_mixed_leaves_by_either_spelling(
    store, monkeypatch, spelling
):
    """A server's deletion names the bare id and RemoveLibrary the stored
    entry; the items are filed under the bare id either way."""
    store.publish([movie()], library=LIB)
    w = worker(store, Server({LIB: [movie()]}), [LIB], monkeypatch)
    state = {"Whitelist": ["Mixed:" + LIB, LIB2]}
    monkeypatch.setattr(library.private, "get_sync", lambda: dict(state))
    monkeypatch.setattr(library.private, "save_sync", state.update)
    w.drop_libraries([spelling])
    assert state["Whitelist"] == [LIB2]
    assert store.state("a").operation == "remove"


def test_a_hidden_library_keeps_syncing(store, monkeypatch):
    """Hidden from My Media is the home screen's setting, not the sync's."""
    server = Server({LIB: [movie()], LIB2: []})
    server.hidden = {LIB}
    w = worker(store, server, [LIB], monkeypatch)
    w.full_sync()
    assert "a" in store.records(pinned=False)


def test_a_library_the_server_says_is_gone_leaves_after_two_404s(gone):
    server, w, seen, check = gone
    server.gone = {LIB2}
    check()
    assert seen["dropped"] == [] and seen["written"] == []
    assert seen["recorded"][-1] == ["Old shows"]
    check(after=library.MISSING_CONFIRM - 1)
    assert seen["dropped"] == []
    check(after=1)
    assert seen["dropped"] == [LIB2]
    assert seen["written"] == [("librarySelection", LIB)]
    assert seen["shown"] == ["Old shows gone"]
    assert seen["recorded"][-1] == []


@pytest.mark.parametrize(
    "error",
    [
        OSError("offline"),
        HttpError(500, "GET /Items -> 500"),
        Unauthorized("GET /Items -> 401"),
    ],
)
def test_a_library_the_server_cannot_answer_for_stays(gone, error):
    """Anything but a 404 is a server that could not say."""
    server, w, seen, check = gone

    def unreachable(item_id):
        raise error

    server.item = unreachable
    check()
    check(after=library.MISSING_CONFIRM * 2)
    assert seen["dropped"] == [] and seen["written"] == [] and seen["shown"] == []


def test_a_library_listed_again_needs_two_new_404s(gone):
    server, w, seen, check = gone
    server.gone = {LIB2}
    check()
    server.views_list.append({"Id": LIB2, "CollectionType": "tvshows", "Name": "B"})
    check(after=library.MISSING_CONFIRM)
    assert seen["recorded"][-1] == []
    server.views_list.pop()
    check(after=library.MISSING_CONFIRM)
    assert seen["dropped"] == []
    check(after=library.MISSING_CONFIRM)
    assert seen["dropped"] == [LIB2]


def test_a_library_selected_but_never_synced_leaves_the_selection_only(
    gone, monkeypatch
):
    server, w, seen, check = gone
    server.gone = {LIB2}
    monkeypatch.setattr(library.private, "get_sync", lambda: {"Whitelist": [LIB]})
    check()
    check(after=library.MISSING_CONFIRM)
    assert seen["dropped"] == []
    assert seen["written"] == [("librarySelection", LIB)]


def test_a_library_found_by_its_id_needs_two_new_404s(gone):
    """An item that answers is there, listed or not."""
    server, w, seen, check = gone
    server.gone = {LIB2}
    check()
    found = server.item
    server.gone = set()
    server.item = lambda item_id: {"Id": item_id}
    check(after=library.MISSING_CONFIRM)
    assert seen["recorded"][-1] == []
    server.item = found
    server.gone = {LIB2}
    check(after=library.MISSING_CONFIRM)
    assert seen["dropped"] == []
    check(after=library.MISSING_CONFIRM)
    assert seen["dropped"] == [LIB2]


def _grouped(server, library, collection="movies"):
    """Jellyfin's answer once the user groups ``library`` into a combined
    view: /UserViews lists the combined view, the library answers by id."""
    server.views_list = [v for v in server.views_list if v["Id"] != library] + [
        {"Id": "c" * 32, "CollectionType": collection, "Name": "Movies"}
    ]
    by_id = server.item

    def item(item_id):
        if item_id == library:
            return {
                "Id": library,
                "Type": "CollectionFolder",
                "CollectionType": collection,
                "Name": "Films",
            }
        return by_id(item_id)

    server.item = item


def test_a_library_grouped_into_a_combined_view_keeps_syncing(store, monkeypatch):
    server = Server({LIB: [movie()], LIB2: []})
    _grouped(server, LIB)
    w = worker(store, server, [LIB], monkeypatch)

    w.full_sync()

    assert "a" in store.records(pinned=False)


def test_a_change_to_an_item_of_a_grouped_library_is_published(store, monkeypatch):
    server = Server({LIB: [movie()], LIB2: []})
    store.publish([movie()], library=LIB)
    _grouped(server, LIB)
    w = worker(store, server, [LIB], monkeypatch)
    w._catchup_due = 10**9
    server.libraries[LIB] = [movie(Name="Renamed")]

    w.command("changed", ["a"])

    assert store.records(pinned=False)["a"].item["Name"] == "Renamed"
