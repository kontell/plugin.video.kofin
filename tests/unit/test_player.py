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


def test_playlist_song_is_claimed_from_the_musicdb_path(monkeypatch):
    """A saved playlist plays ``musicdb://songs/<id>`` with no music tag, so
    Kodi announces the song without a database id (measured on Kodi 21). The
    play still has to be claimed, or the whole playlist goes unreported."""
    from kofin.service import player as player_mod

    _map_song(monkeypatch)
    api = LookupApi()
    path = "musicdb://songs/55.flac"
    monkeypatch.setattr(
        "xbmc.Player",
        lambda: type("P", (), {"getPlayingFile": lambda self: path})(),
    )

    pushed = player_mod.backfill_library_claim(
        {"item": {"title": "04. Golden Earring - Radar Love", "type": "song"}},
        api,  # type: ignore[arg-type]
    )

    assert pushed is True
    claimed = state.claim_play_item(path)
    assert claimed is not None
    assert claimed["Id"] == "jf-song-1"


def test_idless_announcement_off_a_musicdb_path_stays_foreign(monkeypatch):
    """No database id and nothing in the path to recover one: somebody
    else's audio, which must never be claimed."""
    from kofin.service import player as player_mod

    _map_song(monkeypatch)
    api = LookupApi()
    monkeypatch.setattr(
        "xbmc.Player",
        lambda: type("P", (), {"getPlayingFile": lambda self: "/home/me/song.mp3"})(),
    )

    assert (
        player_mod.backfill_library_claim(
            {"item": {"title": "Some Track", "type": "song"}},
            api,  # type: ignore[arg-type]
        )
        is False
    )
    assert api.item_requests == []


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


# --- Jellyfin default tracks and the stream menu -------------------------------
#
# The server resolves both defaults from the viewer's Jellyfin profile and
# returns them on every MediaSource; nothing had ever applied them, so Kodi
# picked from its own language settings instead (plan §2.9).


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
