"""The library-update bar of the API build: gated, rate-limited, never per item."""

import pytest

from kofin.sync.backends.api import progress as progress_module
from tests.unit.apifixtures import (  # noqa: F401
    LIB,
    backend,
    kodi,
    methods,
    movie,
    store,
)


class Bar:
    instances = []

    def __init__(self):
        self.events = []
        Bar.instances.append(self)

    def create(self, heading, message=""):
        self.events.append(("create", heading, message))

    def update(self, percent=0, heading=None, message=None):
        self.events.append(("update", percent, message))

    def close(self):
        self.events.append(("close",))


SETTINGS = {}


@pytest.fixture
def bar(monkeypatch):
    Bar.instances = []
    SETTINGS.clear()
    SETTINGS.update({"showLibraryUpdateProgress": True, "syncProgressThreshold": 0})
    monkeypatch.setattr(progress_module.xbmcgui, "DialogProgressBG", Bar)
    monkeypatch.setattr(progress_module.settings, "get_bool", lambda k: SETTINGS[k])
    monkeypatch.setattr(progress_module.settings, "get_int", lambda k: SETTINGS[k])
    monkeypatch.setattr(progress_module.Progress, "INTERVAL", 0.0)
    return Bar


def test_bar_counts_items_off_and_closes(bar):
    bar_ = progress_module.Progress()
    bar_.begin(4)
    for _ in range(4):
        bar_.step("Movie")
    bar_.close()
    (dialog,) = bar.instances
    assert dialog.events[0][0] == "create" and dialog.events[-1] == ("close",)
    assert [e[1] for e in dialog.events if e[0] == "update"] == [25, 50, 75, 100]
    assert dialog.events[-2][2].endswith("4 / 4")


def test_bar_respects_the_setting_and_the_threshold(bar):
    SETTINGS["syncProgressThreshold"] = 10
    small = progress_module.Progress()
    small.begin(5)
    small.step("Movie")
    small.close()
    assert bar.instances == []
    SETTINGS["showLibraryUpdateProgress"] = False
    off = progress_module.Progress()
    off.begin(50)
    off.close()
    assert bar.instances == []


def test_bar_paints_at_most_once_an_interval_and_on_a_phase_change(bar, monkeypatch):
    """DialogProgressBG.update waits on the app thread; a pass must not paint
    from every item."""
    monkeypatch.setattr(progress_module.Progress, "INTERVAL", 1e9)
    bar_ = progress_module.Progress()
    bar_.begin(300)
    for _ in range(300):
        bar_.step("Movie")
    bar_.note("phase")
    (dialog,) = bar.instances
    updates = [e for e in dialog.events if e[0] == "update"]
    assert len(updates) <= 2 and updates[-1][2] == "phase"


def test_enumeration_tracks_pages_from_the_first_total(bar):
    bar_ = progress_module.Progress()
    bar_.track("Movie", 500, 1788)
    bar_.track("Movie", 1788, 1788)
    bar_.close()
    (dialog,) = bar.instances
    assert [e[1] for e in dialog.events if e[0] == "update"] == [27, 100]


def test_pass_shows_the_bar_and_lets_kodi_show_its_scan(store, backend, kodi, bar):
    store.publish([movie("m%02d" % i) for i in range(3)], library=LIB)
    backend.reconcile()
    (dialog,) = bar.instances
    assert dialog.events[0][0] == "create" and dialog.events[-1] == ("close",)
    assert any(
        e[0] == "update" and (e[2] or "").endswith("3 / 3") for e in dialog.events
    )
    scans = methods(kodi, "VideoLibrary.Scan")
    assert scans and all(p["showdialogs"] is True for p in scans)


def test_pass_keeps_kodi_quiet_when_the_setting_is_off(store, backend, kodi, bar):
    SETTINGS["showLibraryUpdateProgress"] = False
    store.publish([movie("m%02d" % i) for i in range(3)], library=LIB)
    backend.reconcile()
    assert bar.instances == []
    scans = methods(kodi, "VideoLibrary.Scan")
    assert scans and all(p["showdialogs"] is False for p in scans)


def test_an_unreadable_setting_means_no_bar_and_no_failure(bar, monkeypatch):
    """The API profile once shipped without the progress settings; Kodi then
    raises TypeError from getSettingBool, and a pass must not fail on it."""

    def broken(_):
        raise TypeError("Invalid setting type")

    monkeypatch.setattr(progress_module.settings, "get_bool", broken)
    monkeypatch.setattr(progress_module.settings, "get_int", broken)
    bar_ = progress_module.Progress()
    bar_.begin(500)
    bar_.step("Movie")
    bar_.close()
    assert bar.instances == []
    assert progress_module.show_dialogs() is False
