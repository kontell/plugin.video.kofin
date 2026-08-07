"""L1 units for the phase-5 full-sync overhaul: the update-mode prune
planner (ids+Etag three-way diff), the local reference map incl. the TV
child walk, the ids+Etag pager, the newest-first default sort, and restore
points resuming under their recorded sort (plan §6).

Also the pager's failure behaviour: sort pairs the server will accept, a
rejected query failing its pass rather than emptying it, abandoning the
generator (Kodi quitting mid-sync) not deadlocking on executor shutdown, and
the prune pager refusing a map that came back short of the server's count
rather than handing the shortfall to the removal arm.

And the removal arm's confirmation step: a stale candidate the server still
resolves by id is spared, one it does not is still removed, and a failed
confirmation removes nothing at all."""

import threading
import time

import pytest

from kofin.core.http import HttpError
from kofin.sync import db as sync_db
from kofin.sync import downloader
from kofin.sync import kofindb
from kofin.sync import shims
from kofin.sync.full_sync import FullSync
from tests.unit.fakes import FakeAddon, FakeWindow


class _FakeMonitor:
    def abortRequested(self):
        return False

    def waitForAbort(self, seconds=0):
        return False


@pytest.fixture(autouse=True)
def sync_env(monkeypatch, tmp_path):
    FakeAddon.store = {"limitThreads": "3", "limitIndex": "50"}
    FakeWindow.store = {"kofin.online": "true"}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    monkeypatch.setattr("xbmcvfs.exists", lambda path: True)
    monkeypatch.setattr("xbmcvfs.translatePath", lambda path: str(tmp_path))
    monkeypatch.setattr("kofin.sync.shims._monitor", _FakeMonitor())
    monkeypatch.setattr("kofin.sync.full_sync.save_sync", lambda sync: None)
    monkeypatch.setattr("kofin.sync.full_sync.notification", lambda *a, **kw: None)

    sync_db.reset_overrides()
    sync_db.set_path_override("kofin", str(tmp_path / "kofin.db"))
    yield
    sync_db.reset_overrides()


class RecordingLibrary:
    """Stands in for the Library thread: the prune only enqueues."""

    def __init__(self):
        self.calls = {"removed": [], "added": [], "updated": []}

    # The one-sync-at-a-time claim lives on the Library now (audit finding
    # #11): FullSync asks its manager rather than a class-level dict.
    def claim_full_sync(self):
        return True

    def release_full_sync(self):
        pass

    def removed(self, data):
        self.calls["removed"].extend(data)

    def added(self, data):
        self.calls["added"].extend(data)

    def updated(self, data):
        self.calls["updated"].extend(data)


def make_fullsync(library=None):
    sync = FullSync(library=library or RecordingLibrary(), server=None)
    sync.sync = {"Libraries": [], "Whitelist": [], "RestorePoints": {}}
    return sync


def add_ref(db, *args):
    db.add_reference(*args)


# --- prune planner ------------------------------------------------------------


def test_prune_three_way_diff(monkeypatch):
    with sync_db.Database("kofin") as opened:
        db = kofindb.JellyfinDatabase(opened.cursor)
        # (id, kodi_id, fileid, pathid, jf_type, media_type, parent, checksum,
        #  media_folder, jf_parent)
        add_ref(db, "m1", 1, 2, 3, "Movie", "movie", None, "e1|plugin", "lib1", None)
        add_ref(db, "m2", 4, 5, 6, "Movie", "movie", None, "e2|plugin", "lib1", None)
        add_ref(db, "m3", 7, 8, 9, "Movie", "movie", None, None, "lib1", None)
        # A boxset row carries no media_folder and must not count as stale.
        add_ref(db, "b1", 10, None, None, "BoxSet", "set", None, "eb", None, None)

    server_map = {
        "m1": ("e1", "Movie"),  # unchanged
        "m2": ("eX", "Movie"),  # changed
        "m4": ("e4", "Movie"),  # missing locally
        "m5": (None, "Movie"),  # no etag -> safe direction: fetch
    }
    monkeypatch.setattr(
        "kofin.sync.full_sync.server.get_id_etag_map",
        lambda api, parent_id, types: dict(server_map),
    )
    # The server confirms m3 really is gone (see the stale-confirmation tests).
    monkeypatch.setattr(
        "kofin.sync.full_sync.server.get_existing_ids", lambda api, ids: set()
    )

    fullsync = make_fullsync()
    fullsync.prune({"Id": "lib1", "Name": "Movies", "CollectionType": "movies"}, "lib1")

    calls = fullsync.library.calls
    assert calls["added"] == ["m4", "m5"] or set(calls["added"]) == {"m4", "m5"}
    assert calls["updated"] == ["m2"]
    assert calls["removed"] == ["m3"]  # gone from the server


def test_prune_spares_a_stale_candidate_the_server_resolves(monkeypatch):
    """Absence from the library listing is not proof an item is gone.

    Jellyfin can report one id for a season via /Shows/{id}/Seasons and another
    for the same season in the /Items listing the prune diffs. The writers
    reference the former, so it reads as stale forever -- and removing it
    deletes the Kodi row both references share, leaving the survivor pointing
    at nothing. Confirming by id is what stops that.
    """
    with sync_db.Database("kofin") as opened:
        db = kofindb.JellyfinDatabase(opened.cursor)
        add_ref(
            db, "s1", 100, None, 7, "Series", "tvshow", None, "cs|plugin", "lib1", None
        )
        # Two ids, one Kodi season row (kodi_id 200): the alias the /Items
        # listing does not carry, and the one it does.
        for season_id in ("se-alias", "se-listed"):
            add_ref(
                db,
                season_id,
                200,
                None,
                None,
                "Season",
                "season",
                100,
                "cse|plugin",
                None,
                None,
            )

    monkeypatch.setattr(
        "kofin.sync.full_sync.server.get_id_etag_map",
        lambda api, parent_id, types: {
            "s1": ("cs", "Series"),
            "se-listed": ("cse", "Season"),
        },
    )
    # The alias resolves by id -- it is a live season, just not in the listing.
    monkeypatch.setattr(
        "kofin.sync.full_sync.server.get_existing_ids",
        lambda api, ids: {item_id for item_id in ids if item_id == "se-alias"},
    )

    fullsync = make_fullsync()
    fullsync.prune({"Id": "lib1", "Name": "Shows", "CollectionType": "tvshows"}, "lib1")

    assert fullsync.library.calls["removed"] == []


def test_prune_still_removes_what_the_server_cannot_resolve(monkeypatch):
    """The guard is a confirmation step, not an amnesty: an id the server does
    not know is still removed."""
    with sync_db.Database("kofin") as opened:
        db = kofindb.JellyfinDatabase(opened.cursor)
        add_ref(db, "m1", 1, 2, 3, "Movie", "movie", None, "e1|plugin", "lib1", None)
        add_ref(db, "gone", 4, 5, 6, "Movie", "movie", None, "e2|plugin", "lib1", None)

    monkeypatch.setattr(
        "kofin.sync.full_sync.server.get_id_etag_map",
        lambda api, parent_id, types: {"m1": ("e1", "Movie")},
    )
    monkeypatch.setattr(
        "kofin.sync.full_sync.server.get_existing_ids", lambda api, ids: set()
    )

    fullsync = make_fullsync()
    fullsync.prune({"Id": "lib1", "Name": "Movies", "CollectionType": "movies"}, "lib1")

    assert fullsync.library.calls["removed"] == ["gone"]


def test_prune_removes_nothing_when_confirmation_fails(monkeypatch):
    """A confirmation that could not be made is not a confirmation. The error
    propagates rather than falling back to the unverified set."""
    with sync_db.Database("kofin") as opened:
        db = kofindb.JellyfinDatabase(opened.cursor)
        add_ref(db, "gone", 4, 5, 6, "Movie", "movie", None, "e2|plugin", "lib1", None)

    def unreachable(api, ids):
        raise HttpError(500, "server down")

    monkeypatch.setattr(
        "kofin.sync.full_sync.server.get_id_etag_map",
        lambda api, parent_id, types: {},
    )
    monkeypatch.setattr("kofin.sync.full_sync.server.get_existing_ids", unreachable)

    fullsync = make_fullsync()

    with pytest.raises(HttpError):
        fullsync.prune(
            {"Id": "lib1", "Name": "Movies", "CollectionType": "movies"}, "lib1"
        )

    assert fullsync.library.calls["removed"] == []


def test_get_existing_ids_asks_by_id_without_filters():
    """The whole point is to bypass the listing's view of the library."""

    class IdApi:
        user_id = "user1"

        def __init__(self):
            self.calls = []

        def items(self, params):
            self.calls.append(params)
            asked = params["Ids"].split(",")
            return {"Items": [{"Id": i} for i in asked if i != "gone"]}

    api = IdApi()

    found = downloader.get_existing_ids(api, ["alive", "gone"])

    assert found == {"alive"}
    params = api.calls[0]
    assert params["Ids"] == "alive,gone"
    # None of the listing's filters may narrow an existence check.
    for absent in ("LocationTypes", "IsMissing", "IsVirtualUnaired", "ParentId"):
        assert absent not in params


def test_get_existing_ids_batches_large_sets():
    class IdApi:
        user_id = "user1"

        def __init__(self):
            self.batches = []

        def items(self, params):
            asked = params["Ids"].split(",")
            self.batches.append(len(asked))
            return {"Items": [{"Id": i} for i in asked]}

    api = IdApi()
    ids = ["i%d" % n for n in range(downloader.STALE_CONFIRM_BATCH + 5)]

    found = downloader.get_existing_ids(api, ids)

    assert found == set(ids)
    assert api.batches == [downloader.STALE_CONFIRM_BATCH, 5]


def test_prune_enqueues_missing_parents_first(monkeypatch):
    """get_id_etag_map pages in SortName order, so Series/Season/Episode
    interleave and a child could be downloaded and written while its parent
    was still in a later chunk -- the writers heal that by fetching the parent
    inside the write lock, which is a fallback, not a route to plan work
    through. Stable, so SortName order survives within a rank."""
    server_map = {
        "ep-a": ("e1", "Episode"),
        "se-b": ("e2", "Season"),
        "s-c": ("e3", "Series"),
        "ep-d": ("e4", "Episode"),
        "se-e": ("e5", "Season"),
        "s-f": ("e6", "Series"),
    }
    monkeypatch.setattr(
        "kofin.sync.full_sync.server.get_id_etag_map",
        lambda api, parent_id, types: dict(server_map),
    )

    fullsync = make_fullsync()
    fullsync.prune({"Id": "lib1", "Name": "Shows", "CollectionType": "tvshows"}, "lib1")

    assert fullsync.library.calls["added"] == [
        "s-c",
        "s-f",
        "se-b",
        "se-e",
        "ep-a",
        "ep-d",
    ]


def test_prune_orders_music_children_after_albums(monkeypatch):
    """Same ranks carry the music classes: an album before the songs that
    would otherwise create it on demand."""
    server_map = {
        "song-a": ("e1", "Audio"),
        "album-b": ("e2", "MusicAlbum"),
        "song-c": ("e3", "Audio"),
    }
    monkeypatch.setattr(
        "kofin.sync.full_sync.server.get_id_etag_map",
        lambda api, parent_id, types: dict(server_map),
    )

    fullsync = make_fullsync()
    fullsync.prune({"Id": "lib2", "Name": "Music", "CollectionType": "music"}, "lib2")

    assert fullsync.library.calls["added"] == ["album-b", "song-a", "song-c"]


def test_prune_converges_on_unchanged_seasons(monkeypatch):
    """Seasons stored no checksum, so every one of them failed the Etag
    comparison on every pass: a library of 335 seasons re-downloaded and
    rewrote all 335 forever, and the prune never reached a quiet state."""
    with sync_db.Database("kofin") as opened:
        db = kofindb.JellyfinDatabase(opened.cursor)
        add_ref(
            db, "s1", 100, None, 7, "Series", "tvshow", None, "cs|plugin", "lib1", None
        )
        add_ref(
            db,
            "se1",
            200,
            None,
            None,
            "Season",
            "season",
            100,
            "cse|plugin",
            None,
            None,
        )

    server_map = {"s1": ("cs", "Series"), "se1": ("cse", "Season")}
    monkeypatch.setattr(
        "kofin.sync.full_sync.server.get_id_etag_map",
        lambda api, parent_id, types: dict(server_map),
    )

    fullsync = make_fullsync()
    fullsync.prune({"Id": "lib1", "Name": "Shows", "CollectionType": "tvshows"}, "lib1")

    calls = fullsync.library.calls
    assert calls == {"removed": [], "added": [], "updated": []}


def test_prune_mixed_covers_both_classes(monkeypatch):
    requested = []

    monkeypatch.setattr(
        "kofin.sync.full_sync.server.get_id_etag_map",
        lambda api, parent_id, types: requested.append(types) or {},
    )

    fullsync = make_fullsync()
    fullsync.prune(
        {"Id": "lib9", "Name": "Mixed", "CollectionType": "mixed"}, "Mixed:lib9"
    )

    assert requested == ["Movie", "Series,Season,Episode"]


def test_update_mode_routes_to_prune(monkeypatch):
    class FakeServer:
        def item(self, item_id):
            return {"Id": item_id, "Name": "L", "CollectionType": "movies"}

    fullsync = make_fullsync()
    fullsync.server = FakeServer()
    fullsync.update_library = True

    pruned = []
    monkeypatch.setattr(
        fullsync, "prune", lambda library, library_id: pruned.append(library_id)
    )

    assert fullsync.process_library("lib1") is True
    assert pruned == ["lib1"]


# --- local reference map ------------------------------------------------------


def test_local_reference_map_walks_tv_children():
    with sync_db.Database("kofin") as opened:
        db = kofindb.JellyfinDatabase(opened.cursor)
        add_ref(
            db, "s1", 100, None, 7, "Series", "tvshow", None, "cs|plugin", "lib1", None
        )
        # Season: parent_id is the series *kodi* id.
        add_ref(
            db,
            "se1",
            200,
            None,
            None,
            "Season",
            "season",
            100,
            "cse|plugin",
            None,
            None,
        )
        # Episode under the season (parent_id = season kodi id).
        add_ref(
            db, "ep1", 300, 301, 302, "Episode", "episode", 200, "ce|plugin", None, "s1"
        )
        # Episode reachable only through the jellyfin_parent_id fallback arm.
        add_ref(
            db,
            "ep2",
            400,
            401,
            402,
            "Episode",
            "episode",
            None,
            "cf|plugin",
            None,
            "s1",
        )
        # Another library's series must not leak in.
        add_ref(db, "sX", 500, None, 8, "Series", "tvshow", None, "cx", "lib2", None)

    fullsync = make_fullsync()
    local = fullsync._local_reference_map("lib1", "tvshows")

    assert set(local) == {"s1", "se1", "ep1", "ep2"}
    assert local["s1"] == "cs|plugin"
    assert local["ep1"] == "ce|plugin"
    assert local["se1"] == "cse|plugin"


def test_local_reference_map_music_needs_no_walk():
    with sync_db.Database("kofin") as opened:
        db = kofindb.JellyfinDatabase(opened.cursor)
        add_ref(
            db, "ar1", 1, None, None, "MusicArtist", "artist", None, "ca", "lib2", None
        )
        add_ref(
            db,
            "al1",
            2,
            None,
            None,
            "MusicAlbum",
            "album",
            None,
            "cb|plugin",
            "lib2",
            None,
        )
        add_ref(db, "so1", 3, None, 4, "Audio", "song", 2, "cc|plugin", "lib2", None)

    fullsync = make_fullsync()
    local = fullsync._local_reference_map("lib2", "music")

    # Artists are deliberately outside the prune (see _local_reference_map).
    assert set(local) == {"al1", "so1"}


# --- pagers -------------------------------------------------------------------


class PagingApi:
    user_id = "user1"

    def __init__(self, pages, total=None, report_total=True):
        self.pages = list(pages)
        self.total = total if total is not None else sum(len(p) for p in pages)
        self.report_total = report_total
        self.requests = []

    def get(self, url, params=None):
        params = dict(params or {})
        self.requests.append((url, params))
        # The _get_items probe: Limit=1 + EnableTotalRecordCount.
        if params.get("Limit") == 1 and params.get("EnableTotalRecordCount"):
            return {"TotalRecordCount": self.total, "Items": []}
        page = {"Items": self.pages.pop(0) if self.pages else []}
        # get_id_etag_map asks for the count on its first page and pages
        # against it; report_total=False stands in for a server that will
        # not give one.
        if params.get("EnableTotalRecordCount") and self.report_total:
            page["TotalRecordCount"] = self.total
        return page


def test_get_id_etag_map_pages_sequentially():
    full_page = [
        {"Id": "i%d" % n, "Etag": "e%d" % n, "Type": "Movie"}
        for n in range(downloader.PRUNE_PAGE_SIZE)
    ]
    tail_page = [{"Id": "last", "Etag": "eL", "Type": "Movie"}]
    api = PagingApi([full_page, tail_page])

    result = downloader.get_id_etag_map(api, "lib1", "Movie")

    assert len(result) == downloader.PRUNE_PAGE_SIZE + 1
    assert result["last"] == ("eL", "Movie")
    starts = [p["StartIndex"] for _u, p in api.requests]
    assert starts == [0, downloader.PRUNE_PAGE_SIZE]
    # Minimal payload: Etag only, no userdata/images.
    for _url, params in api.requests:
        assert params["Fields"] == "Etag"
        assert params["EnableUserData"] is False
    # Counted once: the pages after the first ride on the sampled total.
    counted = [p["EnableTotalRecordCount"] for _u, p in api.requests]
    assert counted == [True, False]


def test_get_id_etag_map_reasks_after_a_short_page():
    """A short page mid-stream is the server rationing, not end-of-data.

    The old ``len(items) < PRUNE_PAGE_SIZE`` test would have stopped on the
    300-item page and dropped the remaining 201 ids -- which the prune reads
    as 201 stale references and feeds to the removal arm.
    """
    short = [{"Id": "s%d" % n, "Etag": "e%d" % n, "Type": "Movie"} for n in range(300)]
    rest = [{"Id": "r%d" % n, "Etag": "f%d" % n, "Type": "Movie"} for n in range(201)]
    api = PagingApi([short, rest], total=501)

    result = downloader.get_id_etag_map(api, "lib1", "Movie")

    assert len(result) == 501
    # StartIndex advances by what arrived, so nothing is skipped over.
    assert [p["StartIndex"] for _u, p in api.requests] == [0, 300]


def test_get_id_etag_map_raises_rather_than_truncate():
    """Short of the server's count, the map is refused outright.

    Returning it would hand every unlisted id to the prune as ``stale``.
    """
    page = [
        {"Id": "i%d" % n, "Etag": "e%d" % n, "Type": "Movie"}
        for n in range(downloader.PRUNE_PAGE_SIZE)
    ]
    api = PagingApi([page], total=1000)

    with pytest.raises(shims.LibraryException) as excinfo:
        downloader.get_id_etag_map(api, "lib1", "Movie")

    assert "truncated" in str(excinfo.value)
    assert "500 of 1000" in str(excinfo.value)


def test_get_id_etag_map_without_a_count_falls_back_to_short_page():
    """No TotalRecordCount to page against: end on a short page rather than
    loop forever. The heuristic is the old behaviour, kept only for servers
    that will not supply a count."""
    api = PagingApi([[{"Id": "i1", "Etag": "e1", "Type": "Movie"}]], report_total=False)

    result = downloader.get_id_etag_map(api, "lib1", "Movie")

    assert set(result) == {"i1"}
    assert len(api.requests) == 1


def test_get_items_defaults_to_newest_first():
    api = PagingApi([[{"Id": "m1", "Type": "Movie"}]])

    for _batch in downloader.get_items(api, "lib1", "Movie"):
        pass

    for _url, params in api.requests:
        assert params["SortBy"] == "DateCreated,SortName"
        assert params["SortOrder"] == "Descending,Ascending"


def test_item_type_filter_reaches_the_query():
    """The phase-5 sort flip dropped IncludeItemTypes from get_items, so every
    tvshows pass fetched the whole library and applied the wrong writer to
    each item — a show's /Seasons 404s on an episode id and the sync aborted
    on every service start. The three passes are only three different queries
    because of this parameter."""
    for item_type in ("Series", "Season", "Episode"):
        api = PagingApi([[{"Id": "x1", "Type": item_type}]])

        for _batch in downloader.get_items(api, "lib1", item_type):
            pass

        assert api.requests
        for _url, params in api.requests:
            assert params["IncludeItemTypes"] == item_type


def test_item_type_absent_means_unfiltered():
    """Callers that genuinely want every type pass None (boxsets, mixed)."""
    api = PagingApi([[{"Id": "x1", "Type": "Movie"}]])

    for _batch in downloader.get_items(api, "lib1"):
        pass

    for _url, params in api.requests:
        assert params["IncludeItemTypes"] is None


def test_restore_point_resumes_under_recorded_sort():
    """A pre-phase-5 restore point carries SortBy=SortName in its params; the
    resumed query must keep it (never mix sort orders mid-walk)."""
    api = PagingApi([[{"Id": "m2", "Type": "Movie"}]], total=51)
    restore = {
        "ParentId": "lib1",
        "SortBy": "SortName",
        "SortOrder": "Ascending",
        "StartIndex": 50,
    }

    for _batch in downloader.get_items(api, "lib1", "Movie", False, restore):
        pass

    for _url, params in api.requests:
        assert params["SortBy"] == "SortName"
        assert params["SortOrder"] == "Ascending"
    # The paging resumed from the recorded index.
    data_requests = [p for _u, p in api.requests if p.get("Limit") != 1]
    assert data_requests and data_requests[0]["StartIndex"] == 50


def test_music_sort_override_survives():
    api = PagingApi([[{"Id": "al1", "Type": "MusicAlbum"}]])

    for _batch in downloader.get_items(
        api, "lib2", "MusicAlbum", params={"SortBy": "AlbumArtist"}
    ):
        pass

    for _url, params in api.requests:
        assert params["SortBy"] == "AlbumArtist"


# --- sort pair arity ----------------------------------------------------------


def test_sort_order_arity_always_matches_sort_by():
    """Jellyfin 10.11 answers a mismatched SortBy/SortOrder pair with an opaque
    400, and get_items' default pair is composite — so a caller overriding
    SortBy alone used to send two orders for one field. That 400'd every album
    and song page of a music sync."""
    for override, expected_fields in (
        ({"SortBy": "AlbumArtist"}, 1),
        ({"SortBy": "AlbumArtist,SortName"}, 2),
        ({"SortBy": "AlbumArtist,Album,SortName"}, 3),
        ({"SortBy": "SortName", "SortOrder": "Descending,Ascending"}, 1),
        ({"SortBy": "A,B,C", "SortOrder": "Descending"}, 3),
    ):
        api = PagingApi([[{"Id": "x1", "Type": "MusicAlbum"}]])

        for _batch in downloader.get_items(
            api, "lib2", "MusicAlbum", params=dict(override)
        ):
            pass

        assert api.requests
        for _url, params in api.requests:
            assert len(params["SortBy"].split(",")) == expected_fields
            assert len(params["SortOrder"].split(",")) == expected_fields


def test_sort_field_override_alone_gets_ascending():
    """The default pair's directions belong to the default's fields; inheriting
    them would silently reverse a caller's own sort."""
    api = PagingApi([[{"Id": "al1", "Type": "MusicAlbum"}]])

    for _batch in downloader.get_items(
        api, "lib2", "MusicAlbum", params={"SortBy": "AlbumArtist"}
    ):
        pass

    for _url, params in api.requests:
        assert params["SortOrder"] == "Ascending"


def test_default_sort_pair_is_untouched():
    api = PagingApi([[{"Id": "m1", "Type": "Movie"}]])

    for _batch in downloader.get_items(api, "lib1", "Movie"):
        pass

    for _url, params in api.requests:
        assert params["SortBy"] == "DateCreated,SortName"
        assert params["SortOrder"] == "Descending,Ascending"


# --- failure and abandonment --------------------------------------------------


class FailingCountApi:
    """Rejects the count probe the way a bad query does."""

    user_id = "user1"

    def __init__(self):
        self.data_requests = 0

    def get(self, url, params=None):
        params = dict(params or {})
        if params.get("Limit") == 1 and params.get("EnableTotalRecordCount"):
            raise HttpError(400, "GET %s -> 400" % url)
        self.data_requests += 1
        return {"Items": []}


def test_rejected_query_fails_the_pass_instead_of_emptying_it():
    """A swallowed count-probe error yielded zero pages, so the caller wrote
    nothing, the library was dropped from sync.json as done and the sync
    reported success — a library that never landed looked synced."""
    api = FailingCountApi()

    with pytest.raises(HttpError):
        for _batch in downloader.get_items(api, "lib2", "MusicAlbum"):
            pass

    assert api.data_requests == 0


class SlowPagingApi:
    """A library of many pages, each costing a little wall time — so the pager
    still has jobs queued and in flight when the consumer walks away."""

    user_id = "user1"

    def __init__(self, total, page_size, page_seconds=0.02):
        self.total = total
        self.page_size = page_size
        self.page_seconds = page_seconds

    def get(self, url, params=None):
        params = dict(params or {})
        if params.get("Limit") == 1 and params.get("EnableTotalRecordCount"):
            return {"TotalRecordCount": self.total, "Items": []}

        time.sleep(self.page_seconds)
        start = params.get("StartIndex", 0)
        return {"Items": [{"Id": "i%d" % (start + n)} for n in range(self.page_size)]}


def test_music_pages_drop_the_recursive_child_count():
    """RecursiveItemCount makes the server count every album's children. It cost
    a 100-album page 19.8s against 1.6s without, on Jellyfin 10.11 — effectively
    the entire album pass. Only Series and BrowseVideo map it, so no music
    writer loses a field by dropping it."""
    fields = downloader.music_page_info().split(",")

    assert "RecursiveItemCount" not in fields
    # Everything else the full-fidelity set carries is still there — songs read
    # Path and MediaSources, which the lean music_info() does not carry.
    assert set(fields) == set(downloader.info().split(",")) - {"RecursiveItemCount"}
    for needed in ("Path", "MediaSources", "Etag", "ParentId", "DateCreated"):
        assert needed in fields


def test_video_pages_keep_the_recursive_child_count():
    """The Series object map reads it; dropping it library-wide would empty the
    show child counts."""
    api = PagingApi([[{"Id": "s1", "Type": "Series"}]])

    for _batch in downloader.get_items(api, "lib1", "Series"):
        pass

    for _url, params in api.requests:
        assert "RecursiveItemCount" in params["Fields"]


class CountingPagingApi:
    """Counts pages actually fetched, so a test can see how far ahead of the
    consumer the pool runs."""

    user_id = "user1"

    def __init__(self, total, page_size):
        self.total = total
        self.page_size = page_size
        self.served = 0
        self.lock = threading.Lock()

    def get(self, url, params=None):
        params = dict(params or {})
        if params.get("Limit") == 1 and params.get("EnableTotalRecordCount"):
            return {"TotalRecordCount": self.total, "Items": []}

        with self.lock:
            self.served += 1
        start = params.get("StartIndex", 0)
        return {"Items": [{"Id": "i%d" % (start + n)} for n in range(self.page_size)]}


def test_pool_prefetches_deeper_than_it_is_wide(monkeypatch):
    """The buffer permit is held until the consumer is done with a page, so a
    depth equal to the pool width meant no fetch could start while the writer
    worked — the album pass sat idle 26% of its wall time waiting on pages it
    could have had already."""
    FakeAddon.store = dict(FakeAddon.store, limitThreads="2", limitIndex="50")
    dthreads = 2

    api = CountingPagingApi(total=10000, page_size=50)
    pager = downloader.get_items(api, "lib1", "Audio")

    assert next(pager)["Items"]  # one page consumed, one permit released

    # Let the pool run until it stops making progress on its own.
    settled = 0
    for _ in range(200):
        before = api.served
        time.sleep(0.02)
        if api.served == before:
            settled += 1
            if settled == 5:
                break
        else:
            settled = 0

    try:
        # Width alone would cap this at dthreads (+1 for the released permit).
        assert api.served >= dthreads * downloader.PREFETCH_PAGES, (
            "pool only ran %d pages ahead; buffer depth is not deeper than the "
            "pool width" % api.served
        )
        # Still bounded — it must not race off and page the whole library.
        assert api.served <= dthreads * downloader.PREFETCH_PAGES + 1
    finally:
        pager.close()


class _TrackingSemaphore(threading.Semaphore):
    """Hands the test a handle on the pager's buffer semaphore.

    Only so a regression fails instead of hanging: the executor's threads are
    non-daemon and interpreter shutdown joins them, so a pager that strands
    workers wedges the whole suite at exit rather than failing this one test.
    The test drains the semaphore itself before asserting.
    """

    instances: list = []

    def __init__(self, value=1):
        super().__init__(value)
        _TrackingSemaphore.instances.append(self)


def test_abandoned_pager_does_not_deadlock_on_shutdown(monkeypatch):
    """Quitting Kodi mid-sync raises LibraryExitException in a writer, which
    closes this generator mid-page. Every page is submitted up front and each
    worker blocks on the buffer semaphore only the consumer releases, so an
    abandoned generator left ThreadPoolExecutor.shutdown(wait=True) waiting on
    pages nobody would ever unblock — Kodi froze until it force-killed the
    interpreter. Closing must return in about one page's time, not never."""
    _TrackingSemaphore.instances.clear()
    monkeypatch.setattr("threading.Semaphore", _TrackingSemaphore)

    api = SlowPagingApi(total=20000, page_size=50)
    pager = downloader.get_items(api, "lib1", "Audio")

    assert next(pager)["Items"]

    closed = threading.Event()

    def close_it():
        pager.close()
        closed.set()

    closer = threading.Thread(target=close_it, daemon=True)
    closer.start()
    closer.join(timeout=15)
    timed_out = not closed.is_set()

    if timed_out:
        # Unblock the stranded workers so the suite can still exit.
        for semaphore in _TrackingSemaphore.instances:
            for _ in range(500):
                semaphore.release()
        closer.join(timeout=30)

    assert not timed_out, "generator close deadlocked in executor shutdown"
