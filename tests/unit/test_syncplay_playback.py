"""Command execution and correction behaviour (SYNCPLAY.md §5.1, §7, §10).

Ported from the fork; the JSON-RPC seam is kofin's module-level ``_rpc``
and settings come from the FakeAddon store. New kofin coverage at the end:
the play-path re-target (plugin URLs, never resolved paths — plan §2)."""

from contextlib import contextmanager

import pytest

import kofin.syncplay.playback as playback_module
from kofin.syncplay import utils
from kofin.syncplay.playback import PlaybackController
from tests.unit.fakes import FakeAddon, FakeWindow


class FakePlayer:
    current = None  # so the patched Player.Paused condition can see it

    def __init__(self):
        self.playing = True
        self.paused = False
        self.position = 0.0
        self.total = 0.0
        self.audio = False  # PAPlayer semantics when True
        self.broken_clock = False  # getTime() raises (gapless swap window)
        self.clock_advances = False  # getTime() moves while unpaused
        self._reads = 0
        self.actions = []
        FakePlayer.current = self

    def isPlaying(self):
        return self.playing

    def isPlayingAudio(self):
        return self.playing and self.audio

    def getTime(self):
        if not self.playing or self.broken_clock:
            raise RuntimeError("not playing")

        if self.clock_advances and not self.paused:
            self._reads += 1

        return self.position + self._reads * 0.2

    def getTotalTime(self):
        if not self.playing:
            raise RuntimeError("not playing")

        return self.total

    def pause(self):
        self.paused = not self.paused
        self.actions.append("pause")

    def seekTime(self, seconds):
        self.position = seconds
        self._reads = 0
        self.actions.append(("seek", seconds))

        if self.audio and self.paused:
            # PAPlayer::SeekTime() restores playback speed, silently
            # resuming a paused player.
            self.paused = False

    def stop(self):
        self.playing = False
        self.actions.append("stop")

    def play(self, item=None, listitem=None, windowed=False, startpos=-1):
        self.playing = True
        self.actions.append(("play", item, startpos))


class FakeManager:
    def __init__(self, player):
        self.player = player
        self.phase = "synced"
        self.ignore_wait = False
        self.offset = 0.0
        self.reports = []
        self.report_positions = []
        self.unpaused = False
        self.stopped = False

    def in_group(self):
        return True

    def offset_ms(self):
        return self.offset

    def server_now_ms(self):
        return utils.local_ms() + self.offset

    def server_now_iso(self):
        return utils.to_iso(self.server_now_ms())

    @contextmanager
    def programmatic(self):
        yield

    def is_transcoding(self):
        return False

    def post_report(self, kind, position_s=None):
        self.reports.append(kind)
        self.report_positions.append(position_s)

    def on_local_unpaused(self):
        self.unpaused = True

    def on_group_stopped(self):
        self.stopped = True


@pytest.fixture(autouse=True)
def kodi_fakes(monkeypatch):
    FakeAddon.store = {}
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)


@pytest.fixture(autouse=True)
def _player_conditions(monkeypatch):
    """Kodistubs' getCondVisibility is a stub; wire Player.Paused to the
    fake player so pause toggling behaves like the real player."""

    def cond(condition):
        player = FakePlayer.current

        if condition == "Player.Paused" and player is not None:
            return player.paused

        return False

    monkeypatch.setattr(playback_module.xbmc, "getCondVisibility", cond)


def make_controller(paused=False, position=0.0):
    player = FakePlayer()
    player.paused = paused
    player.position = position
    manager = FakeManager(player)
    controller = PlaybackController(manager, player)
    return controller, manager, player


def command(name, when_delta_ms, ticks=0):
    return {
        "Command": name,
        "When": utils.to_iso(utils.local_ms() + when_delta_ms),
        "EmittedAt": utils.to_iso(utils.local_ms()),
        "PositionTicks": ticks,
        "PlaylistItemId": "pl-1",
    }


class TestScheduling:
    def test_future_command_is_timed_not_executed(self):
        controller, manager, player = make_controller(paused=True)
        controller.schedule(command("Unpause", 2000))

        assert controller._timer is not None
        assert player.actions == []  # nothing executed yet
        controller.cancel_pending()

    def test_new_command_cancels_pending(self):
        controller, manager, player = make_controller(paused=True)
        controller.schedule(command("Unpause", 2000))
        first_timer = controller._timer
        controller.schedule(command("Pause", 2000, ticks=0))

        assert controller._timer is not first_timer
        controller.cancel_pending()


class TestUnpause:
    def test_on_time_unpause_resumes(self):
        controller, manager, player = make_controller(paused=True, position=10.0)
        # Position matches the command: no seek expected.
        controller.schedule(command("Unpause", -10, ticks=utils.seconds_to_ticks(10)))

        assert not player.paused
        assert manager.unpaused
        assert ("seek", 10.0) not in player.actions

    def test_late_unpause_extrapolates(self):
        controller, manager, player = make_controller(paused=True, position=10.0)
        # Command was scheduled 5s ago for position 10s: live position 15s.
        controller.schedule(command("Unpause", -5000, ticks=utils.seconds_to_ticks(10)))

        assert not player.paused
        seeks = [a for a in player.actions if isinstance(a, tuple) and a[0] == "seek"]
        assert seeks, "late unpause must jump to the extrapolated position"
        assert abs(seeks[0][1] - 15.0) < 0.5

    def test_reference_set(self):
        controller, manager, player = make_controller(paused=True, position=10.0)
        controller.schedule(command("Unpause", -10, ticks=utils.seconds_to_ticks(10)))

        estimate = controller.estimate_position_ms()
        assert estimate is not None
        assert abs(estimate - 10000.0) < 500


class TestPause:
    def test_pause_lands_on_command_position(self):
        controller, manager, player = make_controller(paused=False, position=12.0)
        controller.schedule(command("Pause", -10, ticks=utils.seconds_to_ticks(10)))

        assert player.paused
        seeks = [a for a in player.actions if isinstance(a, tuple) and a[0] == "seek"]
        assert seeks and abs(seeks[0][1] - 10.0) < 0.01

    def test_pause_within_tolerance_no_seek(self):
        controller, manager, player = make_controller(paused=False, position=10.1)
        controller.schedule(command("Pause", -10, ticks=utils.seconds_to_ticks(10)))

        assert player.paused
        assert not [
            a for a in player.actions if isinstance(a, tuple) and a[0] == "seek"
        ]


class TestSeekAndStop:
    def test_seek_reports_ready(self):
        controller, manager, player = make_controller(paused=False, position=0.0)
        controller.schedule(command("Seek", -10, ticks=utils.seconds_to_ticks(42)))

        assert player.paused  # seek holds until the group resumes
        assert abs(player.position - 42.0) < 0.5
        assert "syncplay_ready" in manager.reports

    def test_stop(self):
        controller, manager, player = make_controller()
        controller.schedule(command("Stop", -10))

        assert not player.playing
        assert manager.stopped

    def test_stop_spares_detached_spectators_media(self):
        # A group Stop must not kill playback SyncPlay is not driving
        # (a detached spectator watching their own thing).
        controller, manager, player = make_controller()
        manager.phase = "idle"

        controller.schedule(command("Stop", -10))

        assert player.playing
        assert manager.stopped


class TestSeekWhilePaused:
    """PAPlayer::SeekTime() silently resumes a paused music player
    (VideoPlayer does not); every seek that expects to stay paused must
    detect and undo that."""

    def test_audio_seek_repauses(self):
        controller, manager, player = make_controller(paused=True, position=18.9)
        player.audio = True

        controller._seek_and_settle(0.0)

        assert player.paused is True  # the forced resume was undone
        assert player.position == 0.0
        assert player.actions.count("pause") == 1

    def test_video_seek_stays_paused_without_repause(self):
        controller, manager, player = make_controller(paused=True, position=18.9)

        controller._seek_and_settle(0.0)

        assert player.paused is True
        assert player.actions.count("pause") == 0

    def test_playing_seek_is_left_playing(self):
        controller, manager, player = make_controller(paused=False, position=18.9)
        player.audio = True

        controller._seek_and_settle(0.0)

        assert player.paused is False
        assert player.actions.count("pause") == 0

    def test_do_pause_on_audio_never_seeks(self):
        # A paused PAPlayer must not be seeked (it queues the seek and
        # swallows the resume toggle on some builds, self-resumes on
        # others): the Pause leaves the position for the Unpause to fix.
        controller, manager, player = make_controller(paused=False, position=5.5)
        player.audio = True

        controller.schedule(command("Pause", -10, ticks=int(3.0 * 10000000)))

        assert player.paused is True
        assert not [
            a for a in player.actions if isinstance(a, tuple) and a[0] == "seek"
        ]
        assert player.position == 5.5

    def test_unpause_on_audio_resumes_before_aligning(self):
        controller, manager, player = make_controller(paused=True, position=18.9)
        player.audio = True
        player.clock_advances = True

        controller.schedule(command("Unpause", -10, ticks=0))

        assert player.paused is False
        assert abs(player.position) < 0.5  # aligned after resuming
        seeks = [a for a in player.actions if isinstance(a, tuple) and a[0] == "seek"]
        assert seeks and seeks[0][1] < 0.5
        # Resume first, then seek: never seek while paused.
        assert player.actions.index("pause") < player.actions.index(seeks[0])

    def test_unpause_retries_a_swallowed_toggle(self):
        # Fork field log 2026-07-10: a paused-at-boundary PAPlayer can
        # swallow the pause toggle; the resume must nudge again until
        # the clock demonstrably moves.
        controller, manager, player = make_controller(paused=True, position=18.9)
        player.audio = True
        player.clock_advances = True
        real_pause = player.pause
        calls = {"n": 0}

        def flaky_pause():
            calls["n"] += 1

            if calls["n"] == 1:
                player.actions.append("pause-swallowed")
                return

            real_pause()

        player.pause = flaky_pause

        assert controller._resume_with_retries() is True
        assert calls["n"] >= 2
        assert player.paused is False

    def test_unpause_gives_up_when_jammed(self, monkeypatch):
        controller, manager, player = make_controller(paused=True, position=5.0)
        player.audio = True
        player.broken_clock = True  # every read reports no media
        monkeypatch.setattr(utils, "UNPAUSE_RETRY_WINDOW_MS", 150.0)

        assert controller._resume_with_retries() is False
        assert "pause" in player.actions  # it nudged despite the reads

    def test_unpause_defers_while_loading(self):
        controller, manager, player = make_controller(paused=True, position=0.0)
        manager.phase = "loading"

        controller.schedule(command("Unpause", -10, ticks=0))

        assert player.actions == []
        assert not manager.unpaused

    def test_unpause_ignored_when_idle(self):
        controller, manager, player = make_controller(paused=True, position=0.0)
        manager.phase = "idle"

        controller.schedule(command("Unpause", -10, ticks=0))

        assert player.actions == []
        assert not manager.unpaused

    def test_group_seek_on_paused_audio_is_deferred(self):
        # The Unpause that follows a group Seek carries the position;
        # promise it in the ready report instead of seeking while paused.
        controller, manager, player = make_controller(paused=True, position=5.0)
        player.audio = True

        controller.schedule(command("Seek", -10, ticks=utils.seconds_to_ticks(42)))

        assert player.paused is True
        assert not [
            a for a in player.actions if isinstance(a, tuple) and a[0] == "seek"
        ]
        assert "syncplay_ready" in manager.reports
        assert manager.report_positions[-1] == 42.0


class TestStartHoldGates:
    """The hold must be able to pause and align during the gapless swap
    window, when getTime() can misbehave (fork field log: align/pause were
    silently skipped and the pause only landed at adopt time)."""

    def test_ensure_paused_survives_a_broken_clock(self):
        controller, manager, player = make_controller()
        player.broken_clock = True

        controller.ensure_paused()

        assert player.paused is True

    def test_prepare_ready_defers_alignment_on_paused_audio(self):
        # The player clock can still read the previous track when a held
        # transition is adopted; a paused PAPlayer must not be seeked, so
        # the ready goes out as-is and the Unpause aligns on resume.
        controller, manager, player = make_controller(paused=True, position=18.9)
        player.audio = True
        controller._jsonrpc = lambda method, params=None: {}
        controller.set_reference(0, utils.local_ms(), False)

        controller.prepare_ready()

        assert not [
            a for a in player.actions if isinstance(a, tuple) and a[0] == "seek"
        ]
        assert player.paused is True
        assert "syncplay_ready" in manager.reports

    def test_prepare_ready_still_aligns_paused_video(self):
        controller, manager, player = make_controller(paused=True, position=18.9)
        controller._jsonrpc = lambda method, params=None: {}
        controller.set_reference(0, utils.local_ms(), False)

        controller.prepare_ready()

        assert abs(player.position) < 0.1
        assert player.paused is True
        assert "syncplay_ready" in manager.reports


class TestAudioTempo:
    def test_no_tempo_without_a_video_player(self):
        controller, manager, player = make_controller()
        responses = {
            "Player.GetActivePlayers": {"result": [{"type": "audio", "playerid": 0}]},
            "Settings.GetSettingValue": {"result": {"value": True}},
        }
        controller._jsonrpc = lambda method, params=None: responses.get(method, {})

        controller._detect_player_features()

        assert controller._can_tempo is False


class TestBufferingWatch:
    def test_caching_debounce_and_recovery(self, monkeypatch):
        controller, manager, player = make_controller(paused=False, position=5.0)
        controller.last_command = {"Command": "Unpause"}

        caching = {"value": True}
        monkeypatch.setattr(
            playback_module.xbmc,
            "getCondVisibility",
            lambda cond: caching["value"] if cond == "Player.Caching" else False,
        )

        # First sighting: starts the debounce clock, no report yet.
        controller._watch_buffering()
        assert manager.reports == []

        # Simulate the debounce period elapsing.
        controller._caching_since -= utils.BUFFERING_DEBOUNCE + 0.1
        controller._watch_buffering()
        assert manager.reports == ["syncplay_buffering"]

        # Recovery: caching clears -> Ready, even if a Pause arrived.
        caching["value"] = False
        controller.last_command = {"Command": "Pause"}
        controller._watch_buffering()
        assert manager.reports == ["syncplay_buffering", "syncplay_ready"]

    def test_no_reports_when_not_expected_to_play(self, monkeypatch):
        controller, manager, player = make_controller()
        controller.last_command = {"Command": "Pause"}

        monkeypatch.setattr(
            playback_module.xbmc, "getCondVisibility", lambda cond: True
        )

        controller._watch_buffering()
        controller._watch_buffering()
        assert manager.reports == []

    def test_buffering_backs_off_correction(self, monkeypatch):
        # A sustained speed-up starves a streamed buffer; when buffering is
        # reported, corrections must disengage and black out, or the group
        # ping-pongs Playing/Waiting forever.
        controller, manager, player = make_controller(paused=False, position=5.0)
        controller.last_command = {"Command": "Unpause"}
        controller._correcting = True

        monkeypatch.setattr(
            playback_module.xbmc,
            "getCondVisibility",
            lambda cond: cond == "Player.Caching",
        )

        controller._watch_buffering()
        controller._caching_since -= utils.BUFFERING_DEBOUNCE + 0.1
        controller._watch_buffering()

        assert manager.reports == ["syncplay_buffering"]
        assert controller._correcting is False
        assert controller._drift_blackout_until > 0.0


class TestDriftIsRateOnly:
    """Drift correction is rate-only and side-effect-free: it must never seek
    and never touch the group's buffering state. A drift-loop seek jumps the
    video; a drift-loop Buffering report pauses the whole group (the source of
    the Playing/Waiting ping-pong)."""

    def _drift(self, can_tempo, ahead_ms):
        FakeAddon.store["syncPlayDriftCorrection"] = "true"
        controller, manager, player = make_controller(position=0.0)
        controller._can_tempo = can_tempo
        controller.last_command = {"Command": "Unpause"}
        # Reference puts the group `ahead_ms` in front of our position (0).
        controller.set_reference(
            utils.ms_to_ticks(ahead_ms), manager.server_now_ms(), True
        )
        controller._correct_drift()
        return controller, manager, player

    def test_gross_drift_never_seeks_or_reports(self):
        # 5s out — far past the tempo band — must be tolerated silently.
        controller, manager, player = self._drift(True, 5000)
        assert not any(isinstance(a, tuple) and a[0] == "seek" for a in player.actions)
        assert manager.reports == []
        assert controller._applied_tempo == 1.0

    def test_no_tempo_control_never_seeks_or_reports(self):
        controller, manager, player = self._drift(False, 5000)
        assert not any(isinstance(a, tuple) and a[0] == "seek" for a in player.actions)
        assert manager.reports == []

    def test_small_drift_nudges_tempo_only(self):
        # ~0.6s behind: within the band -> gentle speed-up, nothing else.
        controller, manager, player = self._drift(True, 600)
        assert controller._applied_tempo > 1.0
        assert not any(isinstance(a, tuple) and a[0] == "seek" for a in player.actions)
        assert manager.reports == []

    def test_drift_correction_off_tolerates(self):
        # syncPlayDriftCorrection=false: never tempo-corrects (S4.3).
        FakeAddon.store["syncPlayDriftCorrection"] = "false"
        controller, manager, player = make_controller(position=0.0)
        controller._can_tempo = True
        controller.last_command = {"Command": "Unpause"}
        controller.set_reference(utils.ms_to_ticks(600), manager.server_now_ms(), True)

        controller._correct_drift()

        assert controller._applied_tempo == 1.0
        assert controller._correcting is False


class RpcRecorder:
    """Stub for the module-level _rpc seam in playback.py.

    Records (method, params) calls and returns a canned response per
    method (default {} — an empty, error-free result).
    """

    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    def __call__(self, method, params=None):
        self.calls.append((method, params))
        return self._responses.get(method, {})


class TestTempoDetection:
    """Player.SetTempo is available only when Kodi's 'Sync playback to
    display' (videoplayer.usedisplayasclock) is enabled; detection reads
    that setting rather than a (nonexistent) player property."""

    def _patch(self, monkeypatch, responses):
        rpc = RpcRecorder(responses)
        monkeypatch.setattr(playback_module, "_rpc", rpc)
        return rpc

    def test_reads_display_clock_setting(self, monkeypatch):
        controller, _, _ = make_controller()
        rpc = self._patch(
            monkeypatch,
            {
                "Player.GetActivePlayers": {
                    "result": [{"type": "video", "playerid": 1}]
                },
                "Settings.GetSettingValue": {"result": {"value": True}},
            },
        )
        controller._detect_player_features()

        assert controller._can_tempo is True
        methods = [m for m, _ in rpc.calls]
        assert "Settings.GetSettingValue" in methods
        # There is no player property for tempo capability.
        assert "Player.GetProperties" not in methods
        setting = next(p for m, p in rpc.calls if m == "Settings.GetSettingValue")
        assert setting == {"setting": "videoplayer.usedisplayasclock"}

    def test_tempo_disabled_when_setting_off(self, monkeypatch):
        controller, _, _ = make_controller()
        self._patch(
            monkeypatch,
            {
                "Player.GetActivePlayers": {
                    "result": [{"type": "video", "playerid": 1}]
                },
                "Settings.GetSettingValue": {"result": {"value": False}},
            },
        )
        controller._detect_player_features()
        assert controller._can_tempo is False

    def test_tempo_disabled_when_setting_unreadable(self, monkeypatch):
        controller, _, _ = make_controller()
        self._patch(
            monkeypatch,
            {
                "Player.GetActivePlayers": {
                    "result": [{"type": "video", "playerid": 1}]
                },
                "Settings.GetSettingValue": {"error": {"code": -32602}},
            },
        )
        controller._detect_player_features()
        assert controller._can_tempo is False

    def test_settempo_rejection_is_a_backstop(self, monkeypatch):
        # Setting reads on, but a live SetTempo is rejected (e.g. realtime
        # stream): tempo is disabled and drift is tolerated from then on.
        controller, _, _ = make_controller()
        controller._can_tempo = True
        controller._applied_tempo = 1.0
        self._patch(monkeypatch, {"Player.SetTempo": {"error": {"code": -32100}}})

        controller._apply_tempo(1.05)
        assert controller._can_tempo is False
        assert controller._applied_tempo == 1.0  # not recorded as applied

    def test_settempo_applied_when_supported(self, monkeypatch):
        controller, _, _ = make_controller()
        controller._can_tempo = True
        controller._applied_tempo = 1.0
        rpc = self._patch(monkeypatch, {"Player.SetTempo": {"result": {"tempo": 1.03}}})

        controller._apply_tempo(1.03)
        assert controller._applied_tempo == 1.03
        assert ("Player.SetTempo", {"playerid": 1, "tempo": 1.03}) in rpc.calls

    def test_tempo_changes_are_rate_limited(self, monkeypatch):
        # Constant re-triggering of the skin speed indicator is what the
        # rate limit prevents: a second *change* soon after is suppressed,
        # but returning to 1.0x is never throttled.
        controller, _, _ = make_controller()
        controller._can_tempo = True
        controller._applied_tempo = 1.0
        self._patch(monkeypatch, {"Player.SetTempo": {"result": {}}})

        controller._apply_tempo(1.03)
        assert controller._applied_tempo == 1.03

        controller._apply_tempo(1.02)  # immediate second change: throttled
        assert controller._applied_tempo == 1.03

        controller._apply_tempo(1.0)  # restore is always allowed
        assert controller._applied_tempo == 1.0

    def test_engage_ms_follows_tolerance_setting(self):
        controller, _, _ = make_controller()
        FakeAddon.store["syncPlayTolerance"] = "150"
        assert controller._engage_ms() == 150.0
        FakeAddon.store["syncPlayTolerance"] = "700"
        assert controller._engage_ms() == 700.0
        FakeAddon.store["syncPlayTolerance"] = "0"  # unset: fork default band
        assert controller._engage_ms() == utils.CORRECTION_ENGAGE_MS

    def test_back_off_drops_speedup_and_blacks_out(self, monkeypatch):
        controller, _, _ = make_controller()
        controller._can_tempo = True
        controller._applied_tempo = 1.03
        controller._correcting = True
        self._patch(monkeypatch, {"Player.SetTempo": {"result": {}}})

        controller._back_off_correction(1000.0)

        assert controller._applied_tempo == 1.0
        assert controller._correcting is False
        assert (
            controller._drift_blackout_until
            == 1000.0 + utils.DRIFT_BLACKOUT_AFTER_GIVEUP
        )

    def test_restore_arms_skip_watch_engage_does_not(self, monkeypatch):
        controller, _, _ = make_controller(position=50.0)
        controller._can_tempo = True
        self._patch(monkeypatch, {"Player.SetTempo": {"result": {}}})

        # Engaging (1.0 -> 1.03) does not arm the restore-skip watch.
        controller._apply_tempo(1.03)
        assert controller._tempo_restore_watch is None

        # Restoring (1.03 -> 1.0) arms it with the pre-restore position.
        controller._apply_tempo(1.0)
        assert controller._tempo_restore_watch is not None
        assert controller._tempo_restore_watch[0] == 50000.0

    def test_detect_respects_tempo_causes_skip(self, monkeypatch):
        controller, _, _ = make_controller()
        controller._tempo_causes_skip = True  # a prior item proved it skips
        self._patch(
            monkeypatch,
            {
                "Player.GetActivePlayers": {
                    "result": [{"type": "video", "playerid": 1}]
                },
                "Settings.GetSettingValue": {"result": {"value": True}},
            },
        )

        controller._detect_player_features()
        # Setting says tempo is available, but we keep it off for this player.
        assert controller._can_tempo is False


class TestTempoRestoreSkip:
    """A player that seeks when tempo returns to 1.0 (a Kodi bug fixed in v22)
    is detected from the position jump and stops using tempo for the session,
    so the drift loop just tolerates the offset instead of glitching."""

    def test_forward_skip_disables_tempo(self):
        controller, _, player = make_controller(position=100.0)
        controller._can_tempo = True
        # Restored ~1.2s ago from position 100s; player leapt to 105s.
        controller._tempo_restore_watch = (100000.0, utils.local_ms() - 1200.0)
        player.position = 105.0

        controller._verify_tempo_restore()

        assert controller._tempo_causes_skip is True
        assert controller._can_tempo is False
        assert controller._tempo_restore_watch is None

    def test_normal_playback_keeps_tempo(self):
        controller, _, player = make_controller(position=100.0)
        controller._can_tempo = True
        controller._tempo_restore_watch = (100000.0, utils.local_ms() - 1200.0)
        player.position = 101.2  # advanced ~1.2s at 1.0x, no skip

        controller._verify_tempo_restore()

        assert controller._tempo_causes_skip is False
        assert controller._can_tempo is True
        assert controller._tempo_restore_watch is None

    def test_waits_for_settle_before_judging(self):
        controller, _, player = make_controller(position=100.0)
        controller._can_tempo = True
        controller._tempo_restore_watch = (100000.0, utils.local_ms() - 200.0)
        player.position = 110.0  # a jump, but only 200ms in — too soon

        controller._verify_tempo_restore()

        assert controller._can_tempo is True
        assert controller._tempo_restore_watch is not None  # still pending


class FakePlaylist:
    instances = {}

    def __init__(self, playlist_type):
        # One playlist object per type per test (Kodi semantics).
        existing = FakePlaylist.instances.get(playlist_type)
        if existing is not None:
            self.__dict__ = existing.__dict__
            return
        self.type = playlist_type
        self.entries = []
        self.cleared = 0
        FakePlaylist.instances[playlist_type] = self

    def clear(self):
        self.cleared += 1
        self.entries = []

    def add(self, url, *args, **kwargs):
        self.entries.append(url)


class TestPlayPathRetarget:
    """The one substantive transplant change (plan §2): a group play goes
    through kofin's plugin play path — a plugin:// URL naming the id and
    start position — never a resolved stream path."""

    @pytest.fixture(autouse=True)
    def _playlist(self, monkeypatch):
        FakePlaylist.instances = {}
        monkeypatch.setattr(playback_module.xbmc, "PlayList", FakePlaylist)

    def test_group_play_resolves_through_plugin_url(self):
        import xbmc

        controller, manager, player = make_controller()
        player.playing = False
        start_ticks = utils.seconds_to_ticks(90)

        controller.play_item({"Id": "item-1", "Type": "Movie"}, start_ticks)

        playlist = FakePlaylist.instances[xbmc.PLAYLIST_VIDEO]
        assert playlist.cleared == 1
        assert len(playlist.entries) == 1
        url = playlist.entries[0]
        assert url.startswith("plugin://plugin.video.kofin/")
        assert "mode=play" in url
        assert "id=item-1" in url
        assert "startticks=%d" % start_ticks in url
        plays = [a for a in player.actions if isinstance(a, tuple) and a[0] == "play"]
        assert plays and plays[0][2] == 0  # startpos 0

    def test_zero_start_omits_startticks_and_stops_current(self):
        import xbmc

        controller, manager, player = make_controller()
        player.playing = True

        controller.play_item({"Id": "item-2", "Type": "Episode"}, 0)

        playlist = FakePlaylist.instances[xbmc.PLAYLIST_VIDEO]
        assert "startticks" not in playlist.entries[0]
        assert "stop" in player.actions  # the previous item was torn down

    def test_audio_items_use_the_music_playlist(self):
        import xbmc

        controller, manager, player = make_controller()
        player.playing = False

        controller.play_item({"Id": "song-1", "Type": "Audio"}, 0)

        assert xbmc.PLAYLIST_MUSIC in FakePlaylist.instances
        assert xbmc.PLAYLIST_VIDEO not in FakePlaylist.instances

    def test_item_without_id_raises(self):
        controller, manager, player = make_controller()

        with pytest.raises(ValueError):
            controller.play_item({"Type": "Movie"}, 0)
