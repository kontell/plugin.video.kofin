"""Command execution and correction behaviour (SYNCPLAY.md §5.1, §7, §10).

Ported from the fork; the JSON-RPC seam is kofin's module-level ``_rpc``
and settings come from the FakeAddon store. New kofin coverage at the end:
the play-path re-target (plugin URLs, never resolved paths — plan §2)."""

import json
import threading
from contextlib import contextmanager

import pytest

import kofin.syncplay.playback as playback_module
from kofin.syncplay import providers as providers_module
from kofin.syncplay import utils
from kofin.syncplay.playback import PlaybackController
from tests.unit.fakes import FakeAddon, FakeWindow, player_ops_rpc


class FakePlayer:
    current = None  # so the patched Player.Paused condition can see it

    def __init__(self):
        self.playing = True
        self._paused = False
        # xbmc.Player.pause() posts TMSG_MEDIA_PAUSE and returns; the state a
        # caller reads immediately afterwards is still the old one. With
        # pause_latency=N the next N reads see the stale value before the
        # toggle lands, which is the window a "toggle if it reads paused"
        # resume falls into. 0 keeps the simple synchronous model.
        self.pause_latency = 0
        self._pending_paused = None
        self._stale_reads = 0
        self.position = 0.0
        self.total = 0.0
        self.audio = False  # PAPlayer semantics when True
        self.resumes_on_seek = False  # Android VideoPlayer semantics
        self.broken_clock = False  # getTime() raises (gapless swap window)
        # A playing player advances, and the controller proves a resume
        # took by sampling the position twice -- so a frozen clock reads as
        # a failure to start. Tests wanting a jammed player set this False.
        self.clock_advances = True
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

    @property
    def paused(self):
        if self._stale_reads > 0:
            self._stale_reads -= 1
            return self._paused

        if self._pending_paused is not None:
            self._paused = self._pending_paused
            self._pending_paused = None

        return self._paused

    @paused.setter
    def paused(self, value):
        self._paused = bool(value)
        self._pending_paused = None
        self._stale_reads = 0

    def pause(self):
        target = not self.paused

        if self.pause_latency:
            self._pending_paused = target
            self._stale_reads = self.pause_latency
        else:
            self._paused = target

        self.actions.append("pause")

    def seekTime(self, seconds):
        self.position = seconds
        self._reads = 0
        self.actions.append(("seek", seconds))

        if (self.audio or self.resumes_on_seek) and self._paused:
            # PAPlayer::SeekTime() restores playback speed, silently resuming
            # a paused player; Android's VideoPlayer was measured doing the
            # same, which resumes_on_seek stands in for.
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
        self.transcoding = False
        self.reloads = 0
        self.claim = None
        self.posted = []

    def in_group(self):
        return True

    def _post(self, func, *args):
        self.posted.append(func)
        func(*args)

    def current_claim(self):
        return self.claim

    def offset_ms(self):
        return self.offset

    def server_now_ms(self):
        return utils.local_ms() + self.offset

    def server_now_iso(self):
        return utils.to_iso(self.server_now_ms())

    @contextmanager
    def programmatic(self):
        yield

    def post_report(self, kind, position_s=None):
        self.reports.append(kind)
        self.report_positions.append(position_s)

    def on_local_unpaused(self):
        self.unpaused = True

    def on_group_stopped(self):
        self.stopped = True

    def is_transcoding(self):
        return self.transcoding

    def reload_current_item(self):
        self.reloads += 1


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


@pytest.fixture(autouse=True)
def _player_ops_over_jsonrpc(monkeypatch):
    """Stops and resumes leave through JSON-RPC, not the player bindings
    (issue #155 for the stop, kodirpc.resume_player for the resume). Wired
    back to the fake player so the observable behaviour is unchanged.
    """
    monkeypatch.setattr(
        "xbmc.executeJSONRPC", player_ops_rpc(lambda: FakePlayer.current)
    )


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

    def test_the_same_command_is_carried_out_once(self):
        """The server re-issues the transport command when the last member
        reports ready, so the member that reloaded to serve a seek is handed
        that seek again as it comes back."""
        controller, manager, player = make_controller(position=10.0)
        seek = command("Seek", -10, ticks=utils.seconds_to_ticks(200))

        controller.schedule(seek)
        after_first = list(player.actions)
        assert after_first, "the first delivery must be carried out"

        controller.schedule(dict(seek))

        assert player.actions == after_first

    def test_a_second_command_of_the_same_kind_is_not_a_repeat(self):
        controller, manager, player = make_controller(position=10.0)

        controller.schedule(command("Seek", -10, ticks=utils.seconds_to_ticks(200)))
        after_first = len(player.actions)
        controller.schedule(command("Seek", -10, ticks=utils.seconds_to_ticks(400)))

        assert len(player.actions) > after_first

    def test_a_repeat_is_allowed_again_once_the_group_playback_ends(self):
        controller, manager, player = make_controller(position=10.0)
        seek = command("Seek", -10, ticks=utils.seconds_to_ticks(200))

        controller.schedule(seek)
        after_first = len(player.actions)

        controller.last_command = None  # what stop_loop and _detach_playback do
        controller.schedule(dict(seek))

        assert len(player.actions) > after_first


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


class TestUnpauseAlignment:
    """A scheduled Unpause names the exact group position: line up at arm
    time (video), inside the scheduling lead, so every start has
    initial-start tightness — barrier restarts and hot joins alike."""

    def seeks(self, player):
        return [a for a in player.actions if isinstance(a, tuple) and a[0] == "seek"]

    def test_prealigns_at_arm_time(self):
        controller, manager, player = make_controller(paused=True, position=10.0)
        controller.schedule(
            command("Unpause", 1000, ticks=utils.seconds_to_ticks(11.0))
        )

        assert self.seeks(player) and abs(self.seeks(player)[0][1] - 11.0) < 0.01
        assert player.paused  # still armed; a paused video seek stays paused
        controller.cancel_pending()

    def test_no_seek_within_the_band(self):
        controller, manager, player = make_controller(paused=True, position=10.05)
        controller.schedule(
            command("Unpause", 1000, ticks=utils.seconds_to_ticks(10.0))
        )

        assert self.seeks(player) == []
        controller.cancel_pending()

    def test_audio_is_never_prealigned(self):
        controller, manager, player = make_controller(paused=True, position=10.0)
        player.audio = True
        controller.schedule(
            command("Unpause", 1000, ticks=utils.seconds_to_ticks(15.0))
        )

        assert self.seeks(player) == []
        controller.cancel_pending()

    def test_fire_time_catch_all_uses_tight_band(self):
        controller, manager, player = make_controller(paused=True, position=10.0)
        # 300ms off used to pass through (< the 1500ms drift threshold); a
        # start must align on the tight band instead.
        controller.schedule(command("Unpause", -10, ticks=utils.seconds_to_ticks(10.3)))

        assert self.seeks(player) and abs(self.seeks(player)[0][1] - 10.3) < 0.05
        assert not player.paused


class TestPause:
    def test_pause_lands_on_command_position(self):
        controller, manager, player = make_controller(paused=False, position=12.0)
        controller.schedule(command("Pause", -10, ticks=utils.seconds_to_ticks(10)))

        assert player.paused
        seeks = [a for a in player.actions if isinstance(a, tuple) and a[0] == "seek"]
        assert seeks and abs(seeks[0][1] - 10.0) < 0.01

    def test_pause_waits_for_the_player_lock(self):
        # Y1: schedule() arms the fire-time timer and then pre-aligns on the
        # dispatcher holding _player_lock, so a timer-thread Pause landing
        # mid-align must queue behind it, not interleave with its seek.
        controller, manager, player = make_controller(paused=False, position=12.0)
        done = threading.Event()

        def pause():
            controller._do_pause(utils.seconds_to_ticks(10))
            done.set()

        with controller._player_lock:  # the pre-align holds it
            worker = threading.Thread(target=pause)
            worker.start()
            assert not done.wait(0.15)  # the Pause is waiting, not pausing
            assert "pause" not in player.actions

        assert done.wait(2.0)  # released: the Pause goes through
        worker.join(2.0)
        assert player.paused

    def test_pause_within_tolerance_no_seek(self):
        controller, manager, player = make_controller(paused=False, position=10.1)
        player.clock_advances = False  # a still clock isolates the band itself
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


class TestUnpauseIsByIntent:
    """A group Unpause must end with the player *playing*, whatever the seek
    that preceded it did to the pause state.

    Measured failure it guards (docs/syncplay-drift-shakedown.md §11): the
    align seek resumed the player, the settle re-paused it, and the toggle that
    was meant to start it read the not-yet-applied pause as "playing" and did
    nothing — so the member sat still while the group played on, and with no
    drift loop nothing ever revisited it.
    """

    def test_unpause_resumes_even_when_the_seek_resumed_the_player(self):
        controller, manager, player = make_controller(paused=True, position=0.0)
        player.resumes_on_seek = True  # Android VideoPlayer
        player.pause_latency = 1  # the re-pause lands after the state read
        controller.schedule(command("Unpause", -10, ticks=utils.seconds_to_ticks(42)))

        assert player.paused is False  # playing, not stranded
        # The post-resume align aims ahead by the seek cost (fake clock: never
        # measured, so the default stands).
        assert player.position == pytest.approx(
            42 + controller.seek_lag_ms / 1000.0, abs=0.2
        )

    def test_align_seek_does_not_re_pause_when_a_resume_follows(self):
        controller, manager, player = make_controller(paused=True, position=0.0)
        player.resumes_on_seek = True
        player.pause_latency = 1
        controller.schedule(command("Unpause", -10, ticks=utils.seconds_to_ticks(42)))

        # One pause toggle in the whole sequence would mean the settle re-paused
        # and something had to undo it; the fixed path never re-pauses at all.
        assert "pause" not in player.actions
        assert player.paused is False

    def test_a_seek_that_must_stay_paused_still_re_pauses(self):
        controller, manager, player = make_controller(paused=True, position=0.0)
        player.resumes_on_seek = True

        with manager.programmatic():
            controller._seek_and_settle(5000.0)  # default: stay paused

        assert player.paused is True


class TestTranscodedSeekReloads:
    """A group Seek on a transcoding member reloads the item rather than
    seeking inside the stream.

    The reason is timing, not accuracy: a transcode seek makes the server
    restart the encode, so the position does not move for seconds and any
    residual read after a normal settle is the pre-seek position. Measured
    live, seeking first and deciding on the result reported the whole 60 s
    seek distance as the residual and made the fallback reload land 10.7 s
    from the group instead of 0.9 s (PR #208, reverted). The playback.py
    comment carries the numbers.
    """

    def test_transcoding_reloads_at_the_target(self):
        controller, manager, player = make_controller(position=10.0)
        manager.transcoding = True

        controller.schedule(command("Seek", -10, ticks=utils.seconds_to_ticks(1200)))

        assert manager.reloads == 1
        assert not [a for a in player.actions if a[0] == "seek"]
        # The reload reports Ready through the load flow, not from here.
        assert manager.reports == []

    def test_transcoding_unpause_carries_the_offset(self):
        # The fire-time align has the same problem as the arm-time one: on a
        # transcode the seek snaps to a segment, so aligning a member that was
        # 224 ms out left it 4.2 s out (measured). Resume where it is instead.
        controller, manager, player = make_controller(paused=True, position=10.0)
        manager.transcoding = True

        controller.schedule(command("Unpause", -10, ticks=utils.seconds_to_ticks(12)))

        assert player.paused is False  # still resumed
        assert not [
            a for a in player.actions if isinstance(a, tuple) and a[0] == "seek"
        ]

    def test_direct_play_still_seeks_in_place(self):
        controller, manager, player = make_controller(position=10.0)
        manager.transcoding = False

        controller.schedule(command("Seek", -10, ticks=utils.seconds_to_ticks(1200)))

        assert manager.reloads == 0
        assert player.position == pytest.approx(1200, abs=0.2)
        assert manager.reports == ["syncplay_ready"]


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
    start position — never a resolved stream path.

    Driven end to end through the G1 provider seam: the Jellyfin provider
    builds the target, the controller plays it, and together they must
    reproduce the pre-seam behaviour byte for byte. (The old
    item-without-an-Id guard is gone with the item dict: the URL is built
    from the queue key, which a queue entry always carries; an item the
    lookup cannot produce is the provider's LookupError, tested in
    test_syncplay_providers.py.)"""

    @pytest.fixture(autouse=True)
    def _playlist(self, monkeypatch):
        FakePlaylist.instances = {}
        monkeypatch.setattr(playback_module.xbmc, "PlayList", FakePlaylist)

    @staticmethod
    def target(item, key, start_ticks):
        class Api:
            def item(self, item_id):
                return item

        return providers_module.JellyfinProvider(Api()).play_target(key, start_ticks)

    def test_group_play_resolves_through_plugin_url(self):
        import xbmc

        controller, manager, player = make_controller()
        player.playing = False
        start_ticks = utils.seconds_to_ticks(90)

        controller.play_item(self.target({"Type": "Movie"}, "item-1", start_ticks))

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

    def test_zero_start_sends_startticks_and_stops_current(self):
        # It used to be omitted. A group start naming position zero must still
        # say so: with no startticks the play route falls back to this member's
        # own resume point, which is how a follower ended up 290 s from the
        # group (docs/syncplay-drift-shakedown.md §11).
        import xbmc

        controller, manager, player = make_controller()
        player.playing = True

        controller.play_item(self.target({"Type": "Episode"}, "item-2", 0))

        playlist = FakePlaylist.instances[xbmc.PLAYLIST_VIDEO]
        assert "startticks=0" in playlist.entries[0]
        assert "stop" in player.actions  # the previous item was torn down

    def test_negative_estimate_is_clamped_not_passed_through(self):
        # A group start's estimate can land fractionally below zero across a
        # clock offset; it reached the play route as startticks=-240000, which
        # the route cannot use, so the member fell back to its own resume point
        # and started 290 s from the group (docs/syncplay-drift-shakedown.md).
        import xbmc

        controller, manager, player = make_controller()
        controller.play_item(self.target({"Type": "Movie"}, "abc", -240000))

        url = FakePlaylist.instances[xbmc.PLAYLIST_VIDEO].entries[0]
        assert "startticks=0" in url
        assert "startticks=-" not in url

    def test_audio_items_use_the_music_playlist(self):
        import xbmc

        controller, manager, player = make_controller()
        player.playing = False

        controller.play_item(self.target({"Type": "Audio"}, "song-1", 0))

        assert xbmc.PLAYLIST_MUSIC in FakePlaylist.instances
        assert xbmc.PLAYLIST_VIDEO not in FakePlaylist.instances


class TestAlignAfterResume:
    """The resume opens a gap the pre-resume alignment cannot see.

    Measured on two devices from one command they agreed on to within 3ms: the
    resume landed 82ms after the fire on one and 1202ms after it on the other,
    four captures running. The alignment happens before the resume, so by the
    time there is a picture it describes a position the player has left -- and
    nothing looked again. That is the whole of why a seek holds sync and a
    resume does not: a seek names a position, so arriving late costs nothing.

    These tests model the resume *taking time*, because a gap that already
    exists when the command arrives is closed by the pre-resume align and
    proves nothing about this one.
    """

    def seeks(self, player):
        return [a for a in player.actions if isinstance(a, tuple) and a[0] == "seek"]

    @staticmethod
    def _slow_resume(controller, manager, monkeypatch, resume_ms):
        """Make the resume cost `resume_ms`, during which the group moves on."""
        clock = [utils.local_ms()]
        monkeypatch.setattr(utils, "local_ms", lambda: clock[0])

        def slow():
            clock[0] += resume_ms  # the group keeps playing meanwhile
            return True

        monkeypatch.setattr(controller, "_resume_and_verify", slow)
        return clock

    def test_a_gap_opened_by_a_slow_resume_is_closed(self, monkeypatch):
        controller, manager, player = make_controller(paused=True, position=100.0)
        manager.phase = "synced"
        now = manager.server_now_ms()
        # In position when the command lands: the pre-resume align has nothing
        # to do, so any seek here belongs to the new code.
        controller.set_reference(utils.ms_to_ticks(100000), now, True)
        self._slow_resume(controller, manager, monkeypatch, 1200.0)

        controller._do_unpause(utils.ms_to_ticks(100000), now)

        assert self.seeks(player), "a 1.2s resume delay was left uncorrected"
        # Where the group will be once the seek has landed, not where it was.
        assert self.seeks(player)[-1][1] == pytest.approx(
            101.2 + controller.seek_lag_ms / 1000.0, abs=0.2
        )

    def test_a_prompt_resume_is_left_alone(self, monkeypatch):
        controller, manager, player = make_controller(paused=True, position=100.0)
        manager.phase = "synced"
        now = manager.server_now_ms()
        controller.set_reference(utils.ms_to_ticks(100000), now, True)
        self._slow_resume(controller, manager, monkeypatch, 30.0)

        controller._do_unpause(utils.ms_to_ticks(100000), now)

        # Inside the band (POST_RESUME_ALIGN_MS): a correction here would cost
        # a visible jump to fix less than the jump itself. Deliberately well
        # under the threshold rather than just under it, so retuning the
        # constant does not silently turn this into a different test.
        assert not self.seeks(player)

    def test_a_transcode_is_never_seeked_after_the_resume(self, monkeypatch):
        controller, manager, player = make_controller(paused=True, position=100.0)
        manager.phase = "synced"
        manager.transcoding = True
        now = manager.server_now_ms()
        controller.set_reference(utils.ms_to_ticks(100000), now, True)
        self._slow_resume(controller, manager, monkeypatch, 1200.0)

        controller._do_unpause(utils.ms_to_ticks(100000), now)

        # A transcode seek snaps to a segment boundary and can widen the gap it
        # was sent to close; carrying is the established policy.
        assert not self.seeks(player)


# ----------------------------------------------------------------------------
# Live items on the source clock (pvr sync plan P4)
# ----------------------------------------------------------------------------

EPOCH_MS = utils.LIVE_PTS_EPOCH_S * 1000.0
PERIOD_MS = utils.LIVE_PTS_PERIOD_S * 1000.0


def write_state(tmp_path, source_ms, player_ms):
    path = str(tmp_path / "tempo")
    with open(path + ".state", "w") as handle:
        handle.write(
            json.dumps(
                {
                    "seq": 3,
                    "event": "anchor",
                    "tempo": 1.0,
                    "content_ms": player_ms,
                    "output_ms": player_ms,
                    "delta_ms": 0.0,
                    "queue_secs": 1.0,
                    "video": True,
                    "source_ms": source_ms,
                    "player_ms": player_ms,
                }
            )
            + "\n"
        )
    return path


def live_claim(path=None):
    claim = {"Id": "chan-1", "Provider": "pvr.kofin", "PlaySessionId": "ps-l"}
    if path:
        claim["Tempo"] = {"File": path, "QueueSecs": 1.0}
    return claim


class TestLiveClock:
    def test_position_reads_the_source_clock(self, tmp_path):
        controller, manager, player = make_controller(position=100.0)
        player.clock_advances = False
        # The head sits at 42 000 s on the broadcast's clock while the
        # player clock reads 120 s for it: the offset is the difference.
        manager.claim = live_claim(write_state(tmp_path, 42_000_000.0, 120_000.0))

        assert controller._position_ms() == pytest.approx(
            EPOCH_MS + 100_000.0 + 42_000_000.0 - 120_000.0
        )

        # A group on the next cycle of the clock: the reading follows it.
        controller._reference = (EPOCH_MS + PERIOD_MS + 41_990_000.0, None, True)
        assert controller._position_ms() == pytest.approx(
            EPOCH_MS + PERIOD_MS + 100_000.0 + 42_000_000.0 - 120_000.0
        )
        assert controller.live_on_source_clock()

    def test_position_falls_back_to_the_player_clock(self, tmp_path):
        controller, manager, player = make_controller(position=100.0)
        player.clock_advances = False

        manager.claim = live_claim()  # no tempo route
        assert controller._position_ms() == pytest.approx(100_000.0)

        manager.claim = live_claim(str(tmp_path / "missing"))  # no state yet
        assert controller._position_ms() == pytest.approx(100_000.0)
        assert controller.live_anchor_ms() is None

        # An item that is not live keeps the player clock whatever its route.
        manager.claim = {
            "Id": "m1",
            "Runtime": 10,
            "Tempo": {"File": write_state(tmp_path, 42_000_000.0, 120_000.0)},
        }
        assert controller._position_ms() == pytest.approx(100_000.0)

    def test_the_shared_clock_needs_an_anchored_group(self, tmp_path):
        controller, manager, player = make_controller(position=100.0)
        player.clock_advances = False
        manager.claim = live_claim(write_state(tmp_path, 42_000_000.0, 120_000.0))

        controller._reference = (30_000.0, None, True)  # a proposer's session time
        assert not controller.live_on_source_clock()
        # What this member reports then is the group's own position.
        assert controller.reported_position_s() == pytest.approx(30.0)

        controller._reference = (EPOCH_MS + 41_990_000.0, None, True)
        assert controller.live_on_source_clock()
        assert controller.reported_position_s() == pytest.approx(
            (EPOCH_MS + 41_990_000.0) / 1000.0
        )

    def test_a_live_member_ahead_holds_for_the_group(self, tmp_path, monkeypatch):
        controller, manager, player = make_controller(position=100.0)
        player.clock_advances = False
        manager.claim = live_claim(write_state(tmp_path, 42_000_000.0, 120_000.0))
        # The group sits 8 s behind this member's reading.
        controller._reference = (
            EPOCH_MS + 100_000.0 + 42_000_000.0 - 120_000.0 - 8_000.0,
            None,
            True,
        )
        timers = []
        monkeypatch.setattr(
            playback_module.utils, "later", lambda s, f, *a: timers.append((s, f, a))
        )

        controller._align_after_resume()

        assert player.paused
        assert controller._live_hold_ms == pytest.approx(8_000.0)
        assert timers and timers[0][0] == pytest.approx(8.0)
        assert not [
            a for a in player.actions if isinstance(a, tuple) and a[0] == "seek"
        ]

        timers[0][1](*timers[0][2])  # the hold ends
        assert not player.paused
        assert controller._live_hold_ms is None

    def test_a_superseded_hold_timer_resumes_nothing(self, tmp_path, monkeypatch):
        controller, manager, player = make_controller(position=100.0)
        player.clock_advances = False
        manager.claim = live_claim(write_state(tmp_path, 42_000_000.0, 120_000.0))
        controller._reference = (
            EPOCH_MS + 100_000.0 + 42_000_000.0 - 120_000.0 - 8_000.0,
            None,
            True,
        )
        timers = []
        monkeypatch.setattr(
            playback_module.utils, "later", lambda s, f, *a: timers.append((s, f, a))
        )

        controller._align_after_resume()
        # A second Unpause lands during the hold: the player resumes and the
        # residual is measured again, starting a second hold.
        player.pause()
        controller._align_after_resume()
        assert player.paused
        assert len(timers) == 2

        timers[0][1](*timers[0][2])  # the first hold's timer: stale
        assert player.paused
        timers[1][1](*timers[1][2])  # the second hold's timer
        assert not player.paused

    def test_a_live_member_behind_is_left_to_fine_sync(self, tmp_path, monkeypatch):
        controller, manager, player = make_controller(position=100.0)
        player.clock_advances = False
        manager.claim = live_claim(write_state(tmp_path, 42_000_000.0, 120_000.0))
        controller._reference = (
            EPOCH_MS + 100_000.0 + 42_000_000.0 - 120_000.0 + 8_000.0,
            None,
            True,
        )
        monkeypatch.setattr(
            playback_module.utils, "later", lambda *a: pytest.fail("no hold")
        )

        controller._align_after_resume()

        assert not player.paused

    def test_a_hold_is_bounded_and_yields_to_a_group_pause(self, tmp_path, monkeypatch):
        controller, manager, player = make_controller(position=100.0)
        player.clock_advances = False
        manager.claim = live_claim(write_state(tmp_path, 42_000_000.0, 120_000.0))
        controller._reference = (
            EPOCH_MS + 100_000.0 + 42_000_000.0 - 120_000.0 - 500_000.0,
            None,
            True,
        )
        timers = []
        monkeypatch.setattr(
            playback_module.utils, "later", lambda s, f, *a: timers.append((s, f, a))
        )

        controller._align_after_resume()

        assert controller._live_hold_ms == pytest.approx(utils.LIVE_HOLD_MAX_S * 1000.0)
        # The group pauses during the hold: the timer must not resume us.
        controller._reference = (controller._reference[0], None, False)
        timers[0][1](*timers[0][2])
        assert player.paused

    def test_a_live_ready_promises_the_group_position(self, tmp_path):
        controller, manager, player = make_controller(position=100.0)
        player.clock_advances = False
        manager.claim = live_claim(write_state(tmp_path, 42_000_000.0, 120_000.0))
        controller._reference = (EPOCH_MS + 41_990_000.0, None, True)

        assert controller.live_on_source_clock()
        assert controller.reported_position_s() == pytest.approx(
            (EPOCH_MS + 41_990_000.0) / 1000.0
        )

    def test_the_anchor_is_a_delay_behind_this_member(self, tmp_path):
        controller, manager, player = make_controller(position=100.0)
        player.clock_advances = False
        manager.claim = live_claim(write_state(tmp_path, 42_000_000.0, 120_000.0))

        assert controller.live_anchor_ms() == pytest.approx(
            utils.live_anchor_ms(100_000.0 + 42_000_000.0 - 120_000.0)
        )

    def test_live_items_are_never_seeked(self, tmp_path):
        controller, manager, player = make_controller(position=100.0)
        player.clock_advances = False
        manager.claim = live_claim(write_state(tmp_path, 42_000_000.0, 120_000.0))

        controller.schedule(command("Seek", -10, ticks=utils.seconds_to_ticks(42)))
        controller.prepare_ready()
        controller.correct_position()

        assert not [
            a for a in player.actions if isinstance(a, tuple) and a[0] == "seek"
        ]
        assert "syncplay_ready" in manager.reports
