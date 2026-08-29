"""The one library walk (docs/sync-refactor-phase1-plan.md P1.3).

``FullSync._walk`` is what movies(), the three tvshows passes,
musicvideos() and boxsets() all run, so the mid-page-404 skip that only the
tvshows copy used to have now covers every caller. These tests drive the
walk with a fake pager and fake writers -- no database, no server -- and
check both halves: the walk's own mechanics, and that each caller really
goes through it."""

import time
from contextlib import contextmanager

import pytest

from kofin.core.http import HttpError
from kofin.sync.full_sync import FullSync
from kofin.sync.shims import LibraryOrphanException

LIBRARY = {"Id": "lib1", "Name": "Movies", "CollectionType": "movies"}


@pytest.fixture
def fullsync(monkeypatch):
    monkeypatch.setattr("kofin.sync.full_sync.save_sync", lambda sync: None)
    monkeypatch.setattr("kofin.sync.full_sync.notification", lambda *a, **kw: None)
    monkeypatch.setattr(
        "kofin.sync.full_sync.server.restore_fingerprint", lambda *a, **kw: "fp"
    )
    sync = FullSync(None, None)
    sync.sync = {"Libraries": [], "Whitelist": [], "RestorePoints": {}}
    return sync


def pages(monkeypatch, *page_items):
    """Stand in for server.get_items: one yielded dict per page, shaped the
    way the pager shapes them (RestorePoint carrying the page's StartIndex)."""
    total = sum(len(p) for p in page_items)
    calls = []

    def get_items(api, parent_id, item_type=None, basic=False, params=None):
        calls.append((parent_id, item_type, params))
        start = 0
        for items in page_items:
            yield {
                "Items": list(items),
                "TotalRecordCount": total,
                "RestorePoint": {"params": {"StartIndex": start}},
            }
            start += len(items)

    monkeypatch.setattr("kofin.sync.full_sync.server.get_items", get_items)
    return calls


@contextmanager
def fake_page():
    yield ("videodb", "jellyfindb")


class Dialog:
    def __init__(self):
        self.updates = []

    def update(self, percent, heading=None, message=None):
        self.updates.append((percent, heading, message))


def item(item_id, name=None):
    return {"Id": item_id, "Name": name or item_id}


# -- the walk itself -----------------------------------------------------------


def test_walk_skips_a_404_and_an_orphan_and_keeps_going(fullsync, monkeypatch):
    pages(monkeypatch, [item("a"), item("gone"), item("orphan")], [item("b")])
    written = []

    def apply(obj, it):
        if it["Id"] == "gone":
            raise HttpError(404, "GET /Items/gone -> 404")
        if it["Id"] == "orphan":
            raise LibraryOrphanException("parent missing")
        written.append(it["Id"])
        return "value-%s" % it["Id"]

    dialog = Dialog()
    resumed, skipped, results = fullsync._walk(
        LIBRARY,
        "Movie",
        "lib1/movies",
        lambda jellyfindb, videodb: "writer",
        apply,
        lambda it: it["Name"],
        dialog,
        "Kofin: Movies",
        fake_page,
    )

    assert resumed is False
    assert skipped == ["gone", "orphan"]
    assert written == ["a", "b"]
    assert [(it["Id"], value) for it, value in results] == [
        ("a", "value-a"),
        ("b", "value-b"),
    ]
    # Every item got a progress line with the caller's heading, and the
    # percentage is the item's position over the total.
    assert [u[0] for u in dialog.updates] == [0, 25, 50, 75]
    assert {u[1] for u in dialog.updates} == {"Kofin: Movies"}
    # The restore point advanced once per page and carries the fingerprint.
    assert fullsync.sync["RestorePoints"]["lib1/movies"]["params"]["StartIndex"] == 3


def test_walk_lets_anything_but_a_404_abort_the_pass(fullsync, monkeypatch):
    pages(monkeypatch, [item("a"), item("bad"), item("never")])
    seen = []

    def apply(obj, it):
        seen.append(it["Id"])
        if it["Id"] == "bad":
            raise HttpError(500, "GET /Items/bad -> 500")

    with pytest.raises(HttpError):
        fullsync._walk(
            LIBRARY,
            "Movie",
            "lib1/movies",
            lambda j, v: None,
            apply,
            lambda it: it["Name"],
            Dialog(),
            "h",
            fake_page,
        )

    assert seen == ["a", "bad"]


def test_walk_resumes_from_a_matching_restore_point(fullsync, monkeypatch):
    calls = pages(monkeypatch, [item("a")])
    fullsync.sync["RestorePoints"]["lib1/movies"] = {
        "params": {"StartIndex": 40},
        "Fingerprint": "fp",
        "SavedAt": time.time(),
    }

    resumed, _, _ = fullsync._walk(
        LIBRARY,
        "Movie",
        "lib1/movies",
        lambda j, v: None,
        lambda obj, it: None,
        lambda it: it["Name"],
        Dialog(),
        "h",
        fake_page,
    )

    assert resumed is True
    # The saved point's params are what the pager was asked to resume from.
    assert calls[0][2] == {"StartIndex": 40}


def test_walk_constructs_the_writer_per_page_inside_the_page_scope(
    fullsync, monkeypatch
):
    pages(monkeypatch, [item("a")], [item("b")])
    events = []

    @contextmanager
    def page():
        events.append("enter")
        yield ("v", "j")
        events.append("exit")

    def writer(jellyfindb, videodb):
        events.append("writer(%s,%s)" % (jellyfindb, videodb))
        return "w"

    fullsync._walk(
        LIBRARY,
        "Movie",
        "k",
        writer,
        lambda obj, it: events.append("apply"),
        lambda it: it["Name"],
        Dialog(),
        "h",
        page,
    )

    assert events == [
        "enter",
        "writer(j,v)",
        "apply",
        "exit",
        "enter",
        "writer(j,v)",
        "apply",
        "exit",
    ]


# -- every caller goes through it ---------------------------------------------


def recording_walk(fullsync, monkeypatch, results=None):
    calls = []

    def _walk(
        library,
        item_type,
        restore_key,
        writer,
        apply,
        describe,
        dialog,
        heading,
        page,
        params=None,
    ):
        calls.append(
            {
                "type": item_type,
                "key": restore_key,
                "heading": heading,
                "params": params,
                "page": page,
                "library": library["Id"],
            }
        )
        return False, [], list(results or [])

    monkeypatch.setattr(fullsync, "_walk", _walk)
    monkeypatch.setattr(fullsync, "_held_connections", fake_scope)
    return calls


@contextmanager
def fake_scope():
    yield fake_page


def test_movies_walks_once_with_its_restore_key(fullsync, monkeypatch):
    calls = recording_walk(fullsync, monkeypatch)

    fullsync.movies(LIBRARY)

    assert [(c["type"], c["key"], c["heading"]) for c in calls] == [
        ("Movie", "lib1/movies", "Kofin: Movies")
    ]
    assert calls[0]["page"] is fake_page
    assert "lib1/movies" not in fullsync.sync["RestorePoints"]


def test_musicvideos_walks_once_with_its_restore_key(fullsync, monkeypatch):
    calls = recording_walk(fullsync, monkeypatch)

    fullsync.musicvideos(LIBRARY)

    assert [(c["type"], c["key"]) for c in calls] == [
        ("MusicVideo", "lib1/musicvideos")
    ]


def test_tvshows_walks_three_passes_parents_first(fullsync, monkeypatch):
    calls = recording_walk(fullsync, monkeypatch)
    fullsync.sync["RestorePoints"]["lib1/tvshows"] = {"legacy": True}

    fullsync.tvshows(LIBRARY)

    assert [(c["type"], c["key"]) for c in calls] == [
        ("Series", "lib1/tvshows-series"),
        ("Season", "lib1/tvshows-seasons"),
        ("Episode", "lib1/tvshows-episodes"),
    ]
    # One connection scope for all three passes, cleared together at the end,
    # the pre-phase-5 key included.
    assert len({id(c["page"]) for c in calls}) == 1
    assert fullsync.sync["RestorePoints"] == {}


def test_boxsets_walks_with_the_child_count_field_and_tallies_outcomes(
    fullsync, monkeypatch
):
    from kofin.sync.writers.movies import BOXSET_GUARDED, BOXSET_WRITTEN

    calls = recording_walk(
        fullsync,
        monkeypatch,
        results=[(item("set1"), BOXSET_WRITTEN), (item("set2"), BOXSET_GUARDED)],
    )
    swept = []
    restamped = []
    monkeypatch.setattr(
        fullsync, "sweep_stale_boxsets", lambda walked: swept.append(set(walked)) or 0
    )

    class Restamper:
        def __init__(self, *args, **kwargs):
            pass

        def restamp_boxset_states(self, guarded):
            restamped.append(set(guarded))

    monkeypatch.setattr("kofin.sync.boxsets.Movies", Restamper)
    monkeypatch.setattr(fullsync, "video_database_locks", fake_page)
    monkeypatch.setattr("kofin.sync.boxsets.localized", lambda code: "Collections")

    fullsync.boxsets({"Id": "cols", "Name": "Collections"})

    assert calls[0]["type"] == "BoxSet"
    assert calls[0]["key"] == "cols/boxsets"
    assert "ChildCount" in calls[0]["params"]["Fields"]
    # Lock-first, fresh connections per page: the boxsets shape.
    assert calls[0]["page"] is fake_page
    assert swept == [{"set1", "set2"}]
    assert restamped == [{"set2"}]


def test_a_movie_gone_mid_page_no_longer_aborts_the_library(fullsync, monkeypatch):
    """Only the tvshows copy of the walk skipped a 404 on an item deleted
    after it was paged; the other three copies let it abort the library.
    The fake writer raises the way an unguarded child fetch does -- the
    boxset writer's get_movies_by_boxset is the live case, and since P2.5c
    the movie writer's own trailer and special-features fetches re-raise a
    404 too (they swallowed it before; assessment §3 erratum). Whatever
    raises, the walk now skips the item and completes."""
    pages(monkeypatch, [item("gone"), item("kept")])
    written = []

    class FakeMovies:
        def __init__(self, *args, **kwargs):
            pass

        def movie(self, movie):
            if movie["Id"] == "gone":
                raise HttpError(404, "GET /Items/gone/LocalTrailers -> 404")
            written.append(movie["Id"])

    monkeypatch.setattr("kofin.sync.full_sync.Movies", FakeMovies)
    monkeypatch.setattr(fullsync, "_held_connections", fake_scope)

    fullsync.movies(LIBRARY)

    assert written == ["kept"]
    assert "lib1/movies" not in fullsync.sync["RestorePoints"]


# -- the gone-probe: a child fetch's status is the endpoint's, not the item's --


class ProbingServer:
    def __init__(self, gone_ids):
        self.gone = set(gone_ids)
        self.asked = []

    def item(self, item_id):
        self.asked.append(item_id)
        if item_id in self.gone:
            raise HttpError(404, "GET /Items/%s -> 404" % item_id)
        return {"Id": item_id}


def test_a_non_404_child_fetch_on_a_gone_item_is_skipped_after_a_probe(
    fullsync, monkeypatch
):
    """Jellyfin 12 answers /Items?ParentId=<deleted set> with 400, not 404
    (live, S-P1.3c). The walk asks for the item itself once and skips it
    when that says gone."""
    fullsync.server = ProbingServer({"gone-set"})
    pages(monkeypatch, [item("gone-set"), item("kept")])
    written = []

    def apply(obj, it):
        if it["Id"] == "gone-set":
            raise HttpError(400, "GET /Items?ParentId=gone-set -> 400")
        written.append(it["Id"])

    _, skipped, _ = fullsync._walk(
        LIBRARY,
        "BoxSet",
        "k",
        lambda j, v: None,
        apply,
        lambda it: it["Name"],
        Dialog(),
        "h",
        fake_page,
    )

    assert skipped == ["gone-set"] and written == ["kept"]
    assert fullsync.server.asked == ["gone-set"]


def test_a_non_404_child_fetch_on_a_present_item_still_aborts(fullsync, monkeypatch):
    """A 400 for an item that is still there is a real failure -- a malformed
    query, a server bug -- and must stop the pass as before."""
    fullsync.server = ProbingServer(set())
    pages(monkeypatch, [item("present")])

    def apply(obj, it):
        raise HttpError(400, "GET /Items?ParentId=present -> 400")

    with pytest.raises(HttpError):
        fullsync._walk(
            LIBRARY,
            "BoxSet",
            "k",
            lambda j, v: None,
            apply,
            lambda it: it["Name"],
            Dialog(),
            "h",
            fake_page,
        )

    assert fullsync.server.asked == ["present"]


def test_a_plain_404_skips_without_probing(fullsync, monkeypatch):
    fullsync.server = ProbingServer(set())
    pages(monkeypatch, [item("gone")])

    def apply(obj, it):
        raise HttpError(404, "GET /Shows/gone/Seasons -> 404")

    _, skipped, _ = fullsync._walk(
        LIBRARY,
        "Series",
        "k",
        lambda j, v: None,
        apply,
        lambda it: it["Name"],
        Dialog(),
        "h",
        fake_page,
    )

    assert skipped == ["gone"] and fullsync.server.asked == []
