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


@pytest.fixture(autouse=True)
def _stop_over_jsonrpc(monkeypatch):
    """Stops leave through JSON-RPC, not ``player.stop()`` (issue #155).

    Wired back to the fake player so the controller's stops still show up as a
    ``"stop"`` action: the seam moved, the observable behaviour did not.
    """
    import json as _json

    def rpc(query):
        payload = _json.loads(query)
        player = FakePlayer.current
        if payload["method"] == "Player.GetActivePlayers":
            if player is None or not player.playing:
                return _json.dumps({"result": []})
            return _json.dumps({"result": [{"playerid": 0 if player.audio else 1}]})
        if payload["method"] == "Player.Stop":
            if player is not None:
                player.actions.append("stop")
                player.playing = False
            return _json.dumps({"result": "OK"})
        return _json.dumps({"result": {}})

    monkeypatch.setattr("xbmc.executeJSONRPC", rpc)
    monkeypatch.setattr("xbmc.Monitor.waitForAbort", lambda self, timeout=-1: False)


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
