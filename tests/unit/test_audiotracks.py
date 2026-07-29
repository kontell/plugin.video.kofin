"""mode=audiotracks plugin entry (PR5 TC audio picker hand-off)."""

import pytest

from kofin.core import ipc, state
from kofin.plugin import audiotracks
from kofin.plugin.router import Request
from tests.unit.fakes import FakeAddon, FakeWindow


@pytest.fixture(autouse=True)
def kodi_fakes(monkeypatch):
    FakeAddon.store = {}
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)


@pytest.fixture
def builtins(monkeypatch):
    calls = []
    monkeypatch.setattr("xbmc.executebuiltin", lambda c: calls.append(c))
    return calls


def request():
    return Request("plugin://plugin.video.kofin/", -1, {"mode": "audiotracks"})


def test_pick_sends_ipc_when_eligible(builtins):
    state.set_playing_id("m1")
    state.set_playing_pick_audio(True)

    audiotracks.pick(request())

    assert len(builtins) == 1
    assert builtins[0].startswith(
        "NotifyAll(%s, %s" % (ipc.SENDER, ipc.PICK_AUDIO_TRACK)
    )


def test_pick_skips_when_not_playing(builtins):
    audiotracks.pick(request())
    assert builtins == []


def test_pick_skips_when_prop_off(builtins):
    state.set_playing_id("m1")
    state.set_playing_pick_audio(False)
    audiotracks.pick(request())
    assert builtins == []


def test_pick_skips_when_syncplay(builtins):
    state.set_playing_id("m1")
    state.set_playing_pick_audio(True)
    state.set_syncplay_active(True)
    audiotracks.pick(request())
    assert builtins == []
