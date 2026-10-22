"""Where fine sync plugs in: the shared state, the play route, the manager's
join/leave and resume, the controller's commands, the service start and the
settings watcher. The scheduler and session themselves are in
test_syncplay_tempo.py."""

import pytest

from kofin.core import state
from kofin.plugin import play
from kofin.plugin.router import Request
from kofin.service import main as service_main
from kofin.service.settings_apply import SettingsApplier
from kofin.syncplay import tempo
from tests.unit.fakes import FakeAddon, FakeWindow
from tests.unit.test_syncplay_manager import join, manager  # noqa: F401 (fixture)
from tests.unit.test_syncplay_playback import make_controller


@pytest.fixture(autouse=True)
def kodi_fakes(monkeypatch):
    FakeAddon.store = {}
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)


# ----------------------------------------------------------------------------
# Shared state
# ----------------------------------------------------------------------------


def test_syncplay_pulse_state_round_trip():
    assert state.syncplay_pulse() == {}
    state.publish_syncplay_pulse({"player": 1})
    assert state.syncplay_pulse() == {"player": 1}
    state.clear_syncplay_pulse()
    assert state.syncplay_pulse() == {}
    FakeWindow.store[state.PROP_SYNCPLAY_PULSE] = "junk"
    assert state.syncplay_pulse() == {}


# ----------------------------------------------------------------------------
# The play route
# ----------------------------------------------------------------------------


class BuiltListItem:
    def __init__(self):
        self.props = {}
        self.path = None

    def setProperty(self, key, value):
        self.props[key] = value

    def setPath(self, path):
        self.path = path

    def setContentLookup(self, flag):
        pass

    def getVideoInfoTag(self):
        class Tag:
            def setDbId(self, dbid):
                pass

        return Tag()


def test_a_play_carries_no_tempo_route(monkeypatch):
    """Kodi's own tempo acts on whatever plays: nothing is stamped on the
    resolved item and nothing rides in the claim, in a group or not."""
    built = BuiltListItem()
    monkeypatch.setattr(play.listitems, "build", lambda *a, **k: built)
    resolved = []
    monkeypatch.setattr(
        "xbmcplugin.setResolvedUrl", lambda handle, ok, li: resolved.append((ok, li))
    )
    request = Request("plugin://plugin.video.kofin/", 7, {"id": "m1"})
    play.resolve_downloaded(
        request,
        {"Id": "m1", "Type": "Movie"},
        "/dl/m1.mkv",
        "http://s",
        "dev",
        0,
        "",
        None,
    )
    assert resolved == [(True, built)]
    assert "inputstream" not in built.props
    claim = state.claim_play_item("/dl/m1.mkv")
    assert "Tempo" not in claim
    assert claim["PlayMethod"] == "DirectPlay"


# ----------------------------------------------------------------------------
# The manager: join arms, leave disarms, a resume settles
# ----------------------------------------------------------------------------


class SessionSpy:
    def __init__(self):
        self.calls = []
        self.active = False

    def begin(self):
        self.calls.append("begin")
        self.active = True

    def end(self):
        self.calls.append("end")
        self.active = False


def test_join_arms_and_leave_disarms(manager):  # noqa: F811
    spy = SessionSpy()
    manager.tempo_session = spy
    join(manager)
    assert spy.calls == ["begin"]
    assert manager.fine_sync_active()
    # A re-join of the same group is not a new session.
    join(manager, version=2)
    assert spy.calls == ["begin"]
    manager._leave_locally()
    assert spy.calls == ["begin", "end"]
    assert not manager.fine_sync_active()


def test_refresh_follows_the_setting(manager, monkeypatch):  # noqa: F811
    spy = SessionSpy()
    manager.tempo_session = spy
    resets = []
    monkeypatch.setattr(manager.playback.tempo, "reset", lambda: resets.append(1))
    manager.refresh_tempo_session()  # not in a group: nothing
    assert spy.calls == []
    join(manager)
    FakeAddon.store["syncPlayTempo"] = "false"
    manager.refresh_tempo_session()
    assert spy.calls == ["begin", "end"]
    assert resets == [1]  # a running pulse does not outlive the setting
    FakeAddon.store["syncPlayTempo"] = "true"
    manager.refresh_tempo_session()
    assert spy.calls == ["begin", "end", "begin"]


def test_rate_mismatch_is_told_once_per_group(manager):  # noqa: F811
    warnings = []
    # The join toast itself goes through _toast too; only the warning counts.
    manager._toast = lambda message, **kw: kw.get("warning") and warnings.append(
        message
    )
    join(manager)
    manager.notify_rate_mismatch()
    manager.notify_rate_mismatch()
    assert len(warnings) == 1
    manager._leave_locally()
    join(manager)
    manager.notify_rate_mismatch()
    assert len(warnings) == 2


def test_a_live_play_without_timeshift_is_told_once_per_group(
    manager,  # noqa: F811
):
    warnings = []
    manager._toast = lambda message, **kw: kw.get("warning") and warnings.append(
        message
    )
    join(manager)
    manager.notify_live_without_timeshift()
    manager.notify_live_without_timeshift()
    assert len(warnings) == 1
    manager._leave_locally()
    join(manager)
    manager.notify_live_without_timeshift()
    assert len(warnings) == 2


def test_current_claim_is_the_players_item(manager):  # noqa: F811
    manager.player.playing = True
    manager.player.item = {"Id": "m1", "PlaySessionId": "ps"}
    assert manager.current_claim()["PlaySessionId"] == "ps"
    manager.player.playing = False
    assert manager.current_claim() is None


def test_a_resume_settles_a_left_rate_first(manager, monkeypatch):  # noqa: F811
    """In a group or not: a member that left while paused resumes its own
    playback, and the rate a cut pulse left must not play on."""
    settled = []
    monkeypatch.setattr(
        manager.playback.tempo, "settle_owed", lambda: settled.append(1)
    )
    manager.on_resumed()  # not in a group
    assert settled == [1]
    join(manager)
    with manager.programmatic():
        manager.on_resumed()  # kofin's own resume
    assert settled == [1, 1]


# ----------------------------------------------------------------------------
# The controller: no pulse across a command, settle after a seek
# ----------------------------------------------------------------------------


class SchedulerSpy:
    def __init__(self):
        self.calls = []

    def cancel(self, reason):
        self.calls.append(("cancel", reason))

    def before_seek(self):
        self.calls.append("before_seek")

    def note_settle(self):
        self.calls.append("settle")

    def reset(self):
        self.calls.append("reset")

    def tick(self):
        self.calls.append("tick")


def command(name, ticks=0):
    from kofin.syncplay import utils

    return {
        "Command": name,
        "When": utils.to_iso(utils.local_ms() - 10),
        "EmittedAt": utils.to_iso(utils.local_ms()),
        "PositionTicks": ticks,
        "PlaylistItemId": "pl-1",
    }


def test_commands_cut_the_pulse_first():
    controller, manager_, player = make_controller(paused=False, position=10.0)
    spy = SchedulerSpy()
    controller.tempo = spy
    controller.schedule(command("Pause", ticks=10 * 10_000_000))
    assert spy.calls[0] == ("cancel", "Pause")


def test_seek_and_settle_brackets_the_seek():
    controller, manager_, player = make_controller(paused=True, position=10.0)
    spy = SchedulerSpy()
    controller.tempo = spy
    controller._seek_and_settle(30000.0)
    assert spy.calls == ["before_seek", "settle"]
    assert ("seek", 30.0) in player.actions


def test_correct_position_aims_ahead_by_the_seek_lag():
    controller, manager_, player = make_controller(paused=False, position=10.0)
    controller.tempo = SchedulerSpy()
    controller.seek_lag_ms = 400.0
    controller.set_reference(20 * 10_000_000, manager_.server_now_ms(), True)
    controller.correct_position()
    seeks = [a for a in player.actions if isinstance(a, tuple) and a[0] == "seek"]
    assert seeks and abs(seeks[0][1] - 20.4) < 0.2


def test_post_resume_residual_is_left_to_fine_sync_when_it_can():
    controller, manager_, player = make_controller(paused=False, position=10.0)

    class CanClose(SchedulerSpy):
        def can_close(self, residual):
            return True

    controller.tempo = CanClose()
    controller.set_reference(10.7 * 10_000_000, manager_.server_now_ms(), True)
    controller._align_after_resume()
    assert not [a for a in player.actions if isinstance(a, tuple) and a[0] == "seek"]


def test_stop_loop_resets_the_scheduler():
    controller, manager_, player = make_controller()
    spy = SchedulerSpy()
    controller.tempo = spy
    controller.stop_loop()
    assert "reset" in spy.calls


def test_remaining_is_what_is_left_of_the_item():
    controller, manager_, player = make_controller(position=100.0)
    player.clock_advances = False
    player.total = 160.0
    assert controller.remaining_ms() == pytest.approx(60_000.0)
    player.total = 0.0  # a live stream, a source with no duration
    assert controller.remaining_ms() is None


def test_without_a_session_the_real_scheduler_leaves_it_to_a_seek():
    """The controller's own scheduler asks the manager whether the group
    has fine sync; a manager without the session never pulses."""
    controller, manager_, player = make_controller(paused=False, position=10.0)
    assert controller.tempo.can_close(100.0) is False


# ----------------------------------------------------------------------------
# The service start
# ----------------------------------------------------------------------------


def test_a_new_service_generation_settles_a_left_rate(monkeypatch):
    calls = []
    monkeypatch.setattr(
        tempo, "settle_left_rate", lambda reason="": calls.append(reason)
    )
    service_main.Service._settle_left_tempo(object())
    assert calls == [" by an earlier service"]


def test_the_settle_never_takes_the_service_down(monkeypatch):
    def boom(reason=""):
        raise RuntimeError("no Kodi")

    monkeypatch.setattr(tempo, "settle_left_rate", boom)
    service_main.Service._settle_left_tempo(object())  # logged, not raised


# ----------------------------------------------------------------------------
# The settings watcher
# ----------------------------------------------------------------------------


def test_toggling_fine_sync_refreshes_the_session():
    class Manager:
        def __init__(self):
            self.refreshed = 0

        def refresh_tempo_session(self):
            self.refreshed += 1

    class Service:
        def __init__(self):
            self.syncplay = Manager()

    service = Service()
    FakeAddon.store["deviceId"] = "dev"
    applier = SettingsApplier(service)
    applier.mark_ready()
    FakeAddon.store["syncPlayTempo"] = "false"
    applier.apply()
    assert service.syncplay.refreshed == 1
    service.syncplay = None
    FakeAddon.store["syncPlayTempo"] = "true"
    applier.apply()  # no manager: nothing to refresh, nothing to raise


# ----------------------------------------------------------------------------
# Seek lag
# ----------------------------------------------------------------------------


class HoldingPlayer:
    """A player whose clock holds near the seek target for a while, then moves:
    what a real VideoPlayer reports while its pipeline refills. ``lands_ms``
    is where the clock actually restarts relative to the target."""

    def __init__(self, hold_reads, lands_ms=0.0):
        self.hold_reads = hold_reads
        self.lands_ms = lands_ms
        self.reads = 0
        self.target = None
        self.position = 10.0
        self.actions = []

    def isPlaying(self):
        return True

    def isPlayingAudio(self):
        return False

    def getTime(self):
        self.reads += 1
        if self.target is None:
            return self.position + self.reads * 0.05
        if self.reads <= self.hold_reads:
            return self.target
        return (
            self.target + self.lands_ms / 1000.0 + (self.reads - self.hold_reads) * 0.05
        )

    def seekTime(self, seconds):
        self.target = seconds
        self.reads = 0
        self.actions.append(("seek", seconds))

    def pause(self):
        self.actions.append("pause")


def _seek_with(player, monkeypatch):
    import time as _time

    from kofin.syncplay.playback import PlaybackController
    from tests.unit.test_syncplay_playback import FakeManager

    controller = PlaybackController(FakeManager(player), player)
    controller.tempo = SchedulerSpy()
    monkeypatch.setattr("xbmc.sleep", lambda ms: _time.sleep(ms / 1000.0))
    monkeypatch.setattr(
        "kofin.syncplay.playback.xbmc.getCondVisibility", lambda c: False
    )
    controller._seek_and_settle(30000.0, stay_paused=False)
    return controller


def test_seek_lag_is_timed_from_the_clock_restarting(monkeypatch):
    # ~12 polls at 50 ms before the clock moves: a ~600 ms restart, landing
    # on the target. Smoothed halfway from the 500 ms default.
    controller = _seek_with(HoldingPlayer(hold_reads=12), monkeypatch)
    assert 500 < controller.seek_lag_ms < 700


def test_seek_lag_includes_an_early_landing(monkeypatch):
    # The Tab's shape: restarts after ~600 ms *and* 350 ms early, so a seek
    # aimed at the group's position leaves ~950 ms behind.
    controller = _seek_with(HoldingPlayer(hold_reads=12, lands_ms=-350.0), monkeypatch)
    assert 650 < controller.seek_lag_ms < 900


def test_seek_lag_is_left_alone_when_no_hold_is_seen():
    controller, manager_, player = make_controller(paused=False, position=10.0)
    controller.tempo = SchedulerSpy()
    controller._seek_and_settle(30000.0, stay_paused=False)
    assert controller.seek_lag_ms == 500.0
