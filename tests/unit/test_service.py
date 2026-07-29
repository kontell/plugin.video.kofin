import pytest

from kofin.core import ipc
from kofin.service.main import Backoff, Service
from tests.unit.fakes import FakeAddon, FakeWindow


@pytest.fixture(autouse=True)
def kodi_fakes(monkeypatch):
    FakeAddon.store = {}
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)


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


def test_restart_and_auth_notifications_set_flag():
    service = Service()
    assert service._restart_requested is False
    service.onNotification("someone.else", "Other.Restart", "[]")
    assert service._restart_requested is False
    service.onNotification(ipc.SENDER, "Other.Restart", "[]")
    assert service._restart_requested is True

    fresh = Service()
    fresh.onNotification(ipc.SENDER, "Other.AuthChanged", "[]")
    assert fresh._restart_requested is True


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


def test_pick_audio_track_ipc_runs_on_worker(monkeypatch):
    service = Service()
    called = []
    monkeypatch.setattr(
        service.player, "pick_audio_track", lambda: called.append(True) or True
    )

    service.onNotification(ipc.SENDER, "Other.PickAudioTrack", "[]")

    thread = service._pick_audio_thread
    assert thread is not None
    thread.join(timeout=2)
    assert called == [True]


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

    FakeAddon.store["dbSyncScreensaver"] = "true"
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
    the toast is the part under test here."""
    FakeAddon.store["notifyConnection"] = "true"
    FakeAddon.store["serverName"] = "minipie"
    service = Service()

    monkeypatch.setattr(service, "_register_capabilities", lambda: None)
    monkeypatch.setattr("xbmc.Monitor", lambda: _NoWaitMonitor())

    service._on_ws_connected()

    assert toasts == [("Kofin", "Connected to minipie")]


class _NoWaitMonitor:
    def waitForAbort(self, seconds=0):
        return False


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
    monkeypatch.setattr(service, "_register_capabilities", lambda: None)
    monkeypatch.setattr("xbmc.Monitor", lambda: _NoWaitMonitor())

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


# --- reconnect catch-up ------------------------------------------------------


class CatchUpLibrary:
    def __init__(self, startup_done=True):
        self.startup_done = startup_done
        self.commands = []

    def enqueue_command(self, name, data=None):
        self.commands.append(name)


def _connected(service, monkeypatch):
    monkeypatch.setattr(service, "_register_capabilities", lambda: None)
    monkeypatch.setattr(service, "_connection_toast", lambda *a: None)
    monkeypatch.setattr("xbmc.Monitor", lambda: _NoWaitMonitor())
    service._on_ws_connected()


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
