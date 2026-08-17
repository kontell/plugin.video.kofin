import threading

import pytest

from kofin.core import ipc, state
from kofin.service.main import Backoff, Service
from tests.unit.fakes import FakeAddon, FakeWindow

# How long a stray worker is given to finish before it is called a leak. Only
# ever paid by a test that leaked one, and bounded so the guard cannot itself
# become the flake it exists to catch.
STRAY_WORKER_GRACE = 5


@pytest.fixture(autouse=True)
def kodi_fakes(monkeypatch):
    FakeAddon.store = {}
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    # Kodistubs' Monitor answers abortRequested() with True — a stub default,
    # not a simulation. These tests run against "Kodi is running", so the
    # default here is False; tests exercising the stop path raise it
    # per-instance.
    monkeypatch.setattr("xbmc.Monitor.abortRequested", lambda self: False)
    yield
    _refuse_stray_post_connect()


def _refuse_stray_post_connect():
    """Fail the test that leaves a post-connect worker running.

    ``_on_ws_connected`` spawns the pass on a thread and returns — that is the
    behaviour under test at ``test_the_connect_callback_does_not_block_on_the_pass``
    — so a test that neither joins it (``_join_post_connect``) nor stubs
    ``_run_post_connect`` hands the next test a live thread. Those workers
    carry a real ``Api`` whose server address is never set in this file, and
    they raise toasts and read settings into whichever test runs next; three
    of the four tests that *do* join went red together in one loaded run,
    which is what sent us looking.

    Named on the thread rather than tracked per service: the leak is only
    visible process-wide, and the teardown that catches it is the one
    belonging to the test that caused it.
    """
    for thread in threading.enumerate():
        if thread.name != "kofin-postconnect" or not thread.is_alive():
            continue
        thread.join(timeout=STRAY_WORKER_GRACE)
        assert not thread.is_alive(), (
            "a post-connect worker outlived its test: join it with "
            "_join_post_connect(service), or stub _run_post_connect"
        )


def test_backoff_doubles_to_ceiling():
    backoff = Backoff(start=5, ceiling=120)
    delays = [backoff.failed(now=0) for _ in range(7)]
    assert delays == [5, 10, 20, 40, 80, 120, 120]


def test_backoff_due_and_reset():
    backoff = Backoff(start=5, ceiling=120)
    assert backoff.due(0) is True
    backoff.failed(now=100)
    assert backoff.due(104) is False
    assert backoff.due(105) is True
    backoff.succeeded()
    assert backoff.due(0) is True
    assert backoff.failed(now=0) == 5


# --- pacing rebuilds of a sync manager that keeps dying -----------------------


class DeadLibrary:
    """A manager whose thread has ended; the flags say how it ended."""

    def __init__(self, startup_done=True, stop_thread=True):
        self.startup_done = startup_done
        self.stop_thread = stop_thread

    def is_alive(self):
        return False


class RunningLibrary:
    """A manager past startup, still in its service loop."""

    startup_done = True
    stop_thread = False

    def is_alive(self):
        return True


def _paced_service(monkeypatch, clock):
    monkeypatch.setattr("kofin.service.main.time.time", lambda: clock[0])
    service = Service()
    starts = []
    monkeypatch.setattr(service, "_start_library", lambda: starts.append(clock[0]))
    return service, starts


def test_a_dead_manager_is_rebuilt_immediately_the_first_time(monkeypatch):
    clock = [1000.0]
    service, starts = _paced_service(monkeypatch, clock)
    service.library = DeadLibrary()

    service._recover_threads()

    assert starts == [1000.0]


def test_consecutive_failures_wait_out_the_backoff(monkeypatch):
    """The schema-gate case: startup fails within a second, forever. Unpaced,
    that was a rebuild every tick — databases opened, error toast raised, and
    toasts queue, so the wall outlives the loop."""
    clock = [1000.0]
    service, starts = _paced_service(monkeypatch, clock)
    service.library = DeadLibrary()

    for _ in range(10):  # ten ticks inside the first 5 s rung
        service._recover_threads()
        clock[0] += 0.5

    assert starts == [1000.0]

    service._recover_threads()  # clock is at 1005.0: the rung is up
    assert starts == [1000.0, 1005.0]

    clock[0] = 1014.0  # inside the doubled 10 s rung
    service._recover_threads()
    assert starts == [1000.0, 1005.0]

    clock[0] = 1015.0
    service._recover_threads()
    assert starts == [1000.0, 1005.0, 1015.0]


def test_a_manager_that_gets_past_startup_resets_the_pacing(monkeypatch):
    clock = [1000.0]
    service, starts = _paced_service(monkeypatch, clock)
    service.library = DeadLibrary()

    service._recover_threads()  # immediate first rebuild arms the ladder
    assert starts == [1000.0]

    service.library = RunningLibrary()
    service._recover_threads()  # healthy: the ladder resets

    clock[0] = 1001.0
    service.library = DeadLibrary()
    service._recover_threads()  # a fresh death rebuilds immediately again
    assert starts == [1000.0, 1001.0]


def test_a_failing_build_on_its_way_out_does_not_reset_the_pacing(monkeypatch):
    """run() raises stop_thread *before* startup_done on the failure path, so
    the moment a failing build is alive with startup_done set, stop_thread is
    already up — and must keep that brief window from resetting the ladder."""
    clock = [1000.0]
    service, starts = _paced_service(monkeypatch, clock)
    service.library = DeadLibrary()

    service._recover_threads()  # arm the ladder
    assert starts == [1000.0]

    class DyingLibrary(DeadLibrary):
        def is_alive(self):
            return True

    clock[0] = 1001.0
    service.library = DyingLibrary()
    service._recover_threads()  # alive but stopping: no reset

    clock[0] = 1002.0
    service.library = DeadLibrary()
    service._recover_threads()  # still inside the 5 s rung
    assert starts == [1000.0]


def _signed(service, payload=None):
    """A guarded message as kofin's own plugin process sends it."""
    import json

    body = dict(payload or {})
    body[ipc.NONCE_KEY] = service._ipc_nonce
    return json.dumps([body])


def test_restart_and_auth_notifications_set_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "xbmcvfs.translatePath", lambda path: str(tmp_path / "ipc.nonce")
    )
    service = Service()
    assert service._restart_requested is False
    service.onNotification("someone.else", "Other.Restart", _signed(service))
    assert service._restart_requested is False
    service.onNotification(ipc.SENDER, "Other.Restart", _signed(service))
    assert service._restart_requested is True

    fresh = Service()
    fresh.onNotification(ipc.SENDER, "Other.AuthChanged", _signed(fresh))
    assert fresh._restart_requested is True


def test_a_forged_restart_is_dropped(monkeypatch, tmp_path):
    """Kodi passes the sender string through from whoever called NotifyAll —
    the builtin and the JSON-RPC method both — so kofin's own name proves
    nothing. Without the secret the destructive commands do not run
    (audit finding #20)."""
    monkeypatch.setattr(
        "xbmcvfs.translatePath", lambda path: str(tmp_path / "ipc.nonce")
    )
    service = Service()

    service.onNotification(ipc.SENDER, "Other.Restart", "[]")
    service.onNotification(ipc.SENDER, "Other.AuthChanged", '[{"_nonce": "guess"}]')

    assert service._restart_requested is False


def test_a_forged_library_removal_never_reaches_the_manager(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "xbmcvfs.translatePath", lambda path: str(tmp_path / "ipc.nonce")
    )
    commands = []

    class RecordingLibrary:
        startup_done = True

        def enqueue_command(self, command, data=None):
            commands.append(command)

    service = Service()
    service.library = RecordingLibrary()
    monkeypatch.setattr(Service, "_start_library", lambda self: None)

    service.onNotification(ipc.SENDER, "Other.RemoveLibrary", '[{"Id": "lib1"}]')
    assert commands == []

    service.onNotification(
        ipc.SENDER, "Other.RemoveLibrary", _signed(service, {"Id": "lib1"})
    )
    assert commands == ["RemoveLibrary"]


def test_ssl_change_triggers_restart():
    FakeAddon.store["sslVerify"] = "true"
    service = Service()
    service.settings_apply.mark_ready()  # past the startup guard
    FakeAddon.store["sslVerify"] = "false"
    service.onSettingsChanged()
    assert service._restart_requested is True


def test_settings_change_ignored_before_ready():
    """Kodi's startup settings-load fires onSettingsChanged with transient
    reads; the service must not act until the applier is ready (S2 regression:
    a plain restart once prompted a library removal)."""
    FakeAddon.store["sslVerify"] = "true"
    service = Service()  # applier not ready
    FakeAddon.store["sslVerify"] = "false"
    service.onSettingsChanged()
    assert service._restart_requested is False


# --- SyncPlay wiring (phase 4) -----------------------------------------------


class RecordingSyncPlay:
    def __init__(self):
        self.events = []
        self.stopped = False

    def on_wake(self):
        self.events.append("on_wake")

    def on_sleep(self):
        self.events.append("on_sleep")

    def on_kodi_play(self, data):
        self.events.append(("on_kodi_play", data))

    def on_notification(self, message_type, data):
        self.events.append((message_type, data))

    def stop(self):
        self.stopped = True


def test_syncplay_disabled_builds_no_manager():
    service = Service()
    service._start_syncplay()
    assert service.syncplay is None
    assert service.player.syncplay is None


def test_syncplay_enabled_builds_and_attaches():
    FakeAddon.store["syncPlayEnabled"] = "true"
    service = Service()
    service._start_syncplay()
    try:
        assert service.syncplay is not None
        assert service.player.syncplay is service.syncplay
        assert service.remote.syncplay is service.syncplay
    finally:
        service._stop_syncplay()
    assert service.syncplay is None
    assert service.player.syncplay is None
    assert service.remote.syncplay is None


def test_syncplay_toggle_applies_live():
    FakeAddon.store["syncPlayEnabled"] = "false"
    service = Service()
    service._online = True
    service.settings_apply.mark_ready()

    FakeAddon.store["syncPlayEnabled"] = "true"
    service.onSettingsChanged()
    assert service.syncplay is not None

    FakeAddon.store["syncPlayEnabled"] = "false"
    service.onSettingsChanged()
    assert service.syncplay is None


def test_syncplay_menu_ipc_runs_menu_thread(monkeypatch):
    import kofin.syncplay

    service = Service()
    manager = RecordingSyncPlay()
    service.syncplay = manager
    shown = []
    monkeypatch.setattr(kofin.syncplay, "show_menu", shown.append)

    service.onNotification(ipc.SENDER, "Other.SyncPlayMenu", "[]")

    menu_thread = service._syncplay_menu
    assert menu_thread is not None
    menu_thread.join(timeout=2)
    assert shown == [manager]


def test_syncplay_menu_without_manager_is_contained():
    service = Service()
    service.onNotification(ipc.SENDER, "Other.SyncPlayMenu", "[]")
    assert service._syncplay_menu is None


def test_who_is_watching_ipc_runs_picker_thread(monkeypatch):
    from kofin.plugin import adduser

    service = Service()
    shown = []
    monkeypatch.setattr(adduser, "show_picker", lambda api, creds: shown.append(api))

    service.onNotification(ipc.SENDER, "Other.WhoIsWatching", "[]")

    picker = service._who_is_watching
    assert picker is not None
    picker.join(timeout=2)
    assert shown == [service.api]


def test_who_is_watching_picker_failure_is_contained(monkeypatch):
    from kofin.plugin import adduser

    service = Service()

    def boom(api, creds):
        raise RuntimeError("dialog exploded")

    monkeypatch.setattr(adduser, "show_picker", boom)

    service.onNotification(ipc.SENDER, "Other.WhoIsWatching", "[]")

    picker = service._who_is_watching
    assert picker is not None
    picker.join(timeout=2)  # the thread must not take the service down


def test_wake_and_sleep_forwarded():
    service = Service()
    manager = RecordingSyncPlay()
    service.syncplay = manager

    service.onNotification("xbmc", "GUI.OnScreensaverDeactivated", "")
    service.onNotification("xbmc", "System.OnWake", "")
    service.onNotification("xbmc", "System.OnSleep", "")

    assert manager.events == ["on_wake", "on_wake", "on_sleep"]


def test_player_onplay_forwarded_decoded():
    service = Service()
    manager = RecordingSyncPlay()
    service.syncplay = manager

    service.onNotification(
        "xbmc", "Player.OnPlay", '{"item": {"id": 42, "type": "movie"}}'
    )

    assert manager.events == [("on_kodi_play", {"item": {"id": 42, "type": "movie"}})]


def test_broken_syncplay_never_suppresses_the_sync_kick():
    """Screensaver fan-out (plan §7): the library catch-up and the SyncPlay
    wake hook are independent — a broken manager can't eat the kick."""

    class ExplodingSyncPlay:
        def on_wake(self):
            raise RuntimeError("boom")

    class RecordingLibrary:
        def __init__(self):
            self.commands = []
            self.startup_done = True

        def enqueue_command(self, name, data=None):
            self.commands.append(name)

    service = Service()
    service.library = RecordingLibrary()
    service.syncplay = ExplodingSyncPlay()

    service.onNotification("xbmc", "GUI.OnScreensaverDeactivated", "")

    assert service.library.commands == ["FastSync"]


def test_shutdown_stops_syncplay():
    service = Service()
    manager = RecordingSyncPlay()
    service.syncplay = manager
    service.player.syncplay = manager  # type: ignore[assignment]
    service._shutdown()
    assert manager.stopped is True
    assert service.syncplay is None
    assert service.player.syncplay is None


# --- connection notifications ------------------------------------------------


class RecordingDialog:
    raised = []
    icons = []

    def notification(self, heading, message, icon=None, time=None, sound=None):
        RecordingDialog.raised.append((heading, message))
        RecordingDialog.icons.append(icon)


@pytest.fixture
def toasts(monkeypatch):
    import xbmcgui

    RecordingDialog.raised = []
    RecordingDialog.icons = []
    monkeypatch.setattr(xbmcgui, "Dialog", RecordingDialog)
    monkeypatch.setattr(
        "kofin.core.settings.localized",
        lambda i: {
            30415: "Connected to %s",
            30416: "Lost connection",
            30417: "Restarting",
            30418: "Shutting down",
        }[i],
    )
    return RecordingDialog.raised


def test_disconnect_is_announced(toasts):
    FakeAddon.store["notifyConnection"] = "true"
    service = Service()

    service._on_ws_disconnected()

    assert toasts == [("Kofin", "Lost connection")]


def test_connect_names_the_server(toasts, monkeypatch):
    """_on_ws_connected also waits on the monitor and registers capabilities;
    the toast is the part under test here, and it is raised on the calling
    thread — so the worker is stubbed out rather than waited for."""
    FakeAddon.store["notifyConnection"] = "true"
    FakeAddon.store["serverName"] = "minipie"
    service = Service()

    # See test_a_live_socket_raises_the_flag: an unstubbed worker outlives the
    # test and goes on talking to a server that is not there.
    monkeypatch.setattr(service, "_run_post_connect", lambda: None)

    service._on_ws_connected()

    assert toasts == [("Kofin", "Connected to minipie")]


class _NoWaitMonitor:
    def waitForAbort(self, seconds=0):
        return False

    def abortRequested(self):
        return False


def _join_post_connect(service):
    """The connect callback spawns its work (W2.5); tests that assert on the
    outcome wait for the pass to finish first."""
    assert service._post_connect is not None
    service._post_connect.join(timeout=10)
    assert not service._post_connect.is_alive()


def test_uncached_string_does_not_break_the_connect_callback(monkeypatch):
    """Kodi caches addon strings for the process lifetime, so an id added in
    this release renders without its placeholder until the next full restart.
    Formatting it then raised TypeError straight out of _on_ws_connected —
    taking capabilities registration and the SyncPlay rejoin down with a
    *notification*. Seen live, not theorised."""
    import xbmcgui

    RecordingDialog.raised = []
    monkeypatch.setattr(xbmcgui, "Dialog", RecordingDialog)
    monkeypatch.setattr("kofin.core.settings.localized", lambda i: "")

    FakeAddon.store["notifyConnection"] = "true"
    FakeAddon.store["serverName"] = "minipie"
    service = Service()

    registered = []
    monkeypatch.setattr(
        service, "_register_capabilities", lambda: registered.append(True)
    )
    monkeypatch.setattr("xbmc.Monitor", lambda: _NoWaitMonitor())

    service._on_ws_connected()
    _join_post_connect(service)

    assert registered == [True]
    assert RecordingDialog.raised == [("Kofin", "")]


def test_a_broken_dialog_does_not_break_the_connect_callback(monkeypatch):
    def explode():
        raise RuntimeError("no gui")

    monkeypatch.setattr("xbmcgui.Dialog", explode)
    monkeypatch.setattr("kofin.core.settings.localized", lambda i: "whatever")

    FakeAddon.store["notifyConnection"] = "true"
    service = Service()
    registered = []
    monkeypatch.setattr(
        service, "_register_capabilities", lambda: registered.append(True)
    )
    monkeypatch.setattr("xbmc.Monitor", lambda: _NoWaitMonitor())

    service._on_ws_connected()
    # registered is filled on the worker, so asserting straight after the
    # callback raced it — the callback's whole point is that it returns before
    # the pass runs (see test_the_connect_callback_does_not_block_on_the_pass).
    _join_post_connect(service)

    assert registered == [True]


def test_server_restart_is_announced(toasts):
    FakeAddon.store["notifyConnection"] = "true"
    service = Service()

    service._on_ws_event("ServerRestarting", {})
    service._on_ws_event("ServerShuttingDown", {})

    assert [message for _heading, message in toasts] == [
        "Restarting",
        "Shutting down",
    ]


def test_only_connecting_is_good_news(toasts, monkeypatch):
    """Connecting carries the addon icon; losing the connection and a server
    going away are adverse, and read faster with Kodi's warning glyph."""
    import xbmcgui

    FakeAddon.store["notifyConnection"] = "true"
    FakeAddon.store["serverName"] = "minipie"
    service = Service()
    # Stubbed, not waited for: every icon asserted below is raised on the
    # calling thread, and a live worker could put one of its own between two
    # of these reads of icons[-1].
    monkeypatch.setattr(service, "_run_post_connect", lambda: None)

    service._on_ws_connected()
    assert RecordingDialog.icons[-1].endswith("icon.png")

    service._on_ws_disconnected()
    assert RecordingDialog.icons[-1] == xbmcgui.NOTIFICATION_WARNING

    service._on_ws_event("ServerRestarting", {})
    assert RecordingDialog.icons[-1] == xbmcgui.NOTIFICATION_WARNING


def test_notifications_can_be_switched_off(toasts):
    FakeAddon.store["notifyConnection"] = "false"
    service = Service()

    service._on_ws_disconnected()
    service._on_ws_event("ServerRestarting", {})

    assert toasts == []


def test_lifecycle_message_does_not_reach_the_library(toasts):
    """A restart notice carries nothing the sync wants; it must not be
    mistaken for a library event."""
    FakeAddon.store["notifyConnection"] = "false"
    service = Service()

    class Boom:
        startup_done = True

        def added(self, ids):
            raise AssertionError("lifecycle message routed to the library")

        updated = removed = userdata = added

    service.library = Boom()
    service._on_ws_event("ServerRestarting", {})


# --- userdata belongs to one user --------------------------------------------


CONOR = "215f5fc3f7ff4a5581e8518b28203a4f"
COWATCHER = "c4bbf728450842f983f637ac870b1de6"


class RecordingLibrary:
    startup_done = True

    def __init__(self):
        self.applied = []

    def userdata(self, data):
        self.applied.append(data)


def _userdata_service(user_id=CONOR):
    FakeAddon.store["userId"] = user_id
    FakeAddon.store["notifyConnection"] = "false"
    service = Service()
    service.library = RecordingLibrary()
    return service


def _message(user_id, item_id="11e6dabd26a8241c9e355306a5aa52bb"):
    return {
        "UserId": user_id,
        "UserDataList": [{"ItemId": item_id, "Played": True, "PlayCount": 1}],
    }


def test_our_own_userdata_is_applied():
    service = _userdata_service()

    service._on_ws_event("UserDataChanged", _message(CONOR))

    assert service.library.applied == [_message(CONOR)["UserDataList"]]


def test_a_co_watchers_userdata_is_not_applied():
    """Who's watching? attaches a co-watcher to this device's session, and
    Jellyfin then sends this client every userdata change of theirs — from
    their phone, a browser, another Kodi box. Live regression: the local box
    (kofin-test) played Fallen Angels, and the Bravia, logged in as conor with
    kofin-test attached, wrote a 3084 s resume point and that lastplayed into
    conor's library. Conor's userdata on the server never moved."""
    service = _userdata_service(CONOR)

    service._on_ws_event("UserDataChanged", _message(COWATCHER))

    assert service.library.applied == []


def test_userdata_ownership_ignores_guid_formatting():
    """The same id arrives dashed or dashless depending on the endpoint."""
    service = _userdata_service("215F5FC3-F7FF-4A55-81E8-518B28203A4F")

    service._on_ws_event("UserDataChanged", _message(CONOR))

    assert len(service.library.applied) == 1


def test_userdata_without_a_subject_is_still_applied():
    """The field is Jellyfin's to send; dropping messages that lack it would
    silently stop userdata sync against a server that omits it."""
    service = _userdata_service()

    service._on_ws_event("UserDataChanged", {"UserDataList": [{"ItemId": "x"}]})

    assert service.library.applied == [[{"ItemId": "x"}]]


# --- reconnect catch-up ------------------------------------------------------


class CatchUpLibrary:
    def __init__(self, startup_done=True):
        self.startup_done = startup_done
        self.commands = []
        self.payloads = []

    def enqueue_command(self, name, data=None):
        self.commands.append(name)
        self.payloads.append(data)


def _connected(service, monkeypatch):
    monkeypatch.setattr(service, "_register_capabilities", lambda: None)
    monkeypatch.setattr(service, "_connection_toast", lambda *a: None)
    monkeypatch.setattr("xbmc.Monitor", lambda: _NoWaitMonitor())
    service._on_ws_connected()
    _join_post_connect(service)


def test_reconnect_catches_up_on_missed_changes(monkeypatch):
    """LibraryChanged is fire-and-forget: anything the server sends while the
    socket is down is gone, and the socket reconnects silently. Seen on the
    Piers box — a re-encoded film was announced at 14:38:45 while that client
    was disconnected, the socket returned at 14:39:51, and the film stayed
    missing while a client with a live socket applied it in seconds."""
    service = Service()
    service.library = CatchUpLibrary()

    _connected(service, monkeypatch)

    assert service.library.commands == ["FastSync"]


def test_reconnect_catch_up_is_stamped_with_the_edge_it_protects(monkeypatch):
    """Everything the socket missed predates the reconnect, and the post-
    connect worker queues the catch-up seconds later — behind the settle,
    capabilities, the who's-watching restore and the userdata replay. Left to
    enqueue_command's own stamp, a just-rebuilt manager's startup pass (which
    lands inside that gap) looked too old to count and the same change-feed
    window was fetched twice ~2 s apart."""
    import time

    from kofin.sync.library import FAST_SYNC_REQUESTED_AT

    service = Service()
    service.library = CatchUpLibrary()

    before = time.monotonic()
    _connected(service, monkeypatch)

    (payload,) = service.library.payloads
    assert payload[FAST_SYNC_REQUESTED_AT] == service._ws_connected_at
    assert before <= payload[FAST_SYNC_REQUESTED_AT] <= time.monotonic()


# --- Who's watching? restore on session attach -------------------------------


class RecordingSessionApi:
    def __init__(self, additional=None):
        self.user_id = "primary"
        self.additional = list(additional or [])
        self.added = []

    def device_sessions(self, device_id):
        return [
            {
                "Id": "sess1",
                "AdditionalUsers": [{"UserId": uid} for uid in self.additional],
            }
        ]

    def session_add_user(self, session_id, user_id):
        self.added.append(user_id)
        self.additional.append(user_id)


def test_ws_connect_restores_who_is_watching(monkeypatch):
    """A fresh session after restart has empty AdditionalUsers; the service
    re-attaches the set the picker saved, after capabilities attach the
    session."""
    FakeAddon.store["whoIsWatching"] = "u2,u4"
    api = RecordingSessionApi()
    service = Service()
    service.api = api
    service.credentials.device_id = "dev1"
    order = []

    monkeypatch.setattr(
        service, "_register_capabilities", lambda: order.append("capabilities")
    )
    monkeypatch.setattr(service, "_connection_toast", lambda *a: None)
    monkeypatch.setattr(
        service, "_catch_up_after_reconnect", lambda: order.append("catchup")
    )
    monkeypatch.setattr("xbmc.Monitor", lambda: _NoWaitMonitor())

    service._on_ws_connected()
    _join_post_connect(service)

    assert order == ["capabilities", "catchup"]
    assert api.added == ["u2", "u4"]


def test_ws_connect_skips_restore_when_nobody_saved(monkeypatch):
    FakeAddon.store.pop("whoIsWatching", None)
    api = RecordingSessionApi()
    service = Service()
    service.api = api
    service.credentials.device_id = "dev1"
    _connected(service, monkeypatch)
    assert api.added == []


def test_ws_connect_survives_a_broken_restore(monkeypatch):
    """Restore is best-effort: an unexpected failure must not block catch-up."""
    FakeAddon.store["whoIsWatching"] = "u2"
    service = Service()
    caught_up = []

    class BoomApi:
        user_id = "primary"

        def device_sessions(self, device_id):
            raise RuntimeError("session lookup exploded")

    service.api = BoomApi()
    service.credentials.device_id = "dev1"
    monkeypatch.setattr(service, "_register_capabilities", lambda: None)
    monkeypatch.setattr(service, "_connection_toast", lambda *a: None)
    monkeypatch.setattr(
        service, "_catch_up_after_reconnect", lambda: caught_up.append(True)
    )
    monkeypatch.setattr("xbmc.Monitor", lambda: _NoWaitMonitor())

    service._on_ws_connected()
    _join_post_connect(service)

    assert caught_up == [True]


def test_first_connect_does_not_double_up_with_startup(monkeypatch):
    """startup() runs the same catch-up a moment later; the library reports
    itself unfinished until then."""
    service = Service()
    service.library = CatchUpLibrary(startup_done=False)

    _connected(service, monkeypatch)

    assert service.library.commands == []


def test_reconnect_without_a_library_is_harmless(monkeypatch):
    service = Service()
    service.library = None

    _connected(service, monkeypatch)  # must not raise


def test_library_update_reaches_the_userdata_watcher():
    service = Service()
    service.credentials.is_logged_in = True
    submitted = []
    service.kodi_userdata.submit = submitted.append  # type: ignore[method-assign]

    service.onNotification(
        "xbmc",
        "VideoLibrary.OnUpdate",
        '{"item": {"id": 5910, "type": "episode"}, "playcount": 1}',
    )

    assert submitted == [{"item": {"id": 5910, "type": "episode"}, "playcount": 1}]


def test_library_update_ignored_while_logged_out():
    service = Service()
    service.credentials.is_logged_in = False
    submitted = []
    service.kodi_userdata.submit = submitted.append  # type: ignore[method-assign]

    service.onNotification(
        "xbmc", "VideoLibrary.OnUpdate", '{"id": 5910, "type": "episode"}'
    )

    assert submitted == []


# --- websocket post-connect worker (perf plan W2.5, audit finding #5) --------


def test_on_ws_connected_returns_while_the_slow_work_runs(monkeypatch):
    """on_open is invoked from the websocket's own receive loop: for as long
    as it runs, nothing is read off the socket. The callback must spawn the
    post-connect work and return."""
    import threading

    service = Service()
    monkeypatch.setattr(service, "_connection_toast", lambda *a, **k: None)
    started = threading.Event()
    release = threading.Event()

    def slow_pass():
        started.set()
        release.wait(5)

    monkeypatch.setattr(service, "_run_post_connect", slow_pass)

    service._on_ws_connected()  # must not block on the pass

    assert started.wait(5)
    assert service._post_connect is not None and service._post_connect.is_alive()
    release.set()
    service._post_connect.join(5)
    assert not service._post_connect.is_alive()


def test_post_connect_reruns_once_for_a_connect_landing_mid_pass(monkeypatch):
    """A reconnect during the pass sets the pending flag; the worker runs one
    more full pass so the new session still gets capabilities and restore —
    neither skipped nor a second thread."""
    service = Service()
    service.syncplay = None
    calls = []
    state = {"reconnected": False}

    monkeypatch.setattr(service, "_register_capabilities", lambda: calls.append("caps"))
    monkeypatch.setattr(
        service, "_restore_additional_users", lambda: calls.append("restore")
    )

    def catch_up():
        calls.append("catchup")
        if not state["reconnected"]:
            state["reconnected"] = True
            service._post_connect_pending.set()

    monkeypatch.setattr(service, "_catch_up_after_reconnect", catch_up)
    monkeypatch.setattr("xbmc.Monitor", lambda: _NoWaitMonitor())

    service._post_connect_pending.set()
    service._run_post_connect()

    assert calls == ["caps", "restore", "catchup"] * 2


# --- teardown must end, and must not lie about it ----------------------------


class StuckLibrary:
    """A library thread that will not stop, which is the case that wedged
    Kodi: a service script that never returns leaves every later Python
    invocation hanging."""

    ident = None  # a real Thread carries one; the dump follows it

    def __init__(self):
        self.stopped = False

    def stop_client(self):
        self.stopped = True

    def is_alive(self):
        return True

    def join(self, timeout=None):
        return None


def test_the_teardown_gives_up_on_a_stuck_library(monkeypatch):
    """Waiting on abortRequested alone never ends on an addon bounce — that
    flag means Kodi is shutting down. The wait has to have a deadline."""
    import kofin.service.main as main_module

    monkeypatch.setattr(main_module, "LIBRARY_JOIN_SECONDS", 10.0)
    service = Service()
    service.library = StuckLibrary()
    monkeypatch.setattr(service, "abortRequested", lambda: False)

    assert service._join_library() is False  # reported, not pretended


def test_a_stuck_library_keeps_the_sync_stop_flag_raised(monkeypatch):
    """The flag is what every worker's @stop guard reads. Clearing it while
    the thread lives un-pauses it against an already-rebuilt service (audit
    finding #10); the deadline must not reintroduce that."""
    import kofin.service.main as main_module

    monkeypatch.setattr(main_module, "LIBRARY_JOIN_SECONDS", 0.0)
    service = Service()
    service.library = StuckLibrary()
    monkeypatch.setattr(service, "abortRequested", lambda: False)
    monkeypatch.setattr(service, "_stop_syncplay", lambda: None)
    monkeypatch.setattr(service, "_join_workers", lambda: None)

    service._shutdown()

    assert state.should_stop() is True


def test_a_stuck_library_is_dumped_not_merely_reported(monkeypatch):
    """The deadline makes a stuck thread survivable without saying what stuck
    it, and the event is too rare to reproduce on demand. Both ends of the
    wait are written down so the log answers it after the fact."""
    import kofin.service.main as main_module

    dumps = []
    monkeypatch.setattr(main_module, "LIBRARY_JOIN_SECONDS", 10.0)
    monkeypatch.setattr(
        main_module.diag, "thread_dump", lambda reason: dumps.append(reason) or {}
    )
    service = Service()
    service.library = StuckLibrary()
    monkeypatch.setattr(service, "abortRequested", lambda: False)

    assert service._join_library() is False
    assert len(dumps) == 2  # the first slow tick, and the deadline
    assert "5s" in dumps[0] and "deadline" in dumps[1]


def test_a_library_that_stops_in_time_is_never_dumped(monkeypatch):
    """sys._current_frames over every thread is not free, and a teardown that
    works is not an anomaly worth logging."""
    import kofin.service.main as main_module

    dumps = []
    monkeypatch.setattr(
        main_module.diag, "thread_dump", lambda reason: dumps.append(reason) or {}
    )

    class SlowButFinishing(StuckLibrary):
        alive = True

        def join(self, timeout=None):
            self.alive = False

        def is_alive(self):
            return self.alive

    service = Service()
    service.library = SlowButFinishing()

    assert service._join_library() is True
    assert dumps == []


class FinishedLibrary(StuckLibrary):
    def is_alive(self):
        return False


def _teardown_service(monkeypatch, tmp_path):
    """A service whose teardown reaches the shared-state writes."""
    monkeypatch.setattr(
        "xbmcvfs.translatePath", lambda path: str(tmp_path / "ipc.nonce")
    )
    service = Service()
    service.library = FinishedLibrary()
    monkeypatch.setattr(service, "_stop_syncplay", lambda: None)
    monkeypatch.setattr(service, "_join_workers", lambda: None)
    return service


def test_a_teardown_clears_the_state_it_still_owns(monkeypatch, tmp_path):
    """The control for the superseded case below: a teardown that is still the
    current generation clears the lot, as it always did."""
    service = _teardown_service(monkeypatch, tmp_path)
    state.set_online(True)

    service._shutdown()

    assert state.is_online() is False
    assert state.should_stop() is False


def test_a_superseded_teardown_leaves_the_live_generation_its_state(
    monkeypatch, tmp_path
):
    """Kodi starts the replacement service before the old one has exited, and
    core.state is one set of window properties with no room for a generation.

    Measured on a Piers box across an 0.14.0 -> 0.15.0 update: the successor
    connected at 18:31:17 and raised PROP_ONLINE, the predecessor's teardown
    cleared it at 18:31:27, and nothing ever put it back — _connect is the
    only other publisher and it runs on the offline->online edge alone. Every
    library thread then died at its first @stop guard (357 rebuilds in 90
    minutes) and both the SyncPlay menu and the who's-watching picker refused,
    because both routes gate on state.is_online().
    """
    service = _teardown_service(monkeypatch, tmp_path)
    # What the successor's Service.__init__ does: mint its own nonce, connect,
    # publish. Everything below is now *its* state, not this service's.
    successor_nonce = ipc.rotate_nonce()
    assert successor_nonce != service._ipc_nonce
    state.set_online(True)

    service._shutdown()

    assert state.is_online() is True  # the successor's flag, left alone
    assert state.should_stop() is False  # nor is its sync paused


def test_a_superseded_teardown_still_stops_its_own_threads(monkeypatch, tmp_path):
    """Skipping the shared properties must not become skipping the teardown:
    what actually ends this generation's threads is instance-scoped — the
    _stopping Event its transports read, and Library.stop_client."""
    service = _teardown_service(monkeypatch, tmp_path)
    library = service.library
    ipc.rotate_nonce()

    service._shutdown()

    assert service._stopping.is_set() is True
    assert library.stopped is True  # the instance flag that ends the thread
    assert service.library is None


def test_a_clean_teardown_clears_everything(monkeypatch):
    service = Service()
    service.library = FinishedLibrary()
    monkeypatch.setattr(service, "_stop_syncplay", lambda: None)
    monkeypatch.setattr(service, "_join_workers", lambda: None)

    service._shutdown()

    assert state.should_stop() is False


def test_a_new_generation_never_inherits_the_stop_flag(monkeypatch):
    """A teardown that could not join its library thread leaves PROP_SYNC_STOP
    raised on purpose, and nothing else ever lowers it. Measured on Omega:
    one stuck teardown then disabled syncing until Kodi restarted — every
    later library thread exited at its first @stop guard, on one warning."""
    import kofin.service.main as main_module

    state.set_should_stop(True)
    seen = []

    class OneShot(main_module.Service):
        def __init__(self):
            seen.append(state.should_stop())

        def run(self):
            return False

    monkeypatch.setattr(main_module, "Service", OneShot)
    main_module.run_forever()

    assert seen == [False]  # the flag was down before the generation was built


def test_the_transports_abort_survives_the_next_generation(monkeypatch):
    """Measured: with the abort reading PROP_SYNC_STOP, the replacement
    service lowered the flag on its way up and the thread orphaned by the
    previous teardown went straight back to riding the full retry ladder —
    125s of a thread that had been told to stop. The signal has to belong to
    the generation that raised it."""
    service = Service()
    abort = service.http._abort
    assert abort is not None and abort() is False

    service._stopping.set()
    assert abort() is True

    state.set_should_stop(False)  # what the next generation does on the way up
    assert abort() is True

    # the per-worker sessions the download pool and writers use, likewise
    assert service._new_api()._http._abort() is True


def test_the_transports_abort_hears_kodis_script_stop():
    """abortRequested() is the stop signal _stopping cannot cover: Kodi
    raises it and waits — an addon bounce, a profile switch, Kodi exiting —
    while _shutdown has not run yet, so the Event is still down. A ladder
    consulting only the Event rode out the full budget, blew Kodi's
    five-second stop grace on a profile switch, and left the profile with no
    kofin service and a dead webserver (measured 2026-08-08)."""
    service = Service()
    assert service.http._abort() is False

    service.abortRequested = lambda: True  # type: ignore[method-assign]

    assert service._stopping.is_set() is False
    assert service.http._abort() is True
    assert service._new_api()._http._abort() is True


def test_connect_probes_on_the_probe_budget():
    """_connect runs on the service loop — the thread whose 1 s tick is also
    what notices stop requests — so its reachability check must be the
    single-attempt probe, never a default-budget call that holds the loop
    for the transport's full ladder (~29 s per offline probe, measured)."""
    from kofin.core.http import JellyfinError

    service = Service()
    calls = []

    class ProbeOnlyApi:
        def probe_info(self):
            calls.append(1)
            raise JellyfinError("down")

    service.api = ProbeOnlyApi()  # type: ignore[assignment]
    service._connect()

    assert calls == [1]
    assert service._online is False


class FakeDownloadManager:
    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.submitted = []
        self.origins = []
        self.media_types = []
        self.cancelled = []
        self.removed = []

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def submit(self, ids, origin="user", media_types=None):
        self.submitted.append(list(ids))
        self.origins.append(origin)
        self.media_types.append(list(media_types or []))

    def cancel(self, item_id):
        self.cancelled.append(item_id)

    def remove(self, item_id):
        self.removed.append(item_id)


def test_download_manager_builds_only_when_enabled(monkeypatch):
    built = []

    class Recorder(FakeDownloadManager):
        def __init__(self, api_factory, refresh, stopping):
            super().__init__()
            built.append(self)

    monkeypatch.setattr("kofin.downloads.manager.DownloadManager", Recorder)

    service = Service()
    service._start_downloads()
    assert built == [] and service.downloads is None  # disabled: nothing built

    FakeAddon.store["downloadsEnabled"] = "true"
    service._start_downloads()
    assert len(built) == 1 and built[0].started == 1


def test_download_ipc_routes_to_the_manager_and_forgeries_do_not(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "xbmcvfs.translatePath", lambda path: str(tmp_path / "ipc.nonce")
    )
    FakeAddon.store["downloadsEnabled"] = "true"
    service = Service()
    manager = FakeDownloadManager()
    service.downloads = manager

    service.onNotification(
        ipc.SENDER, "Other.DownloadAdd", _signed(service, {"Ids": ["a", "b"]})
    )
    service.onNotification(
        ipc.SENDER, "Other.DownloadCancel", _signed(service, {"Id": "c"})
    )
    service.onNotification(
        ipc.SENDER, "Other.DownloadRemove", _signed(service, {"Id": "d"})
    )
    assert manager.submitted == [["a", "b"]]
    assert manager.origins == ["user"]  # absent Origin is a user download
    assert manager.cancelled == ["c"] and manager.removed == ["d"]

    service.onNotification(
        ipc.SENDER,
        "Other.DownloadAdd",
        _signed(service, {"Ids": ["e"], "Origin": "auto:s1"}),
    )
    service.onNotification(
        ipc.SENDER,
        "Other.DownloadAdd",
        _signed(service, {"Ids": ["f"], "Origin": "junk"}),
    )
    assert manager.origins[-2:] == ["auto:s1", "user"]  # junk coerces to user

    import json

    forged = json.dumps([{"Ids": ["evil"]}])  # kofin's name, no secret
    service.onNotification(ipc.SENDER, "Other.DownloadAdd", forged)
    assert manager.submitted == [["a", "b"], ["e"], ["f"]]


def test_download_add_pairs_types_with_ids_and_survives_gaps(monkeypatch, tmp_path):
    """The Types list rides along positionally; a blank id must not shift it."""
    monkeypatch.setattr(
        "xbmcvfs.translatePath", lambda path: str(tmp_path / "ipc.nonce")
    )
    FakeAddon.store["downloadsEnabled"] = "true"
    service = Service()
    manager = FakeDownloadManager()
    service.downloads = manager

    service.onNotification(
        ipc.SENDER,
        "Other.DownloadAdd",
        _signed(
            service,
            {"Ids": ["a", "", "c"], "Types": ["Movie", "Episode", "Audio"]},
        ),
    )
    assert manager.submitted == [["a", "c"]]
    # "c" keeps Audio, not Episode: the pairing happens before the blank is
    # dropped, so the survivors carry their own types.
    assert manager.media_types == [["Movie", "Audio"]]

    # A short list leaves the rest unknown rather than mislabelling them.
    service.onNotification(
        ipc.SENDER,
        "Other.DownloadAdd",
        _signed(service, {"Ids": ["d", "e"], "Types": ["Movie"]}),
    )
    assert manager.media_types[-1] == ["Movie", ""]


def test_shutdown_stops_the_download_manager(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "xbmcvfs.translatePath", lambda path: str(tmp_path / "ipc.nonce")
    )
    service = Service()
    manager = FakeDownloadManager()
    service.downloads = manager

    service._shutdown()

    assert manager.stopped == 1 and service.downloads is None


# --- honest online flag and offline replay (plan W2.1 / W2.4) ---------------


def test_a_dropped_socket_asks_a_question_not_a_verdict(monkeypatch, tmp_path):
    """The flag gates real behaviour once honest — sync.shims.stop raises
    out of an in-flight writer — so a blinking socket must not abandon a
    running sync. The probe decides."""
    monkeypatch.setattr(
        "xbmcvfs.translatePath", lambda path: str(tmp_path / "ipc.nonce")
    )
    service = Service()
    service._online = True
    state.set_online(True)

    class AliveApi:
        def probe_info(self):
            return {"ServerName": "still here"}

    service.api = AliveApi()
    service._on_ws_disconnected()
    assert service._verify_online is True

    service._tick()
    assert service._online is True and state.is_online() is True


def test_a_confirmed_outage_lowers_the_flag(monkeypatch, tmp_path):
    from kofin.core.http import ServerUnreachable

    monkeypatch.setattr(
        "xbmcvfs.translatePath", lambda path: str(tmp_path / "ipc.nonce")
    )
    service = Service()
    service._online = True
    state.set_online(True)

    class DeadApi:
        def probe_info(self):
            raise ServerUnreachable("gone")

    service.api = DeadApi()
    service._on_ws_disconnected()
    service._verify_connection()

    assert service._online is False and state.is_online() is False


def test_a_live_socket_raises_the_flag(monkeypatch, tmp_path):
    """The websocket reconnects itself, so its open is a raising edge in its
    own right — otherwise the flag would wait for the connect loop."""
    monkeypatch.setattr(
        "xbmcvfs.translatePath", lambda path: str(tmp_path / "ipc.nonce")
    )
    service = Service()
    state.set_online(False)
    monkeypatch.setattr(service, "_connection_toast", lambda *a, **k: None)
    # The callback also spawns the post-connect worker, which would go on to
    # talk to a server that is not there for the rest of the session.
    monkeypatch.setattr(service, "_run_post_connect", lambda: None)

    service._on_ws_connected()

    assert service._online is True and state.is_online() is True


def test_a_lowered_flag_is_raised_again_under_a_live_connection(monkeypatch, tmp_path):
    """The other half of the split brain above: _connect publishes the flag on
    the offline->online edge only, so once something else clears it while this
    generation stays online, nothing here ever writes it again. The tick
    re-asserts it instead — which is what heals a box whose clobberer is an
    *older* build's teardown, unreachable by any fix in that file.
    """
    monkeypatch.setattr(
        "xbmcvfs.translatePath", lambda path: str(tmp_path / "ipc.nonce")
    )
    service = Service()
    service._online = True
    state.set_online(True)
    monkeypatch.setattr(service, "_recover_threads", lambda: None)
    # Exactly what a superseded generation's state.clear_all() leaves behind:
    # absent, which reads as offline everywhere.
    FakeWindow.store.clear()
    assert state.is_online() is False

    service._tick()

    assert state.is_online() is True


def test_an_already_raised_flag_is_not_rewritten(monkeypatch, tmp_path):
    """The tick runs once a second for the life of the service, so the heal is
    conditional: an unconditional write would log a repair on every tick and
    bury the one line that says the flag was actually clobbered."""
    monkeypatch.setattr(
        "xbmcvfs.translatePath", lambda path: str(tmp_path / "ipc.nonce")
    )
    service = Service()
    service._online = True
    state.set_online(True)
    monkeypatch.setattr(service, "_recover_threads", lambda: None)
    writes = []
    monkeypatch.setattr(state, "set_online", lambda online: writes.append(online))

    service._tick()

    assert writes == []


def test_reconnecting_does_not_build_a_second_websocket(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "xbmcvfs.translatePath", lambda path: str(tmp_path / "ipc.nonce")
    )
    service = Service()
    built = []
    monkeypatch.setattr(service, "_start_websocket", lambda: built.append(1))
    monkeypatch.setattr(service, "_start_syncplay", lambda: None)
    monkeypatch.setattr(service, "_start_library", lambda: None)
    monkeypatch.setattr(service, "_start_downloads", lambda: None)
    monkeypatch.setattr(service, "_start_backdrop", lambda force=False: None)
    monkeypatch.setattr(service.artcache, "start", lambda: None)
    service.api = type("A", (), {"probe_info": lambda self: {}})()

    service._connect()
    service._go_offline()
    service.ws = _FakeWs(alive=True)  # the previous client is still retrying
    service._connect()

    assert built == [1]


def test_reconnecting_rebuilds_a_websocket_whose_thread_died(monkeypatch, tmp_path):
    """The companion case, and the one that broke: a client whose thread has
    ended still fills the slot, so the reconnect read it as "one is already
    running" and came back with no websocket at all."""
    monkeypatch.setattr(
        "xbmcvfs.translatePath", lambda path: str(tmp_path / "ipc.nonce")
    )
    service = Service()
    built = []
    monkeypatch.setattr(service, "_start_websocket", lambda: built.append(1))
    monkeypatch.setattr(service, "_start_syncplay", lambda: None)
    monkeypatch.setattr(service, "_start_library", lambda: None)
    monkeypatch.setattr(service, "_start_downloads", lambda: None)
    monkeypatch.setattr(service, "_start_backdrop", lambda force=False: None)
    monkeypatch.setattr(service.artcache, "start", lambda: None)
    service.api = type("A", (), {"probe_info": lambda self: {}})()

    service._connect()
    service._go_offline()
    dead = _FakeWs(alive=False)
    service.ws = dead
    service._connect()

    assert built == [1, 1]
    assert dead.stopped == 1


def test_replay_sends_the_resolved_payload_before_the_catch_up(monkeypatch, tmp_path):
    monkeypatch.setattr("xbmcvfs.translatePath", lambda path: str(tmp_path))
    from kofin.downloads import pending
    from kofin.sync import db as sync_db

    sync_db.reset_overrides()
    sync_db.set_path_override("kofin", str(tmp_path / "kofin.db"))
    try:
        pending.enqueue("i1", "episode", played=True, snapshot={"LastPlayedDate": "T1"})
        service = Service()
        sent = []

        class ReplayApi:
            def item(self, item_id):
                return {"UserData": {"LastPlayedDate": "T1"}}

            def update_user_data(self, item_id, payload):
                sent.append((item_id, payload))

        service.api = ReplayApi()
        service._replay_pending_userdata()

        assert sent == [("i1", {"Played": True, "PlaybackPositionTicks": 0})]
        assert pending.rows() == []  # settled, not replayed forever
    finally:
        sync_db.reset_overrides()


def test_a_failed_replay_keeps_the_row_for_next_time(monkeypatch, tmp_path):
    monkeypatch.setattr("xbmcvfs.translatePath", lambda path: str(tmp_path))
    from kofin.core.http import ServerUnreachable
    from kofin.downloads import pending
    from kofin.sync import db as sync_db

    sync_db.reset_overrides()
    sync_db.set_path_override("kofin", str(tmp_path / "kofin.db"))
    try:
        pending.enqueue("i1", played=True)
        service = Service()

        class DeadApi:
            def item(self, item_id):
                raise ServerUnreachable("gone")

        service.api = DeadApi()
        service._replay_pending_userdata()

        (row,) = pending.rows()
        assert row.attempts == 1
    finally:
        sync_db.reset_overrides()


def test_a_cold_boot_away_from_the_server_states_the_outage(monkeypatch, tmp_path):
    """The common case is a device booted away from home: nothing was ever
    online, so _go_offline never runs and the flag would stay absent —
    which every user-facing refusal reads as "not known yet" and answers by
    waiting for the transport (found live)."""
    from kofin.core.http import ServerUnreachable

    monkeypatch.setattr(
        "xbmcvfs.translatePath", lambda path: str(tmp_path / "ipc.nonce")
    )
    service = Service()
    service.api = type(
        "Dead",
        (),
        {
            "probe_info": lambda self: (_ for _ in ()).throw(
                ServerUnreachable("no route")
            )
        },
    )()

    service._connect()

    assert state.is_offline() is True
    assert state.is_online() is False


# --- rebuilding threads that died on their own -------------------------------


class _FakeLibrary:
    # A live fake reads as a *healthy* manager — past startup, not stopping —
    # which is the shape _recover_threads consults when resetting its pacing.
    startup_done = True
    stop_thread = False

    def __init__(self, alive=False, workers=False):
        self._alive = alive
        self._workers = workers
        self.stopped = 0

    def is_alive(self):
        return self._alive

    def stop_client(self):
        self.stopped += 1

    def workers_alive(self):
        return self._workers


class _FakeWs:
    def __init__(self, alive=False):
        self._alive = alive
        self.stopped = 0

    def is_alive(self):
        return self._alive

    def stop(self):
        self.stopped += 1


def test_a_live_library_is_left_alone():
    service = Service()
    live = _FakeLibrary(alive=True)
    service.library = live

    service._reap_library()

    assert service.library is live
    assert live.stopped == 0


def test_a_dead_library_slot_is_cleared_for_a_rebuild():
    """The slot outlives the thread. Guarding the restart on the slot rather
    than the thread is what left a reconnect with no sync manager at all —
    the manager exits itself on any LibraryException, most routinely the
    offline one."""
    service = Service()
    service.library = _FakeLibrary(alive=False)

    service._reap_library()

    assert service.library is None


def test_a_dead_library_with_workers_in_flight_is_not_rebuilt_yet():
    """A Library owns its own database_lock, so a second graph built over one
    that still has writers running puts two independent locks in front of the
    same SQLite files. The slot stays until the workers are done."""
    service = Service()
    corpse = _FakeLibrary(alive=False, workers=True)
    service.library = corpse

    service._reap_library()

    assert service.library is corpse
    assert corpse.stopped == 1  # told to stop, just not replaced yet


def test_a_dead_websocket_is_stopped_before_the_slot_is_cleared():
    """The thread can die with the socket still open — an upstream raise out
    of run_forever does exactly that — and the client object is the only
    remaining handle on the descriptor."""
    service = Service()
    client = _FakeWs(alive=False)
    service.ws = client

    service._reap_websocket()

    assert service.ws is None
    assert client.stopped == 1


def test_a_live_websocket_is_left_alone():
    service = Service()
    live = _FakeWs(alive=True)
    service.ws = live

    service._reap_websocket()

    assert service.ws is live
    assert live.stopped == 0


def test_threads_that_die_while_online_are_rebuilt(monkeypatch):
    """Nothing else looks: _connect is the only other rebuild path and it runs
    only on the offline→online edge, so a thread that dies while the server
    stays reachable stays dead until Kodi restarts."""
    service = Service()
    service._online = True
    service.ws = _FakeWs(alive=False)
    service.library = _FakeLibrary(alive=False)

    built = []
    monkeypatch.setattr(service, "_start_websocket", lambda: built.append("ws"))
    monkeypatch.setattr(service, "_start_library", lambda: built.append("library"))

    service._recover_threads()

    assert built == ["ws", "library"]
    assert service.ws is None  # reaped; the fake _start_websocket fills no slot


def test_recovery_does_nothing_while_both_threads_live(monkeypatch):
    service = Service()
    service._online = True
    service.ws = _FakeWs(alive=True)
    service.library = _FakeLibrary(alive=True)

    built = []
    monkeypatch.setattr(service, "_start_websocket", lambda: built.append("ws"))
    monkeypatch.setattr(service, "_start_library", lambda: built.append("library"))

    service._recover_threads()

    assert built == []
