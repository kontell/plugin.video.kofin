"""L1 units for the sync orchestrator: queue routing, priority rules,
watermark handling and the ws-event wiring (plan §5 step 3)."""

import queue
import time
from datetime import datetime, timedelta

import pytest

from kofin.core import state
from kofin.core.http import JellyfinError, ServerUnreachable
from kofin.sync import db as sync_db
from kofin.sync import kofindb
from kofin.sync import library as library_mod
from kofin.sync import newcontent
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
    playing = False

    def isPlayingVideo(self):
        return self.playing


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
    manager = Library(api, FakePlayer(), lambda: api)
    # Kodistubs' Monitor.waitForAbort answers True ("aborting"), which would
    # end every poll loop on its first wait.
    manager.monitor = _FakeMonitor()
    return manager, api


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


def test_commands_never_blanket_refresh(builtins, monkeypatch):
    """A processed command fires no builtins of its own: refreshes belong to
    the paths that write (FullSync's end, removals, the drain). The old tail
    refresh cost an UpdateLibrary(video) per command — one per screensaver
    wake for the FastSync kick alone (widget-refresh-plan F1/D4)."""
    manager, _api = make_library()
    monkeypatch.setattr(manager, "update_status_strings", lambda: None)

    manager.enqueue_command("FastSync")  # tier none: queues nothing
    manager.enqueue_command("SyncMusicPlaylists")  # setting off: no-op
    manager.process_commands()

    assert builtins == []


def test_remove_library_refreshes_the_removed_kind(builtins, monkeypatch):
    """Removing a music library refreshes *music* — the old blanket refresh
    aimed at video, so removed albums lingered in the music widgets
    indefinitely (widget-refresh-plan F5)."""
    seed_views(("lib2", "Tunes", "music"))

    manager, _api = make_library()
    monkeypatch.setattr(manager, "update_status_strings", lambda: None)
    monkeypatch.setattr(manager, "remove_library", lambda lib: True)

    manager.enqueue_command("RemoveLibrary", {"Id": "lib2"})
    manager.process_commands()

    assert builtins == ["UpdateLibrary(music,%s)" % library_mod.MUSIC_REFRESH_PROBE]


def test_first_content_reload_polls_for_hascontent(monkeypatch, tmp_path):
    """The reload waits for the scan cycle to flip Library.HasContent instead
    of guessing 2 s: a reload against still-false bools builds Home without
    its widget sections, and the hidden-content probe is self-disarming so
    that race could never retry (widget-refresh-plan D5)."""
    calls = []
    progress = {"polls": 0, "flips_after": 3}

    def cond(flag):
        if flag.startswith("Library.HasContent"):
            return progress["polls"] >= progress["flips_after"]
        return False

    class CountingMonitor:
        def waitForAbort(self, seconds=0):
            progress["polls"] += 1
            return False

        def abortRequested(self):
            return False

    monkeypatch.setattr("xbmc.executebuiltin", lambda cmd: calls.append(cmd))
    monkeypatch.setattr("xbmc.getCondVisibility", cond)
    _fake_video_db(monkeypatch, tmp_path, rows=True)

    manager, _api = make_library()
    manager.monitor = CountingMonitor()
    manager.refresh_libraries({"video"})

    assert calls == ["UpdateLibrary(video)", "ReloadSkin()"]
    assert progress["polls"] == progress["flips_after"]


def test_first_content_reload_held_during_playback(monkeypatch, tmp_path):
    """A skin reload rebuilds the OSD under the viewer: with video playing
    the reveal waits, and the service tick fires it once playback ends."""
    calls = []
    monkeypatch.setattr("xbmc.executebuiltin", lambda cmd: calls.append(cmd))
    monkeypatch.setattr("xbmc.getCondVisibility", lambda cond: False)
    _fake_video_db(monkeypatch, tmp_path, rows=True)

    manager, _api = make_library()
    manager.player.playing = True
    manager.refresh_libraries({"video"})

    assert calls == ["UpdateLibrary(video)"]
    assert manager.pending_skin_reload is True

    manager.flush_pending_reload()
    assert calls == ["UpdateLibrary(video)"]  # still playing: held

    manager.player.playing = False
    manager.flush_pending_reload()
    assert calls == ["UpdateLibrary(video)", "ReloadSkin()"]
    assert manager.pending_skin_reload is False


def _fake_music_db(tmp_path, rows):
    """A music database whose album/song tables are (non)empty."""
    import sqlite3

    path = str(tmp_path / "MyMusic83.db")
    conn = sqlite3.connect(path)
    for table in ("album", "song"):
        conn.execute("CREATE TABLE %s (id INTEGER)" % table)
    if rows:
        conn.execute("INSERT INTO album VALUES (1)")
    conn.commit()
    conn.close()
    sync_db.set_path_override("music", path)


def test_first_music_content_reloads_skin(monkeypatch, tmp_path):
    """A first *music* sync has the same empty->populated blindness as video
    and previously no reveal path at all (widget-refresh-plan F6): the probe
    scan fires, and the skin reloads (here via the poll's timeout fallback —
    reloading late beats leaving the section invisible until restart)."""
    seed_views(("lib2", "Tunes", "music"))
    seed_whitelist("lib2")

    calls = []
    monkeypatch.setattr("xbmc.executebuiltin", lambda cmd: calls.append(cmd))
    monkeypatch.setattr("xbmc.getCondVisibility", lambda cond: False)
    _fake_music_db(tmp_path, rows=True)

    manager, _api = make_library()
    manager.refresh_libraries({"music"})

    assert calls == [
        "UpdateLibrary(music,%s)" % library_mod.MUSIC_REFRESH_PROBE,
        "ReloadSkin()",
    ]


def test_no_music_probe_for_unsynced_music(monkeypatch, tmp_path):
    """Without a music library in the whitelist the hidden-content check must
    not open MyMusic at all: opening it puts the music schema gate in front
    of users who never asked kofin to touch their music."""
    calls = []
    monkeypatch.setattr("xbmc.executebuiltin", lambda cmd: calls.append(cmd))
    monkeypatch.setattr("xbmc.getCondVisibility", lambda cond: False)

    opened = []
    original_init = sync_db.Database.__init__

    def spying_init(self, file="video", *args, **kwargs):
        opened.append(file)
        original_init(self, file, *args, **kwargs)

    monkeypatch.setattr(sync_db.Database, "__init__", spying_init)

    manager, _api = make_library()
    manager.refresh_libraries({"music"})

    assert "music" not in opened
    assert calls == ["UpdateLibrary(music,%s)" % library_mod.MUSIC_REFRESH_PROBE]


def test_drain_refresh_waits_out_the_settle(builtins):
    """Two mini-cycles seconds apart — a track change's pair of userdata
    echoes — produce one refresh, not two (widget-refresh-plan F3/D3)."""
    manager, _api = make_library()

    manager._arm_refresh_settle({"video"})
    manager.flush_refresh_settle()  # inside the settle: nothing fires
    assert builtins == []

    manager._arm_refresh_settle({"music"})  # the second echo folds in

    manager.refresh_due_at = datetime.now() - timedelta(seconds=1)
    manager.flush_refresh_settle()

    assert builtins == [
        "UpdateLibrary(video)",
        "UpdateLibrary(music,%s)" % library_mod.MUSIC_REFRESH_PROBE,
    ]
    assert manager.refresh_pending == set()
    assert manager.refresh_hold_until is None


def test_settle_holds_while_a_cycle_is_draining(builtins):
    """An active cycle keeps the deferred refresh back: its completion folds
    its own databases in and re-arms, so the eventual refresh covers both."""
    manager, _api = make_library()
    manager._arm_refresh_settle({"video"})
    manager.refresh_due_at = datetime.now() - timedelta(seconds=1)
    manager.pending_refresh = True

    manager.flush_refresh_settle()

    assert builtins == []
    assert manager.refresh_pending == {"video"}


def test_settle_cap_bounds_the_wait(builtins):
    """A steady event stream re-arms the settle forever; the hold cap fires
    the refresh anyway, mid-drain or not, so staleness stays bounded."""
    manager, _api = make_library()
    manager._arm_refresh_settle({"video"})
    manager.pending_refresh = True
    manager.refresh_due_at = datetime.now() + timedelta(seconds=60)
    manager.refresh_hold_until = datetime.now() - timedelta(seconds=1)

    manager.flush_refresh_settle()

    assert builtins == ["UpdateLibrary(video)"]


def test_immediate_refresh_settles_the_deferred_debt(builtins):
    """refresh_added and command-owned refreshes fire immediately; the
    databases they cover leave the deferred set (and the clocks clear with
    the last of them) so the settle cannot double-refresh them."""
    manager, _api = make_library()
    manager._arm_refresh_settle({"video"})

    manager.refresh_libraries({"video"})

    assert builtins == ["UpdateLibrary(video)"]
    assert manager.refresh_pending == set()
    assert manager.refresh_hold_until is None

    manager.refresh_due_at = datetime.now() - timedelta(seconds=1)
    manager.flush_refresh_settle()
    assert builtins == ["UpdateLibrary(video)"]  # no second refresh


def test_status_strings_write_only_on_change(monkeypatch):
    """Every settings write rewrites settings.xml and fires onSettingsChanged;
    the old per-command rewrites raced the applier's re-read into transient
    load failures (widget-refresh-plan F9). Unchanged values write nothing."""
    seed_views(("lib1", "Movies", "movies"))
    seed_whitelist("lib1")
    monkeypatch.setattr(library_mod.schema, "gate_status", lambda kinds=None: None)

    manager, _api = make_library()

    writes = []
    original = FakeAddon.setSetting

    def counting(self, key, value):
        writes.append(key)
        original(self, key, value)

    monkeypatch.setattr(FakeAddon, "setSetting", counting)

    manager.update_status_strings()
    assert "syncStatus" in writes and "syncedLibraries" in writes

    writes.clear()
    manager.update_status_strings()
    assert writes == []


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


# --- progress accounting -----------------------------------------------------


def test_progress_rises_as_work_drains():
    """The percentage used to count down. total_updates counts work when it is
    *enqueued*, but the numerator only looked at the output queues, which are
    empty at that moment because everything is still waiting to be downloaded
    — so progress opened at 100% and fell as downloads landed."""
    lib, _api = make_library()

    lib.added(["m%d" % n for n in range(120)])
    assert lib.total_updates == 120
    # Nothing written yet: freshly queued work must read as 0%, not 100%.
    assert lib.progress_percent() == 0
    assert lib.pending_items() == 120

    # Downloads land: items move from the download queue to a writer queue.
    # That is a sideways move, not progress, and must not move the bar.
    chunk = lib.added_queue.get_nowait()
    for item_id in chunk:
        lib.added_output["Movie"].put({"Id": item_id, "Type": "Movie"})
    assert lib.pending_items() == 120
    assert lib.progress_percent() == 0

    # Writers drain half of that chunk: now it is real progress.
    for _ in range(len(chunk) // 2):
        lib.added_output["Movie"].get_nowait()
    assert lib.progress_percent() > 0


def test_progress_never_leaves_the_range():
    """Work enqueued mid-drain lifts both sides of the ratio, and items in
    flight are counted in neither, so the raw value can stray."""
    lib, _api = make_library()

    assert lib.progress_percent() == 0  # nothing enqueued: no division by zero

    lib.added(["a1", "a2"])
    lib.total_updates = 1  # denominator smaller than what is pending
    assert 0 <= lib.progress_percent() <= 100


def test_pending_counts_items_not_chunks():
    """The download queues hold chunks; total_updates counts ids. Comparing
    the two directly would make one chunk of 50 look like a single item."""
    lib, _api = make_library()

    lib.added(["m%d" % n for n in range(library_mod.DOWNLOAD_CHUNK + 10)])

    assert lib.added_queue.qsize() == 2  # two chunks
    assert lib.pending_items() == library_mod.DOWNLOAD_CHUNK + 10


def test_removed_queue_holds_ids_not_chunks():
    lib, _api = make_library()

    lib.removed(["r1", "r2", "r3"])

    assert lib.pending_items() == 3


# --- resuming an unfinished full sync ----------------------------------------


def _pending(*library_ids):
    sync = sync_db.get_sync()
    sync["Libraries"] = list(library_ids)
    sync_db.save_sync(sync)


def _arm_resume(lib):
    """Make the next tick due (the interval is otherwise a minute away)."""
    lib.resume_at = None


def test_resume_reenters_a_pending_full_sync(monkeypatch):
    """startup() resumes the queue once, at thread start. If the server was
    unreachable then — or went away mid-sync — the entry sat in sync.json
    until Kodi restarted: resumed after a restart, but not after a
    reconnection."""
    lib, _api = make_library()
    _pending("lib1")
    resumed = []
    monkeypatch.setattr(
        lib, "add_library", lambda lib_id: resumed.append(lib_id) or True
    )
    monkeypatch.setattr(lib, "update_status_strings", lambda: None)
    _arm_resume(lib)

    lib.resume_pending_libraries()

    # None is the whole-queue resume, the same entry point startup() takes.
    assert resumed == [None]


def test_resume_does_nothing_with_an_empty_queue(monkeypatch):
    lib, _api = make_library()
    _pending()
    called = []
    monkeypatch.setattr(
        lib, "add_library", lambda lib_id: called.append(lib_id) or True
    )
    _arm_resume(lib)

    lib.resume_pending_libraries()

    assert called == []


def test_resume_yields_to_a_running_sync(monkeypatch):
    """FullSync is a Borg: re-entering while one runs raises at us."""
    lib, _api = make_library()
    _pending("lib1")
    called = []
    monkeypatch.setattr(
        lib, "add_library", lambda lib_id: called.append(lib_id) or True
    )
    FakeWindow.store["kofin.sync.active"] = "true"
    _arm_resume(lib)

    lib.resume_pending_libraries()

    assert called == []


def test_resume_waits_while_offline(monkeypatch):
    lib, _api = make_library()
    _pending("lib1")
    called = []
    monkeypatch.setattr(
        lib, "add_library", lambda lib_id: called.append(lib_id) or True
    )
    FakeWindow.store.pop("kofin.online", None)
    _arm_resume(lib)

    lib.resume_pending_libraries()

    assert called == []


def test_resume_is_rate_limited(monkeypatch):
    """Without this the 'Resuming interrupted library sync' toast would fire
    on every tick for as long as the queue stayed pending."""
    lib, _api = make_library()
    _pending("lib1")
    calls = []
    monkeypatch.setattr(lib, "add_library", lambda lib_id: calls.append(lib_id) or True)
    monkeypatch.setattr(lib, "update_status_strings", lambda: None)
    _arm_resume(lib)

    lib.resume_pending_libraries()
    lib.resume_pending_libraries()  # immediately again: still inside the window
    lib.resume_pending_libraries()

    assert len(calls) == 1


def test_resume_backs_off_while_it_keeps_failing(monkeypatch):
    """A server that stays down must not be retried on the base interval."""
    lib, _api = make_library()
    _pending("lib1")
    monkeypatch.setattr(lib, "add_library", lambda lib_id: False)

    delays = []
    for _ in range(4):
        _arm_resume(lib)
        lib.resume_pending_libraries()
        delays.append(lib.resume_delay)

    assert delays == [120, 240, 480, 960]
    assert lib.resume_delay <= library_mod.RESUME_POLL_MAX_SECONDS


def test_resume_delay_resets_after_a_success(monkeypatch):
    lib, _api = make_library()
    _pending("lib1")
    monkeypatch.setattr(lib, "add_library", lambda lib_id: False)
    monkeypatch.setattr(lib, "update_status_strings", lambda: None)

    _arm_resume(lib)
    lib.resume_pending_libraries()
    assert lib.resume_delay > library_mod.RESUME_POLL_SECONDS

    monkeypatch.setattr(lib, "add_library", lambda lib_id: True)
    _arm_resume(lib)
    lib.resume_pending_libraries()

    assert lib.resume_delay == library_mod.RESUME_POLL_SECONDS


# --- recovery from items that never applied ----------------------------------


def test_writer_failure_schedules_a_recovery_prune():
    """Only the download side held the watermark back, so anything that failed
    *after* a good download was logged and forgotten while the watermark moved
    past it — and the change feed, queried from that watermark, can never offer
    it again. That is a film added on the 22nd still missing on the 25th."""
    lib, _api = make_library()

    lib.flag_unapplied("m1", "Movie: boom")
    lib.flag_unapplied("m2", "Movie: boom")
    lib.schedule_recovery_prune()

    assert [c for c, _d in list(lib.commands.queue)] == ["UpdateLibrary"]


def test_no_failures_schedules_nothing():
    lib, _api = make_library()

    lib.schedule_recovery_prune()

    assert list(lib.commands.queue) == []


def test_self_healing_paths_are_silent(monkeypatch):
    """A heal the user cannot act on and that fixes itself does not get a
    toast: it only invites worry about work already in hand. The LOG.warning
    on each path stays as the record."""
    toasts = []
    monkeypatch.setattr(
        "kofin.sync.library.notification", lambda *a, **kw: toasts.append(a)
    )

    lib, _api = make_library()

    lib.flag_unapplied("m1", "Movie: boom")
    lib.schedule_recovery_prune()
    lib.schedule_retry()

    assert [c for c, _d in list(lib.commands.queue)] == ["UpdateLibrary"]
    assert toasts == []


# --- divergence probe --------------------------------------------------------


def probe_library(monkeypatch, remote, local_ids, media="movies"):
    """A library whose server count and local reference map the test sets."""
    seed_views(("lib1", "Movies", media))
    seed_whitelist("lib1")

    lib, _api = make_library()

    monkeypatch.setattr(
        "kofin.sync.library.get_prune_count", lambda api, lid, types: remote
    )
    monkeypatch.setattr(
        "kofin.sync.library.local_reference_map",
        lambda lid, media_class: dict.fromkeys(local_ids),
    )
    return lib


def commands_of(lib):
    return [c for c, _d in list(lib.commands.queue)]


def test_probe_is_quiet_when_the_counts_agree(monkeypatch):
    """No gap, no heal -- the steady state on every boot."""
    lib = probe_library(monkeypatch, remote=3, local_ids=["a", "b", "c"])

    lib.probe_divergence()

    assert commands_of(lib) == []


def test_probe_heals_on_any_gap(monkeypatch):
    """Two seasons vanishing left no server-side record, so no watermark pass
    could see them. A count can.

    Any gap counts, which holds only because the writers now reference
    flat-layout ("virtual") seasons. While they did not, a library sat
    permanently short by however many it had."""
    lib = probe_library(monkeypatch, remote=10, local_ids=["a", "b", "c"])

    lib.probe_divergence()

    assert commands_of(lib) == ["UpdateLibrary"]


def test_probe_heals_when_the_local_side_is_ahead(monkeypatch):
    """A stale local row is divergence too: the prune's third arm removes it."""
    lib = probe_library(monkeypatch, remote=1, local_ids=["a", "b"])

    lib.probe_divergence()

    assert commands_of(lib) == ["UpdateLibrary"]


def test_probe_skips_while_a_full_sync_is_pending(monkeypatch):
    """A resuming library is legitimately short; every count would read as
    divergence and FullSync is a Borg that would raise at a second entry."""
    lib = probe_library(monkeypatch, remote=10, local_ids=[])
    sync = sync_db.get_sync()
    sync["Libraries"] = ["lib1"]
    sync_db.save_sync(sync)

    lib.probe_divergence()

    assert commands_of(lib) == []


def test_probe_skips_when_the_catch_up_queued_work(monkeypatch):
    """The gap would be the work already in hand: it would read as divergence
    now, and again as divergence once it lands."""
    lib = probe_library(monkeypatch, remote=10, local_ids=[])
    lib.total_updates = 12

    lib.probe_divergence()

    assert commands_of(lib) == []


def test_probe_runs_during_playback_only_when_allowed(monkeypatch):
    """syncDuringPlay makes playback a good time to probe -- the box is awake
    and nobody is waiting on the library."""
    monkeypatch.setattr("xbmc.getCondVisibility", lambda cond: False)

    lib = probe_library(monkeypatch, remote=10, local_ids=["a"])
    lib.player.isPlayingVideo = lambda: True

    lib.probe_divergence()
    assert commands_of(lib) == []

    FakeAddon.store["syncDuringPlay"] = "true"
    lib.probe_divergence()
    assert commands_of(lib) == ["UpdateLibrary"]


def test_probe_survives_an_unreachable_server(monkeypatch):
    lib = probe_library(monkeypatch, remote=10, local_ids=["a"])

    def boom(api, lid, types):
        raise ServerUnreachable("down")

    monkeypatch.setattr("kofin.sync.library.get_prune_count", boom)

    lib.probe_divergence()

    assert commands_of(lib) == []


# --- boxset drift probe -------------------------------------------------------


def boxset_probe_library(tmp_path):
    """A library with a real (pristine) MyVideos and a boxsets view.

    The drift probe is pure-local — kofin.db against MyVideos — so unlike
    probe_library above there is nothing to monkeypatch; the tests seed the
    two databases directly.
    """
    from tests.unit import kodifixtures

    seed_views(("bs1", "Collections", "boxsets"))
    sync_db.set_path_override(
        "video",
        kodifixtures.create_video_db(
            str(tmp_path / "MyVideos131.db"), kodifixtures.VIDEO_VERSION
        ),
    )
    lib, _api = make_library()
    return lib


def seed_set_reference(jellyfin_id, kodi_id, state=None):
    with sync_db.Database("kofin") as opened:
        mapping = kofindb.JellyfinDatabase(opened.cursor)
        mapping.add_reference(
            jellyfin_id,
            kodi_id,
            None,
            None,
            "BoxSet",
            "set",
            None,
            "x|plugin",
            None,
            None,
        )
        if state is not None:
            mapping.add_boxset_state(jellyfin_id, state)


def seed_kodi_set(kodi_id, linked, first_movie_id=100):
    import sqlite3

    conn = sqlite3.connect(str(sync_db._path_overrides["video"]))
    try:
        conn.execute(
            "INSERT INTO sets(idSet, strSet) VALUES (?, ?)",
            (kodi_id, "Set %s" % kodi_id),
        )
        for offset in range(linked):
            conn.execute(
                "INSERT INTO movie(idMovie, idSet) VALUES (?, ?)",
                (first_movie_id + offset, kodi_id),
            )
        conn.commit()
    finally:
        conn.close()


def boxset_commands(lib):
    return list(lib.commands.queue)


def test_boxset_probe_quiet_when_state_agrees(tmp_path):
    lib = boxset_probe_library(tmp_path)
    seed_set_reference("set1", 1, state=2)
    seed_kodi_set(1, linked=2)

    lib.probe_boxset_drift()

    assert boxset_commands(lib) == []


def test_boxset_probe_quiet_on_a_legitimately_empty_set(tmp_path):
    """A mixed collection can hold zero movies forever; stored 0 == linked 0
    must never loop."""
    lib = boxset_probe_library(tmp_path)
    seed_set_reference("set1", 1, state=0)
    seed_kodi_set(1, linked=0)

    lib.probe_boxset_drift()

    assert boxset_commands(lib) == []


def test_boxset_probe_schedules_on_count_drift(tmp_path):
    """The V1 shape: a member removed and re-added dropped the MyVideos link
    while the stamped expectation still says two."""
    lib = boxset_probe_library(tmp_path)
    seed_set_reference("set1", 1, state=2)
    seed_kodi_set(1, linked=1)

    lib.probe_boxset_drift()

    assert boxset_commands(lib) == [("SyncLibrary", {"Id": "Boxsets:"})]


def test_boxset_probe_schedules_on_missing_state(tmp_path):
    """First boot after upgrade: no state rows exist, every set heals once."""
    lib = boxset_probe_library(tmp_path)
    seed_set_reference("set1", 1, state=None)
    seed_kodi_set(1, linked=2)

    lib.probe_boxset_drift()

    assert boxset_commands(lib) == [("SyncLibrary", {"Id": "Boxsets:"})]


def test_boxset_probe_schedules_on_missing_sets_row(tmp_path):
    """Kodi's clean-library dropped the memberless sets row while the
    reference lives on."""
    lib = boxset_probe_library(tmp_path)
    seed_set_reference("set1", 1, state=0)

    lib.probe_boxset_drift()

    assert boxset_commands(lib) == [("SyncLibrary", {"Id": "Boxsets:"})]


def test_boxset_probe_quiet_without_a_boxsets_view(tmp_path):
    """No collections view: nothing can have synced and the pass it would
    schedule has nothing to walk. Returns before touching MyVideos."""
    seed_views(("lib1", "Movies", "movies"))
    lib, _api = make_library()
    seed_set_reference("set1", 1, state=2)

    lib.probe_boxset_drift()

    assert boxset_commands(lib) == []


def test_boxset_probe_skips_while_a_full_sync_is_pending(tmp_path):
    lib = boxset_probe_library(tmp_path)
    seed_set_reference("set1", 1, state=2)
    seed_kodi_set(1, linked=0)
    sync = sync_db.get_sync()
    sync["Libraries"] = ["lib1"]
    sync_db.save_sync(sync)

    lib.probe_boxset_drift()

    assert boxset_commands(lib) == []


def test_container_types_are_ignored_not_flagged():
    """A UserView/CollectionFolder can never route anywhere, and the server
    broadcasts LibraryChanged for them on its own schedule. Flagging one cost
    a user-facing "some items did not sync" plus a full prune of every
    library, on nothing at all."""
    flagged = []
    work = queue.Queue()
    work.put(["v1", "c1", "m1"])

    class Api:
        def items(self, params):
            return {
                "Items": [
                    {"Id": "v1", "Type": "UserView", "Name": "Playlists"},
                    {"Id": "c1", "Type": "CollectionFolder", "Name": "Movies"},
                    {"Id": "m1", "Type": "Movie", "Name": "real"},
                ]
            }

    movies = queue.Queue()
    worker = GetItemWorker(
        Api(), work, {"Movie": movies}, unapplied=lambda i, r: flagged.append(i)
    )
    worker.start()
    worker.join(timeout=5)

    assert flagged == []
    assert movies.get_nowait()["Id"] == "m1"


def test_synced_container_types_still_flag():
    """The ignore list is about non-content, not about folders: a Season with
    no queue is a real routing failure and must still be recovered."""
    flagged = []
    work = queue.Queue()
    work.put(["s1"])

    class Api:
        def items(self, params):
            return {"Items": [{"Id": "s1", "Type": "Season", "Name": "S1"}]}

    worker = GetItemWorker(
        Api(), work, {"Movie": queue.Queue()}, unapplied=lambda i, r: flagged.append(i)
    )
    worker.start()
    worker.join(timeout=5)

    assert flagged == ["s1"]


# --- writer-queue backpressure -----------------------------------------------


class _SlowWriterApi:
    """Returns a chunk of items of one type, so the test can fill a queue."""

    def __init__(self, item_type, count):
        self.item_type = item_type
        self.count = count
        self.calls = 0

    def items(self, params):
        self.calls += 1
        return {
            "Items": [
                {"Id": "i%d-%d" % (self.calls, n), "Type": self.item_type, "Name": "n"}
                for n in range(self.count)
            ]
        }


def test_only_the_item_carrying_queues_are_bounded():
    """userdata_output is fed straight from the library thread by userdata(),
    so bounding it would block the service tick itself; removed_output holds
    ids, not items."""
    lib, _api = make_library()

    assert lib.added_output["Movie"].maxsize == library_mod.WRITE_QUEUE_MAX
    assert lib.updated_output["Movie"].maxsize == library_mod.WRITE_QUEUE_MAX
    assert lib.userdata_output["Movie"].maxsize == 0
    assert lib.removed_output["Movie"].maxsize == 0


def test_downloader_waits_instead_of_growing_the_queue():
    """Against the library's own queues, not a hand-made bounded one — the
    point is that the real write queues have a ceiling. Nothing else throttles
    the download side: workers run until their id queue is empty, so a
    whole-library catch-up used to end up resident all at once (~490 MB across
    the three libraries measured here)."""
    lib, _api = make_library()
    bound = library_mod.WRITE_QUEUE_MAX
    work = queue.Queue()
    work.put(["a"])  # one chunk yielding more items than the bound

    worker = GetItemWorker(_SlowWriterApi("Movie", bound + 50), work, lib.added_output)
    worker.daemon = True
    worker.start()
    try:
        deadline = time.time() + 10
        while lib.added_output["Movie"].qsize() < bound and time.time() < deadline:
            time.sleep(0.01)

        # Parked at the ceiling rather than pulling the whole chunk into RAM.
        time.sleep(0.3)
        assert lib.added_output["Movie"].qsize() == bound
        assert worker.is_alive()

        # Draining a slot lets exactly one more through.
        lib.added_output["Movie"].get_nowait()
        deadline = time.time() + 5
        while lib.added_output["Movie"].qsize() < bound and time.time() < deadline:
            time.sleep(0.01)
        assert lib.added_output["Movie"].qsize() == bound
    finally:
        FakeWindow.store["kofin.sync.stop"] = "true"
        worker.join(timeout=10)


def test_a_blocked_downloader_still_stops():
    """A bare blocking put is how a slow writer becomes a Kodi that will not
    quit — the same trap the page pool fell into."""
    out = {"Movie": queue.Queue(maxsize=2)}
    work = queue.Queue()
    work.put(["a"])

    worker = GetItemWorker(_SlowWriterApi("Movie", 50), work, out)
    worker.daemon = True
    worker.start()

    deadline = time.time() + 5
    while out["Movie"].qsize() < 2 and time.time() < deadline:
        time.sleep(0.01)

    FakeWindow.store["kofin.sync.stop"] = "true"
    worker.join(timeout=10)

    assert not worker.is_alive(), "downloader hung on a full queue during shutdown"


# -- music playlist poll --------------------------------------------------


def _playlist_library(monkeypatch):
    """A library whose playlist refresh only records that it ran."""
    lib, _api = make_library()
    runs = []
    monkeypatch.setattr(lib, "sync_music_playlists", lambda: runs.append(1))
    FakeAddon.store["syncMusicPlaylists"] = "true"
    state.set_online(True)
    return lib, runs


def test_playlists_are_polled_on_the_first_tick(monkeypatch):
    """Nothing pushes playlist edits (Jellyfin sends no websocket message for
    them), so an edit made while Kodi was off must be picked up at startup."""
    lib, runs = _playlist_library(monkeypatch)

    lib.poll_music_playlists()

    assert runs == [1]


def test_playlist_poll_is_rate_limited(monkeypatch):
    """The library thread ticks every two seconds; the poll is per-interval."""
    lib, runs = _playlist_library(monkeypatch)

    lib.poll_music_playlists()
    lib.poll_music_playlists()
    lib.poll_music_playlists()

    assert len(runs) == 1

    lib.playlist_poll_at = datetime.now() - timedelta(seconds=1)
    lib.poll_music_playlists()

    assert len(runs) == 2


def test_playlist_poll_waits_for_the_setting(monkeypatch):
    lib, runs = _playlist_library(monkeypatch)
    FakeAddon.store["syncMusicPlaylists"] = "false"

    lib.poll_music_playlists()

    assert runs == []


def test_playlist_poll_yields_to_a_running_sync(monkeypatch):
    """The refresh reads song rows a drain is still writing; it would only
    have to run again once the cycle finished."""
    lib, runs = _playlist_library(monkeypatch)
    lib.pending_refresh = True

    lib.poll_music_playlists()

    assert runs == []
    # Not consumed either: the poll must still happen once the sync lands.
    assert lib.playlist_poll_at is None


def test_playlist_poll_waits_while_offline(monkeypatch):
    lib, runs = _playlist_library(monkeypatch)
    state.set_online(False)

    lib.poll_music_playlists()

    assert runs == []


# --- new-content notifications ------------------------------------------------


def _notify_library(monkeypatch, *entries):
    """A library holding ``entries`` on its writers' notify queue, plus the
    list its toasts land in."""
    lib, _api = make_library()
    FakeAddon.store["notifyNewContent"] = "true"
    sent = []
    monkeypatch.setattr(
        library_mod, "notification", lambda message, **kwargs: sent.append(message)
    )
    monkeypatch.setattr(
        library_mod.settings, "localized", lambda string_id: "L%d %%s" % string_id
    )
    # Kodistubs answers every condition True, which would read as live TV.
    monkeypatch.setattr(library_mod.xbmc, "getCondVisibility", lambda condition: False)

    for entry in entries:
        lib.notify_output.put(entry)

    return lib, sent


MOVIE_ENTRY = newcontent.Entry("Movie", "movie1", "The Example")
ALBUM_ENTRY = newcontent.Entry("MusicAlbum", "album1", "Inner Song")


def test_new_content_toasts_once_per_content_type(monkeypatch):
    lib, sent = _notify_library(monkeypatch, MOVIE_ENTRY, ALBUM_ENTRY)

    lib.notify_new_content()

    assert len(sent) == 2
    assert lib.new_content == []


def test_new_content_waits_for_the_cycle_to_finish_adding(monkeypatch):
    """The same predicate refresh_added uses: the toast belongs with the
    content, not with whatever is still being downloaded for it."""
    lib, sent = _notify_library(monkeypatch, MOVIE_ENTRY)
    lib.added_queue.put(["movie2"])

    lib.notify_new_content()

    assert sent == []
    # Held, not dropped -- and off the queue, so nothing re-reads it.
    assert lib.new_content == [MOVIE_ENTRY]
    assert lib.notify_output.qsize() == 0

    lib.added_queue.get()
    lib.notify_new_content()

    assert len(sent) == 1


def test_new_content_summarizes_a_whole_cycle_not_each_writer(monkeypatch):
    """Two writers reporting a movie each is one message, which is the whole
    point of collecting them here instead of toasting per item."""
    lib, sent = _notify_library(
        monkeypatch, MOVIE_ENTRY, newcontent.Entry("Movie", "movie2", "Another One")
    )
    lib.added_queue.put(["movie3"])

    lib.notify_new_content()
    lib.added_queue.get()
    lib.notify_output.put(newcontent.Entry("Movie", "movie3", "A Third"))
    lib.notify_new_content()

    assert sent == ["L30625 3"]


def test_new_content_respects_the_setting(monkeypatch):
    lib, sent = _notify_library(monkeypatch, MOVIE_ENTRY)
    FakeAddon.store["notifyNewContent"] = "false"

    lib.notify_new_content()

    assert sent == []
    # Cleared rather than held: turning it off silences the cycle in flight.
    assert lib.new_content == []


def test_new_content_holds_while_video_plays(monkeypatch):
    """Toasting over fullscreen video is the intrusion this feature must not
    become; the news keeps until playback ends."""
    lib, sent = _notify_library(monkeypatch, MOVIE_ENTRY)
    lib.player.playing = True

    lib.notify_new_content()

    assert sent == []
    assert lib.new_content == [MOVIE_ENTRY]

    lib.player.playing = False
    lib.notify_new_content()

    assert len(sent) == 1


def test_new_content_toasts_over_live_tv(monkeypatch):
    lib, sent = _notify_library(monkeypatch, MOVIE_ENTRY)
    lib.player.playing = True
    monkeypatch.setattr(
        library_mod.xbmc,
        "getCondVisibility",
        lambda condition: condition == "VideoPlayer.Content(livetv)",
    )

    lib.notify_new_content()

    assert len(sent) == 1


def test_new_content_does_not_repeat_itself_next_cycle(monkeypatch):
    lib, sent = _notify_library(monkeypatch, MOVIE_ENTRY)

    lib.notify_new_content()
    lib.notify_new_content()

    assert len(sent) == 1


def test_an_empty_cycle_says_nothing(monkeypatch):
    lib, sent = _notify_library(monkeypatch)

    lib.notify_new_content()

    assert sent == []


def test_a_broken_message_costs_the_toast_and_not_the_thread(monkeypatch):
    """service() ends the library thread on an exception, so a summary that
    cannot be built has to stop here."""
    lib, sent = _notify_library(monkeypatch, MOVIE_ENTRY)
    monkeypatch.setattr(
        library_mod.newcontent,
        "summarize",
        lambda entries: (_ for _ in ()).throw(TypeError("not enough arguments")),
    )

    lib.notify_new_content()

    assert sent == []
    assert lib.new_content == []
