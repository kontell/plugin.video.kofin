"""The widget-refresh policy on its own (P2.3, sync/refresh.py): the settle
window, the hold cap, the fingerprint gate and the held skin reload."""

from datetime import datetime, timedelta

import pytest

from kofin.sync import refresh as refresh_mod
from kofin.sync.refresh import Refresher
from tests.unit.fakes import FakeAddon, FakeWindow


class Clock:
    def __init__(self):
        self.now = datetime(2026, 8, 27, 12, 0, 0)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class Player:
    playing = False

    def isPlayingVideo(self):
        return self.playing


class Monitor:
    def waitForAbort(self, seconds=0):
        return False


class Owner:
    def __init__(self):
        self.player = Player()
        self.monitor = Monitor()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    FakeAddon.store = {}
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)


@pytest.fixture
def builtins(monkeypatch):
    calls = []
    monkeypatch.setattr(
        refresh_mod.xbmc, "executebuiltin", lambda cmd: calls.append(cmd)
    )
    monkeypatch.setattr(refresh_mod.xbmc, "getCondVisibility", lambda cond: False)
    monkeypatch.setattr(refresh_mod.xbmc, "getInfoLabel", lambda label: "")
    return calls


def make(clock=None, cycle_active=lambda: False, kinds=frozenset({"video", "music"})):
    return Refresher(Owner(), lambda: set(kinds), cycle_active, now=clock or Clock())


def test_settle_folds_a_burst_of_drains_into_one_refresh():
    clock = Clock()
    refresher = make(clock)

    refresher.arm(["video"])
    clock.advance(3)
    refresher.arm(["music"])  # a second echo inside the window re-arms

    clock.advance(3)
    assert refresher.settled() is None  # the window restarted

    clock.advance(1)
    assert refresher.settled() == {"video", "music"}
    assert refresher.settled() is None  # taken once
    assert not refresher.settle.armed


def test_an_active_cycle_holds_the_settle_until_the_cap(monkeypatch):
    clock = Clock()
    active = {"on": True}
    refresher = make(clock, cycle_active=lambda: active["on"])

    refresher.arm(["video"])
    clock.advance(5)
    assert refresher.settled() is None  # settled, but a cycle is draining

    clock.advance(11)  # past the 15 s cap from the first arm
    assert refresher.settled() == {"video"}


def test_the_cap_is_stamped_by_the_first_drain_only():
    clock = Clock()
    refresher = make(clock)

    refresher.arm(["video"])
    for _ in range(10):
        clock.advance(2)
        refresher.arm(["video"])  # a steady stream, each inside the window

    # 20 s in: the window says wait, the cap (15 s) says fire.
    assert refresher.settled() == {"video"}


def test_refresh_takes_its_databases_off_the_pending_set(builtins, monkeypatch):
    clock = Clock()
    refresher = make(clock)
    monkeypatch.setattr(refresher, "moved", lambda databases: set(databases))
    refresher.arm(["video", "music"])

    refresher.refresh(["video"])  # an immediate refresh settles the debt

    assert refresher.pending == {"music"}
    assert refresher.settle.armed  # music still owed

    refresher.refresh(["music"])
    assert refresher.pending == set() and not refresher.settle.armed


def test_an_unmoved_fingerprint_suppresses_the_refresh(builtins, monkeypatch):
    refresher = make()
    monkeypatch.setattr(refresh_mod.widgetstate, "fingerprint", lambda db: {"a": "1"})
    refresher.widget_fingerprints["video"] = {"a": "1"}

    refresher.refresh(["video"])

    assert builtins == []


def test_a_moved_fingerprint_refreshes_and_is_remembered(builtins, monkeypatch):
    refresher = make()
    monkeypatch.setattr(refresh_mod.widgetstate, "fingerprint", lambda db: {"a": "2"})
    refresher.widget_fingerprints["video"] = {"a": "1"}

    refresher.refresh(["video"])

    assert builtins == ["UpdateLibrary(video)"]
    assert refresher.widget_fingerprints["video"] == {"a": "2"}


def test_an_unreadable_fingerprint_fails_open(builtins, monkeypatch):
    """Firing for nothing is recoverable; suppressing a real change is not."""
    refresher = make()

    def explode(db):
        raise RuntimeError("locked")

    monkeypatch.setattr(refresh_mod.widgetstate, "fingerprint", explode)
    refresher.widget_fingerprints["video"] = {"a": "1"}

    refresher.refresh(["video"])

    assert builtins == ["UpdateLibrary(video)"]
    assert "video" not in refresher.widget_fingerprints


def test_music_outside_the_whitelist_always_counts_as_moved(builtins, monkeypatch):
    refresher = make(kinds=frozenset({"video"}))
    monkeypatch.setattr(
        refresh_mod.widgetstate, "fingerprint", lambda db: pytest.fail("no probe")
    )

    assert refresher.moved({"music"}) == {"music"}


def test_a_first_content_reload_is_held_while_video_plays(builtins, monkeypatch):
    refresher = make()
    monkeypatch.setattr(refresh_mod, "CONTENT_FLAG_TIMEOUT_SECONDS", 0.5)
    refresher.owner.player.playing = True

    refresher.reload_for_content(("Library.HasContent(Movies)",))

    assert builtins == []
    assert refresher.pending_skin_reload is True
    assert refresher.flush_pending_reload() is False  # still playing

    refresher.owner.player.playing = False
    assert refresher.flush_pending_reload() is True
    assert builtins == ["ReloadSkin()"]
    assert refresher.pending_skin_reload is False


def test_reload_after_repair_covers_the_kinds_it_was_given(builtins, monkeypatch):
    refresher = make()
    seen = []
    monkeypatch.setattr(
        refresher, "reload_for_content", lambda flags: seen.append(flags)
    )

    refresher.reload_after_repair({"music"})
    refresher.reload_after_repair(set())

    assert seen[0] == refresh_mod.MUSIC_CONTENT_FLAGS
    assert seen[1] == refresh_mod.VIDEO_CONTENT_FLAGS + refresh_mod.MUSIC_CONTENT_FLAGS


def test_music_refresh_is_the_probe_scan_and_never_while_scanning(
    builtins, monkeypatch
):
    refresher = make()
    refresher.refresh_music()
    assert builtins == ["UpdateLibrary(music,%s)" % refresh_mod.MUSIC_REFRESH_PROBE]

    monkeypatch.setattr(refresh_mod.xbmc, "getCondVisibility", lambda cond: True)
    refresher.refresh_music()
    assert len(builtins) == 1
