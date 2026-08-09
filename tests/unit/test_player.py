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


def drain(player):
    """Reports post on the reporter's own thread (W2.2); tests that assert on
    api.calls wait for the pipe to run dry first."""
    assert player._reporter.flush(5)


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
    drain(player)
    assert api.calls[0][0] == "playing"
    start = api.calls[0][1]
    assert start["ItemId"] == "m1"
    assert start["PositionTicks"] == 100_000_000
    assert start["VolumeLevel"] == 77
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


def test_callbacks_return_while_the_post_is_still_in_flight(monkeypatch):
    """The reporter's whole point (audit finding #2): Kodi delivers player
    callbacks on the announcement thread every addon shares, so the callback
    must hand the network off and return — a server that answers slowly may
    hold the *report*, never the thread."""
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


def test_transcode_stop_closes_encoding(monkeypatch):
    player, api = make_player(monkeypatch)
    queue_item(method="Transcode")
    player.onPlayBackStarted()
    player.onPlayBackEnded()
    drain(player)
    assert ("close_transcode", "dev1") in api.calls


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


def test_an_account_that_cannot_delete_is_never_asked(monkeypatch):
    """Without this the prompt came up after every single episode for an
    account with no EnableContentDeletion, and every yes answered
    "Server request failed"."""
    player, _api = make_player(monkeypatch)
    enable_delete()

    offered, prompted = offer_and_wait(player, finished_item(can_delete=False))

    assert offered is False
    assert prompted == []


# -- auto-next (plan W4.1) and the remove-after-watching offer (W4.5) ---------


def test_auto_next_fires_once_at_eighty_percent(monkeypatch):
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
        "CurrentPosition": 750.0,
        "MediaSourceId": "src",
        "PlaySessionId": "ps",
    }
    player._maybe_auto_next()
    assert fired == []  # 75%: not yet

    player._item["CurrentPosition"] = 810.0
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
    assert notified == [(player_module.ipc.DOWNLOAD_REMOVE, {"Id": "d1"})]

    notified.clear()
    FakeAddon.store["downloadsDeleteAutomatically"] = "false"
    monkeypatch.setattr(player_module.threading, "Thread", ImmediateThread)

    class YesDialog:
        def yesno(self, heading, message, **kwargs):
            return True

    monkeypatch.setattr(player_module.xbmcgui, "Dialog", YesDialog)
    assert player.offer_remove_download(item) is True
    assert notified == [(player_module.ipc.DOWNLOAD_REMOVE, {"Id": "d1"})]

    class NoDialog:
        def yesno(self, heading, message, **kwargs):
            return False

    notified.clear()
    monkeypatch.setattr(player_module.xbmcgui, "Dialog", NoDialog)
    assert player.offer_remove_download(item) is True  # raised, declined
    assert notified == []


def test_remove_offer_covers_automatic_downloads_too(monkeypatch):
    """One answer per watched download, whoever queued it.

    This path used to refuse anything auto-origin and leave it to the
    retention sweep, while the sweep refused anything the user had queued —
    so the same watched episode was handled two different ways depending on
    how it had arrived, under two settings that did not mention each other.
    """
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
    assert notified == [(player_module.ipc.DOWNLOAD_REMOVE, {"Id": "d1"})]

    # Nothing downloaded, nothing to offer.
    notified.clear()
    monkeypatch.setattr("kofin.downloads.store.get", lambda item_id: None)
    assert player.offer_remove_download(_watched_download_item()) is False
    assert notified == []


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


# -- the offline segment cache and offline claims (plan W4.7) -----------------


def _seed_local_rows(tmp_path):
    import sqlite3

    from kofin.downloads import store as downloads_store
    from kofin.sync import db as sync_db

    sync_db.reset_overrides()
    sync_db.set_path_override("kofin", str(tmp_path / "kofin.db"))
    video_path = tmp_path / "MyVideos.db"
    connection = sqlite3.connect(str(video_path))
    connection.execute("CREATE TABLE episode (idEpisode INTEGER PRIMARY KEY, c00 TEXT)")
    connection.execute(
        "CREATE TABLE streamdetails (idFile INTEGER, iStreamType INTEGER, iVideoDuration INTEGER)"
    )
    connection.execute("INSERT INTO episode VALUES (11, 'Blood Test')")
    connection.execute("INSERT INTO streamdetails VALUES (7, 0, 1260)")
    connection.commit()
    connection.close()
    sync_db.set_path_override("video", str(video_path))

    with sync_db.Database("kofin") as opened:
        opened.cursor.execute(
            "INSERT INTO jellyfin (jellyfin_id, kodi_id, kodi_fileid, kodi_pathid, media_type) "
            "VALUES ('j1', 11, 7, 1, 'episode')"
        )
    downloads_store.queue(
        downloads_store.Download(
            jellyfin_id="j1", media_type="episode", series_id="s1", queued_at=100
        )
    )
    downloads_store.claim()
    downloads_store.finish("j1", "TV/S/Season 01/e.mkv", "mkv", 1)
    return sync_db


def test_offline_claim_builds_from_local_rows(monkeypatch, tmp_path):
    from kofin.service import player as player_module

    sync_db = _seed_local_rows(tmp_path)
    try:
        claim = player_module._offline_claim("j1", "episode", "/dl/e.mkv")
    finally:
        pass
    assert claim is not None
    assert claim["Type"] == "Episode" and claim["Name"] == "Blood Test"
    assert claim["SeriesId"] == "s1"
    assert claim["Runtime"] == 1260 * 10_000_000  # watched_to_end works offline
    assert claim["Path"] == "/dl/e.mkv"

    # Anything not downloaded stays unclaimed: foreign playback is foreign.
    assert player_module._offline_claim("stranger", "episode", "/x.mkv") is None
    sync_db.reset_overrides()


def test_backfill_attaches_the_cached_segments_offline(monkeypatch, tmp_path):
    import json as json_module

    from kofin.downloads import store as downloads_store
    from kofin.service import player as player_module

    sync_db = _seed_local_rows(tmp_path)
    downloads_store.set_segments(
        "j1",
        json_module.dumps(
            {"Items": [{"Type": "Intro", "StartTicks": 0, "EndTicks": 30 * 10**7}]}
        ),
    )

    class PlayingStub:
        def getPlayingFile(self):
            return "/dl/e.mkv"

    monkeypatch.setattr(player_module.xbmc, "Player", PlayingStub)
    monkeypatch.setattr(player_module, "mapped_jellyfin_id", lambda k, m: "j1")
    monkeypatch.setattr(player_module, "library_claim", lambda *a: None)  # offline

    api = RecordingApi()
    assert (
        player_module.backfill_library_claim(
            {"item": {"id": 11, "type": "episode"}}, api
        )
        is True
    )
    claimed = state.claim_play_item("/dl/e.mkv")
    assert claimed is not None
    assert claimed["Segments"] == [{"Type": "Introduction", "Start": 0.0, "End": 30.0}]
    sync_db.reset_overrides()


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
    player._segments_loaded = False

    import threading as threading_module

    player.prepare_segment_state(threading_module.Event())

    assert player._segments == [] and player._segments_loaded is True
    assert player._next_episode is None
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
