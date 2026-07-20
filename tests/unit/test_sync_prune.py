"""L1 units for the phase-5 full-sync overhaul: the update-mode prune
planner (ids+Etag three-way diff), the local reference map incl. the TV
child walk, the ids+Etag pager, the newest-first default sort, and restore
points resuming under their recorded sort (plan §6)."""

import pytest

from kofin.sync import db as sync_db
from kofin.sync import downloader
from kofin.sync import kofindb
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
    FullSync._shared_state.clear()
    yield
    FullSync._shared_state.clear()
    sync_db.reset_overrides()


class RecordingLibrary:
    """Stands in for the Library thread: the prune only enqueues."""

    def __init__(self):
        self.calls = {"removed": [], "added": [], "updated": []}

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

    fullsync = make_fullsync()
    fullsync.prune({"Id": "lib1", "Name": "Movies", "CollectionType": "movies"}, "lib1")

    calls = fullsync.library.calls
    assert calls["added"] == ["m4", "m5"] or set(calls["added"]) == {"m4", "m5"}
    assert calls["updated"] == ["m2"]
    assert calls["removed"] == ["m3"]  # gone from the server


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
        add_ref(db, "se1", 200, None, None, "Season", "season", 100, None, None, None)
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
    assert local["se1"] is None


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

    def __init__(self, pages, total=None):
        self.pages = list(pages)
        self.total = total if total is not None else sum(len(p) for p in pages)
        self.requests = []

    def get(self, url, params=None):
        params = dict(params or {})
        self.requests.append((url, params))
        # The _get_items probe: Limit=1 + EnableTotalRecordCount.
        if params.get("Limit") == 1 and params.get("EnableTotalRecordCount"):
            return {"TotalRecordCount": self.total, "Items": []}
        return {"Items": self.pages.pop(0) if self.pages else []}


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
