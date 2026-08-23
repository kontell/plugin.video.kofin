"""Fine sync through inputstream.tempo (lib/kofin/syncplay/tempo.py).

The planner and the state-file parser are pure; the scheduler runs against a
fake controller and a real tempo file in tmp_path, with the add-on's answer
(the ``.state`` line) written by the test — which is exactly the contract the
add-on documents, so a change on either side fails here first.
"""

import json
import os
import time

import pytest

from kofin.core import state
from kofin.syncplay import tempo
from kofin.syncplay.tempo import (
    PulseScheduler,
    TempoFile,
    TempoSession,
    parse_state,
    plan_pulse,
)
from tests.unit.fakes import FakeAddon, FakeWindow


@pytest.fixture(autouse=True)
def kodi_fakes(monkeypatch):
    FakeAddon.store = {}
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    monkeypatch.setattr(tempo, "APPLY_TIMEOUT_S", 0.5)
    monkeypatch.setattr(tempo, "APPLY_POLL_S", 0.005)


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

    def test_displacement_equals_the_residual(self):
        for residual in (80.0, 120.0, 200.0, 290.0, -95.0):
            rate, seconds = plan_pulse(residual)
            assert abs((rate - 1.0) * seconds * 1000.0 - residual) < 1.0

    def test_rate_stays_inside_the_band(self):
        for residual in (76.0, 100.0, 250.0, 300.0, 2000.0):
            rate, seconds = plan_pulse(residual, 0.03)
            assert tempo.RATE_MIN - 1e-9 <= abs(rate - 1.0) <= 0.03 + 1e-9
            assert 0 < seconds <= tempo.PULSE_MAX_S

    def test_budget_saturates_at_max_rate_and_duration(self):
        rate, seconds = plan_pulse(50000.0)
        assert rate == pytest.approx(1.0 + tempo.RATE_MAX_DEFAULT)
        assert seconds == tempo.PULSE_MAX_S
        # The user's ceiling, never above the hard one.
        assert plan_pulse(50000.0, 0.10)[0] == pytest.approx(1.10)
        assert plan_pulse(50000.0, 0.90)[0] == pytest.approx(
            1.0 + tempo.RATE_MAX_CEILING
        )

    def test_large_residual_uses_the_full_rate(self):
        # 2.5 s at 25 %: the ten-second pulse the budget default is sized for.
        rate, seconds = plan_pulse(2500.0)
        assert rate == pytest.approx(1.25) and seconds == pytest.approx(10.0)


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
        # Ramp steps are RAMP_DT apart.
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


class TestStateParsing:
    def test_parses_the_last_line(self):
        text = (
            '{"seq":1,"event":"anchor","tempo":1.0000,"delta_ms":0.0}\n'
            '{"seq":2,"event":"tempo","tempo":1.0300,"content_ms":1.0,'
            '"output_ms":1.0,"delta_ms":12.5,"queue_secs":1.00,"video":true}\n'
        )
        parsed = parse_state(text)
        assert parsed["seq"] == 2 and parsed["tempo"] == 1.03
        assert parsed["video"] is True

    def test_garbage_is_none(self):
        assert parse_state("") is None
        assert parse_state("not json") is None
        assert parse_state("[1,2]") is None
        assert parse_state('{"tempo": 1.0}') is None  # no seq: not a state line

    def test_addon_floor(self):
        assert tempo.addon_is_recent("22.4.1") and tempo.addon_is_recent("21.4.1")
        assert tempo.addon_is_recent("22.5.0") and tempo.addon_is_recent("23.4.1")
        assert not tempo.addon_is_recent("22.4.0") and not tempo.addon_is_recent(
            "22.3.11"
        )
        assert not tempo.addon_is_recent("") and not tempo.addon_is_recent("x")

    def test_regrowth(self):
        assert tempo.regrowth_ppm(50.0, 5.0) == pytest.approx(10000.0)
        assert tempo.regrowth_ppm(50.0, 0.0) == 0.0

    def test_head_delta_prefers_the_counters(self):
        # delta_ms is the reported Δ, a queue behind; the counters are the head.
        line = {"content_ms": 5150.0, "output_ms": 5000.0, "delta_ms": 120.0}
        assert tempo.head_delta(line) == pytest.approx(150.0)
        assert (
            tempo.head_delta({"content_ms": -1.0, "output_ms": -1.0, "delta_ms": 120.0})
            == 120.0
        )
        assert tempo.head_delta({}) == 0.0


# ----------------------------------------------------------------------------
# The tempo file and the add-on's answer
# ----------------------------------------------------------------------------


class FakeAddonSide:
    """Plays inputstream.tempo: answers a tempo write with a state line."""

    def __init__(self, tempo_file, delta_ms=0.0):
        self.file = tempo_file
        self.seq = 0
        self.delta_ms = delta_ms
        self.applied = []

    def rate_written(self):
        with open(self.file.path) as handle:
            return float(handle.read().strip())

    def answer(self, event="tempo", delta_ms=None):
        if delta_ms is not None:
            self.delta_ms = delta_ms
        self.seq += 1
        rate = self.rate_written()
        self.applied.append(rate)
        with open(self.file.state_path, "w") as handle:
            handle.write(
                json.dumps(
                    {
                        "seq": self.seq,
                        "event": event,
                        "tempo": rate,
                        "content_ms": 100000.0 + self.delta_ms,
                        "output_ms": 100000.0,
                        "delta_ms": self.delta_ms,
                        "queue_secs": 1.0,
                        "video": True,
                    }
                )
                + "\n"
            )


def test_tempo_file_round_trip(tmp_path):
    f = TempoFile(str(tmp_path / "tempo"))
    f.write(1.03)
    assert open(f.path).read() == "1.0300\n"
    assert not os.path.exists(f.path + ".tmp")
    assert f.read_state() is None and f.current_seq() == 0

    side = FakeAddonSide(f)
    side.answer()
    assert f.current_seq() == 1
    assert f.wait_applied(1.03, 0, timeout_s=0.2)["seq"] == 1
    assert f.wait_applied(1.03, 1, timeout_s=0.05) is None  # no newer line
    assert f.wait_applied(1.0, 0, timeout_s=0.05) is None  # wrong rate

    f.reset()
    assert open(f.path).read() == "1.0000\n"
    assert f.read_state() is None


# ----------------------------------------------------------------------------
# The scheduler
# ----------------------------------------------------------------------------


class FakeManager:
    def __init__(self):
        self.claim = None
        self.mismatch = 0

    def current_claim(self):
        return self.claim

    def notify_rate_mismatch(self):
        self.mismatch += 1


class FakeController:
    def __init__(self):
        self.manager = FakeManager()
        self.paused = False
        self.playing = True
        self.group_ms = 100000.0
        self.local_ms = 100000.0
        self.corrections = 0

    def _is_paused(self):
        return self.paused

    def reference_is_playing(self):
        return self.playing

    def estimate_position_ms(self):
        return self.group_ms

    def _position_ms(self):
        return self.local_ms

    def correct_position(self):
        self.corrections += 1
        self.local_ms = self.group_ms


def routed_claim(path, session="ps-1"):
    return {
        "Id": "item-1",
        "PlaySessionId": session,
        "Tempo": {"File": path, "QueueSecs": 1.0},
    }


@pytest.fixture
def rig(tmp_path):
    controller = FakeController()
    path = str(tmp_path / "tempo")
    TempoFile(path).reset()
    controller.manager.claim = routed_claim(path)
    scheduler = PulseScheduler(controller)
    side = FakeAddonSide(TempoFile(path))
    return scheduler, controller, side


def fill_window(scheduler, controller, residual_ms, extra=0):
    controller.local_ms = controller.group_ms - residual_ms
    for _ in range(tempo.WINDOW_SAMPLES - 1 + extra):
        scheduler.tick()


def start_pulse(scheduler, controller, side, residual_ms):
    """Fill the window, then tick once with the add-on answering the write."""
    fill_window(scheduler, controller, residual_ms)
    original = TempoFile.wait_applied

    def answering(self, rate, after_seq, timeout_s=tempo.APPLY_TIMEOUT_S):
        side.answer()
        return original(self, rate, after_seq, timeout_s)

    TempoFile.wait_applied = answering
    try:
        scheduler.tick()
    finally:
        TempoFile.wait_applied = original


class TestScheduler:
    def test_arms_from_the_claim(self, rig):
        scheduler, controller, side = rig
        scheduler.tick()
        assert scheduler.file is not None
        assert scheduler.queue_secs == 1.0

    def test_unrouted_item_is_left_alone(self, rig):
        scheduler, controller, side = rig
        controller.manager.claim = {"Id": "x", "PlaySessionId": "ps-2"}
        fill_window(scheduler, controller, 200.0, extra=4)
        assert scheduler.file is None
        assert side.rate_written() == 1.0

    def test_inside_the_deadband_nothing_happens(self, rig):
        scheduler, controller, side = rig
        fill_window(scheduler, controller, 30.0, extra=4)
        assert scheduler._pulse is None
        assert side.rate_written() == 1.0

    def test_window_must_fill_before_acting(self, rig):
        scheduler, controller, side = rig
        fill_window(scheduler, controller, 150.0)  # one short of the window
        assert scheduler._pulse is None

    def test_pulse_is_written_confirmed_and_ended(self, rig, monkeypatch):
        scheduler, controller, side = rig
        start_pulse(scheduler, controller, side, 150.0)
        pulse = scheduler._pulse
        assert pulse is not None
        rate, seconds = plan_pulse(150.0)
        assert side.applied[-1] == pytest.approx(rate)
        assert pulse["seconds"] == pytest.approx(seconds)

        # Not over yet: nothing changes.
        scheduler.tick()
        assert scheduler._pulse is pulse

        # Time's up: the add-on confirms 1.0 and the quiet window starts.
        end_at = pulse["writes"][-1][0]
        monkeypatch.setattr(tempo.time, "time", lambda: end_at + 0.01)
        original = TempoFile.wait_applied

        def answering(self, r, after_seq, timeout_s=tempo.APPLY_TIMEOUT_S):
            side.answer(delta_ms=150.0)
            return original(self, r, after_seq, timeout_s)

        monkeypatch.setattr(TempoFile, "wait_applied", answering)
        scheduler.tick()
        assert scheduler._pulse is None
        assert side.applied[-1] == 1.0
        assert scheduler._settle_until == pytest.approx(
            end_at + 0.01 + scheduler.queue_secs + tempo.SETTLE_EXTRA_S
        )
        assert len(scheduler._history) == 1

    def test_unanswered_write_turns_fine_sync_off_for_the_item(self, rig):
        scheduler, controller, side = rig
        fill_window(scheduler, controller, 150.0)
        scheduler.tick()  # the add-on never answers (APPLY_TIMEOUT_S is short)
        assert scheduler._pulse is None
        assert scheduler.file is None
        assert side.rate_written() == 1.0  # not left running

    def test_settle_window_blocks_measurement(self, rig):
        scheduler, controller, side = rig
        scheduler.tick()
        scheduler.note_settle()
        fill_window(scheduler, controller, 200.0, extra=4)
        assert scheduler._window == []
        assert scheduler._pulse is None

    def test_paused_or_stopped_group_clears_the_window(self, rig):
        scheduler, controller, side = rig
        fill_window(scheduler, controller, 200.0)
        controller.paused = True
        scheduler.tick()
        assert scheduler._window == []
        controller.paused = False
        controller.playing = False
        fill_window(scheduler, controller, 200.0)
        scheduler.tick()
        assert scheduler._window == []

    def test_cancel_returns_to_one_and_settles(self, rig):
        scheduler, controller, side = rig
        start_pulse(scheduler, controller, side, 150.0)
        assert side.rate_written() > 1.0
        scheduler.cancel("Pause")
        assert scheduler._pulse is None
        assert side.rate_written() == 1.0
        assert scheduler._settle_until > time.time()
        assert len(scheduler._history) == 1

    def test_before_seek_waits_for_the_return_to_one(self, rig, monkeypatch):
        scheduler, controller, side = rig
        start_pulse(scheduler, controller, side, 150.0)
        original = TempoFile.wait_applied
        seen = []

        def answering(self, rate, after_seq, timeout_s=tempo.APPLY_TIMEOUT_S):
            seen.append((rate, timeout_s))
            side.answer()
            return original(self, rate, after_seq, timeout_s)

        monkeypatch.setattr(TempoFile, "wait_applied", answering)
        scheduler.before_seek()
        assert seen == [(1.0, 1.0)]
        assert scheduler._pulse is None
        assert side.rate_written() == 1.0

    def test_residual_beyond_the_budget_seeks(self, rig):
        scheduler, controller, side = rig
        fill_window(scheduler, controller, 3000.0, extra=1)
        assert controller.corrections == 1
        assert side.rate_written() == 1.0
        assert scheduler._seek_blackout_until > time.time()
        # The blackout holds a second seek off — and what the seek left behind
        # is closed by a saturated pulse instead of waited out.
        scheduler._settle_until = 0.0
        start_pulse(scheduler, controller, side, 3000.0)
        assert controller.corrections == 1
        assert scheduler._pulse is not None
        # Ramped: the first write is the first step, the plan is the ceiling.
        assert side.applied[-1] == pytest.approx(1.0 + tempo.RAMP_STEP)
        assert scheduler._pulse["rate"] == pytest.approx(1.0 + tempo.RATE_MAX_DEFAULT)
        assert scheduler._pulse["seconds"] == tempo.PULSE_MAX_S

    def test_mixed_window_pulses_instead_of_seeking(self, rig, monkeypatch):
        scheduler, controller, side = rig
        # Eleven samples beyond the budget, one inside: no seek — and no
        # waiting either, a saturated pulse takes it.
        fill_window(scheduler, controller, 3000.0)
        controller.local_ms = controller.group_ms - 100.0
        original = TempoFile.wait_applied

        def answering(self, rate, after_seq, timeout_s=tempo.APPLY_TIMEOUT_S):
            side.answer()
            return original(self, rate, after_seq, timeout_s)

        monkeypatch.setattr(TempoFile, "wait_applied", answering)
        scheduler.tick()
        assert controller.corrections == 0
        assert scheduler._pulse is not None
        assert scheduler._pulse["rate"] == pytest.approx(1.0 + tempo.RATE_MAX_DEFAULT)

    def test_new_session_rearms(self, rig, tmp_path):
        scheduler, controller, side = rig
        scheduler.tick()
        scheduler._gave_up = True
        other = str(tmp_path / "other")
        TempoFile(other).reset()
        controller.manager.claim = routed_claim(other, session="ps-2")
        scheduler.tick()
        assert scheduler._gave_up is False
        assert scheduler.file.path == other

    def test_reset_drops_everything(self, rig):
        scheduler, controller, side = rig
        start_pulse(scheduler, controller, side, 150.0)
        scheduler.reset()
        assert scheduler.file is None and scheduler._pulse is None
        assert side.rate_written() == 1.0


class TestGiveUp:
    def _history(self, scheduler, direction, ppm, count=tempo.GIVEUP_PULSES):
        now = time.time()
        scheduler._history = [
            {"direction": direction, "ppm": ppm, "ended_at": now - 1.0}
            for _ in range(count)
        ]

    def test_one_signed_steady_regrowth_gives_up(self, rig):
        scheduler, controller, side = rig
        scheduler.tick()
        # 3 % mismatch: ~30 ms regrown per second between pulses, steadily.
        self._history(scheduler, 1, 30000.0)
        assert scheduler._losing(30.0, time.time()) is True
        scheduler._give_up()
        assert scheduler._gave_up is True
        assert controller.manager.mismatch == 1

    def test_a_step_is_not_a_rate(self, rig):
        scheduler, controller, side = rig
        scheduler.tick()
        self._history(scheduler, 1, 30000.0)
        # 1 s regrown in ~1 s is a stall or a seek, not a mismatch.
        assert scheduler._losing(1000.0, time.time()) is False

    def test_rates_must_agree(self, rig):
        scheduler, controller, side = rig
        scheduler.tick()
        self._history(scheduler, 1, 4000.0)
        scheduler._history[-1]["ppm"] = 40000.0  # one pulse ten times the others
        assert scheduler._losing(30.0, time.time()) is False

    def test_a_command_is_a_fresh_start(self, rig):
        scheduler, controller, side = rig
        scheduler.tick()
        self._history(scheduler, 1, 30000.0)
        scheduler._gave_up = True
        scheduler.cancel("Seek")
        assert scheduler._history == [] and scheduler._gave_up is False
        self._history(scheduler, 1, 30000.0)
        scheduler.note_settle()
        assert scheduler._history == []

    def test_slow_regrowth_is_drift_not_mismatch(self, rig):
        scheduler, controller, side = rig
        scheduler.tick()
        self._history(scheduler, 1, tempo.GIVEUP_PPM / 2)
        assert scheduler._losing(1.5, time.time()) is False

    def test_alternating_directions_never_give_up(self, rig):
        scheduler, controller, side = rig
        scheduler.tick()
        self._history(scheduler, 1, tempo.GIVEUP_PPM * 2)
        scheduler._history[1]["direction"] = -1
        assert scheduler._losing(6.0, time.time()) is False

    def test_too_few_pulses(self, rig):
        scheduler, controller, side = rig
        self._history(scheduler, 1, tempo.GIVEUP_PPM * 2, count=1)
        assert scheduler._losing(6.0, time.time()) is False

    def test_given_up_item_is_not_pulsed(self, rig):
        scheduler, controller, side = rig
        scheduler.tick()
        scheduler._gave_up = True
        fill_window(scheduler, controller, 150.0, extra=2)
        assert scheduler._pulse is None
        assert side.rate_written() == 1.0


# ----------------------------------------------------------------------------
# The session: arming at join, disarming at leave
# ----------------------------------------------------------------------------


class RpcStub:
    """Kodi's settings and add-on list over JSON-RPC, as the session sees them."""

    def __init__(self, settings=None, addon=None):
        self.settings = dict(settings or {})
        self.addon = addon  # None = not installed; else {"enabled": bool}
        self.writes = []

    def __call__(self, query):
        request = json.loads(query)
        method = request["method"]
        params = request.get("params") or {}
        if method == "Settings.GetSettingValue":
            name = params["setting"]
            if name not in self.settings:
                return json.dumps({"error": {"code": -32602}})
            return json.dumps({"result": {"value": self.settings[name]}})
        if method == "Settings.SetSettingValue":
            self.settings[params["setting"]] = params["value"]
            self.writes.append((params["setting"], params["value"]))
            return json.dumps({"result": True})
        if method == "Addons.GetAddonDetails":
            if self.addon is None:
                return json.dumps({"error": {"code": -32602}})
            addon = {"addonid": params["addonid"], "version": "22.4.1", **self.addon}
            return json.dumps({"result": {"addon": addon}})
        raise AssertionError(method)


@pytest.fixture
def session_env(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "xbmcvfs.translatePath", lambda path: str(tmp_path / os.path.basename(path))
    )
    FakeAddon.store["syncPlayTempo"] = "true"
    FakeAddon.store["syncPlayShortQueue"] = "true"

    def install(rpc):
        monkeypatch.setattr("xbmc.executeJSONRPC", rpc)
        return rpc

    return install


def test_session_arms_on_piers_and_shortens_the_queue(session_env):
    rpc = session_env(
        RpcStub(
            {"videoplayer.queuetimesize": 40, "audiooutput.passthrough": False},
            {"enabled": True},
        )
    )
    session = TempoSession()
    session.begin()
    assert session.active
    published = state.syncplay_tempo()
    assert published["queue_secs"] == 1.0
    assert open(published["file"]).read() == "1.0000\n"
    assert rpc.writes == [("videoplayer.queuetimesize", 10)]
    assert FakeAddon.store["syncPlayQueueRestore"] == "40"

    session.end()
    assert not session.active
    assert state.syncplay_tempo() == {}
    assert rpc.settings["videoplayer.queuetimesize"] == 40
    assert FakeAddon.store["syncPlayQueueRestore"] == ""


def test_session_on_omega_keeps_the_fixed_queue(session_env):
    rpc = session_env(RpcStub({"audiooutput.passthrough": False}, {"enabled": True}))
    session = TempoSession()
    session.begin()
    assert session.active
    assert state.syncplay_tempo()["queue_secs"] == tempo.OMEGA_QUEUE_SECS
    assert rpc.writes == []


def test_session_respects_a_queue_already_short(session_env):
    rpc = session_env(
        RpcStub(
            {"videoplayer.queuetimesize": 5, "audiooutput.passthrough": False},
            {"enabled": True},
        )
    )
    TempoSession().begin()
    assert state.syncplay_tempo()["queue_secs"] == 0.5
    assert rpc.writes == []


def test_session_leaves_the_queue_when_told_to(session_env):
    FakeAddon.store["syncPlayShortQueue"] = "false"
    rpc = session_env(
        RpcStub(
            {"videoplayer.queuetimesize": 40, "audiooutput.passthrough": False},
            {"enabled": True},
        )
    )
    TempoSession().begin()
    assert state.syncplay_tempo()["queue_secs"] == 4.0
    assert rpc.writes == []


@pytest.mark.parametrize(
    "addon, setting",
    [
        (None, "true"),  # not installed
        ({"enabled": False}, "true"),  # disabled
        ({"enabled": True}, "false"),  # feature off
        ({"enabled": True, "version": "22.4.0"}, "true"),  # drops post-seek packets
    ],
)
def test_session_does_not_arm(session_env, addon, setting):
    FakeAddon.store["syncPlayTempo"] = setting
    rpc = session_env(RpcStub({"videoplayer.queuetimesize": 40}, addon))
    session = TempoSession()
    session.begin()
    assert not session.active
    assert state.syncplay_tempo() == {}
    assert rpc.writes == []


def test_session_arms_with_passthrough_on(session_env):
    # Passthrough is suspended for the session, and the help text says so;
    # there is no second toggle.
    session_env(
        RpcStub(
            {"videoplayer.queuetimesize": 40, "audiooutput.passthrough": True},
            {"enabled": True},
        )
    )
    session = TempoSession()
    session.begin()
    assert session.active


def test_session_leaves_the_queue_when_the_record_cannot_be_kept(
    session_env, monkeypatch
):
    rpc = session_env(RpcStub({"videoplayer.queuetimesize": 40}, {"enabled": True}))
    monkeypatch.setattr("kofin.core.settings.set_str", lambda key, value: None)
    TempoSession().begin()
    assert rpc.writes == []  # not shortened: nobody could have restored it
    assert state.syncplay_tempo()["queue_secs"] == 4.0


def test_scheduler_reads_the_sliders(rig):
    scheduler, controller, side = rig
    FakeAddon.store["syncPlayPulseBudget"] = "1200"
    FakeAddon.store["syncPlayMaxRate"] = "10"
    scheduler.tick()
    assert scheduler.budget_ms == 1200.0 and scheduler.rate_max == pytest.approx(0.10)
    assert scheduler.can_close(1000.0) and not scheduler.can_close(1500.0)
    scheduler.file = None
    assert not scheduler.can_close(100.0)


def test_restore_queue_after_an_interrupted_session(session_env):
    rpc = session_env(RpcStub({"videoplayer.queuetimesize": 10}, {"enabled": True}))
    FakeAddon.store["syncPlayQueueRestore"] = "40"
    assert tempo.restore_queue() is True
    assert rpc.settings["videoplayer.queuetimesize"] == 40
    assert FakeAddon.store["syncPlayQueueRestore"] == ""
    # Nothing recorded: nothing to do.
    assert tempo.restore_queue() is False
    # Garbage is discarded rather than retried forever.
    FakeAddon.store["syncPlayQueueRestore"] = "forty"
    assert tempo.restore_queue() is False
    assert FakeAddon.store["syncPlayQueueRestore"] == ""
