"""L1 units for the sync orchestrator: queue routing, priority rules,
watermark handling and the ws-event wiring (plan §5 step 3)."""

import queue

import pytest

from kofin.core.http import JellyfinError, ServerUnreachable
from kofin.sync import db as sync_db
from kofin.sync import kofindb
from kofin.sync import library as library_mod
from kofin.sync.downloader import GetItemWorker
from kofin.sync.library import Library
from tests.unit.fakes import FakeAddon, FakeWindow


class FakeApi:
    server = "http://server:8096"
    user_id = "user1"

    def __init__(self):
        self.sync_queue_result = None
        self.server_time_result = {"ServerDateTime": "2026-07-17T10:00:00Z"}
        # Default: no KofinSyncQueue installed — the ladder lands on tier 2.
        self.kofin_info_result = JellyfinError("404")
        self.kofin_queue_result = {}
        self.kofin_queue_requests = []
        self.items_requests = []
        self.items_result = {"Items": []}

    def sync_queue(self, last_sync, filters=""):
        self.filters = filters
        return self.sync_queue_result

    def server_time(self):
        if isinstance(self.server_time_result, Exception):
            raise self.server_time_result
        return self.server_time_result

    def kofin_sync_info(self):
        if isinstance(self.kofin_info_result, Exception):
            raise self.kofin_info_result
        return self.kofin_info_result

    def kofin_sync_queue(self, since, types):
        self.kofin_queue_requests.append((since, types))
        if isinstance(self.kofin_queue_result, Exception):
            raise self.kofin_queue_result
        return self.kofin_queue_result

    def items(self, params):
        self.items_requests.append(params)
        if isinstance(self.items_result, Exception):
            raise self.items_result
        return self.items_result


class FakePlayer:
    def isPlayingVideo(self):
        return False


@pytest.fixture(autouse=True)
def sync_env(monkeypatch, tmp_path):
    FakeAddon.store = {"limitThreads": "3"}
    FakeWindow.store = {"kofin.online": "true"}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    monkeypatch.setattr("xbmcvfs.exists", lambda path: True)
    monkeypatch.setattr("xbmcvfs.translatePath", lambda path: str(tmp_path))
    monkeypatch.setattr("kofin.sync.shims._monitor", _FakeMonitor())

    sync_db.reset_overrides()
    sync_db.set_path_override("kofin", str(tmp_path / "kofin.db"))
    yield
    sync_db.reset_overrides()


class _FakeMonitor:
    def abortRequested(self):
        return False

    def waitForAbort(self, seconds=0):
        return False


def make_library():
    api = FakeApi()
    return Library(api, FakePlayer(), lambda: api), api


def seed_views(*views):
    with sync_db.Database("kofin") as opened:
        mapping = kofindb.JellyfinDatabase(opened.cursor)
        for view_id, name, media in views:
            mapping.add_view(view_id, name, media)


def seed_whitelist(*ids):
    sync = sync_db.get_sync()
    sync["Whitelist"] = list(ids)
    sync_db.save_sync(sync)


def drain(q):
    result = []
    while True:
        try:
            result.append(q.get_nowait())
        except queue.Empty:
            return result


# --- fast_sync routing -------------------------------------------------------


def test_fast_sync_routes_and_dedupes(monkeypatch):
    seed_views(("lib1", "Movies", "movies"), ("lib2", "Tunes", "music"))
    seed_whitelist("lib1")

    manager, api = make_library()
    manager.detect_companion()
    api.sync_queue_result = {
        "ItemsAdded": ["new1", "new2"],
        "ItemsUpdated": ["upd1", "both1"],
        "UserDataChanged": [
            {"ItemId": "both1"},  # also updated -> dropped from userdata pass
            {"ItemId": "watch1"},
        ],
        "ItemsRemoved": ["gone1", "gone1", "gone2"],
    }

    # watch1 is a tracked movie; both1 tracked too.
    with sync_db.Database("kofin") as opened:
        mapping = kofindb.JellyfinDatabase(opened.cursor)
        mapping.add_reference(
            "watch1", 10, 11, 12, "Movie", "movie", None, "etag", "lib1", None
        )
        mapping.add_reference(
            "both1", 20, 21, 22, "Movie", "movie", None, "etag", "lib1", None
        )

    assert manager.fast_sync() is True

    # Music excluded from the queue query (not whitelisted); movies/boxsets not.
    assert "music" in api.filters
    assert "movies" not in api.filters
    assert "boxsets" not in api.filters

    assert drain(manager.added_queue) == [["new1", "new2"]]
    assert drain(manager.updated_queue) == [["upd1", "both1"]]

    # Userdata: overlap dropped, remainder applied from payload (no download).
    assert drain(manager.userdata_queue) == []
    userdata_items = drain(manager.userdata_output["Movie"])
    assert [x["Id"] for x in userdata_items] == ["watch1"]

    # The changed-ids tag set carries the *full* userdata id list.
    assert manager.userdata_changed_ids == {"both1", "watch1"}

    removed = drain(manager.removed_queue)
    assert removed == ["gone1", "gone2"]  # deduped

    assert manager.total_updates == 1 + 2 + 2 + 2


def test_fast_sync_music_userdata_falls_back_to_download():
    seed_views(("lib2", "Tunes", "music"))
    seed_whitelist("lib2")

    manager, api = make_library()
    manager.detect_companion()
    api.sync_queue_result = {
        "ItemsAdded": [],
        "ItemsUpdated": [],
        "UserDataChanged": [{"ItemId": "alb1"}],
        "ItemsRemoved": [],
    }

    with sync_db.Database("kofin") as opened:
        kofindb.JellyfinDatabase(opened.cursor).add_reference(
            "alb1", 30, None, None, "MusicAlbum", "album", None, "etag", "lib2", None
        )

    assert manager.fast_sync() is True
    assert drain(manager.userdata_queue) == [["alb1"]]
    assert drain(manager.userdata_output["MusicAlbum"]) == []


def test_fast_sync_failure_returns_false():
    seed_whitelist("lib1")
    manager, api = make_library()
    manager.detect_companion()

    class Boom(Exception):
        pass

    def raise_boom(last_sync, filters=""):
        raise Boom()

    api.sync_queue = raise_boom
    assert manager.fast_sync() is False


def test_untracked_userdata_skipped():
    manager, _api = make_library()
    manager.userdata([{"ItemId": "unknown1"}])
    assert manager.total_updates == 0
    assert drain(manager.userdata_output["Movie"]) == []


# --- widget refresh policy ---------------------------------------------------


@pytest.fixture
def builtins(monkeypatch):
    """Capture executebuiltin calls; default to a non-media window with Kodi
    already aware the library has content (the normal, steady state)."""
    calls = []
    monkeypatch.setattr("xbmc.executebuiltin", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(
        "xbmc.getCondVisibility", lambda cond: cond.startswith("Library.HasContent")
    )
    return calls


def test_video_refresh_scans_video_only(builtins):
    """UpdateLibrary(video) is a no-op scan (writers set noUpdate=1 on every
    path) but it is the only thing that clears Kodi's cached
    Library.HasContent, so a first sync becomes visible. Upstream parity."""
    manager, _api = make_library()
    manager.refresh_libraries({"video"})
    assert builtins == ["UpdateLibrary(video)"]


def _fake_video_db(monkeypatch, tmp_path, rows):
    """A video database whose movie/tvshow/musicvideo tables are (non)empty."""
    import sqlite3

    path = str(tmp_path / "MyVideos131.db")
    conn = sqlite3.connect(path)
    for table in ("movie", "tvshow", "musicvideo"):
        conn.execute("CREATE TABLE %s (id INTEGER)" % table)
    if rows:
        conn.execute("INSERT INTO tvshow VALUES (1)")
    conn.commit()
    conn.close()
    sync_db.set_path_override("video", path)


def test_first_content_reloads_skin_for_home_widgets(monkeypatch, tmp_path):
    """Kodi says the library is empty but rows exist: the Home window's
    videodb:// widget containers were built empty and only a window rebuild
    repopulates them."""
    calls = []
    monkeypatch.setattr("xbmc.executebuiltin", lambda cmd: calls.append(cmd))
    monkeypatch.setattr("xbmc.getCondVisibility", lambda cond: False)
    _fake_video_db(monkeypatch, tmp_path, rows=True)

    manager, _api = make_library()
    manager.refresh_libraries({"video"})

    assert calls == ["UpdateLibrary(video)", "ReloadSkin()"]


def test_no_skin_reload_once_kodi_knows_about_content(monkeypatch, tmp_path):
    """The steady state: cache already true -> never reload. This is what keeps
    the reload to at most once, instead of on every sync."""
    calls = []
    monkeypatch.setattr("xbmc.executebuiltin", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(
        "xbmc.getCondVisibility", lambda cond: cond == "Library.HasContent(TVShows)"
    )
    _fake_video_db(monkeypatch, tmp_path, rows=True)

    manager, _api = make_library()
    manager.refresh_libraries({"video"})

    assert calls == ["UpdateLibrary(video)"]


def test_no_skin_reload_when_library_genuinely_empty(monkeypatch, tmp_path):
    """Cache says empty and the database agrees -> nothing to reveal."""
    calls = []
    monkeypatch.setattr("xbmc.executebuiltin", lambda cmd: calls.append(cmd))
    monkeypatch.setattr("xbmc.getCondVisibility", lambda cond: False)
    _fake_video_db(monkeypatch, tmp_path, rows=False)

    manager, _api = make_library()
    manager.refresh_libraries({"video"})

    assert calls == ["UpdateLibrary(video)"]


def test_music_only_sync_never_reloads_skin(monkeypatch, tmp_path):
    """A music-only sync must not scan video or bounce the skin. It does fire
    the nonexistent-path probe, which is what makes direct writes visible."""
    calls = []
    monkeypatch.setattr("xbmc.executebuiltin", lambda cmd: calls.append(cmd))
    monkeypatch.setattr("xbmc.getCondVisibility", lambda cond: False)
    _fake_video_db(monkeypatch, tmp_path, rows=True)

    manager, _api = make_library()
    manager.refresh_libraries({"music"})

    assert calls == ["UpdateLibrary(music,%s)" % library_mod.MUSIC_REFRESH_PROBE]
    assert not any("ReloadSkin" in c or "video" in c for c in calls)


def test_music_refresh_never_scans_the_real_library(builtins):
    """A *bare* UpdateLibrary(music) would probe every song's remote path
    (~21k requests) and overlapping scans have crashed Kodi -- fork e4f8dc3f.
    The probe path must always be present, and must not exist on disk."""
    manager, _api = make_library()
    manager.refresh_libraries({"music"})

    assert builtins == ["UpdateLibrary(music,%s)" % library_mod.MUSIC_REFRESH_PROBE]
    assert "UpdateLibrary(music)" not in builtins


def test_music_refresh_skipped_while_a_scan_is_running(monkeypatch):
    """Stacking scans is the crash path: cancelling an in-flight music scan
    is what took Kodi down on Android."""
    calls = []
    monkeypatch.setattr("xbmc.executebuiltin", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(
        "xbmc.getCondVisibility", lambda cond: cond == "Library.IsScanningMusic"
    )

    manager, _api = make_library()
    manager.refresh_libraries({"music"})

    assert calls == []


def test_mixed_refresh_scans_video_but_not_music(builtins):
    """Video gets its real (no-op) scan; music gets the nonexistent-path probe.
    Neither may ever be a bare UpdateLibrary(music)."""
    manager, _api = make_library()
    manager.refresh_libraries({"video", "music"})
    assert builtins == [
        "UpdateLibrary(video)",
        "UpdateLibrary(music,%s)" % library_mod.MUSIC_REFRESH_PROBE,
    ]
    assert "UpdateLibrary(music)" not in builtins


def test_container_refresh_only_in_media_window(monkeypatch):
    calls = []
    monkeypatch.setattr("xbmc.executebuiltin", lambda cmd: calls.append(cmd))
    monkeypatch.setattr("xbmc.getCondVisibility", lambda cond: cond == "Window.IsMedia")
    manager, _api = make_library()
    manager.refresh_libraries({"music"})
    assert calls == [
        "UpdateLibrary(music,%s)" % library_mod.MUSIC_REFRESH_PROBE,
        "Container.Refresh",
    ]


def test_refresh_noop_without_databases(builtins):
    manager, _api = make_library()
    manager.refresh_libraries(set())
    assert builtins == []


# --- retry / watermark -------------------------------------------------------


def test_schedule_retry_backs_off():
    manager, _api = make_library()
    assert manager.retry_delay == 60
    manager.schedule_retry()
    assert manager.retry_at is not None
    assert manager.retry_delay == 120
    manager.schedule_retry()
    assert manager.retry_delay == 240


def test_save_last_sync_prefers_server_clock():
    manager, api = make_library()
    manager.companion_tier = library_mod.TIER_OFFICIAL
    manager.save_last_sync()
    # Two-minute tolerance subtracted from the plugin's clock.
    assert FakeAddon.store["lastIncrementalSync"] == "2026-07-17T09:58:00Z"


def test_save_last_sync_falls_back_to_client_clock():
    manager, api = make_library()
    manager.companion_tier = library_mod.TIER_OFFICIAL
    api.server_time_result = ServerUnreachable("gone")
    manager.save_last_sync()
    assert FakeAddon.store["lastIncrementalSync"]  # set, parseable shape
    assert FakeAddon.store["lastIncrementalSync"].endswith("Z")


def test_companion_probe_sets_tier():
    manager, api = make_library()
    assert manager.detect_companion() == library_mod.TIER_OFFICIAL

    api.server_time_result = ServerUnreachable("404")
    assert manager.detect_companion() == library_mod.TIER_NONE


# --- GetItemWorker -----------------------------------------------------------


def test_get_item_worker_tags_and_routes():
    api = FakeApi()
    api.items_result = {
        "Items": [
            {"Id": "m1", "Type": "Movie", "Name": "M"},
            {"Id": "e1", "Type": "Episode", "Name": "E"},
            {"Id": "x1", "Type": "Trailer", "Name": "ignored"},
        ]
    }
    work = queue.Queue()
    work.put(["m1", "e1", "x1"])
    output = {"Movie": queue.Queue(), "Episode": queue.Queue()}

    worker = GetItemWorker(api, work, output, userdata_ids={"e1"})
    worker.run()

    movie = output["Movie"].get_nowait()
    episode = output["Episode"].get_nowait()
    assert movie["_userdata_changed"] is False
    assert episode["_userdata_changed"] is True
    assert api.items_requests[0]["Ids"] == "m1,e1,x1"
    assert worker.is_done


def test_get_item_worker_flags_errors_and_stops_on_unreachable():
    import threading

    api = FakeApi()
    api.items_result = ServerUnreachable("dead")
    work = queue.Queue()
    work.put(["m1"])
    work.put(["m2"])
    error_event = threading.Event()

    worker = GetItemWorker(api, work, {}, error_event)
    worker.run()

    assert error_event.is_set()
    assert worker.is_done
    # The second chunk was left unconsumed: watermark must not advance.
    assert work.qsize() == 1


# --- ws-event wiring ---------------------------------------------------------


def test_ws_events_route_into_library(monkeypatch):
    from kofin.service.main import Service

    calls = {}

    class FakeLibrary:
        startup_done = True

        def added(self, data):
            calls["added"] = data

        def updated(self, data):
            calls["updated"] = data

        def removed(self, data):
            calls["removed"] = data

        def userdata(self, data):
            calls["userdata"] = data

    service = Service.__new__(Service)
    service.remote = type("R", (), {"handle": lambda self, m, d: False})()
    service.library = FakeLibrary()

    service._on_ws_event(
        "LibraryChanged",
        {"ItemsAdded": ["a"], "ItemsUpdated": ["u"], "ItemsRemoved": ["r"]},
    )
    assert calls == {"added": ["a"], "updated": ["u"], "removed": ["r"]}

    service._on_ws_event("UserDataChanged", {"UserDataList": [{"ItemId": "x"}]})
    assert calls["userdata"] == [{"ItemId": "x"}]


def test_ws_events_ignored_before_startup_done():
    from kofin.service.main import Service

    class FakeLibrary:
        startup_done = False

        def added(self, data):  # pragma: no cover - must not run
            raise AssertionError("routed before startup finished")

    service = Service.__new__(Service)
    service.remote = type("R", (), {"handle": lambda self, m, d: False})()
    service.library = FakeLibrary()

    service._on_ws_event("LibraryChanged", {"ItemsAdded": ["a"]})


def test_library_commands_enqueue(monkeypatch):
    from kofin.core import ipc
    from kofin.service.main import Service

    commands = []

    class FakeLibrary:
        startup_done = True

        def enqueue_command(self, command, data=None):
            commands.append((command, data))

    service = Service.__new__(Service)
    service.library = FakeLibrary()
    monkeypatch.setattr(Service, "_start_library", lambda self: None)

    payload = '"[{\\"Id\\": \\"lib1\\"}]"'
    import json

    encoded = json.dumps([{"Id": "lib1"}])
    service.onNotification("plugin.video.kofin", "Other.SyncLibrary", encoded)
    assert commands == [("SyncLibrary", {"Id": "lib1"})]


# --- phase 5: tier-1 change feed ---------------------------------------------


def make_tier1_library(server_time=1789000000):
    manager, api = make_library()
    api.kofin_info_result = {
        "ProtocolVersion": 1,
        "ServerTime": server_time,
        "RetentionCutoff": 0,
    }
    manager.detect_companion()
    return manager, api


def test_tier1_detection_sets_tier_and_provider():
    manager, _api = make_tier1_library()
    assert manager.companion_tier == library_mod.TIER_KOFIN
    assert manager.changefeed is not None


def test_tier1_protocol_mismatch_falls_to_official():
    manager, api = make_library()
    api.kofin_info_result = {"ProtocolVersion": 2}
    assert manager.detect_companion() == library_mod.TIER_OFFICIAL


def test_tier1_fast_sync_skips_orders_and_routes():
    from kofin.sync.changefeed import unix_to_watermark

    seed_views(("lib1", "Movies", "movies"))
    seed_whitelist("lib1")
    manager, api = make_tier1_library()

    with sync_db.Database("kofin") as opened:
        mapping = kofindb.JellyfinDatabase(opened.cursor)
        mapping.add_reference(
            "m_skip", 1, 2, 3, "Movie", "movie", None, "eS|plugin", "lib1", None
        )
        mapping.add_reference(
            "m_art", 4, 5, 6, "Movie", "movie", None, "old|plugin", "lib1", None
        )

    api.kofin_queue_result = {
        "ServerTime": 1789000123,
        "RetentionCutoff": 0,
        "Items": [
            {
                "Id": "m_new",
                "Status": "Added",
                "ItemType": "Movie",
                "MediaType": "movies",
                "LastModified": 10,
                "Etag": "eN",
            },
            {"Id": "m_skip", "Status": "Updated", "ItemType": "Movie", "Etag": "eS"},
            {
                "Id": "m_art",
                "Status": "Updated",
                "ItemType": "Movie",
                "UpdateReason": "ImageUpdate",
                "Etag": "eA",
            },
            {"Id": "gone1", "Status": "Removed", "ItemType": "Movie"},
        ],
        "UserData": [{"ItemId": "m_skip"}],
    }

    assert manager.fast_sync() is True

    # Include list sent directly — no exclude inversion on tier 1.
    assert api.kofin_queue_requests[-1][1] == "movies,boxsets"

    assert drain(manager.added_queue) == [["m_new"]]
    assert drain(manager.updated_queue) == []  # Etag match: skipped pre-download
    assert drain(manager.artwork_queue) == [["m_art"]]
    assert drain(manager.removed_queue) == ["gone1"]
    assert manager.artwork_only_ids == {"m_art"}

    # The skipped item's userdata still applies through the dto path.
    userdata_items = drain(manager.userdata_output["Movie"])
    assert [x["Id"] for x in userdata_items] == ["m_skip"]

    # Watermark: envelope ServerTime exact — no fudge, no extra round trip.
    manager.save_last_sync()
    assert FakeAddon.store["lastIncrementalSync"] == unix_to_watermark(1789000123)


def test_tier1_envelope_consumed_once():
    from kofin.sync.changefeed import Envelope, unix_to_watermark

    manager, api = make_tier1_library()
    manager.last_envelope = Envelope(server_time=1789000123)
    manager.save_last_sync()
    assert FakeAddon.store["lastIncrementalSync"] == unix_to_watermark(1789000123)

    # Envelope gone: a later (realtime) drain probes Info fresh instead of
    # rewinding to the stale sample.
    api.kofin_info_result = {"ProtocolVersion": 1, "ServerTime": 1789000200}
    manager.save_last_sync()
    assert FakeAddon.store["lastIncrementalSync"] == unix_to_watermark(1789000200)


def test_retention_overrun_schedules_update_and_holds_watermark():
    from kofin.sync.changefeed import watermark_to_unix

    seed_views(("lib1", "Movies", "movies"))
    seed_whitelist("lib1")
    manager, api = make_tier1_library()
    FakeAddon.store["lastIncrementalSync"] = "2026-01-01T00:00:00Z"

    api.kofin_queue_result = {
        "ServerTime": 1789000123,
        "RetentionCutoff": watermark_to_unix("2026-06-01T00:00:00Z"),
        "Items": [],
        "UserData": [],
    }

    assert manager.fast_sync() is True
    assert manager.retention_repair_pending is True
    assert ("UpdateLibrary", {}) in drain(manager.commands)

    # Held until the targeted pass completes.
    manager.save_last_sync()
    assert FakeAddon.store["lastIncrementalSync"] == "2026-01-01T00:00:00Z"

    manager.retention_repair_pending = False
    manager.save_last_sync()
    assert FakeAddon.store["lastIncrementalSync"] != "2026-01-01T00:00:00Z"


def test_stamp_watermark_if_empty():
    from kofin.sync.changefeed import unix_to_watermark

    manager, _api = make_tier1_library(server_time=1789000555)
    FakeAddon.store.pop("lastIncrementalSync", None)

    manager.stamp_watermark_if_empty()
    assert FakeAddon.store["lastIncrementalSync"] == unix_to_watermark(1789000555)

    # An existing watermark is never touched (older = safer).
    FakeAddon.store["lastIncrementalSync"] = "2026-01-01T00:00:00Z"
    manager.stamp_watermark_if_empty()
    assert FakeAddon.store["lastIncrementalSync"] == "2026-01-01T00:00:00Z"


def test_artwork_downloads_gated_and_light():
    manager, api = make_tier1_library()
    manager.artwork_only_ids = {"a1"}
    manager.updated_queue.put(["u1"])
    manager.artwork_queue.put(["a1"])
    api.items_result = {"Items": [{"Id": "u1", "Type": "Movie", "Name": "U"}]}

    manager.worker_downloads()
    assert {t.source for t in manager.download_threads} == {"updated"}
    for thread in manager.download_threads:
        thread.join(5)
    manager.download_threads = []

    api.items_result = {"Items": [{"Id": "a1", "Type": "Movie", "Name": "A"}]}
    manager.worker_downloads()
    assert {t.source for t in manager.download_threads} == {"artwork"}
    for thread in manager.download_threads:
        thread.join(5)

    fields_used = [r["Fields"] for r in api.items_requests]
    assert fields_used[0] != "Etag" and fields_used[-1] == "Etag"

    items = drain(manager.updated_output["Movie"])
    tags = {i["Id"]: i.get("_artwork_only", False) for i in items}
    assert tags == {"u1": False, "a1": True}


def test_requeue_full_untags_and_requeues():
    manager, _api = make_library()
    manager.artwork_only_ids = {"x1"}
    manager.requeue_full("x1")
    assert manager.artwork_only_ids == set()
    assert drain(manager.updated_queue) == [["x1"]]


class FakeWriter:
    def __init__(self, name):
        self.name = name
        self.removed = []

    def remove(self, item_id):
        self.removed.append(item_id)


def _writers():
    return (
        FakeWriter("movies"),
        FakeWriter("tvshows"),
        FakeWriter("music"),
        FakeWriter("musicvideos"),
    )


def test_boxset_removal_routes_to_the_movies_writer():
    """Live on tier 1: the feed delivers BoxSet removals, which the fork's
    dispatch never matched — the removal raised UnboundLocalError and the
    collections stayed in Kodi. Movies.remove dispatches on the mapping row's
    media, so a set id belongs there."""
    movies, tvshows, music, musicvideos = _writers()

    writer = library_mod.removal_writer_for(
        "BoxSet", movies, tvshows, music, musicvideos
    )

    assert writer is not None
    writer("set-1")
    assert movies.removed == ["set-1"]


def test_every_synced_kind_has_a_removal_writer():
    """The nine kinds the change feed records must each route somewhere;
    a kind with no writer silently never gets deleted."""
    movies, tvshows, music, musicvideos = _writers()

    for kind in (
        "Movie",
        "BoxSet",
        "Series",
        "Season",
        "Episode",
        "MusicVideo",
        "MusicAlbum",
        "MusicArtist",
        "Audio",
    ):
        assert (
            library_mod.removal_writer_for(kind, movies, tvshows, music, musicvideos)
            is not None
        ), kind


def test_unknown_kind_returns_none_never_a_stale_writer():
    """The hazard the old loop carried: an unhandled kind kept the previous
    iteration's writer and was deleted through it."""
    movies, tvshows, music, musicvideos = _writers()

    assert (
        library_mod.removal_writer_for("Photo", movies, tvshows, music, musicvideos)
        is None
    )
    assert movies.removed == []


def test_dispatch_tolerates_the_unbuilt_writer_family():
    """A RemovedWorker builds video *or* music writers, never both, so the
    other family arrives as None. Handing those to the dispatch must return
    None rather than raise — the music worker sees a BoxSet record on tier 1
    only as a routing miss, not a crash."""
    movies, tvshows, _music, musicvideos = _writers()

    # Music worker: video writers unbuilt.
    assert library_mod.removal_writer_for("Movie", None, None, None, None) is None
    assert library_mod.removal_writer_for("BoxSet", None, None, None, None) is None

    # Video worker: music writer unbuilt, video kinds still route.
    assert (
        library_mod.removal_writer_for("Audio", movies, tvshows, None, musicvideos)
        is None
    )
    assert (
        library_mod.removal_writer_for("Movie", movies, tvshows, None, musicvideos)
        is not None
    )
