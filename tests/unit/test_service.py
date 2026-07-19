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
