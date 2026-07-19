"""The mode=syncplay plugin entry: root-entry gating and the menu IPC."""

import pytest

from kofin.core import ipc
from kofin.plugin import syncplay
from kofin.plugin.router import Request
from tests.unit.fakes import FakeAddon, FakeWindow


@pytest.fixture(autouse=True)
def kodi_fakes(monkeypatch):
    FakeAddon.store = {}
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    monkeypatch.setattr("xbmcvfs.exists", lambda path: False)


@pytest.fixture
def builtins(monkeypatch):
    calls = []
    monkeypatch.setattr("xbmc.executebuiltin", lambda c: calls.append(c))
    return calls


def request():
    return Request("plugin://plugin.video.kofin/", -1, {"mode": "syncplay"})


class TestAvailability:
    def test_hidden_until_enabled(self):
        assert syncplay.available() is False
        FakeAddon.store["syncPlayEnabled"] = "true"
        assert syncplay.available() is True

    def test_hidden_with_external_player(self, monkeypatch):
        FakeAddon.store["syncPlayEnabled"] = "true"
        monkeypatch.setattr(
            "xbmcvfs.exists",
            lambda path: path == "special://profile/playercorefactory.xml",
        )
        assert syncplay.available() is False

    def test_masterprofile_override_also_hides(self, monkeypatch):
        FakeAddon.store["syncPlayEnabled"] = "true"
        monkeypatch.setattr(
            "xbmcvfs.exists",
            lambda path: path == "special://masterprofile/playercorefactory.xml",
        )
        assert syncplay.available() is False


class TestMenu:
    def test_menu_sends_the_ipc_message(self, builtins):
        FakeAddon.store["syncPlayEnabled"] = "true"
        FakeWindow.store["kofin.online"] = "true"

        syncplay.menu(request())

        assert len(builtins) == 1
        assert builtins[0].startswith("NotifyAll(%s, %s" % (ipc.SENDER, "SyncPlayMenu"))

    def test_menu_disabled_sends_nothing(self, builtins):
        syncplay.menu(request())
        assert builtins == []

    def test_menu_offline_sends_nothing(self, builtins):
        FakeAddon.store["syncPlayEnabled"] = "true"

        syncplay.menu(request())

        assert builtins == []

    def test_menu_external_player_refuses(self, builtins, monkeypatch):
        FakeAddon.store["syncPlayEnabled"] = "true"
        FakeWindow.store["kofin.online"] = "true"
        monkeypatch.setattr("xbmcvfs.exists", lambda path: True)

        syncplay.menu(request())

        assert builtins == []
