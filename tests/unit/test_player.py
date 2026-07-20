import pytest

from kofin.core import state
from kofin.service.player import Player
from tests.unit.fakes import FakeAddon, FakeWindow


class RecordingApi:
    def __init__(self):
        self.calls = []

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


def make_player(monkeypatch, url="http://s/stream"):
    api = RecordingApi()
    player = Player(api)  # type: ignore[arg-type]
    monkeypatch.setattr(player, "getPlayingFile", lambda: url)
    monkeypatch.setattr(player, "getTime", lambda: 42.0)
    monkeypatch.setattr(player, "_start_ticker", lambda: None)
    return player, api


def queue_item(url="http://s/stream", method="DirectStream"):
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
        }
    )


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
