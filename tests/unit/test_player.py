import json

import pytest

from kofin.core import state
from kofin.service.player import Player
from tests.unit.fakes import FakeAddon, FakeWindow


class RecordingApi:
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
        self.calls.append(("close_transcode", device_id))


class FakeMonitor:
    def waitForAbort(self, seconds=0):
        return False

    def abortRequested(self):
        return False


def _fake_jsonrpc(query: str) -> str:
    req = json.loads(query)
    method = req.get("method")
    if method == "Application.GetProperties":
        return json.dumps({"result": {"volume": 77, "muted": False}})
    if method == "Player.GetActivePlayers":
        return json.dumps({"result": [{"playerid": 1, "type": "video"}]})
    if method == "Player.GetProperties":
        props = set(req.get("params", {}).get("properties") or [])
        result = {}
        if "currentaudiostream" in props or "currentsubtitle" in props:
            result["currentaudiostream"] = {"index": 1, "language": "jpn"}
            result["currentsubtitle"] = {"index": 2, "name": "00.eng.srt"}
            result["subtitleenabled"] = True
        if "subtitles" in props:
            result["subtitles"] = [
                {"index": 0, "name": "English PGS"},
                {"index": 1, "name": "French PGS"},
                {"index": 2, "name": "00.eng.srt"},
            ]
        return json.dumps({"result": result})
    return json.dumps({"result": {}})


@pytest.fixture(autouse=True)
def kodi_fakes(monkeypatch):
    FakeWindow.store = {}
    # Every toggle reads false: no segment engine unless a test opts in.
    FakeAddon.store = {}
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmc.Monitor", FakeMonitor)
    monkeypatch.setattr("xbmc.executeJSONRPC", _fake_jsonrpc)


def make_player(monkeypatch, url="http://s/stream"):
    api = RecordingApi()
    player = Player(api)  # type: ignore[arg-type]
    monkeypatch.setattr(player, "getPlayingFile", lambda: url)
    monkeypatch.setattr(player, "getTime", lambda: 42.0)
    monkeypatch.setattr(player, "_start_ticker", lambda: None)
    monkeypatch.setattr(
        player,
        "getAvailableSubtitleStreams",
        lambda: ["English PGS", "French PGS", "00.eng.srt"],
    )
    return player, api


def queue_item(url="http://s/stream", method="DirectStream", **extra):
    payload = {
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
        "AudioMap": {"1": 0, "2": 1},
        "EmbeddedSubMap": {"5": 0},
        "SubsAttachOrder": [3],
        "SubsPaths": ["/cache/ps1/00.eng.srt"],
        "SubsMapping": {},
        "SubsMappingReady": False,
    }
    payload.update(extra)
    state.push_play_item(payload)


def test_claim_and_report_lifecycle(monkeypatch):
    player, api = make_player(monkeypatch)
    queue_item()

    player.onPlayBackStarted()
    assert api.calls[0][0] == "playing"
    start = api.calls[0][1]
    assert start["ItemId"] == "m1"
    assert start["PositionTicks"] == 100_000_000
    assert start["VolumeLevel"] == 77
    assert state.get_playing_id() == "m1"

    player.onPlayBackSeek(65_000, 0)
    seek = api.calls[-1][1]
    assert seek["PositionTicks"] == 650_000_000

    player.report_progress()
    tick = api.calls[-1][1]
    assert tick["PositionTicks"] == 420_000_000

    player.onPlayBackStopped()
    kinds = [kind for kind, _data in api.calls]
    assert kinds[-1] == "stopped"
    assert api.calls[-1][1]["PositionTicks"] == 420_000_000
    assert state.get_playing_id() == ""


def test_foreign_playback_is_ignored(monkeypatch):
    player, api = make_player(monkeypatch)
    player.onPlayBackStarted()
    assert api.calls == []
    player.onPlayBackStopped()
    assert api.calls == []


def test_transcode_stop_closes_encoding(monkeypatch):
    player, api = make_player(monkeypatch)
    queue_item(method="Transcode")
    player.onPlayBackStarted()
    player.onPlayBackEnded()
    assert ("close_transcode", "dev1") in api.calls


def test_pause_resume_report_state(monkeypatch):
    player, api = make_player(monkeypatch)
    queue_item()
    player.onPlayBackStarted()
    player.onPlayBackPaused()
    assert api.calls[-1][1]["IsPaused"] is True
    player.onPlayBackResumed()
    assert api.calls[-1][1]["IsPaused"] is False


# --- SyncPlay forwarding (phase 4) -------------------------------------------


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


def test_on_av_started_reconciles_subs_and_observes_indexes(monkeypatch):
    player, api = make_player(monkeypatch)
    queue_item()
    player.onPlayBackStarted()
    player.onAVStarted()

    item = player.current_item()
    assert item is not None
    assert item["SubsMappingReady"] is True
    # Embedded 5→0,  external 3 at absolute 2 (basename 00.eng.srt)
    assert item["SubsMapping"]["0"] == 5
    assert item["SubsMapping"]["2"] == 3
    # JSON-RPC current audio index 1 → JF 2; sub index 2 → JF 3
    assert item["AudioStreamIndex"] == 2
    assert item["SubtitleStreamIndex"] == 3

    player.report_progress()
    progress = [data for kind, data in api.calls if kind == "progress"][-1]
    assert progress["AudioStreamIndex"] == 2
    assert progress["SubtitleStreamIndex"] == 3


def test_on_av_change_updates_indexes(monkeypatch):
    player, api = make_player(monkeypatch)
    queue_item()
    player.onPlayBackStarted()
    player.onAVStarted()

    def rpc_switched(query: str) -> str:
        req = json.loads(query)
        if req.get("method") == "Player.GetProperties":
            props = set(req.get("params", {}).get("properties") or [])
            if "currentaudiostream" in props:
                return json.dumps(
                    {
                        "result": {
                            "currentaudiostream": {"index": 0},
                            "currentsubtitle": {"index": 0},
                            "subtitleenabled": True,
                        }
                    }
                )
        return _fake_jsonrpc(query)

    monkeypatch.setattr("xbmc.executeJSONRPC", rpc_switched)
    player.onAVChange()
    item = player.current_item()
    assert item["AudioStreamIndex"] == 1
    assert item["SubtitleStreamIndex"] == 5


def test_apply_stream_switch_local_audio(monkeypatch):
    player, api = make_player(monkeypatch)
    queue_item()
    player.onPlayBackStarted()
    player.onAVStarted()
    applied = []
    monkeypatch.setattr(player, "setAudioStream", lambda i: applied.append(i))
    ok = player.apply_stream_switch("audio", 2)
    assert ok is True
    assert applied == [1]  # AudioMap 2 -> kodi 1
    assert player.current_item()["AudioStreamIndex"] == 2
    assert any(kind == "progress" for kind, _ in api.calls)


def test_apply_stream_switch_refuses_unready_external_sub(monkeypatch):
    player, api = make_player(monkeypatch)
    queue_item(SubsMappingReady=False, SubsMapping={})
    player.onPlayBackStarted()
    # Do not reconcile — mapping stays unready with SubsAttachOrder present.
    applied = []
    monkeypatch.setattr(player, "setSubtitleStream", lambda i: applied.append(i))
    ok = player.apply_stream_switch("subtitle", 3)
    assert ok is False
    assert applied == []


def test_apply_stream_switch_subtitle_when_ready(monkeypatch):
    player, api = make_player(monkeypatch)
    queue_item()
    player.onPlayBackStarted()
    player.onAVStarted()
    applied = []
    shown = []
    monkeypatch.setattr(player, "setSubtitleStream", lambda i: applied.append(i))
    monkeypatch.setattr(player, "showSubtitles", lambda v: shown.append(v))
    ok = player.apply_stream_switch("subtitle", 3)
    assert ok is True
    assert applied == [2]
    assert shown == [True]
    assert player.current_item()["SubtitleStreamIndex"] == 3


def test_transcode_audio_restart_no_double_stop_or_delete(monkeypatch):
    player, api = make_player(monkeypatch)
    played = []
    monkeypatch.setattr(player, "play", lambda url, li=None: played.append(url))
    monkeypatch.setattr(player, "getTime", lambda: 120.0)

    source = {
        "Id": "src1",
        "SupportsDirectStream": False,
        "TranscodingUrl": "/videos/m1/master.m3u8?x=1",
        "TranscodingSubProtocol": "hls",
        "DefaultAudioStreamIndex": 2,
        "DefaultSubtitleStreamIndex": None,
        "MediaStreams": [
            {"Type": "Audio", "Index": 1, "Language": "eng", "Codec": "aac"},
            {"Type": "Audio", "Index": 2, "Language": "jpn", "Codec": "aac"},
        ],
        "Bitrate": 5_000_000,
    }

    class RestartApi(RecordingApi):
        server = "http://s:8096"

        def item(self, item_id):
            return {"Id": item_id, "Type": "Movie", "Name": "M", "RunTimeTicks": 0}

        def playback_info(self, item_id, profile, start_ticks=0, **kwargs):
            return {"MediaSources": [source], "PlaySessionId": "ps-new"}

    api2 = RestartApi()
    player.api = api2  # type: ignore[assignment]

    queue_item(
        method="Transcode",
        ForceTranscode=True,
        BitrateOverrideMbps=2.0,
        AudioMap={"1": 0, "2": 1},
        AudioStreamIndex=1,
        Path="http://s/old",
    )
    # Claim with matching path
    monkeypatch.setattr(player, "getPlayingFile", lambda: "http://s/old")
    player.onPlayBackStarted()
    assert player.current_item()["PlayMethod"] == "Transcode"

    # Attach helpers used by restart
    monkeypatch.setattr(
        "kofin.plugin.play.attach_text_subtitles",
        lambda api, source, ps: ([], {}),
    )
    monkeypatch.setattr(
        "kofin.plugin.listitems.build",
        lambda *a, **k: type(
            "LI",
            (),
            {
                "setPath": lambda self, p: None,
                "setMimeType": lambda self, m: None,
                "setContentLookup": lambda self, v: None,
                "setSubtitles": lambda self, s: None,
            },
        )(),
    )

    offered = []
    monkeypatch.setattr(
        player, "offer_delete", lambda item: offered.append(item) or False
    )

    ok = player.apply_stream_switch("audio", 2)
    assert ok is True
    assert played  # Player.play called with new URL
    assert player._stream_restart is True or player._restart_teardown_done is True
    # Synthetic stop must not delete
    player.onPlayBackStopped()
    assert offered == []
    # Exactly one session_stopped for old session (restart teardown)
    stopped = [c for c in api2.calls if c[0] == "stopped"]
    assert len(stopped) == 1
    assert stopped[0][1]["PlaySessionId"] == "ps1"


def test_syncplay_refuses_stream_switch(monkeypatch):
    player, api = make_player(monkeypatch)
    queue_item(method="Transcode", AudioMap={"1": 0, "2": 1})
    player.onPlayBackStarted()
    player.syncplay_group_active = True
    ok = player.apply_stream_switch("audio", 2)
    assert ok is False


def _tc_multi_audio_extra():
    return {
        "method": "Transcode",
        "AudioMap": {"1": 0, "2": 1},
        "AudioStreamIndex": 1,
        "AudioStreams": [
            {"Index": 1, "DisplayTitle": "English", "Language": "eng", "Codec": "aac"},
            {"Index": 2, "DisplayTitle": "Japanese", "Language": "jpn", "Codec": "aac"},
        ],
    }


def test_claim_sets_pick_audio_prop_for_tc_multi(monkeypatch):
    player, api = make_player(monkeypatch)
    queue_item(**_tc_multi_audio_extra())
    player.onPlayBackStarted()
    assert state.is_playing_pick_audio() is True
    player.onPlayBackStopped()
    assert state.is_playing_pick_audio() is False


def test_claim_clears_pick_audio_for_directstream(monkeypatch):
    player, api = make_player(monkeypatch)
    queue_item(
        method="DirectStream",
        AudioStreams=[
            {"Index": 1, "DisplayTitle": "A"},
            {"Index": 2, "DisplayTitle": "B"},
        ],
    )
    player.onPlayBackStarted()
    assert state.is_playing_pick_audio() is False


def test_pick_audio_track_applies_choice(monkeypatch):
    player, api = make_player(monkeypatch)
    queue_item(**_tc_multi_audio_extra())
    player.onPlayBackStarted()

    class FakeDialog:
        def select(self, heading, labels, preselect=0):
            assert "English" in labels
            assert "Japanese" in labels
            assert preselect == 0  # current AudioStreamIndex 1
            return 1  # Japanese

    switched = []
    monkeypatch.setattr("xbmcgui.Dialog", FakeDialog)
    monkeypatch.setattr(
        player,
        "apply_stream_switch",
        lambda kind, idx: switched.append((kind, idx)) or True,
    )
    assert player.pick_audio_track() is True
    assert switched == [("audio", 2)]


def test_pick_audio_track_cancel_and_same_index(monkeypatch):
    player, api = make_player(monkeypatch)
    queue_item(**_tc_multi_audio_extra())
    player.onPlayBackStarted()
    switched = []
    monkeypatch.setattr(
        player,
        "apply_stream_switch",
        lambda kind, idx: switched.append((kind, idx)) or True,
    )

    class CancelDialog:
        def select(self, heading, labels, preselect=0):
            return -1

    monkeypatch.setattr("xbmcgui.Dialog", CancelDialog)
    assert player.pick_audio_track() is False
    assert switched == []

    class KeepDialog:
        def select(self, heading, labels, preselect=0):
            return 0  # same as current index 1

    monkeypatch.setattr("xbmcgui.Dialog", KeepDialog)
    assert player.pick_audio_track() is True
    assert switched == []


def test_pick_audio_track_refuses_syncplay_and_empty(monkeypatch):
    player, api = make_player(monkeypatch)
    assert player.pick_audio_track() is False  # nothing playing

    queue_item(**_tc_multi_audio_extra())
    player.onPlayBackStarted()
    player.syncplay_group_active = True
    assert player.pick_audio_track() is False


def test_syncplay_detached_is_a_noop(monkeypatch):
    player, api = make_player(monkeypatch)
    queue_item()
    player.onPlayBackStarted()  # syncplay is None: nothing to forward
    player.onAVStarted()
    player.onPlayBackStopped()
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


# --- library-originated claims (music) ---------------------------------------


class LookupApi(RecordingApi):
    """Serves the one item the back-fill fetches after the id mapping."""

    def __init__(self, item=None, error=None):
        super().__init__()
        self.item_requests = []
        self._item = item or {
            "Id": "jf-song-1",
            "Type": "Audio",
            "RunTimeTicks": 1800000000,
            "MediaSources": [{"Id": "src-1"}],
        }
        self._error = error

    def item(self, item_id):
        self.item_requests.append(item_id)
        if self._error:
            raise self._error
        return self._item


def _map_song(monkeypatch, jellyfin_id="jf-song-1"):
    """Stand in for the kofin.db kodi_id -> jellyfin_id lookup."""

    class FakeDb:
        def __init__(self, cursor):
            pass

        def get_item_by_kodi_id(self, kodi_id, media):
            return jellyfin_id if (kodi_id, media) == (55, "song") else None

    class FakeOpened:
        cursor = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("kofin.sync.db.Database", lambda name: FakeOpened())
    monkeypatch.setattr("kofin.sync.kofindb.JellyfinDatabase", FakeDb)


def test_song_playback_is_claimed_via_backfill(monkeypatch):
    """Songs are written as direct stream URLs, so nothing queues them from
    the play route -- the Player.OnPlay notification has to."""
    from kofin.service import player as player_mod

    _map_song(monkeypatch)
    api = LookupApi()
    monkeypatch.setattr(
        "xbmc.Player",
        lambda: type(
            "P", (), {"getPlayingFile": lambda self: "http://s/Audio/x/stream.mp3"}
        )(),
    )

    pushed = player_mod.backfill_library_claim(
        {"item": {"id": 55, "type": "song"}}, api  # type: ignore[arg-type]
    )

    assert pushed is True
    assert api.item_requests == ["jf-song-1"]
    claimed = state.claim_play_item("http://s/Audio/x/stream.mp3")
    assert claimed is not None
    assert claimed["Id"] == "jf-song-1"
    assert claimed["PlayMethod"] == "DirectStream"
    assert claimed["MediaSourceId"] == "src-1"


def test_plugin_song_is_not_claimed_twice_when_still_queued(monkeypatch):
    """musicTranscode on: the play route queued this playback already, and the
    OnPlay notification must not add a second entry."""
    from kofin.service import player as player_mod

    _map_song(monkeypatch)
    api = LookupApi()
    path = "http://s/audio/x/stream.opus?AudioBitrate=128000"
    monkeypatch.setattr(
        "xbmc.Player",
        lambda: type("P", (), {"getPlayingFile": lambda self: path})(),
    )
    state.push_play_item({"Id": "jf-song-1", "Path": path})

    assert (
        player_mod.backfill_library_claim(
            {"item": {"id": 55, "type": "song"}}, api  # type: ignore[arg-type]
        )
        is False
    )
    assert api.item_requests == []
    # The play route's entry is untouched and still the only one.
    assert state.claim_play_item(path) is not None
    assert state.claim_play_item(path) is None


def test_plugin_song_is_not_claimed_twice_after_the_player_claimed(monkeypatch):
    """The same guard for the ordering seen live: onPlayBackStarted claims the
    queued entry *before* Player.OnPlay arrives, so the queue is already empty
    and only the published playing id says the play route handled it."""
    from kofin.service import player as player_mod

    _map_song(monkeypatch)
    api = LookupApi()
    path = "http://s/audio/x/stream.opus?AudioBitrate=128000"
    monkeypatch.setattr(
        "xbmc.Player",
        lambda: type("P", (), {"getPlayingFile": lambda self: path})(),
    )
    state.set_playing_id("jf-song-1")

    assert (
        player_mod.backfill_library_claim(
            {"item": {"id": 55, "type": "song"}}, api  # type: ignore[arg-type]
        )
        is False
    )
    assert api.item_requests == []
    assert state.claim_play_item(path) is None

    # A different item playing is no reason to skip: that is a genuine
    # direct-path song starting while the previous one has not stopped yet.
    state.set_playing_id("jf-other")
    assert (
        player_mod.backfill_library_claim(
            {"item": {"id": 55, "type": "song"}}, api  # type: ignore[arg-type]
        )
        is True
    )


def test_video_playback_is_never_backfilled(monkeypatch):
    """Video always goes through plugin:// and is claimed the normal way;
    back-filling it would risk double-claiming a legitimate play."""
    from kofin.service import player as player_mod

    _map_song(monkeypatch)
    api = LookupApi()

    for media in ("movie", "episode", "musicvideo"):
        assert (
            player_mod.backfill_library_claim(
                {"item": {"id": 55, "type": media}}, api  # type: ignore[arg-type]
            )
            is False
        )
    assert api.item_requests == []


def test_unmapped_row_stays_foreign(monkeypatch):
    """A song Kodi knows about but kofin does not is somebody else's."""
    from kofin.service import player as player_mod

    _map_song(monkeypatch, jellyfin_id=None)
    api = LookupApi()
    monkeypatch.setattr(
        "xbmc.Player",
        lambda: type("P", (), {"getPlayingFile": lambda self: "http://s/x.mp3"})(),
    )

    assert (
        player_mod.backfill_library_claim(
            {"item": {"id": 55, "type": "song"}}, api  # type: ignore[arg-type]
        )
        is False
    )
    assert api.item_requests == []


def test_backfill_survives_an_unreachable_server(monkeypatch):
    """A failed fetch leaves the play unreported, never breaks playback."""
    from kofin.service import player as player_mod

    _map_song(monkeypatch)
    api = LookupApi(error=RuntimeError("offline"))
    monkeypatch.setattr(
        "xbmc.Player",
        lambda: type("P", (), {"getPlayingFile": lambda self: "http://s/x.mp3"})(),
    )

    assert (
        player_mod.backfill_library_claim(
            {"item": {"id": 55, "type": "song"}}, api  # type: ignore[arg-type]
        )
        is False
    )


# --- delete after watching ---------------------------------------------------


TICK = 10_000_000  # RunTimeTicks per second


def finished_item(item_type="Movie", position=95.0, runtime=100.0):
    return {
        "Id": "m1",
        "Type": item_type,
        "Name": "Some Film",
        "Runtime": int(runtime * TICK),
        "CurrentPosition": position,
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
