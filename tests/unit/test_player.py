import os

import pytest

from kofin.core import state
from kofin.service.player import Player
from tests.unit.fakes import FakeAddon, FakeWindow


class RecordingApi:
    server = "http://s:8096"
    user_id = "uid"

    def __init__(self):
        self.calls = []
        self.deleted = []
        self.delete_error = None

    def delete_item(self, item_id):
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(item_id)

    def session_playing(self, data):
        self.calls.append(("playing", data))

    def session_progress(self, data):
        self.calls.append(("progress", data))

    def session_stopped(self, data):
        self.calls.append(("stopped", data))

    def close_transcode(self, device_id, play_session_id):
        self.calls.append(("close_transcode", (device_id, play_session_id)))

    def close_live_stream(self, live_stream_id):
        self.calls.append(("close_live_stream", live_stream_id))


class FakeMonitor:
    def waitForAbort(self, seconds=0):
        return False

    def abortRequested(self):
        return False


@pytest.fixture(autouse=True)
def kodi_fakes(monkeypatch):
    FakeWindow.store = {}
    # Every toggle reads false: no segment engine unless a test opts in.
    FakeAddon.store = {}
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmc.Monitor", FakeMonitor)
    monkeypatch.setattr(
        "xbmc.executeJSONRPC",
        lambda q: '{"result": {"volume": 77, "muted": false}}',
    )


_players = []


@pytest.fixture(autouse=True)
def _stop_player_threads():
    """Join the _Reporter/ticker threads every constructed Player starts."""
    yield
    while _players:
        try:
            _players.pop().stop_threads()
        except Exception:
            pass


def make_player(monkeypatch, url="http://s/stream"):
    api = RecordingApi()
    player = Player(api)  # type: ignore[arg-type]
    _players.append(player)
    monkeypatch.setattr(player, "getPlayingFile", lambda: url)
    monkeypatch.setattr(player, "getTime", lambda: 42.0)
    monkeypatch.setattr(player, "_start_ticker", lambda: None)
    return player, api


def drain(player):
    """Reports post on the reporter's own thread; tests that assert on
    api.calls wait for the pipe to run dry first."""
    assert player._reporter.flush(5)


def queue_item(url="http://s/stream", method="DirectStream", **extra):
    state.push_play_item(
        {
            "Id": "m1",
            "Type": "Movie",
            "Path": url,
            "PlayMethod": method,
            "PlaySessionId": "ps1",
            "MediaSourceId": "src1",
            "DeviceId": "dev1",
            "Runtime": 0,
            "AudioStreamIndex": 1,
            "SubtitleStreamIndex": None,
            "CurrentPosition": 10.0,
            **extra,
        }
    )


def test_a_claim_installed_on_a_paused_player_says_so(monkeypatch):
    """A SyncPlay hold pauses the start before the claim exists, and a pause
    callback run during the claim's wait has no item to mark: the claim
    starts from the player's own state, which SyncPlay reads to tell a
    viewer's play from the resume Kodi raises before an open."""
    player, api = make_player(monkeypatch)
    queue_item()
    monkeypatch.setattr(
        "xbmc.getCondVisibility", lambda condition: condition == "Player.Paused"
    )

    player.onPlayBackStarted()

    assert player.current_item()["Paused"] is True
    drain(player)
    assert api.calls[0][1]["IsPaused"] is True


def test_claim_and_report_lifecycle(monkeypatch):
    player, api = make_player(monkeypatch)
    queue_item()

    player.onPlayBackStarted()
    drain(player)
    assert api.calls[0][0] == "playing"
    start = api.calls[0][1]
    assert start["ItemId"] == "m1"
    assert start["PositionTicks"] == 100_000_000
    assert start["VolumeLevel"] == 77
    assert "LiveStreamId" not in start
    assert state.get_playing_id() == "m1"

    player.onPlayBackSeek(65_000, 0)
    drain(player)
    seek = api.calls[-1][1]
    assert seek["PositionTicks"] == 650_000_000

    player.report_progress()
    drain(player)
    tick = api.calls[-1][1]
    assert tick["PositionTicks"] == 420_000_000

    player.onPlayBackStopped()
    drain(player)
    kinds = [kind for kind, _data in api.calls]
    assert kinds[-1] == "stopped"
    assert api.calls[-1][1]["PositionTicks"] == 420_000_000
    assert state.get_playing_id() == ""


def test_a_live_play_names_its_live_stream_in_every_report(monkeypatch):
    """Jellyfin closes a live stream, and frees its tuner, at stop or when the
    session ends only when a report names it."""
    player, api = make_player(monkeypatch)
    queue_item(LiveStreamId="ls1")

    player.onPlayBackStarted()
    player.report_progress()
    player.onPlayBackStopped()
    drain(player)

    assert [kind for kind, _data in api.calls] == ["playing", "progress", "stopped"]
    assert all(data["LiveStreamId"] == "ls1" for _kind, data in api.calls)


def test_a_failed_stop_report_closes_the_live_stream(monkeypatch):
    """The parked stop carries the position alone, and the report that named
    the stream was what would have closed it on the server."""
    player, api = make_player(monkeypatch)
    queue_item(LiveStreamId="ls1")
    player.onPlayBackStarted()
    drain(player)

    def refuse(data):
        raise RuntimeError("server unreachable")

    api.session_stopped = refuse
    monkeypatch.setattr("kofin.downloads.pending.enqueue", lambda *a, **k: None)
    player.onPlayBackStopped()
    drain(player)

    assert ("close_live_stream", "ls1") in api.calls


def test_a_failed_stop_past_the_watched_share_parks_a_played_mark(monkeypatch):
    """The server's own stop rule marks a play past the watched share Played
    with no resume point; the parked stop says the same, so the replay cannot
    put a finished item into Continue Watching."""
    player, api = make_player(monkeypatch)
    queue_item(Runtime=1245 * 10_000_000)
    player.onPlayBackStarted()
    monkeypatch.setattr(player, "getTime", lambda: 1240.0)
    player.report_progress()
    drain(player)

    def refuse(data):
        raise RuntimeError("server unreachable")

    api.session_stopped = refuse
    parked = []
    monkeypatch.setattr(
        "kofin.downloads.pending.enqueue", lambda *a, **k: parked.append((a, k))
    )
    player.onPlayBackStopped()
    drain(player)

    ((args, kwargs),) = parked
    assert args[0] == "m1"
    assert kwargs["played"] is True and kwargs["position_ticks"] == 0


def test_a_failed_stop_short_of_the_watched_share_parks_the_position(monkeypatch):
    player, api = make_player(monkeypatch)
    queue_item(Runtime=1245 * 10_000_000)
    player.onPlayBackStarted()
    monkeypatch.setattr(player, "getTime", lambda: 600.0)
    player.report_progress()
    drain(player)

    def refuse(data):
        raise RuntimeError("server unreachable")

    api.session_stopped = refuse
    parked = []
    monkeypatch.setattr(
        "kofin.downloads.pending.enqueue", lambda *a, **k: parked.append((a, k))
    )
    player.onPlayBackStopped()
    drain(player)

    ((args, kwargs),) = parked
    assert kwargs.get("played") is None
    assert kwargs["position_ticks"] == 6_000_000_000


def test_the_sweep_closes_the_live_streams_of_plays_kodi_never_started(
    monkeypatch, tmp_path
):
    import json
    import time

    queue = tmp_path / "queue"
    queue.mkdir()
    monkeypatch.setattr(state, "_queue_dir", lambda: str(queue))

    def stale(name, live_stream_id):
        stamp = time.time_ns() - (state.PLAY_QUEUE_LIVE_TTL_SECONDS + 5) * 1_000_000_000
        (queue / ("%019d-%s.json" % (stamp, name))).write_text(
            json.dumps({"Path": "http://" + name, "LiveStreamId": live_stream_id})
        )

    player, api = make_player(monkeypatch)
    stale("a", "ls1")
    player.sweep_live_streams()
    drain(player)
    assert ("close_live_stream", "ls1") in api.calls

    # At most every LIVE_SWEEP_SECONDS.
    stale("b", "ls2")
    player.sweep_live_streams()
    drain(player)
    assert ("close_live_stream", "ls2") not in api.calls


def test_callbacks_return_while_the_post_is_still_in_flight(monkeypatch):
    """Kodi delivers player callbacks on the announcement thread every addon
    shares, so the callback hands the network off and returns — a server that
    answers slowly may hold the *report*, never the thread."""
    import threading

    player, api = make_player(monkeypatch)
    queue_item()
    posting = threading.Event()
    release = threading.Event()

    def slow_playing(data):
        posting.set()
        assert release.wait(5)
        api.calls.append(("playing", data))

    monkeypatch.setattr(api, "session_playing", slow_playing)

    player.onPlayBackStarted()  # must return while slow_playing blocks

    assert posting.wait(5)
    assert ("playing", None) not in api.calls and not any(
        kind == "playing" for kind, _data in api.calls
    )
    release.set()
    drain(player)
    assert any(kind == "playing" for kind, _data in api.calls)


def test_foreign_playback_is_ignored(monkeypatch):
    player, api = make_player(monkeypatch)
    player.onPlayBackStarted()
    drain(player)
    assert api.calls == []
    player.onPlayBackStopped()
    drain(player)
    assert api.calls == []


def test_a_stopped_player_leaves_the_claim_to_the_live_one(monkeypatch):
    """Kodi calls a Player until the object is freed, and a stopped service
    generation outlives its run, so its Player must not race the live one
    for a claim."""
    stopped, _ = make_player(monkeypatch)
    stopped.stop_threads()
    live, api = make_player(monkeypatch)
    queue_item()

    stopped.onPlayBackStarted()  # Kodi calls the older registration first
    live.onPlayBackStarted()
    drain(live)

    assert stopped.current_item() is None
    assert live.current_item()["Id"] == "m1"
    assert api.calls[0][0] == "playing"


def handing_over(monkeypatch, url="http://s/stream", **extra):
    """A generation stopped in the middle of a play it claimed; its api."""
    monkeypatch.setattr("xbmc.getCondVisibility", lambda condition: False)
    old, api = make_player(monkeypatch, url=url)
    queue_item(url=url, **extra)
    old.onPlayBackStarted()
    drain(old)
    api.calls.clear()
    old.stop_threads()
    return api


def adopting(monkeypatch, playing="http://s/stream"):
    """The next generation's player, with Kodi playing ``playing``."""
    new, api = make_player(monkeypatch, url=playing)
    monkeypatch.setattr(new, "isPlaying", lambda: bool(playing))
    return new, api


def test_the_next_generation_reports_the_play_kodi_goes_on_with(monkeypatch):
    """A restart (an add-on update, a sign-in) stops a generation in the
    middle of a play. The next one reports it on, in the same play session
    and never as a new play, since the server counts a play when it starts;
    and its stop still names the live stream."""
    old_api = handing_over(monkeypatch, LiveStreamId="ls1")
    new, api = adopting(monkeypatch)

    new.adopt()
    drain(new)

    assert new.current_item()["PlaySessionId"] == "ps1"
    assert state.get_playing_id() == "m1"
    assert [kind for kind, _data in api.calls] == ["progress"]
    assert api.calls[0][1]["PositionTicks"] == 420_000_000  # where Kodi is
    assert api.calls[0][1]["LiveStreamId"] == "ls1"

    new.onPlayBackStopped()
    drain(new)
    assert api.calls[-1][0] == "stopped"
    assert api.calls[-1][1]["PlaySessionId"] == "ps1"
    assert api.calls[-1][1]["LiveStreamId"] == "ls1"
    assert old_api.calls == []


LIVE_CLOSED = [("close_live_stream", "ls1")]
ENCODING_CLOSED = [("close_transcode", ("dev1", "ps1"))]


@pytest.mark.parametrize(
    "playing,user,method,calls",
    [
        # The play ended between the generations: nothing reports it now, so
        # what the server holds open for it is closed.
        ("", "uid", "DirectStream", LIVE_CLOSED),
        ("http://s/other", "uid", "DirectStream", LIVE_CLOSED),
        ("", "uid", "Transcode", LIVE_CLOSED + ENCODING_CLOSED),
        ("http://s/other", "uid", "Transcode", LIVE_CLOSED + ENCODING_CLOSED),
        # A sign-in to another account: not this login's to report or close.
        ("http://s/stream", "someone-else", "DirectStream", []),
        ("", "someone-else", "Transcode", []),
    ],
)
def test_a_play_not_this_generations_to_report_is_dropped(
    monkeypatch, playing, user, method, calls
):
    handing_over(monkeypatch, method=method, LiveStreamId="ls1")
    new, api = adopting(monkeypatch, playing=playing)
    monkeypatch.setattr(api, "user_id", user, raising=False)

    new.adopt()
    drain(new)

    assert new.current_item() is None
    assert api.calls == calls
    assert not os.path.exists(state._handover_path())


def test_a_generation_with_a_play_of_its_own_adopts_nothing(monkeypatch):
    handing_over(monkeypatch)
    new, api = adopting(monkeypatch, playing="http://s/next")
    queue_item(url="http://s/next")
    new.onPlayBackStarted()

    new.adopt()

    assert new.current_item()["Path"] == "http://s/next"
    assert os.path.exists(state._handover_path())


@pytest.mark.parametrize(
    "user,calls",
    [
        ("uid", LIVE_CLOSED + ENCODING_CLOSED),
        ("someone-else", []),
    ],
)
def test_a_hand_over_never_taken_is_retired_before_it_is_replaced(
    monkeypatch, user, calls
):
    """A generation that stops with a play of its own replaces the claim it
    never took, whose play ended before its own began."""
    handing_over(monkeypatch, method="Transcode", LiveStreamId="ls1")
    new, api = adopting(monkeypatch, playing="http://s/next")
    monkeypatch.setattr(api, "user_id", user, raising=False)
    queue_item(url="http://s/next", PlaySessionId="ps2")
    new.onPlayBackStarted()
    new.adopt()
    drain(new)
    api.calls.clear()

    new.stop_threads()

    assert api.calls == calls
    assert state.take_handed_over_play()["Claim"]["PlaySessionId"] == "ps2"


def test_transcode_stop_closes_encoding(monkeypatch):
    player, api = make_player(monkeypatch)
    queue_item(method="Transcode")
    player.onPlayBackStarted()
    player.onPlayBackEnded()
    drain(player)
    assert ("close_transcode", ("dev1", "ps1")) in api.calls


def test_pause_resume_report_state(monkeypatch):
    player, api = make_player(monkeypatch)
    queue_item()
    player.onPlayBackStarted()
    player.onPlayBackPaused()
    drain(player)
    assert api.calls[-1][1]["IsPaused"] is True
    player.onPlayBackResumed()
    drain(player)
    assert api.calls[-1][1]["IsPaused"] is False


def test_resume_without_a_manager_settles_an_owed_tempo(monkeypatch):
    player, api = make_player(monkeypatch)
    reasons = []
    monkeypatch.setattr(
        "kofin.syncplay.tempo.settle_left_rate",
        lambda reason="", correction=None: reasons.append(reason) or True,
    )
    queue_item()
    player.onPlayBackStarted()
    player.onPlayBackResumed()
    assert reasons == []  # nothing owed, nothing asked

    # A pulse's rate left by an earlier generation, refused while paused: no
    # manager yet (the service has not connected), so the resume settles it.
    state.publish_syncplay_pulse({"player": 1})
    player.onPlayBackResumed()
    assert reasons == [" at a resume"]

    # With a manager, its on_resumed is what settles (settle_owed).
    player.syncplay = RecordingSyncPlay()
    player.onPlayBackResumed()
    assert reasons == [" at a resume"]
    drain(player)


# --- SyncPlay forwarding -----------------------------------------------------


class RecordingSyncPlay:
    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        def hook(*args):
            self.events.append((name,) + args)

        return hook


def test_syncplay_callbacks_forwarded_when_attached(monkeypatch):
    player, api = make_player(monkeypatch)
    syncplay = RecordingSyncPlay()
    player.syncplay = syncplay
    queue_item()

    player.onPlayBackStarted()
    player.onAVStarted()
    player.onPlayBackPaused()
    player.onPlayBackResumed()
    player.onPlayBackSeek(65_000, 0)
    player.onPlayBackStopped()

    names = [event[0] for event in syncplay.events]
    assert names == [
        "on_playback_started",
        "on_avstarted",
        "on_paused",
        "on_resumed",
        "on_seek",
        "on_stopped",
    ]
    # The seek forwards seconds, as the manager expects.
    assert syncplay.events[4] == ("on_seek", 65.0)


def test_syncplay_ended_and_error_forwarded(monkeypatch):
    player, api = make_player(monkeypatch)
    syncplay = RecordingSyncPlay()
    player.syncplay = syncplay
    queue_item()
    player.onPlayBackStarted()

    player.onPlayBackEnded()
    assert ("on_ended",) in syncplay.events

    queue_item()
    player.onPlayBackStarted()
    player.onPlayBackError()
    assert ("on_error",) in syncplay.events


def test_syncplay_detached_is_a_noop(monkeypatch):
    player, api = make_player(monkeypatch)
    queue_item()
    player.onPlayBackStarted()  # syncplay is None: nothing to forward
    player.onAVStarted()
    player.onPlayBackStopped()
    drain(player)
    kinds = [kind for kind, _data in api.calls]
    assert kinds[0] == "playing" and kinds[-1] == "stopped"


def test_broken_syncplay_hook_never_breaks_reporting(monkeypatch):
    player, api = make_player(monkeypatch)

    class Exploding:
        def __getattr__(self, name):
            def hook(*args):
                raise RuntimeError("boom")

            return hook

    player.syncplay = Exploding()
    queue_item()
    player.onPlayBackStarted()
    player.onPlayBackStopped()
    drain(player)
    kinds = [kind for kind, _data in api.calls]
    assert "playing" in kinds and "stopped" in kinds


def test_current_item_exposes_claim(monkeypatch):
    player, api = make_player(monkeypatch)
    assert player.current_item() is None
    queue_item()
    player.onPlayBackStarted()
    item = player.current_item()
    assert item is not None and item["Id"] == "m1"
    player.onPlayBackStopped()
    assert player.current_item() is None


def test_the_claim_wait_is_logged_with_its_outcome(monkeypatch):
    """One line per start with the outcome, the seconds waited and the kind
    of playback, so the log says whether the backfill grace is the GET's
    latency or dead time."""
    from kofin.service import player as player_mod

    player, api = make_player(monkeypatch)
    lines = []
    monkeypatch.setattr(
        player_mod.LOG, "debug", lambda msg, *args: lines.append(msg % args)
    )

    queue_item()
    player.onPlayBackStarted()

    claim_lines = [line for line in lines if line.startswith("claim ")]
    assert len(claim_lines) == 1
    assert claim_lines[0].startswith("claim claimed after 0.00s (")


# --- delete after watching ---------------------------------------------------


TICK = 10_000_000  # RunTimeTicks per second


def finished_item(item_type="Movie", position=95.0, runtime=100.0, can_delete=True):
    return {
        "Id": "m1",
        "Type": item_type,
        "Name": "Some Film",
        "Runtime": int(runtime * TICK),
        "CurrentPosition": position,
        # The server's per-account answer, carried through the play queue by
        # plugin/play.play_state.
        "CanDelete": can_delete,
    }


def enable_delete(**extra):
    FakeAddon.store.update(
        dict({"enableDelete": "true", "deleteAfterWatching": "true"}, **extra)
    )


def test_watched_to_end_uses_a_share_of_the_runtime():
    from kofin.service.player import watched_to_end

    assert watched_to_end(finished_item(position=90.0)) is True
    assert watched_to_end(finished_item(position=89.9)) is False
    # An item with no runtime cannot be judged, so it is never "finished".
    assert watched_to_end(finished_item(runtime=0.0)) is False


def offer_and_wait(player, item):
    """``offer_delete`` dispatches the prompt onto its own thread; hand back
    what that thread was asked to prompt for."""
    import threading

    prompted = []
    fired = threading.Event()

    def record(prompt_item):
        prompted.append(prompt_item)
        fired.set()

    player._delete_prompt = record  # type: ignore[assignment]
    offered = player.offer_delete(item)
    if offered:
        assert fired.wait(5), "the prompt thread never ran"
    return offered, prompted


def test_finished_movie_offers_deletion(monkeypatch):
    player, _api = make_player(monkeypatch)
    enable_delete()

    offered, prompted = offer_and_wait(player, finished_item())

    assert offered is True
    assert prompted[0]["Id"] == "m1"


def test_partly_watched_item_is_left_alone(monkeypatch):
    player, _api = make_player(monkeypatch)
    enable_delete()

    assert offer_and_wait(player, finished_item(position=40.0))[0] is False


def test_sub_option_off_never_offers(monkeypatch):
    player, _api = make_player(monkeypatch)
    enable_delete(deleteAfterWatching="false")

    assert offer_and_wait(player, finished_item())[0] is False


def test_delete_opt_in_owns_the_sub_option(monkeypatch):
    """The Advanced-tab opt-in gates every deletion path, so a stale
    sub-option left on cannot delete anything by itself."""
    player, _api = make_player(monkeypatch)
    enable_delete(enableDelete="false")

    assert offer_and_wait(player, finished_item())[0] is False


def test_music_is_not_offered_for_deletion(monkeypatch):
    player, _api = make_player(monkeypatch)
    enable_delete()

    assert offer_and_wait(player, finished_item(item_type="Audio"))[0] is False


def test_playback_end_offers_the_item_it_just_finished(monkeypatch):
    """The offer is made against the claim ``finalize`` is about to clear."""
    player, _api = make_player(monkeypatch)
    offered = []
    monkeypatch.setattr(player, "offer_delete", lambda item: offered.append(item))
    queue_item()

    player.onPlayBackStarted()
    player.onPlayBackEnded()

    assert [item["Id"] for item in offered] == ["m1"]


def test_a_stale_play_cleaned_up_at_the_next_start_offers_nothing(monkeypatch):
    """``finalize`` also runs as cleanup when a previous play never got its
    stop event; that is not a playback anyone just finished."""
    player, _api = make_player(monkeypatch)
    offered = []
    monkeypatch.setattr(player, "offer_delete", lambda item: offered.append(item))
    queue_item()
    player.onPlayBackStarted()

    queue_item()
    player.onPlayBackStarted()  # finalizes the first play on the way in

    assert offered == []


def prompt_dialog(monkeypatch, answer=True):
    """A dialog fake plus a localized() that carries the item-name
    placeholder — the stub's "string-30506" has none, which would fail the
    formatting rather than exercise the prompt."""
    from kofin.service import player as player_mod

    dialog = PromptDialog(answer=answer)
    monkeypatch.setattr("xbmcgui.Dialog", lambda: dialog)
    monkeypatch.setattr(player_mod.settings, "localized", lambda i: "L%d %%s" % i)
    return dialog


class PromptDialog:
    def __init__(self, answer=True):
        self.answer = answer
        self.asked = []
        self.notified = []

    def yesno(self, heading, message, **kwargs):
        self.asked.append(message)
        return self.answer

    def notification(self, *args, **kwargs):
        self.notified.append(args)


def test_prompt_deletes_on_yes(monkeypatch):
    player, api = make_player(monkeypatch)
    dialog = prompt_dialog(monkeypatch, answer=True)

    player._delete_prompt(finished_item())

    assert api.deleted == ["m1"]
    assert "Some Film" in dialog.asked[0]


def test_prompt_keeps_the_item_on_no(monkeypatch):
    player, api = make_player(monkeypatch)
    prompt_dialog(monkeypatch, answer=False)

    player._delete_prompt(finished_item())

    assert api.deleted == []


def test_a_failed_delete_notifies_and_does_not_raise(monkeypatch):
    player, api = make_player(monkeypatch)
    api.delete_error = RuntimeError("offline")
    dialog = prompt_dialog(monkeypatch, answer=True)

    player._delete_prompt(finished_item())

    assert dialog.notified


# --- Jellyfin default tracks and the stream menu -------------------------------
#
# The server resolves both defaults from the viewer's Jellyfin profile and
# returns them on every MediaSource; the player applies them over whatever
# Kodi picked from its own language settings.


AUDIO_1 = {"Index": 1, "Type": "Audio", "Codec": "ac3"}
AUDIO_2 = {"Index": 2, "Type": "Audio", "Codec": "aac"}
SUB_3 = {
    "Index": 3,
    "Type": "Subtitle",
    "Codec": "subrip",
    "IsTextSubtitleStream": True,
}
SUB_4 = {
    "Index": 4,
    "Type": "Subtitle",
    "Codec": "PGSSUB",
    "IsTextSubtitleStream": False,
}


class TrackRecorder:
    def __init__(self):
        self.audio = []
        self.subtitle = []
        self.shown = []


def stream_player(
    monkeypatch,
    method="DirectStream",
    audio=1,
    subtitle=3,
    attached=(),
    media_streams=None,
):
    player, api = make_player(monkeypatch)
    tracks = TrackRecorder()
    monkeypatch.setattr(player, "setAudioStream", tracks.audio.append)
    monkeypatch.setattr(player, "setSubtitleStream", tracks.subtitle.append)
    monkeypatch.setattr(player, "showSubtitles", tracks.shown.append)
    state.push_play_item(
        {
            "Id": "m1",
            "Type": "Movie",
            "Path": "http://s/stream",
            "PlayMethod": method,
            "PlaySessionId": "ps1",
            "MediaSourceId": "src1",
            "DeviceId": "dev1",
            "Runtime": 0,
            "AudioStreamIndex": audio,
            "SubtitleStreamIndex": subtitle,
            "CurrentPosition": 0.0,
            "Streams": {
                "MediaStreams": media_streams or [AUDIO_1, AUDIO_2, SUB_3, SUB_4],
                "Attached": list(attached),
                "Request": {},
            },
        }
    )
    FakeAddon.store["honourJellyfinDefaultTracks"] = "true"
    return player, tracks


def test_direct_play_starts_on_the_jellyfin_default_tracks(monkeypatch):
    player, tracks = stream_player(monkeypatch, audio=2, subtitle=4)
    player.onPlayBackStarted()
    player.onAVStarted()
    # Ordinal within its kind: Jellyfin audio 2 is Kodi 1, subtitle 4 is Kodi 1.
    assert tracks.audio == [1]
    assert tracks.subtitle == [1]
    assert tracks.shown == [True]


def test_already_current_tracks_are_not_reselected(monkeypatch):
    # setAudioStream / setSubtitleStream seek the demuxer even for a no-op.
    player, tracks = stream_player(monkeypatch, audio=2, subtitle=4)
    monkeypatch.setattr("kofin.service.player.kodirpc.current_audio", lambda: 1)
    monkeypatch.setattr("kofin.service.player.kodirpc.current_subtitle", lambda: 1)
    player.onPlayBackStarted()
    player.onAVStarted()
    assert tracks.audio == []
    assert tracks.subtitle == []
    assert tracks.shown == []


def test_a_mismatching_audio_track_is_still_applied(monkeypatch):
    player, tracks = stream_player(monkeypatch, audio=2, subtitle=None)
    monkeypatch.setattr("kofin.service.player.kodirpc.current_audio", lambda: 0)
    player.onPlayBackStarted()
    player.onAVStarted()
    assert tracks.audio == [1]
    assert tracks.shown == [False]


def test_already_current_audio_still_applies_a_subtitle(monkeypatch):
    player, tracks = stream_player(monkeypatch, audio=2, subtitle=4)
    monkeypatch.setattr("kofin.service.player.kodirpc.current_audio", lambda: 1)
    player.onPlayBackStarted()
    player.onAVStarted()
    assert tracks.audio == []
    assert tracks.subtitle == [1]
    assert tracks.shown == [True]


def test_a_transcode_only_applies_the_subtitle(monkeypatch):
    # The transcode carries the one audio track the server already encoded to
    # our request, so there is nothing to select.
    player, tracks = stream_player(
        monkeypatch, method="Transcode", audio=2, subtitle=3, attached=[3]
    )
    player.onPlayBackStarted()
    player.onAVStarted()
    assert tracks.audio == []
    assert tracks.subtitle == [0]  # attached first and only


def test_no_default_subtitle_turns_them_off(monkeypatch):
    # A Jellyfin profile that wants no subtitle must be obeyed, not left to
    # whatever Kodi auto-selected.
    player, tracks = stream_player(monkeypatch, subtitle=None)
    player.onPlayBackStarted()
    player.onAVStarted()
    assert tracks.shown == [False]
    assert tracks.subtitle == []


def test_a_downloaded_play_keeps_kodis_own_tracks(monkeypatch):
    """The claim of a downloaded play carries no streams: the server's
    defaults index a source's tracks, not the file's, so nothing is put over
    Kodi's own choice, a forced or sidecar subtitle included."""
    from kofin.plugin import play as play_module

    local = "/storage/downloads/Movies/M (2019)/m.mkv"
    state.push_play_item(
        play_module.play_state(
            {"Id": "m1", "Type": "Movie", "Name": "M"},
            {"Id": "m1"},
            local,
            "DirectPlay",
            "ps1",
            "dev1",
            0.0,
            streams_menu=False,
        )
    )
    player, _ = make_player(monkeypatch, url=local)
    monkeypatch.setattr("kofin.service.player.downloaded_path", lambda path: True)
    tracks = TrackRecorder()
    monkeypatch.setattr(player, "setAudioStream", tracks.audio.append)
    monkeypatch.setattr(player, "setSubtitleStream", tracks.subtitle.append)
    monkeypatch.setattr(player, "showSubtitles", tracks.shown.append)
    FakeAddon.store["honourJellyfinDefaultTracks"] = "true"

    player.onPlayBackStarted()
    assert player.current_item()["Id"] == "m1"
    player.onAVStarted()

    assert tracks.audio == [] and tracks.subtitle == [] and tracks.shown == []


def test_the_setting_is_respected(monkeypatch):
    player, tracks = stream_player(monkeypatch)
    FakeAddon.store["honourJellyfinDefaultTracks"] = "false"
    player.onPlayBackStarted()
    player.onAVStarted()
    assert tracks.audio == [] and tracks.subtitle == [] and tracks.shown == []


def test_streams_are_published_for_the_context_item(monkeypatch):
    player, _ = stream_player(monkeypatch, method="Transcode", attached=[3])
    player.onPlayBackStarted()
    published = state.playing_streams()
    assert published["Id"] == "m1"
    assert published["PlayMethod"] == "Transcode"
    assert published["Attached"] == [3]
    # Two audio tracks and a selectable subtitle: the menu offers both.
    assert FakeWindow.store[state.PROP_PLAYING_MENU] == "both"


def test_publishing_stops_when_the_playback_does(monkeypatch):
    player, _ = stream_player(monkeypatch)
    player.onPlayBackStarted()
    assert state.playing_streams()
    player.onPlayBackStopped()
    assert state.playing_streams() == {}
    assert state.PROP_PLAYING_MENU not in FakeWindow.store


def test_a_syncplay_group_withdraws_the_menu(monkeypatch):
    # A restart to change audio would desync everyone else in the group.
    player, _ = stream_player(monkeypatch)
    player.onPlayBackStarted()
    assert FakeWindow.store[state.PROP_PLAYING_MENU] == "both"
    player.syncplay_group_active = True
    assert state.PROP_PLAYING_MENU not in FakeWindow.store
    player.syncplay_group_active = False
    assert FakeWindow.store[state.PROP_PLAYING_MENU] == "both"


def test_a_burned_in_subtitle_leaves_kodis_own_off(monkeypatch):
    """It is already in the picture. Anything Kodi auto-selects on top of it is
    a second subtitle over the first -- and there is an attached text track
    here for it to pick."""
    player, tracks = stream_player(
        monkeypatch,
        method="Transcode",
        subtitle=4,
        attached=[3],
        media_streams=[AUDIO_1, AUDIO_2, SUB_3, dict(SUB_4, DeliveryMethod="Encode")],
    )
    player.onPlayBackStarted()
    player.onAVStarted()
    assert tracks.subtitle == []
    assert tracks.shown == [False]


def test_the_published_payload_carries_both_indices(monkeypatch):
    """A burned-in subtitle is not a Kodi track, so this is the only thing the
    menu can identify it from."""
    player, _ = stream_player(monkeypatch, method="Transcode", subtitle=4, attached=[3])
    player.onPlayBackStarted()
    published = state.playing_streams()
    assert published["AudioStreamIndex"] == 1
    assert published["SubtitleStreamIndex"] == 4


def test_an_account_that_cannot_delete_is_never_asked(monkeypatch):
    """An account without EnableContentDeletion would see the prompt after
    every episode, and every yes would fail on the server."""
    player, _api = make_player(monkeypatch)
    enable_delete()

    offered, prompted = offer_and_wait(player, finished_item(can_delete=False))

    assert offered is False
    assert prompted == []


# -- auto-next and the remove-after-watching offer ----------------------------


def test_auto_next_fires_once_at_half_way(monkeypatch):
    player, _api = make_player(monkeypatch)
    from kofin.service import player as player_module

    fired = []
    monkeypatch.setattr(
        player_module.downloads_auto, "trigger_next", lambda api, item: fired.append(1)
    )
    player._item = {
        "Id": "e1",
        "Type": "Episode",
        "SeriesId": "s1",
        "Runtime": 1000 * 10_000_000,
        "CurrentPosition": 450.0,
        "MediaSourceId": "src",
        "PlaySessionId": "ps",
    }
    player._maybe_auto_next()
    assert fired == []  # 45%: not yet

    player._item["CurrentPosition"] = 510.0
    player._maybe_auto_next()
    player._maybe_auto_next()
    assert fired == [1]  # once, latched

    player.finalize()
    assert player._auto_next_latch == ""  # a new playback may fire again


def test_auto_next_ignores_non_episodes(monkeypatch):
    player, _api = make_player(monkeypatch)
    from kofin.service import player as player_module

    fired = []
    monkeypatch.setattr(
        player_module.downloads_auto, "trigger_next", lambda api, item: fired.append(1)
    )
    player._item = {
        "Id": "m1",
        "Type": "Movie",
        "Runtime": 1000 * 10_000_000,
        "CurrentPosition": 990.0,
    }
    player._maybe_auto_next()
    assert fired == []


class ImmediateThread:
    """Runs the offer's dialog thread inline so the test sees its effects."""

    def __init__(self, target=None, args=(), **kwargs):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


def _watched_download_item():
    return {
        "Id": "d1",
        "Type": "Episode",
        "Name": "The One",
        "Runtime": 100 * 10_000_000,
        "CurrentPosition": 97.0,
        "MediaSourceId": "src",
        "PlaySessionId": "ps",
    }


def test_remove_offer_modes(monkeypatch):
    from kofin.downloads import store as downloads_store
    from kofin.service import player as player_module

    player, _api = make_player(monkeypatch)
    notified = []
    monkeypatch.setattr(
        player_module.ipc, "notify", lambda m, d=None: notified.append((m, d))
    )
    row = downloads_store.Download(
        jellyfin_id="d1", state=downloads_store.DONE, origin="user"
    )
    monkeypatch.setattr("kofin.downloads.store.get", lambda item_id: row)
    monkeypatch.setattr(player_module.settings, "localized", lambda i: "L%d %%s" % i)
    FakeAddon.store["downloadsEnabled"] = "true"
    item = _watched_download_item()

    assert player.offer_remove_download(item) is False  # off by default

    FakeAddon.store["downloadsDeleteAfterWatching"] = "true"
    FakeAddon.store["downloadsDeleteAutomatically"] = "true"
    assert player.offer_remove_download(item) is True
    assert notified == [(player_module.ipc.DOWNLOAD_REMOVE, {"Ids": ["d1"]})]

    notified.clear()
    FakeAddon.store["downloadsDeleteAutomatically"] = "false"
    monkeypatch.setattr(player_module.threading, "Thread", ImmediateThread)

    class YesDialog:
        def yesno(self, heading, message, **kwargs):
            return True

    monkeypatch.setattr(player_module.xbmcgui, "Dialog", YesDialog)
    assert player.offer_remove_download(item) is True
    assert notified == [(player_module.ipc.DOWNLOAD_REMOVE, {"Ids": ["d1"]})]

    class NoDialog:
        def yesno(self, heading, message, **kwargs):
            return False

    notified.clear()
    monkeypatch.setattr(player_module.xbmcgui, "Dialog", NoDialog)
    assert player.offer_remove_download(item) is True  # raised, declined
    assert notified == []


def test_remove_offer_covers_automatic_downloads_too(monkeypatch):
    """One answer per watched download, whoever queued it."""
    from kofin.downloads import store as downloads_store
    from kofin.service import player as player_module

    player, _api = make_player(monkeypatch)
    notified = []
    monkeypatch.setattr(
        player_module.ipc, "notify", lambda m, d=None: notified.append((m, d))
    )
    FakeAddon.store["downloadsEnabled"] = "true"
    FakeAddon.store["downloadsDeleteAfterWatching"] = "true"
    FakeAddon.store["downloadsDeleteAutomatically"] = "true"
    row = downloads_store.Download(
        jellyfin_id="d1", state=downloads_store.DONE, origin="auto:s1"
    )
    monkeypatch.setattr("kofin.downloads.store.get", lambda item_id: row)

    assert player.offer_remove_download(_watched_download_item()) is True
    assert notified == [(player_module.ipc.DOWNLOAD_REMOVE, {"Ids": ["d1"]})]

    # Nothing downloaded, nothing to offer.
    notified.clear()
    monkeypatch.setattr("kofin.downloads.store.get", lambda item_id: None)
    assert player.offer_remove_download(_watched_download_item()) is False
    assert notified == []


def test_a_finished_song_is_never_offered_for_removal(monkeypatch):
    """Video only, like ``offer_delete``.

    Both modes are asserted: the silent one must not notify, and the
    prompting one must not raise a dialog. An episode in exactly the same
    state still goes through, or the gate would be a mute button.
    """
    from kofin.downloads import store as downloads_store
    from kofin.service import player as player_module

    player, _api = make_player(monkeypatch)
    notified = []
    dialogs = []
    monkeypatch.setattr(
        player_module.ipc, "notify", lambda m, d=None: notified.append((m, d))
    )
    monkeypatch.setattr(player_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(player_module.settings, "localized", lambda i: "L%d %%s" % i)

    class YesDialog:
        def yesno(self, heading, message, **kwargs):
            dialogs.append(heading)
            return True

    monkeypatch.setattr(player_module.xbmcgui, "Dialog", YesDialog)
    row = downloads_store.Download(
        jellyfin_id="d1", media_type="song", state=downloads_store.DONE, origin="user"
    )
    monkeypatch.setattr("kofin.downloads.store.get", lambda item_id: row)
    FakeAddon.store["downloadsEnabled"] = "true"
    FakeAddon.store["downloadsDeleteAfterWatching"] = "true"

    song = dict(_watched_download_item(), Type="Audio")

    FakeAddon.store["downloadsDeleteAutomatically"] = "true"
    assert player.offer_remove_download(song) is False
    assert notified == []

    FakeAddon.store["downloadsDeleteAutomatically"] = "false"
    assert player.offer_remove_download(song) is False
    assert dialogs == []

    # The gate is about the type, not about the settings around it.
    assert player.offer_remove_download(_watched_download_item()) is True
    assert dialogs == ["L30710 %s"]


def test_finish_offers_local_remove_only_without_the_delete_prompt(monkeypatch):
    player, _api = make_player(monkeypatch)
    offered = []
    monkeypatch.setattr(player, "offer_delete", lambda item: True)
    monkeypatch.setattr(player, "offer_remove_download", lambda item: offered.append(1))
    player._item = _watched_download_item()
    player._finish()
    assert offered == []  # the server prompt ran; never two dialogs

    player._item = _watched_download_item()
    monkeypatch.setattr(player, "offer_delete", lambda item: False)
    player._finish()
    assert offered == [1]


# -- the offline segment cache and offline claims -----------------------------


def test_prepare_segment_state_offline_asks_nothing(monkeypatch):
    player, api = make_player(monkeypatch)
    FakeWindow.store["kofin.online"] = "false"
    FakeAddon.store["playNextEnabled"] = "true"
    player._item = {
        "Id": "e1",
        "Type": "Episode",
        "SeriesId": "s1",
        "MediaSourceId": "src",
        "PlaySessionId": "ps",
    }
    player.segments._segments_loaded = False

    import threading as threading_module

    player.segments.prepare_segment_state(threading_module.Event())

    assert player.segments._segments == [] and player.segments._segments_loaded is True
    assert player.segments._next_episode is None
    assert api.calls == []  # neither segments nor adjacency was fetched


def test_progress_reports_stay_home_offline(monkeypatch):
    player, api = make_player(monkeypatch)
    FakeWindow.store["kofin.online"] = "false"
    player._item = {
        "Id": "e1",
        "Type": "Episode",
        "Runtime": 1000 * 10_000_000,
        "CurrentPosition": 10.0,
        "MediaSourceId": "src",
        "PlaySessionId": "ps",
    }
    player.report_progress()
    drain(player)
    assert api.calls == []  # the position still updated for the segment tick
    assert player._item["CurrentPosition"] == 42.0  # getTime stub


# --- a track queued ahead -------------------------------------------------------

SONG = "5" * 32
NATIVE_SONG = "plugin://plugin.video.kofin/native/%s/%s/music/%s/%s.flac" % (
    "a" * 32,
    "b" * 32,
    "c" * 32,
    SONG,
)


def _queue_song(item_id=SONG, session="ps1"):
    state.push_play_item(
        {
            "Id": item_id,
            "Type": "Audio",
            "Path": "http://s/Audio/%s/stream.flac?static=true&playSessionId=%s"
            % (item_id, session),
            "PlayMethod": "DirectStream",
            "PlaySessionId": session,
            "MediaSourceId": "src1",
            "DeviceId": "dev1",
            "Runtime": 0,
            "AudioStreamIndex": None,
            "SubtitleStreamIndex": None,
            "CurrentPosition": 0.0,
        }
    )


def test_a_track_kodi_queued_ahead_is_claimed_by_its_item(monkeypatch):
    """PAPlayer asks for the next track of an album a few seconds before
    the current one ends, and when it starts Kodi names it by the plugin URL
    it was listed under, not by the stream the play route resolved. Matched
    on the path alone, every track after the first would go unclaimed:
    played and counted in Kodi, never reported to the server."""
    player, api = make_player(monkeypatch, url=NATIVE_SONG)
    # An entry an earlier, unclaimed play of the same song left behind, and
    # one for another song that is not this playback's.
    _queue_song(session="stale")
    _queue_song("6" * 32, session="other")
    _queue_song(session="fresh")

    player.onPlayBackStarted()
    drain(player)
    assert api.calls[0][0] == "playing"
    assert api.calls[0][1]["ItemId"] == SONG
    assert api.calls[0][1]["PlaySessionId"] == "fresh"
    assert state.get_playing_id() == SONG
    # The stale entry went with the claim; the other song's is untouched.
    assert state.claim_play_item_of(SONG) is None
    assert state.claim_play_item_of("6" * 32)["PlaySessionId"] == "other"


def test_a_plugin_url_that_is_not_kofins_claims_nothing(monkeypatch):
    foreign = "plugin://plugin.audio.other/?id=%s" % SONG
    player, api = make_player(monkeypatch, url=foreign)
    _queue_song()
    monkeypatch.setattr(player, "isPlayingAudio", lambda: False)
    player.onPlayBackStarted()
    drain(player)
    assert api.calls == []
    assert state.claim_play_item_of(SONG) is not None
