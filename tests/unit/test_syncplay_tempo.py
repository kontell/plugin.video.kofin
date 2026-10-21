"""Fine sync through Kodi's own tempo (lib/kofin/syncplay/tempo.py).

The planner is pure; the scheduler runs against a fake controller and a fake
Kodi that answers ``Player.SetTempo`` and the ``tempo`` property the way
kontell/xbmc #15 and #16 do: a tempo is refused while paused and kept across
a pause, refused for an item that cannot take one, and read back once the
player has taken it — at once on PAPlayer, a moment later on VideoPlayer.
"""

import json
import time

import pytest

from kofin.core import state
from kofin.syncplay import tempo, utils
from kofin.syncplay.tempo import (
    KodiTempo,
    Profile,
    PulseScheduler,
    TempoSession,
    plan_pulse,
)
from tests.unit.fakes import FakeAddon, FakeWindow


class FakeKodi:
    """The player side of the tempo JSON-RPC."""

    def __init__(self):
        self.tempo = 1.0
        self.paused = False
        self.enabled = True
        self.playing = True
        self.playerid = 1
        # Reads that still return the previous tempo after a set: VideoPlayer
        # takes a speed message on its player thread.
        self.read_lag = 0
        self._stale = None
        self._stale_reads = 0
        self.sets = []
        self.refused = []
        # kontell/xbmc #38: Player.SetTempo declares "correction"
        self.correction_declared = True
        self.corrections = []
        self.settings = {"audiooutput.atempothreshold": 2, "musicplayer.crossfade": 0}

    def take(self, value):
        """The player takes a tempo of its own accord (Kodi's live-edge
        return, a viewer's OSD)."""
        self.tempo = value
        self._stale_reads = 0

    def __call__(self, query):
        request = json.loads(query)
        method = request["method"]
        params = request.get("params") or {}

        if method == "JSONRPC.Introspect":
            assert params["filter"] == {"id": "Player.SetTempo", "type": "method"}
            declared = [{"name": "playerid"}, {"name": "tempo"}]
            if self.correction_declared:
                declared.append({"name": "correction", "type": "boolean"})
            methods = {"Player.SetTempo": {"params": declared}}
            return json.dumps({"result": {"methods": methods}})

        if method == "Player.SetTempo":
            assert isinstance(params["tempo"], float)  # never a JSON integer
            if "correction" in params and not self.correction_declared:
                # Kodi refuses a parameter its schema does not name
                return json.dumps(
                    {"error": {"code": -32602, "message": "Invalid params."}}
                )
            if self.paused or not self.enabled or not self.playing:
                self.refused.append(params["tempo"])
                return json.dumps({"error": {"code": -32100, "message": "Failed"}})
            self.sets.append(params["tempo"])
            self.corrections.append(params.get("correction", False))
            self._stale = self.tempo
            self._stale_reads = self.read_lag
            self.tempo = round(params["tempo"], 3)
            return json.dumps({"result": {"tempo": self._stale}})

        if method == "Player.GetProperties":
            if not self.playing:
                return json.dumps({"error": {"code": -32100, "message": "Failed"}})
            if self._stale_reads > 0:
                self._stale_reads -= 1
                return json.dumps({"result": {"tempo": self._stale}})
            return json.dumps({"result": {"tempo": self.tempo}})

        if method == "Settings.GetSettingValue":
            if params["setting"] not in self.settings:
                return json.dumps({"error": {"code": -32602, "message": "Invalid"}})
            return json.dumps({"result": {"value": self.settings[params["setting"]]}})

        if method == "Player.GetActivePlayers":
            players = [{"playerid": self.playerid}] if self.playing else []
            return json.dumps({"result": players})

        raise AssertionError(method)


@pytest.fixture
def kodi(monkeypatch):
    fake = FakeKodi()
    monkeypatch.setattr("xbmc.executeJSONRPC", fake)
    monkeypatch.setattr(
        tempo.xbmc,
        "getCondVisibility",
        lambda condition: fake.enabled if condition == "Player.TempoEnabled" else False,
    )
    return fake


@pytest.fixture
def logged(monkeypatch):
    lines = []
    monkeypatch.setattr("xbmc.log", lambda message, level=0: lines.append(message))
    # Secrets registered by earlier tests would mask parts of these lines.
    monkeypatch.setattr("kofin.core.log._secrets", {})
    return lines


@pytest.fixture(autouse=True)
def kodi_fakes(monkeypatch):
    FakeAddon.store = {}
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)


# ----------------------------------------------------------------------------
# Pure arithmetic
# ----------------------------------------------------------------------------


class TestPlanner:
    def test_deadband_is_left_alone(self):
        assert plan_pulse(0.0) is None
        assert plan_pulse(tempo.DEADBAND_MS) is None
        assert plan_pulse(-tempo.DEADBAND_MS) is None

    def test_sign_follows_the_residual(self):
        behind = plan_pulse(120.0)
        ahead = plan_pulse(-120.0)
        assert behind is not None and ahead is not None
        assert behind[0] > 1.0 > ahead[0]
        assert behind[1] == ahead[1]

    def test_rates_are_on_kodis_grid(self):
        # Kodi plays a tempo to three decimals, whatever it is asked.
        for residual in (76.0, 99.0, 151.0, 333.0, 2001.0, -87.0):
            rate, _seconds = plan_pulse(residual)
            assert rate == round(rate, 3)

    def test_displacement_equals_the_residual(self):
        # The length is worked out from the rate Kodi will play, so rounding
        # the rate costs nothing of the displacement.
        for residual in (80.0, 99.0, 120.0, 200.0, 290.0, -95.0):
            rate, seconds = plan_pulse(residual)
            assert (rate - 1.0) * seconds * 1000.0 == pytest.approx(residual, abs=0.5)

    def test_rate_stays_inside_the_band(self):
        profile = Profile("video", tempo.DEADBAND_MS, tempo.RATE_MIN, 0.03, 5.0, 5000.0)
        for residual in (76.0, 100.0, 250.0, 300.0, 2000.0):
            rate, seconds = plan_pulse(residual, profile)
            assert tempo.RATE_MIN - 1e-9 <= abs(rate - 1.0) <= 0.03 + 1e-9
            assert 0 < seconds <= tempo.PULSE_MAX_S

    def test_budget_saturates_at_max_rate_and_duration(self):
        rate, seconds = plan_pulse(50000.0)
        assert rate == pytest.approx(1.0 + tempo.RATE_MAX_DEFAULT)
        assert seconds == tempo.PULSE_MAX_S

    def test_large_residual_uses_the_full_rate(self):
        # Rate ceiling identity: duration stretches only once the rate is
        # capped. 2.5 s at 25 % is still a 10 s pulse; the 5 s default budget
        # is 20 s; the 10 s slider max is one 40 s pulse (PULSE_MAX_S).
        rate, seconds = plan_pulse(2500.0)
        assert rate == pytest.approx(1.25) and seconds == pytest.approx(10.0)
        rate, seconds = plan_pulse(5000.0)
        assert rate == pytest.approx(1.25) and seconds == pytest.approx(20.0)
        rate, seconds = plan_pulse(10000.0)
        assert rate == pytest.approx(1.25) and seconds == pytest.approx(40.0)
        rate, seconds = plan_pulse(20000.0)
        assert rate == pytest.approx(1.25) and seconds == tempo.PULSE_MAX_S
        assert (rate - 1.0) * seconds * 1000.0 == pytest.approx(10000.0)

    def test_the_ceiling_is_on_the_grid(self):
        assert Profile("x", 0, 0.001, 0.0189, 5, 0).rate_max == pytest.approx(0.018)
        assert Profile("x", 0, 0.001, 0.25, 5, 0).rate_max == pytest.approx(0.25)
        assert Profile("x", 0, 0.001, 0.0, 5, 0).rate_max == pytest.approx(0.001)


class TestProfile:
    def test_video_reads_the_sliders(self):
        FakeAddon.store["syncPlayPulseBudget"] = "1200"
        FakeAddon.store["syncPlayMaxRate"] = "10"
        profile = tempo.video_profile()
        assert profile.budget_ms == 1200.0
        assert profile.rate_max == pytest.approx(0.10)
        assert profile.deadband_ms == tempo.DEADBAND_MS

    def test_video_defaults(self):
        profile = tempo.video_profile()
        assert profile.budget_ms == tempo.BUDGET_DEFAULT_MS
        assert profile.rate_max == pytest.approx(tempo.RATE_MAX_DEFAULT)


class TestSchedule:
    def test_small_rate_is_one_write_and_a_return(self):
        assert tempo.pulse_schedule(1.03, 5.0) == [(0.0, 1.03), (5.0, 1.0)]
        assert tempo.pulse_schedule(0.96, 4.0) == [(0.0, 0.96), (4.0, 1.0)]

    def test_large_rate_ramps_in_and_out(self):
        schedule = tempo.pulse_schedule(1.25, 10.0)
        values = [value for _offset, value in schedule]
        assert values == [1.05, 1.10, 1.15, 1.20, 1.25, 1.20, 1.15, 1.10, 1.05, 1.0]
        offsets = [offset for offset, _value in schedule]
        assert offsets == sorted(offsets)
        assert offsets[1] - offsets[0] == pytest.approx(tempo.RAMP_DT)
        assert offsets[-1] - offsets[-2] == pytest.approx(tempo.RAMP_DT)

    def test_ramped_schedule_moves_what_the_plan_promised(self):
        for rate, seconds in ((1.25, 10.0), (0.80, 8.0), (1.12, 6.0)):
            schedule = tempo.pulse_schedule(rate, seconds)
            moved = 0.0
            for (t0, value), (t1, _next) in zip(schedule, schedule[1:]):
                moved += (value - 1.0) * (t1 - t0)
            assert moved == pytest.approx((rate - 1.0) * seconds, rel=1e-6)

    def test_a_short_pulse_keeps_a_minimum_hold(self):
        schedule = tempo.pulse_schedule(1.25, 1.0)
        hold = schedule[5][0] - schedule[4][0]
        assert hold == pytest.approx(2.0 * tempo.RAMP_DT)

    def test_regrowth(self):
        assert tempo.regrowth_ppm(50.0, 5.0) == pytest.approx(10000.0)
        assert tempo.regrowth_ppm(50.0, 0.0) == 0.0


# ----------------------------------------------------------------------------
# The actuator
# ----------------------------------------------------------------------------


class TestKodiTempo:
    def test_set_reads_and_refusals(self, kodi):
        actuator = KodiTempo(1)
        assert actuator.set(1.03) is True
        assert kodi.sets == [1.03]
        assert actuator.read() == pytest.approx(1.03)
        # A whole number still goes as a double: Kodi refuses an integer.
        assert actuator.set(1) is True
        assert kodi.sets[-1] == 1.0

        kodi.paused = True
        assert actuator.set(1.05) is False
        assert actuator.read() == pytest.approx(1.0)  # kept across the pause

        kodi.paused = False
        kodi.enabled = False
        assert actuator.set(1.05) is False
        assert KodiTempo.enabled() is False

    def test_read_without_a_player_is_none(self, kodi):
        kodi.playing = False
        assert KodiTempo(1).read() is None

    def test_every_rate_goes_as_a_correction(self, kodi):
        actuator = KodiTempo(1)
        assert actuator.set(1.03) is True
        assert actuator.set(1.0) is True
        assert kodi.corrections == [True, True]

    def test_a_kodi_without_the_flag_is_asked_plainly(self, kodi):
        kodi.correction_declared = False
        actuator = KodiTempo(1)
        assert actuator.correction is False
        assert actuator.set(1.03) is True
        assert kodi.sets == [1.03]
        assert kodi.corrections == [False]


class TestLeftRate:
    def test_nothing_recorded_is_nothing_owed(self, kodi):
        assert tempo.settle_left_rate() is True
        assert kodi.sets == []

    def test_a_left_rate_is_put_back(self, kodi, logged):
        kodi.tempo = 1.08
        state.publish_syncplay_pulse({"player": 1})
        assert tempo.settle_left_rate(" by an earlier service") is True
        assert kodi.tempo == 1.0
        assert kodi.corrections == [True]  # put back off the OSD too
        assert state.syncplay_pulse() == {}
        assert any("1.080x left in force by an earlier service" in l for l in logged)

    def test_paused_it_stays_owed(self, kodi):
        kodi.tempo = 1.08
        kodi.paused = True
        state.publish_syncplay_pulse({"player": 1})
        assert tempo.settle_left_rate() is False
        assert state.syncplay_pulse() == {"player": 1}
        kodi.paused = False
        assert tempo.settle_left_rate() is True
        assert kodi.tempo == 1.0 and state.syncplay_pulse() == {}

    def test_already_back_or_gone_drops_the_record(self, kodi):
        state.publish_syncplay_pulse({"player": 1})
        assert tempo.settle_left_rate() is True  # a new item starts at 1.0
        assert kodi.sets == [] and state.syncplay_pulse() == {}

        state.publish_syncplay_pulse({"player": 1})
        kodi.playing = False
        assert tempo.settle_left_rate() is True
        assert state.syncplay_pulse() == {}

        FakeWindow.store[state.PROP_SYNCPLAY_PULSE] = '{"player": "x"}'
        assert tempo.settle_left_rate() is True
        assert state.syncplay_pulse() == {}

    def test_the_record_outlives_clear_all(self):
        # A service generation ends with clear_all; a rate it left in force
        # must still be found by the next one.
        state.publish_syncplay_pulse({"player": 1})
        state.clear_all()
        assert state.syncplay_pulse() == {"player": 1}


# ----------------------------------------------------------------------------
# The scheduler
# ----------------------------------------------------------------------------


class FakeManager:
    def __init__(self):
        self.claim = None
        self.mismatch = 0
        self.active = True

    def current_claim(self):
        return self.claim

    def fine_sync_active(self):
        return self.active

    def notify_rate_mismatch(self):
        self.mismatch += 1


class FakeController:
    def __init__(self, kodi):
        self.kodi = kodi
        self.manager = FakeManager()
        self.playing = True
        self.audio = False
        self.group_ms = 100000.0
        self.local_ms = 100000.0
        self.remaining = None
        self.corrections = 0
        self.on_source_clock = True  # a live item: the group shares the clock

    def _is_paused(self):
        return self.kodi.paused

    def _is_audio(self):
        return self.audio

    def live_on_source_clock(self):
        return self.on_source_clock

    def reference_is_playing(self):
        return self.playing

    def estimate_position_ms(self):
        return self.group_ms

    def _position_ms(self):
        return self.local_ms

    def remaining_ms(self):
        return self.remaining

    def correct_position(self):
        self.corrections += 1
        self.local_ms = self.group_ms
        self.lock_held_at_seek = self.scheduler._lock._is_owned()


def claim(session="ps-1", **extra):
    payload = {"Id": "item-1", "PlaySessionId": session}
    payload.update(extra)
    return payload


def live_claim(session="ps-live"):
    """A public-bus claim with no runtime: the contract's live spelling."""
    return claim(session, Provider="pvr.kofin")


@pytest.fixture
def rig(kodi):
    controller = FakeController(kodi)
    controller.manager.claim = claim()
    scheduler = PulseScheduler(controller)
    controller.scheduler = scheduler
    return scheduler, controller, kodi


class Clock:
    def __init__(self, monkeypatch):
        self.now = time.time()
        monkeypatch.setattr(tempo.time, "time", lambda: self.now)

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


def fill_window(scheduler, controller, residual_ms, extra=0):
    controller.local_ms = controller.group_ms - residual_ms
    for _ in range(tempo.WINDOW_SAMPLES - 1 + extra):
        scheduler.tick()


def start_pulse(scheduler, controller, residual_ms):
    """Fill the window, tick (the first write goes out), tick again (it is
    read back and the pulse runs)."""
    fill_window(scheduler, controller, residual_ms)
    scheduler.tick()
    assert scheduler._awaiting is not None and scheduler._awaiting["kind"] == "start"
    scheduler.tick()
    assert scheduler._pulse is not None


def finish_pulse(scheduler, clock):
    """Time's up: the scheduler asks for 1.0 and reads it back."""
    clock.now = scheduler._pulse["writes"][-1][0] + 0.01
    scheduler.tick()
    assert scheduler._pulse is None and scheduler._awaiting["kind"] == "end"
    scheduler.tick()
    assert scheduler._awaiting is None


class TestArming:
    def test_arms_a_video_claim(self, rig):
        scheduler, controller, kodi = rig
        scheduler.tick()
        assert scheduler.profile is not None and scheduler.profile.name == "video"
        assert scheduler.actuator.playerid == tempo.VIDEO_PLAYER

    def test_nothing_outside_a_fine_sync_session(self, rig):
        scheduler, controller, kodi = rig
        controller.manager.active = False
        fill_window(scheduler, controller, 200.0, extra=4)
        assert scheduler.profile is None and kodi.sets == []
        assert not scheduler.can_close(100.0)

    def test_audio_arms_with_the_music_limits(self, rig):
        scheduler, controller, kodi = rig
        controller.audio = True
        kodi.playerid = tempo.MUSIC_PLAYER
        scheduler.tick()
        assert scheduler.profile.name == "music"
        assert scheduler.actuator.playerid == tempo.MUSIC_PLAYER
        assert scheduler.profile.deadband_ms == tempo.MUSIC_DEADBAND_MS
        assert scheduler.profile.rate_max == pytest.approx(tempo.MUSIC_RATE_MAX)
        assert scheduler.profile.budget_ms == tempo.MUSIC_BUDGET_MS

    def test_live_audio_is_left_to_the_commands(self, rig, logged):
        scheduler, controller, kodi = rig
        controller.audio = True
        controller.manager.claim = live_claim()
        fill_window(scheduler, controller, 200.0, extra=4)
        assert scheduler.profile is None and kodi.sets == []
        assert sum("is live audio; command-only" in line for line in logged) == 1

    def test_can_close_arms_before_the_first_tick(self, rig):
        """A command asks before the loop has ticked on a new item."""
        scheduler, controller, kodi = rig
        assert scheduler.can_close(1000.0)
        assert scheduler.profile is not None

    def test_can_close_needs_the_budget_and_a_tempo(self, rig):
        scheduler, controller, kodi = rig
        FakeAddon.store["syncPlayPulseBudget"] = "1200"
        assert scheduler.can_close(1000.0) and not scheduler.can_close(1500.0)
        kodi.enabled = False  # a realtime stream with no range, say
        assert not scheduler.can_close(100.0)

    def test_new_session_rearms(self, rig):
        scheduler, controller, kodi = rig
        scheduler.tick()
        scheduler._gave_up = True
        controller.manager.claim = claim(session="ps-2")
        scheduler.tick()
        assert scheduler._gave_up is False
        assert scheduler._session_id == "ps-2"


class TestPulses:
    def test_inside_the_deadband_nothing_happens(self, rig):
        scheduler, controller, kodi = rig
        fill_window(scheduler, controller, 30.0, extra=4)
        assert scheduler._pulse is None and kodi.sets == []

    def test_window_must_fill_before_acting(self, rig):
        scheduler, controller, kodi = rig
        fill_window(scheduler, controller, 150.0)  # one short of the window
        assert scheduler._pulse is None and scheduler._awaiting is None

    def test_pulse_is_set_confirmed_and_ended(self, rig, clock, logged):
        scheduler, controller, kodi = rig
        start_pulse(scheduler, controller, 150.0)
        rate, seconds = plan_pulse(150.0)
        assert kodi.sets == [rate]
        assert scheduler._pulse["seconds"] == pytest.approx(seconds)
        assert state.syncplay_pulse() == {"player": 1}  # a rate is in force

        pulse = scheduler._pulse
        scheduler.tick()  # not over yet
        assert scheduler._pulse is pulse

        finish_pulse(scheduler, clock)
        assert kodi.sets[-1] == 1.0 and kodi.tempo == 1.0
        assert state.syncplay_pulse() == {}
        assert scheduler._settle_until == pytest.approx(clock.now + tempo.SETTLE_S)
        assert len(scheduler._history) == 1

        # Once the settle has passed, what the pulse moved is read off the
        # player clock: here the member came forward by the whole residual.
        controller.local_ms = controller.group_ms
        clock.advance(tempo.SETTLE_S + 0.1)
        scheduler.tick()
        assert scheduler._measure is None
        assert any("moved" in line and "wanted +150ms" in line for line in logged)

    def test_videoplayer_confirms_a_tick_later(self, rig, logged):
        scheduler, controller, kodi = rig
        kodi.read_lag = 1  # the player thread has not taken it yet
        fill_window(scheduler, controller, 150.0)
        scheduler.tick()
        scheduler.tick()  # still reads the old tempo
        assert scheduler._awaiting is not None
        scheduler.tick()
        assert scheduler._pulse is not None
        assert any("applied after" in line for line in logged)

    def test_an_unconfirmed_rate_is_taken_back_and_the_item_written_off(
        self, rig, clock
    ):
        scheduler, controller, kodi = rig
        kodi.read_lag = 1000  # the player never takes it
        fill_window(scheduler, controller, 150.0)
        scheduler.tick()
        clock.advance(tempo.APPLY_TIMEOUT_S + 0.1)
        scheduler.tick()
        assert scheduler._pulse is None and scheduler._awaiting is None
        assert kodi.sets[-1] == 1.0
        assert scheduler.profile is None  # command-only for this item

    def test_an_item_that_cannot_take_a_tempo_now_is_not_written_off(self, rig):
        scheduler, controller, kodi = rig
        kodi.enabled = False  # a timeshift buffer with no range yet
        fill_window(scheduler, controller, 150.0, extra=1)
        assert kodi.sets == [] and scheduler.profile is not None
        kodi.enabled = True
        start_pulse(scheduler, controller, 150.0)
        assert kodi.sets

    def test_a_refused_rate_writes_the_item_off(self, rig, monkeypatch):
        scheduler, controller, kodi = rig
        monkeypatch.setattr(KodiTempo, "enabled", staticmethod(lambda: True))
        kodi.enabled = False  # Kodi refuses what TempoEnabled allowed
        fill_window(scheduler, controller, 150.0, extra=1)
        assert scheduler.profile is None
        assert kodi.refused

    def test_a_late_tick_collapses_the_ramp_to_the_latest_write(self, rig, clock):
        scheduler, controller, kodi = rig
        start_pulse(scheduler, controller, 2000.0)  # 25 %, ramped
        writes = list(scheduler._pulse["writes"])
        assert len(writes) > 2
        clock.now = writes[4][0] + 0.01  # every ramp-up step overdue at once
        scheduler.tick()
        assert kodi.tempo == pytest.approx(writes[4][1])
        assert len(scheduler._pulse["writes"]) == len(writes) - 5

    def test_a_pulse_ends_before_its_item_does(self, rig):
        scheduler, controller, kodi = rig
        # 20 s left: a 4.5 s pulse fits whole.
        controller.remaining = 20000.0
        start_pulse(scheduler, controller, 150.0)
        assert scheduler._pulse["seconds"] == pytest.approx(plan_pulse(150.0)[1])

    def test_a_pulse_is_shortened_or_skipped_near_the_end(self, rig):
        scheduler, controller, kodi = rig
        room = 2.5  # seconds left for the pulse once settle and margin are off
        controller.remaining = (room + tempo.SETTLE_S + tempo.END_MARGIN_S) * 1000.0
        start_pulse(scheduler, controller, 400.0)  # would want 5 s
        assert scheduler._pulse["seconds"] == pytest.approx(room)

        scheduler.cancel("test")
        controller.remaining = (tempo.SETTLE_S + tempo.END_MARGIN_S + 0.5) * 1000.0
        scheduler._settle_until = 0.0
        fill_window(scheduler, controller, 400.0, extra=1)
        assert scheduler._pulse is None and scheduler._awaiting is None

    def test_residual_beyond_the_budget_seeks(self, rig):
        scheduler, controller, kodi = rig
        controller.lock_held_at_seek = None
        fill_window(scheduler, controller, 6000.0, extra=1)
        assert controller.corrections == 1
        assert controller.lock_held_at_seek is False
        assert kodi.sets == []
        assert scheduler._seek_blackout_until > time.time()
        # The blackout holds a second seek off — and what the seek left behind
        # is closed by a saturated pulse instead of waited out.
        scheduler._settle_until = 0.0
        start_pulse(scheduler, controller, 15000.0)
        assert controller.corrections == 1
        # Ramped: the first write is the first step, the plan is the ceiling.
        assert kodi.sets[0] == pytest.approx(1.0 + tempo.RAMP_STEP)
        assert scheduler._pulse["rate"] == pytest.approx(1.0 + tempo.RATE_MAX_DEFAULT)
        assert scheduler._pulse["seconds"] == tempo.PULSE_MAX_S

    def test_mixed_window_pulses_instead_of_seeking(self, rig):
        scheduler, controller, kodi = rig
        fill_window(scheduler, controller, 6000.0)
        controller.local_ms = controller.group_ms - 100.0
        scheduler.tick()
        assert controller.corrections == 0
        assert scheduler._awaiting is not None
        scheduler.tick()
        assert scheduler._pulse["rate"] == pytest.approx(1.0 + tempo.RATE_MAX_DEFAULT)

    def test_settle_window_blocks_measurement(self, rig):
        scheduler, controller, kodi = rig
        scheduler.tick()
        scheduler.note_settle()
        fill_window(scheduler, controller, 200.0, extra=4)
        assert scheduler._window == [] and scheduler._pulse is None

    def test_a_stopped_group_clears_the_window(self, rig):
        scheduler, controller, kodi = rig
        fill_window(scheduler, controller, 200.0)
        controller.playing = False
        scheduler.tick()
        assert scheduler._window == []


class TestPause:
    def test_a_pause_cuts_the_pulse_and_the_resume_settles_it(self, rig, logged):
        scheduler, controller, kodi = rig
        start_pulse(scheduler, controller, 150.0)
        rate = kodi.tempo
        kodi.paused = True  # a viewer pauses
        scheduler.tick()
        assert scheduler._pulse is None
        assert kodi.tempo == rate  # refused while paused: kept, and owed
        assert state.syncplay_pulse() == {"player": 1}

        kodi.paused = False
        scheduler.tick()
        assert kodi.tempo == 1.0
        assert state.syncplay_pulse() == {}
        # Settled, not mistaken for someone else's tempo.
        assert not any("set outside fine sync" in line for line in logged)

    def test_a_command_on_a_paused_player_owes_the_one(self, rig):
        scheduler, controller, kodi = rig
        start_pulse(scheduler, controller, 150.0)
        kodi.paused = True
        scheduler.cancel("Unpause")  # the group's own command, while paused
        assert kodi.tempo != 1.0 and state.syncplay_pulse()

        kodi.paused = False  # the resume lands
        scheduler.settle_owed()  # what SyncPlayManager.on_resumed calls
        assert kodi.tempo == 1.0 and state.syncplay_pulse() == {}

    def test_the_owed_one_survives_a_reset(self, rig):
        """A member that leaves the group while paused resumes its own
        playback; the rate the cut pulse left must not play on."""
        scheduler, controller, kodi = rig
        start_pulse(scheduler, controller, 150.0)
        kodi.paused = True
        scheduler.reset()
        kodi.paused = False
        scheduler.settle_owed()
        assert kodi.tempo == 1.0

    def test_cancel_while_playing_returns_to_one_at_once(self, rig):
        scheduler, controller, kodi = rig
        start_pulse(scheduler, controller, 150.0)
        started = time.time()
        scheduler.cancel("Pause")
        assert time.time() - started < 0.2
        assert scheduler._pulse is None and kodi.tempo == 1.0
        assert state.syncplay_pulse() == {}
        assert scheduler._settle_until > time.time()
        assert len(scheduler._history) == 1

    def test_settle_owed_leaves_a_running_pulse_alone(self, rig):
        scheduler, controller, kodi = rig
        start_pulse(scheduler, controller, 150.0)
        scheduler.settle_owed()
        assert kodi.tempo != 1.0 and scheduler._pulse is not None

    def test_before_seek_returns_to_one_without_waiting(self, rig):
        scheduler, controller, kodi = rig
        start_pulse(scheduler, controller, 150.0)
        kodi.read_lag = 1000  # not read back for a long time: nothing waits
        started = time.time()
        scheduler.before_seek()
        assert time.time() - started < 0.2
        assert kodi.sets[-1] == 1.0
        assert scheduler._pulse is None and scheduler._awaiting is None


class TestKodiMovesTheTempo:
    def test_a_viewers_tempo_is_put_back_once_it_holds(self, rig, clock, logged):
        scheduler, controller, kodi = rig
        scheduler.tick()
        clock.advance(tempo.CONFIRM_GRACE_S)
        kodi.take(1.5)  # the OSD's play speed
        scheduler.tick()
        assert kodi.tempo == 1.5  # not at once
        clock.advance(tempo.FOREIGN_GRACE_S)
        scheduler.tick()
        assert kodi.tempo == 1.0
        kodi.take(1.6)
        clock.advance(tempo.CONFIRM_GRACE_S)
        scheduler.tick()
        clock.advance(tempo.FOREIGN_GRACE_S)
        scheduler.tick()
        assert kodi.tempo == 1.0
        assert sum("set outside fine sync" in line for line in logged) == 1

    def test_a_short_step_is_left_alone_and_settled_after(self, rig, clock):
        """A live gate steps a member through Player.SetTempo: 0.75 s at 1.2x
        is +150 ms, which the scheduler must then find and close."""
        scheduler, controller, kodi = rig
        scheduler.tick()
        clock.advance(tempo.CONFIRM_GRACE_S)
        kodi.take(1.2)
        scheduler.tick()
        assert scheduler._window == []  # nothing measured while it holds
        clock.advance(0.75)
        kodi.take(1.0)
        scheduler.tick()
        assert kodi.sets == []  # kofin asked for nothing
        assert scheduler._settle_until > clock.now

    def test_a_viewers_tempo_mid_pulse_ends_it(self, rig, clock):
        scheduler, controller, kodi = rig
        start_pulse(scheduler, controller, 150.0)
        clock.advance(tempo.CONFIRM_GRACE_S)
        kodi.take(1.5)
        scheduler.tick()
        assert scheduler._pulse is None and kodi.tempo == 1.0

    def test_kodis_own_return_ends_a_pulse(self, rig, clock, logged):
        scheduler, controller, kodi = rig
        start_pulse(scheduler, controller, 150.0)
        clock.advance(tempo.CONFIRM_GRACE_S)
        kodi.take(1.0)
        scheduler.tick()
        assert scheduler._pulse is None and state.syncplay_pulse() == {}
        assert scheduler._edge_pulses == 0  # not live: not the edge
        assert any("Kodi returned to 1.0x; pulse over" in line for line in logged)

    def test_a_read_that_lags_the_ask_is_not_someone_elses(self, rig, clock):
        scheduler, controller, kodi = rig
        start_pulse(scheduler, controller, 2000.0)  # ramped
        writes = scheduler._pulse["writes"]
        clock.now = writes[0][0] + 0.01
        kodi.read_lag = 2
        scheduler.tick()  # the second step goes out
        scheduler.tick()  # reads the first step back: within the grace
        assert scheduler._pulse is not None


class TestLive:
    @pytest.fixture
    def live_rig(self, rig):
        scheduler, controller, kodi = rig
        controller.manager.claim = live_claim()
        scheduler.tick()
        assert scheduler._live
        scheduler._window = []  # the arming tick's sample
        return scheduler, controller, kodi

    def test_a_live_residual_beyond_the_budget_pulses_instead_of_seeking(
        self, live_rig
    ):
        scheduler, controller, kodi = live_rig
        fill_window(scheduler, controller, 15000.0, extra=1)
        assert controller.corrections == 0
        scheduler.tick()
        assert scheduler._pulse["rate"] == pytest.approx(1.0 + tempo.RATE_MAX_DEFAULT)
        assert scheduler._pulse["seconds"] == tempo.PULSE_MAX_S
        assert scheduler.can_close(30_000.0)  # nothing else will

    def test_live_without_the_shared_clock_measures_nothing(self, live_rig):
        scheduler, controller, kodi = live_rig
        controller.on_source_clock = False
        fill_window(scheduler, controller, 500.0, extra=3)
        assert scheduler._awaiting is None and scheduler._pulse is None
        assert scheduler._window == []

    def test_kodi_ending_two_forward_pulses_is_the_live_edge(self, live_rig, clock):
        scheduler, controller, kodi = live_rig
        for count in (1, 2):
            scheduler._settle_until = 0.0
            start_pulse(scheduler, controller, 500.0)
            clock.advance(tempo.CONFIRM_GRACE_S)
            kodi.take(1.0)  # #25: nothing to read for half a second
            scheduler.tick()
            assert scheduler._edge_pulses == count
        assert scheduler._gave_up
        assert controller.manager.mismatch == 0  # the edge is not a fault

    def test_a_fed_pulse_resets_the_edge_count(self, live_rig, clock):
        scheduler, controller, kodi = live_rig
        start_pulse(scheduler, controller, 500.0)
        clock.advance(tempo.CONFIRM_GRACE_S)
        kodi.take(1.0)
        scheduler.tick()
        assert scheduler._edge_pulses == 1

        scheduler._settle_until = 0.0
        start_pulse(scheduler, controller, 500.0)
        finish_pulse(scheduler, clock)
        assert scheduler._edge_pulses == 0 and not scheduler._gave_up

    def test_kodi_ending_a_pulse_back_is_not_the_edge(self, live_rig, clock):
        scheduler, controller, kodi = live_rig
        start_pulse(scheduler, controller, -500.0)
        clock.advance(tempo.CONFIRM_GRACE_S)
        kodi.take(1.0)
        scheduler.tick()
        assert scheduler._edge_pulses == 0

    def test_a_live_item_has_no_end_to_keep_clear_of(self, live_rig):
        scheduler, controller, kodi = live_rig
        controller.remaining = 1.0  # nonsense for a live stream; never asked
        start_pulse(scheduler, controller, 500.0)
        assert scheduler._pulse is not None


class TestGiveUp:
    def _history(self, scheduler, direction, ppm, count=tempo.GIVEUP_PULSES):
        now = time.time()
        scheduler._history = [
            {"direction": direction, "ppm": ppm, "ended_at": now - 1.0}
            for _ in range(count)
        ]

    def test_one_signed_steady_regrowth_gives_up(self, rig):
        scheduler, controller, kodi = rig
        scheduler.tick()
        # 3 % mismatch: ~30 ms regrown per second between pulses, steadily.
        self._history(scheduler, 1, 30000.0)
        assert scheduler._losing(30.0, time.time()) is True
        scheduler._give_up()
        assert scheduler._gave_up is True
        assert controller.manager.mismatch == 1

    def test_a_step_is_not_a_rate(self, rig):
        scheduler, controller, kodi = rig
        scheduler.tick()
        self._history(scheduler, 1, 30000.0)
        # 1 s regrown in ~1 s is a stall or a seek, not a mismatch.
        assert scheduler._losing(1000.0, time.time()) is False

    def test_rates_must_agree(self, rig):
        scheduler, controller, kodi = rig
        scheduler.tick()
        self._history(scheduler, 1, 4000.0)
        scheduler._history[-1]["ppm"] = 40000.0  # one pulse ten times the others
        assert scheduler._losing(30.0, time.time()) is False

    def test_a_command_is_a_fresh_start(self, rig):
        scheduler, controller, kodi = rig
        scheduler.tick()
        self._history(scheduler, 1, 30000.0)
        scheduler._gave_up = True
        scheduler.cancel("Seek")
        assert scheduler._history == [] and scheduler._gave_up is False
        self._history(scheduler, 1, 30000.0)
        scheduler.note_settle()
        assert scheduler._history == []

    def test_slow_regrowth_is_drift_not_mismatch(self, rig):
        scheduler, controller, kodi = rig
        scheduler.tick()
        self._history(scheduler, 1, tempo.GIVEUP_PPM / 2)
        assert scheduler._losing(1.5, time.time()) is False

    def test_alternating_directions_never_give_up(self, rig):
        scheduler, controller, kodi = rig
        scheduler.tick()
        self._history(scheduler, 1, tempo.GIVEUP_PPM * 2)
        scheduler._history[1]["direction"] = -1
        assert scheduler._losing(6.0, time.time()) is False

    def test_too_few_pulses(self, rig):
        scheduler, controller, kodi = rig
        self._history(scheduler, 1, tempo.GIVEUP_PPM * 2, count=1)
        assert scheduler._losing(6.0, time.time()) is False

    def test_given_up_item_is_not_pulsed(self, rig):
        scheduler, controller, kodi = rig
        scheduler.tick()
        scheduler._gave_up = True
        fill_window(scheduler, controller, 150.0, extra=2)
        assert scheduler._pulse is None and kodi.sets == []


class TestReset:
    def test_reset_drops_everything(self, rig):
        scheduler, controller, kodi = rig
        start_pulse(scheduler, controller, 150.0)
        scheduler.reset()
        assert scheduler.profile is None and scheduler._pulse is None
        assert kodi.tempo == 1.0 and state.syncplay_pulse() == {}


# ----------------------------------------------------------------------------
# The session
# ----------------------------------------------------------------------------


def test_session_follows_the_setting(logged):
    FakeAddon.store["syncPlayTempo"] = "true"
    session = TempoSession()
    session.begin()
    assert session.active
    session.begin()  # idempotent
    session.end()
    assert not session.active
    session.end()

    FakeAddon.store["syncPlayTempo"] = "false"
    session.begin()
    assert not session.active
    assert any("fine sync is off in settings" in line for line in logged)


# ----------------------------------------------------------------------------
# The source clock (pvr sync plan P4)
# ----------------------------------------------------------------------------

EPOCH_MS = utils.LIVE_PTS_EPOCH_S * 1000.0
PERIOD_MS = utils.LIVE_PTS_PERIOD_S * 1000.0


class TestSourceClock:
    def test_anchor_sits_a_delay_behind_on_the_epoch(self):
        anchor = utils.live_anchor_ms(42_000_000.0)
        assert anchor == pytest.approx(
            EPOCH_MS + 42_000_000.0 - utils.LIVE_DELAY_S * 1000.0
        )
        assert utils.live_anchored(anchor)
        assert not utils.live_anchored(0.0)
        assert not utils.live_anchored(24 * 3600_000.0)  # a day of session time
        assert not utils.live_anchored(None)

    def test_anchor_wraps_below_the_delay(self):
        # Half a delay into the clock: the anchor sits on the previous cycle.
        reading_ms = utils.LIVE_DELAY_S * 500.0
        anchor = utils.live_anchor_ms(reading_ms)
        assert anchor == pytest.approx(
            EPOCH_MS + PERIOD_MS + reading_ms - utils.LIVE_DELAY_S * 1000.0
        )

    def test_unwrap_places_a_reading_on_the_reference_cycle(self):
        # A member that opened after the wrap reads 3 s while the group sits
        # 2 s before it: the same instant, one cycle apart on the wire.
        reference = EPOCH_MS + PERIOD_MS - 2_000.0
        assert utils.unwrap_live_ms(3_000.0, reference) == pytest.approx(
            EPOCH_MS + PERIOD_MS + 3_000.0
        )
        assert utils.unwrap_live_ms(PERIOD_MS - 2_000.0, EPOCH_MS + 3_000.0) == (
            pytest.approx(EPOCH_MS - 2_000.0)
        )
        # ffmpeg's own unwrap past the period lands in the same place.
        assert utils.unwrap_live_ms(PERIOD_MS + 3_000.0, reference) == pytest.approx(
            EPOCH_MS + PERIOD_MS + 3_000.0
        )
        assert utils.unwrap_live_ms(3_000.0, None) == pytest.approx(EPOCH_MS + 3_000.0)

    def test_time_objects(self):
        assert utils.time_object_ms(
            {"hours": 1, "minutes": 2, "seconds": 3, "milliseconds": 4}
        ) == pytest.approx(3723004.0)
        assert utils.time_object_ms(None) is None
        assert utils.time_object_ms({"hours": "x"}) is None

    def test_the_presentation_offset_is_the_streams_clock_at_time_zero(self):
        def at(ms):
            return utils_time(ms)

        props = {"time": at(120_000), "presentationtime": at(42_000_000)}
        assert utils.presentation_offset_ms(props) == pytest.approx(41_880_000.0)

    def test_a_null_presentationtime_is_no_clock(self):
        # PAPlayer, a PVR-demuxed stream, ffmpegdirect without the offset.
        props = {"time": utils_time(120_000), "presentationtime": None}
        assert utils.presentation_offset_ms(props) is None
        assert utils.presentation_offset_ms(None) is None
        assert utils.presentation_offset_ms({}) is None

    def test_a_transcode_job_clock_is_refused(self):
        # Jellyfin's remux restamps from the job's start: about ten seconds.
        props = {"time": utils_time(22_164), "presentationtime": utils_time(32_164)}
        assert utils.presentation_offset_ms(props) is None
        props = {"time": utils_time(22_164), "presentationtime": utils_time(82_164)}
        assert utils.presentation_offset_ms(props) == pytest.approx(60_000.0)

    def test_claim_is_live(self):
        assert utils.claim_is_live({"Id": "c", "Provider": "pvr.kofin"})
        assert not utils.claim_is_live(
            {"Id": "c", "Provider": "pvr.kofin", "RunTimeTicks": 10}
        )
        assert utils.claim_is_live({"Id": "c", "Runtime": 0, "Live": True})
        # kofin's own claim for an item whose runtime the server does not
        # know is not live: it still seeks.
        assert not utils.claim_is_live({"Id": "c", "Runtime": 0})
        assert not utils.claim_is_live({"Id": "c"})
        assert not utils.claim_is_live(None)


def utils_time(ms):
    seconds, millis = divmod(int(ms), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return {
        "hours": hours,
        "minutes": minutes,
        "seconds": seconds,
        "milliseconds": millis,
    }


def test_arming_on_a_live_play_without_timeshift_tells_the_group(rig):
    scheduler, controller, kodi = rig
    told = []
    controller.manager.notify_live_without_timeshift = lambda: told.append(1)
    controller.manager.claim = claim("ps-live", Live=True, TimeshiftMissing=True)
    scheduler.tick()
    assert told == [1]
    controller.manager.claim = claim("ps-vod")  # not live: nothing to say
    scheduler.tick()
    assert told == [1]


# ----------------------------------------------------------------------------
# Music
# ----------------------------------------------------------------------------


class TestMusicProfile:
    def test_defaults(self):
        profile = tempo.music_profile(2, 0)
        assert profile.name == "music"
        assert profile.rate_min == pytest.approx(tempo.RATE_STEP)
        assert profile.rate_max == pytest.approx(0.01)
        assert profile.end_margin_s == tempo.END_MARGIN_S

    def test_stays_within_the_atempo_threshold(self):
        # Resampled, never stretched: a 1 % threshold keeps music below it.
        assert tempo.music_profile(1, 0).rate_max == pytest.approx(0.009)
        # A threshold of 0 stretches everything; nothing to stay under.
        assert tempo.music_profile(0, 0).rate_max == pytest.approx(0.01)

    def test_unreadable_settings_count_as_kodis_defaults(self):
        from kofin.core import kodirpc

        profile = tempo.music_profile(kodirpc.FAILED, None)
        assert profile.rate_max == pytest.approx(0.01)
        assert profile.end_margin_s == tempo.END_MARGIN_S

    def test_a_crossfade_widens_the_end_margin(self):
        assert tempo.music_profile(2, 5).end_margin_s == tempo.END_MARGIN_S + 5

    def test_music_pulses_are_slight(self):
        profile = tempo.music_profile(2, 0)
        assert plan_pulse(20.0, profile) is None  # inside the 25 ms deadband
        rate, seconds = plan_pulse(30.0, profile)
        assert rate == pytest.approx(1.002) and seconds == pytest.approx(15.0)
        rate, seconds = plan_pulse(-26.0, profile)
        assert rate == pytest.approx(0.998) and seconds == pytest.approx(13.0)
        rate, seconds = plan_pulse(135.0, profile)
        assert rate == pytest.approx(1.009) and seconds == pytest.approx(15.0)
        # Saturated: never above 1 %, never longer than PULSE_MAX_S.
        rate, seconds = plan_pulse(600.0, profile)
        assert rate == pytest.approx(1.01) and seconds == tempo.PULSE_MAX_S
        # Under one ramp step: one write and the return.
        assert tempo.pulse_schedule(1.009, 15.0) == [(0.0, 1.009), (15.0, 1.0)]


@pytest.fixture
def music_rig(rig):
    scheduler, controller, kodi = rig
    controller.audio = True
    kodi.playerid = tempo.MUSIC_PLAYER
    return scheduler, controller, kodi


class TestMusicPulses:
    def test_a_drift_is_closed_by_one_slight_pulse(self, music_rig, clock):
        scheduler, controller, kodi = music_rig
        start_pulse(scheduler, controller, 30.0)
        assert kodi.sets == [1.002]
        finish_pulse(scheduler, clock)
        assert kodi.tempo == 1.0

    def test_one_step_from_one_is_not_confirmed_by_a_stale_read(self, music_rig):
        # 1.001 is a whole step from 1.0: a read of 1.0 must not confirm it.
        scheduler, controller, kodi = music_rig
        kodi.read_lag = 1
        fill_window(scheduler, controller, 26.0)
        scheduler.tick()
        scheduler.tick()  # reads the old 1.0
        assert scheduler._awaiting is not None and scheduler._pulse is None

    def test_a_pulse_ends_before_its_track_does(self, music_rig):
        scheduler, controller, kodi = music_rig
        kodi.settings["musicplayer.crossfade"] = 5
        margin = tempo.SETTLE_S + tempo.END_MARGIN_S + 5
        controller.remaining = (margin + 6.0) * 1000.0
        start_pulse(scheduler, controller, 135.0)  # would want 15 s
        assert scheduler._pulse["seconds"] == pytest.approx(6.0)

    def test_beyond_the_music_budget_a_seek_while_playing(self, music_rig):
        scheduler, controller, kodi = music_rig
        fill_window(scheduler, controller, 800.0, extra=1)
        assert controller.corrections == 1 and kodi.sets == []

    def test_the_resume_is_left_to_pulses_inside_the_budget(self, music_rig):
        scheduler, controller, kodi = music_rig
        assert scheduler.armed()
        assert scheduler.can_close(300.0)
        assert not scheduler.can_close(800.0)
