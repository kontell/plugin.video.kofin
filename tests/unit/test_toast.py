"""L1 units for the toast helper: which icon a level resolves to, and that a
toast can never take its caller down with it."""

import os

import pytest

import xbmcgui

from kofin.core import toast
from tests.unit.fakes import FakeAddon


class RecordingDialog:
    calls = []

    def notification(self, heading, message, icon="", time=0, sound=True):
        RecordingDialog.calls.append(
            {
                "heading": heading,
                "message": message,
                "icon": icon,
                "time": time,
                "sound": sound,
            }
        )


@pytest.fixture(autouse=True)
def kodi(monkeypatch):
    RecordingDialog.calls = []
    FakeAddon.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Dialog", RecordingDialog)
    return RecordingDialog


def test_info_carries_the_addon_icon():
    """An info toast is kofin reporting something; the addon icon says who is
    talking, which is more use than Kodi's blue "i"."""
    icon = toast.icon_for(toast.INFO)

    assert icon.endswith(os.path.join("resources", "media", "icon.png"))
    assert os.path.isabs(icon)
    assert icon != xbmcgui.NOTIFICATION_INFO


def test_warning_and_error_keep_kodis_glyphs():
    """A failure should read as one at a glance; branding only softens it."""
    assert toast.icon_for(toast.WARNING) == xbmcgui.NOTIFICATION_WARNING
    assert toast.icon_for(toast.ERROR) == xbmcgui.NOTIFICATION_ERROR


def test_info_falls_back_when_the_addon_path_is_unreadable(monkeypatch):
    """Kodi can hand back an empty addon path when it fails to load the addon
    document; a path to nowhere draws a blank icon, so take Kodi's glyph."""
    monkeypatch.setattr(toast.settings, "addon_path", lambda: "")

    assert toast.icon_for(toast.INFO) == xbmcgui.NOTIFICATION_INFO


def test_info_falls_back_when_the_settings_read_raises(monkeypatch):
    def boom():
        raise RuntimeError("no addon")

    monkeypatch.setattr(toast.settings, "addon_path", boom)

    assert toast.icon_for(toast.INFO) == xbmcgui.NOTIFICATION_INFO


def test_show_defaults(kodi):
    toast.show("hello")

    call = kodi.calls[0]
    assert call["heading"] == "Kofin"
    assert call["message"] == "hello"
    assert call["time"] == toast.DEFAULT_TIME_MS
    # Kodi's own default is sound=True, which is how two error toasts ended up
    # beeping while the other fourteen did not.
    assert call["sound"] is False


def test_show_passes_heading_level_and_time(kodi):
    toast.show("nope", toast.ERROR, heading="SyncPlay", time_ms=3000)

    call = kodi.calls[0]
    assert call["heading"] == "SyncPlay"
    assert call["icon"] == xbmcgui.NOTIFICATION_ERROR
    assert call["time"] == 3000


def test_show_never_raises(monkeypatch):
    """Every caller sits somewhere losing the message is survivable and losing
    the work behind it is not — a websocket callback that goes on to register
    capabilities, a sync worker mid-library, a player callback thread."""

    class Broken:
        def notification(self, *args, **kwargs):
            raise RuntimeError("no GUI")

    monkeypatch.setattr("xbmcgui.Dialog", Broken)

    toast.show("still fine")  # must not raise
