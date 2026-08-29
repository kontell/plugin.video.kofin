"""Where fine sync plugs in: the play route, the shared state, the JSON-RPC
helpers, the manager's join/leave, the controller's commands, and the settings
watcher. The scheduler and session themselves are in test_syncplay_tempo.py."""

import json

import pytest

from kofin.core import kodirpc, state
from kofin.plugin import play
from kofin.plugin.router import Request
from kofin.service.settings_apply import SettingsApplier
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


def test_syncplay_tempo_state_round_trip():
    assert state.syncplay_tempo() == {}
    state.publish_syncplay_tempo({"file": "/tmp/t", "queue_secs": 1.0})
    assert state.syncplay_tempo() == {"file": "/tmp/t", "queue_secs": 1.0}
    state.clear_syncplay_tempo()
    assert state.syncplay_tempo() == {}
    FakeWindow.store[state.PROP_SYNCPLAY_TEMPO] = "junk"
    assert state.syncplay_tempo() == {}


def test_clear_all_drops_the_session():
    state.publish_syncplay_tempo({"file": "/tmp/t"})
    state.clear_all()
    assert state.syncplay_tempo() == {}


# ----------------------------------------------------------------------------
# JSON-RPC helpers
# ----------------------------------------------------------------------------


def responder(payload):
    return lambda query: json.dumps(payload)


def test_kodi_setting_reads_a_value_and_none_for_a_missing_one(monkeypatch):
    monkeypatch.setattr("xbmc.executeJSONRPC", responder({"result": {"value": 40}}))
    assert kodirpc.kodi_setting("videoplayer.queuetimesize") == 40
    monkeypatch.setattr(
        "xbmc.executeJSONRPC", responder({"error": {"code": -32602, "message": "x"}})
    )
    assert kodirpc.kodi_setting("videoplayer.queuetimesize") is None
    # C3: an unreadable answer is a blip, not "this Kodi lacks the setting" —
    # collapsing the two is what made tempo mistake a failure for Omega.
    monkeypatch.setattr("xbmc.executeJSONRPC", lambda query: "not json")
    assert kodirpc.kodi_setting("anything") is kodirpc.FAILED


def test_set_kodi_setting_reports_acceptance(monkeypatch):
    sent = []

    def rpc(query):
        sent.append(json.loads(query))
        return json.dumps({"result": True})

    monkeypatch.setattr("xbmc.executeJSONRPC", rpc)
    assert kodirpc.set_kodi_setting("videoplayer.queuetimesize", 10) is True
    assert sent[0]["params"] == {"setting": "videoplayer.queuetimesize", "value": 10}
    monkeypatch.setattr("xbmc.executeJSONRPC", responder({"result": False}))
    assert kodirpc.set_kodi_setting("x", 1) is False
    monkeypatch.setattr("xbmc.executeJSONRPC", responder({"error": {"code": -1}}))
    assert kodirpc.set_kodi_setting("x", 1) is False


# ----------------------------------------------------------------------------
# The play route
# ----------------------------------------------------------------------------


class RecordingListItem:
    def __init__(self):
        self.props = {}

    def setProperty(self, key, value):
        self.props[key] = value


def test_tempo_route_needs_a_session_and_a_video():
    movie = {"Type": "Movie", "Id": "m1"}
    assert play.tempo_route(movie, "DirectStream") is None  # no session
    state.publish_syncplay_tempo({"file": "/tmp/tempo", "queue_secs": 1.0})
    assert play.tempo_route(movie, "DirectStream") == {
        "File": "/tmp/tempo",
        "QueueSecs": 1.0,
    }
    assert play.tempo_route(movie, "DirectPlay")["File"] == "/tmp/tempo"
    # Audio is still never routed: PAPlayer has its own choreography.
    assert play.tempo_route({"Type": "Audio", "Id": "a1"}, "DirectStream") is None
    state.publish_syncplay_tempo({"file": "/tmp/tempo"})
    assert play.tempo_route(movie, "DirectStream")["QueueSecs"] == 8.0


def test_the_player_queue_is_sized_for_the_route_not_the_session(monkeypatch):
    """Fine sync shortens the queue to 1s so a pulse is audible in ~2s. A
    segmented stream cannot live on that — the server's HLS segments are 3s and
    a queue shorter than a segment cannot bridge a boundary, which was measured
    as a stall every ~3s and a stream sampling between 0.75x and 1.49x. The
    queue is read per player construction, so the route picks."""
    writes = []
    monkeypatch.setattr(
        kodirpc,
        "set_kodi_setting",
        lambda key, value: writes.append((key, value)) is None,
    )
    movie = {"Type": "Movie", "Id": "m1"}
    state.publish_syncplay_tempo(
        {
            "file": "/tmp/tempo",
            "queue_secs": 1.0,
            "queue_short_tenths": 10,
            "queue_full_tenths": 60,
        }
    )

    direct = play.tempo_route(movie, "DirectStream")
    assert writes[-1] == ("videoplayer.queuetimesize", 10)
    assert direct["QueueSecs"] == 1.0

    transcode = play.tempo_route(movie, "Transcode", {})
    assert writes[-1] == ("videoplayer.queuetimesize", 60)
    assert transcode["QueueSecs"] == 6.0

    # A user whose own queue is below the segment duration is still floored:
    # the alternative is the stutter above.
    state.publish_syncplay_tempo(
        {
            "file": "/tmp/tempo",
            "queue_secs": 1.0,
            "queue_short_tenths": 10,
            "queue_full_tenths": 20,
        }
    )
    assert play.tempo_route(movie, "Transcode", {})["QueueSecs"] == 4.0
    assert writes[-1] == ("videoplayer.queuetimesize", 40)


def test_a_session_that_never_shortened_sizes_nothing(monkeypatch):
    """Kodi 21 has no such setting and is fixed at 8s; a session that could not
    record a restore leaves the queue alone. Either way the published
    queue_secs stands and nothing is written."""
    writes = []
    monkeypatch.setattr(
        kodirpc,
        "set_kodi_setting",
        lambda key, value: writes.append((key, value)) is None,
    )
    movie = {"Type": "Movie", "Id": "m1"}
    state.publish_syncplay_tempo({"file": "/tmp/tempo", "queue_secs": 8.0})
    assert play.tempo_route(movie, "Transcode", {})["QueueSecs"] == 8.0
    assert writes == []


def test_tempo_route_carries_the_manifest_type_for_a_transcode():
    """A transcode IS routed now — it is the case that needs a rate pulse most,
    being the one transport that cannot be seeked accurately. It carries its
    manifest type so the add-on's ffmpeg open path is not left guessing at a URL
    with no container extension."""
    movie = {"Type": "Movie", "Id": "m1"}
    state.publish_syncplay_tempo({"file": "/tmp/tempo", "queue_secs": 1.0})

    hls = play.tempo_route(movie, "Transcode", {"TranscodingSubProtocol": "hls"})
    assert hls["ManifestType"] == "hls"
    assert hls["File"] == "/tmp/tempo"

    # Jellyfin defaults a video transcode to HLS when it says nothing.
    assert play.tempo_route(movie, "Transcode", {})["ManifestType"] == "hls"

    # The music profile is plain http: no manifest, so no manifest_type.
    plain = play.tempo_route(movie, "Transcode", {"TranscodingSubProtocol": "http"})
    assert plain["ManifestType"] == ""

    # A direct stream carries none at all.
    assert "ManifestType" not in play.tempo_route(movie, "DirectStream")


def test_stamp_tempo_route_is_the_addon_contract():
    li = RecordingListItem()
    play.stamp_tempo_route(li, {"File": "/tmp/tempo", "QueueSecs": 1.0})
    assert li.props == {
        "inputstream": "inputstream.tempo",
        "inputstream.tempo.tempo": "1.0",
        "inputstream.tempo.tempo_file": "/tmp/tempo",
        "inputstream.tempo.queue_secs": "1",
    }
    assert "inputstream.tempo.start_time" not in li.props
    # No manifest_type unless the route carries one: a file or a static stream
    # must not be announced as a playlist.
    assert "inputstream.tempo.manifest_type" not in li.props


def test_stamp_tempo_route_announces_a_playlist_stream():
    li = RecordingListItem()
    play.stamp_tempo_route(
        li, {"File": "/tmp/tempo", "QueueSecs": 1.0, "ManifestType": "hls"}
    )
    assert li.props["inputstream.tempo.manifest_type"] == "hls"

    plain = RecordingListItem()
    play.stamp_tempo_route(
        plain, {"File": "/tmp/tempo", "QueueSecs": 1.0, "ManifestType": ""}
    )
    assert "inputstream.tempo.manifest_type" not in plain.props


class BuiltListItem(RecordingListItem):
    def __init__(self):
        super().__init__()
        self.path = None

    def setPath(self, path):
        self.path = path

    def setContentLookup(self, flag):
        pass

    def getVideoInfoTag(self):
        class Tag:
            def setDbId(self, dbid):
                pass

        return Tag()


def test_resolve_downloaded_routes_and_claims(monkeypatch):
    built = BuiltListItem()
    monkeypatch.setattr(play.listitems, "build", lambda *a, **k: built)
    resolved = []
    monkeypatch.setattr(
        "xbmcplugin.setResolvedUrl", lambda handle, ok, li: resolved.append((ok, li))
    )
    state.publish_syncplay_tempo({"file": "/tmp/tempo", "queue_secs": 1.0})
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
    assert built.props["inputstream"] == "inputstream.tempo"
    claim = state.claim_play_item("/dl/m1.mkv")
    assert claim["Tempo"] == {"File": "/tmp/tempo", "QueueSecs": 1.0}
    assert claim["PlayMethod"] == "DirectPlay"


def test_resolve_downloaded_outside_a_session_is_untouched(monkeypatch):
    built = BuiltListItem()
    monkeypatch.setattr(play.listitems, "build", lambda *a, **k: built)
    monkeypatch.setattr("xbmcplugin.setResolvedUrl", lambda *a: None)
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
    assert "inputstream" not in built.props
    assert "Tempo" not in state.claim_play_item("/dl/m1.mkv")


# ----------------------------------------------------------------------------
# The manager: join arms, leave disarms
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
    # A re-join of the same group is not a new session.
    join(manager, version=2)
    assert spy.calls == ["begin"]
    manager._leave_locally()
    assert spy.calls == ["begin", "end"]


def test_refresh_follows_the_setting(manager):  # noqa: F811
    spy = SessionSpy()
    manager.tempo_session = spy
    manager.refresh_tempo_session()  # not in a group: nothing
    assert spy.calls == []
    join(manager)
    FakeAddon.store["syncPlayTempo"] = "false"
    manager.refresh_tempo_session()
    assert spy.calls == ["begin", "end"]
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


def test_current_claim_is_the_players_item(manager):  # noqa: F811
    manager.player.playing = True
    manager.player.item = {"Id": "m1", "Tempo": {"File": "/t"}}
    assert manager.current_claim()["Tempo"]["File"] == "/t"
    manager.player.playing = False
    assert manager.current_claim() is None


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
