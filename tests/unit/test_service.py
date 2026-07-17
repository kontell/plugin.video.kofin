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
