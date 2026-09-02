import pytest

from kofin.core.http import JellyfinError
from kofin.plugin import play
from kofin.plugin.router import dispatch
from tests.unit.fakes import FakeApi

SERVER = "http://s:8096"


def test_stream_url_direct_video():
    url, method = play.stream_url(
        SERVER,
        {"Type": "Movie", "Id": "m1"},
        {"Id": "src1", "SupportsDirectStream": True, "Container": "mkv"},
        "dev1",
        "ps1",
    )
    assert method == "DirectStream"
    assert url == (
        "http://s:8096/Videos/m1/stream.mkv"
        "?static=true&mediaSourceId=src1&deviceId=dev1&playSessionId=ps1"
    )


def test_stream_url_audio_kind_and_multi_container():
    url, method = play.stream_url(
        SERVER,
        {"Type": "Audio", "Id": "a1"},
        {"Id": "src1", "SupportsDirectPlay": True, "Container": "flac,ogg"},
        "dev1",
        "ps1",
    )
    assert url.startswith("http://s:8096/Audio/a1/stream.flac?")
    assert method == "DirectStream"


def test_stream_url_transcode():
    url, method = play.stream_url(
        SERVER,
        {"Type": "Movie", "Id": "m1"},
        {"Id": "src1", "TranscodingUrl": "/videos/m1/master.m3u8?x=1"},
        "dev1",
        "ps1",
    )
    assert method == "Transcode"
    assert url == "http://s:8096/videos/m1/master.m3u8?x=1"


def test_stream_url_remux_still_reports_transcode():
    # Every stream off the transcoding endpoint reports as Transcode, remux
    # included — matching pvr.kofin and jellyfin-kodi. The dashboard takes its
    # remux wording from the server's TranscodingInfo, not from this value.
    _, method = play.stream_url(
        SERVER,
        {"Type": "Movie", "Id": "m1"},
        {
            "Id": "src1",
            "TranscodingUrl": (
                "/videos/m1/master.m3u8?VideoCodec=av1"
                "&TranscodeReasons=DirectPlayError"
            ),
        },
        "d",
        "p",
    )
    assert method == "Transcode"


def test_stream_url_unplayable_raises():
    with pytest.raises(JellyfinError):
        play.stream_url(SERVER, {"Id": "m1"}, {"Id": "s"}, "d", "p")


def test_stream_url_live_channel_never_takes_the_static_branch():
    # A live channel's source claims SupportsDirectPlay, but
    # /stream?static=true of an infinite stream hands the demuxer a download
    # that never begins (pvr sync plan P0b). The opened live stream's
    # TranscodingUrl is the stream that plays.
    url, method = play.stream_url(
        SERVER,
        {"Type": "TvChannel", "Id": "c1"},
        {
            "Id": "src1",
            "SupportsDirectPlay": True,
            "IsInfiniteStream": True,
            "TranscodingUrl": "/videos/c1/live.m3u8?x=1",
        },
        "dev1",
        "ps1",
    )
    assert method == "Transcode"
    assert url == "http://s:8096/videos/c1/live.m3u8?x=1"


def test_stream_url_live_channel_plays_the_provider_direct():
    # The server allowed direct play: the source's Path is the provider's
    # own stream, and that is what plays — the one live stream whose clock
    # every member shares (P4).
    url, method = play.stream_url(
        SERVER,
        {"Type": "TvChannel", "Id": "c1"},
        {
            "Id": "src1",
            "SupportsDirectPlay": True,
            "IsInfiniteStream": True,
            "Container": "hls",
            "Path": "https://provider.example/live/abc.m3u8",
        },
        "dev1",
        "ps1",
    )
    assert method == "DirectPlay"
    assert url == "https://provider.example/live/abc.m3u8"


def test_stream_url_live_channel_direct_needs_a_web_path():
    # A tuner's local path is nothing this Kodi can open: the transcode
    # stays the route.
    url, method = play.stream_url(
        SERVER,
        {"Type": "TvChannel", "Id": "c1"},
        {
            "Id": "src1",
            "SupportsDirectPlay": True,
            "IsInfiniteStream": True,
            "Path": "/dev/dvb/adapter0",
            "TranscodingUrl": "/videos/c1/live.m3u8?x=1",
        },
        "dev1",
        "ps1",
    )
    assert method == "Transcode"


def test_stream_url_infinite_source_without_transcode_raises():
    # No static fallback for a live stream: failing loudly beats handing
    # Kodi a URL that times out at the demuxer half a minute later.
    with pytest.raises(JellyfinError):
        play.stream_url(
            SERVER,
            {"Type": "TvChannel", "Id": "c1"},
            {"Id": "src1", "SupportsDirectPlay": True, "IsInfiniteStream": True},
            "d",
            "p",
        )


def test_mime_for():
    assert play.mime_for({"Container": "mkv"}, "DirectStream") == "video/x-matroska"
    assert play.mime_for({"Container": "mkv,mp4"}, "DirectStream") == "video/x-matroska"
    assert play.mime_for({"Container": "anything"}, "Transcode") == play.HLS_MIME
    assert play.mime_for({"Container": "unknown"}, "DirectStream") == ""


def test_mime_for_http_transcode_is_not_hls():
    # The music transcoding profile is plain http; calling its stream an HLS
    # playlist makes Kodi parse the audio as m3u8.
    source = {
        "Container": "flac",
        "TranscodingSubProtocol": "http",
        "TranscodingContainer": "opus",
    }
    assert play.mime_for(source, "Transcode") == "audio/ogg"
    assert play.mime_for(dict(source, TranscodingContainer="mp3"), "Transcode") == (
        "audio/mpeg"
    )
    # An hls sub-protocol, or none at all, still means HLS.
    assert play.mime_for(dict(source, TranscodingSubProtocol="hls"), "Transcode") == (
        play.HLS_MIME
    )


def test_rewrite_bitrates_replaces_server_values():
    url = (
        "http://s:8096/videos/m1/master.m3u8"
        "?PlaySessionId=abc&VideoBitrate=139616000&AudioBitrate=384000&api_key=k"
    )
    out = play.rewrite_bitrates(url, 10_000_000, 384)
    assert out.startswith("http://s:8096/videos/m1/master.m3u8?")
    # Opaque params survive untouched, in their original order.
    assert "PlaySessionId=abc" in out and "api_key=k" in out
    assert out.count("VideoBitrate=") == 1 and out.count("AudioBitrate=") == 1
    # audio = min(384k, budget/10) = 384k; video takes the rest.
    assert "AudioBitrate=384000" in out
    assert "VideoBitrate=9616000" in out


def test_rewrite_bitrates_small_budget_keeps_video_positive():
    # The bug a fixed 384k reservation causes: a 0.5 Mbit/s context transcode
    # would leave VideoBitrate at 116000 with a flat reservation, and the
    # server rejects anything negative once the budget drops below it.
    out = play.rewrite_bitrates("http://s/x?a=1", 500_000, 384)
    assert "AudioBitrate=50000" in out
    assert "VideoBitrate=450000" in out


def test_rewrite_bitrates_without_query_is_left_alone():
    assert play.rewrite_bitrates("http://s/x", 500_000, 384) == "http://s/x"


def test_deny_video_stream_copy_states_the_intent():
    url = "http://s:8096/videos/m1/master.m3u8?PlaySessionId=abc&VideoBitrate=1"
    out = play.deny_video_stream_copy(url)
    assert out.startswith("http://s:8096/videos/m1/master.m3u8?")
    assert "PlaySessionId=abc" in out and "VideoBitrate=1" in out
    assert out.endswith("&allowVideoStreamCopy=false")


def test_deny_video_stream_copy_replaces_a_server_supplied_value():
    out = play.deny_video_stream_copy("http://s/x?a=1&allowVideoStreamCopy=true&b=2")
    assert out.count("allowVideoStreamCopy=") == 1
    assert "allowVideoStreamCopy=false" in out
    assert "a=1" in out and "b=2" in out


def test_deny_video_stream_copy_without_query_is_left_alone():
    assert play.deny_video_stream_copy("http://s/x") == "http://s/x"


def test_stream_index_param_parsing():
    # -1 is meaningful — it is how "no subtitle" is stated, as distinct from
    # omitting the parameter and letting the Jellyfin profile choose.
    assert play._stream_index("-1") == -1
    assert play._stream_index("3") == 3
    assert play._stream_index(None) is None
    assert play._stream_index("") is None
    assert play._stream_index("junk") is None


def test_play_state_payload():
    item = {"Id": "m1", "Type": "Movie", "RunTimeTicks": 100}
    source = {
        "Id": "src1",
        "RunTimeTicks": 200,
        "DefaultAudioStreamIndex": 1,
        "DefaultSubtitleStreamIndex": 3,
    }
    payload = play.play_state(
        item, source, "http://u", "DirectStream", "ps", "dev", 12.5
    )
    assert payload["Runtime"] == 200
    assert payload["AudioStreamIndex"] == 1
    assert payload["CurrentPosition"] == 12.5
    assert payload["Path"] == "http://u"


def test_play_state_carries_series_id():
    item = {"Id": "e1", "Type": "Episode", "SeriesId": "show9"}
    payload = play.play_state(item, {}, "http://u", "DirectStream", "ps", "dev", 0)
    assert payload["SeriesId"] == "show9"


def test_prefetch_segments_warm_path(monkeypatch):
    monkeypatch.setattr("kofin.core.settings.get_bool", lambda sid: True)
    api = FakeApi(
        media_segments={
            "Items": [{"Type": "Intro", "StartTicks": 0, "EndTicks": 300_000_000}]
        }
    )
    segments = play.prefetch_segments(api, {"Id": "e1", "Type": "Episode"})
    assert segments == [{"Type": "Introduction", "Start": 0.0, "End": 30.0}]


def test_prefetch_segments_skips_non_video_and_disabled(monkeypatch):
    monkeypatch.setattr("kofin.core.settings.get_bool", lambda sid: True)
    api = FakeApi(media_segments={"Items": []})
    assert play.prefetch_segments(api, {"Id": "a1", "Type": "Audio"}) == []
    assert api.calls == []

    monkeypatch.setattr("kofin.core.settings.get_bool", lambda sid: False)
    assert play.prefetch_segments(api, {"Id": "e1", "Type": "Episode"}) == []
    assert api.calls == []


def test_prefetch_segments_failure_defers_to_service(monkeypatch):
    monkeypatch.setattr("kofin.core.settings.get_bool", lambda sid: True)
    api = FakeApi(media_segments=JellyfinError("segments down"))
    assert play.prefetch_segments(api, {"Id": "e1", "Type": "Episode"}) is None


def test_router_parses_resume_argument(monkeypatch):
    seen = {}

    def fake_play(request):
        seen["resume"] = request.resume

    monkeypatch.setattr(
        "kofin.plugin.router._resolve", lambda mode: {"play": fake_play}.get(mode)
    )
    dispatch(["plugin://x/", "7", "?mode=play&id=1", "resume:true"])
    assert seen["resume"] is True
    dispatch(["plugin://x/", "7", "?mode=play&id=1", "resume:false"])
    assert seen["resume"] is False
    dispatch(["plugin://x/", "7", "?mode=play&id=1"])
    assert seen["resume"] is False


def test_choose_bitrate_single_bypasses_dialog(monkeypatch):
    from kofin.plugin import context

    class ExplodingDialog:
        def select(self, *args):
            raise AssertionError("dialog should be bypassed")

    monkeypatch.setattr("xbmcgui.Dialog", ExplodingDialog)
    assert context.choose_bitrate(["10"]) == "10"


def test_choose_bitrate_multi_uses_dialog(monkeypatch):
    from kofin.plugin import context

    class PickSecond:
        def select(self, heading, labels):
            assert labels == ["3 Mbit/s", "10 Mbit/s", "20 Mbit/s"]
            return 1

    monkeypatch.setattr("xbmcgui.Dialog", PickSecond)
    monkeypatch.setattr(
        "kofin.core.settings.localized", lambda sid: "Play with transcoding"
    )
    assert context.choose_bitrate(["3", "10", "20"]) == "10"


def test_choose_bitrate_cancel_and_garbage(monkeypatch):
    from kofin.plugin import context

    class Cancel:
        def select(self, heading, labels):
            return -1

    monkeypatch.setattr("xbmcgui.Dialog", Cancel)
    monkeypatch.setattr("kofin.core.settings.localized", lambda sid: "x")
    assert context.choose_bitrate(["3", "10"]) is None
    # No usable bitrate means nothing to offer; addon.xml hides the item, so
    # inventing a default would transcode at a rate the user never picked.
    assert context.choose_bitrate(["junk", "-5"]) is None
    assert context.choose_bitrate([]) is None


def test_choose_bitrate_source_and_fractional(monkeypatch):
    from kofin.plugin import context

    captured = {}

    class PickFirst:
        def select(self, heading, labels):
            captured["labels"] = labels
            return 0

    monkeypatch.setattr("xbmcgui.Dialog", PickFirst)
    monkeypatch.setattr(
        "kofin.core.settings.localized",
        lambda sid: "Default" if sid == 30206 else "Play with transcoding",
    )
    # 0 == no override, plus a fractional option; both are valid tokens now.
    assert context.choose_bitrate(["0", "0.5", "10"]) == "0"
    assert captured["labels"] == ["Default", "0.5 Mbit/s", "10 Mbit/s"]


def test_choose_bitrate_single_source_bypasses_dialog(monkeypatch):
    from kofin.plugin import context

    class ExplodingDialog:
        def select(self, *args):
            raise AssertionError("dialog should be bypassed")

    monkeypatch.setattr("xbmcgui.Dialog", ExplodingDialog)
    assert context.choose_bitrate(["0"]) == "0"


# --- the resolved item's resume point ----------------------------------------
#
# A resume point on the resolved item overrides the choice the user made at
# Kodi's prompt, and cannot be cleared once stamped — so the play route builds
# the item with the position it resolved, and 0 builds it without one.


class ResumeTagRecorder:
    def __init__(self):
        self.resume_point = None
        self.dbid = None

    def setResumePoint(self, time, totaltime=0.0):
        self.resume_point = time

    def setDbId(self, dbid, *args):
        self.dbid = dbid


class ResumeListItem:
    def __init__(self):
        self.tag = ResumeTagRecorder()
        self.path = ""

    def getVideoInfoTag(self):
        return self.tag

    def getMusicInfoTag(self):
        return self.tag

    def setPath(self, path):
        self.path = path

    def setMimeType(self, mime):
        pass

    def setContentLookup(self, lookup):
        pass

    def setSubtitles(self, subtitles):
        pass


class ResumeApi:
    server = "http://s:8096"

    def __init__(self, item):
        self._item = item
        self.start_ticks = []
        self.kwargs = []

    def item(self, item_id):
        return self._item

    def playback_info(self, item_id, profile, start_ticks=0, **kwargs):
        self.start_ticks.append(start_ticks)
        self.kwargs.append(kwargs)
        return {
            "MediaSources": [
                {
                    "Id": "src1",
                    "SupportsDirectStream": True,
                    "Container": "mkv",
                    "MediaStreams": [
                        {"Index": 1, "Type": "Audio", "Codec": "ac3"},
                        {
                            "Index": 2,
                            "Type": "Subtitle",
                            "Codec": "subrip",
                            "IsExternal": True,
                            "IsTextSubtitleStream": True,
                            "DeliveryMethod": "External",
                            "DeliveryUrl": "/subs/2.srt",
                        },
                    ],
                }
            ],
            "PlaySessionId": "ps1",
        }

    def media_segments(self, item_id):
        return {"Items": []}


@pytest.fixture
def resume_env(monkeypatch):
    from kofin.core import state
    from tests.unit.fakes import FakeAddon, FakeWindow

    FakeAddon.store = {}
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    # No Kodi library row unless a test seeds one.
    monkeypatch.setattr(
        "kofin.core.kodirpc.resume_seconds", lambda kodi_id, media: None
    )

    episode = {
        "Id": "ep1",
        "Type": "Episode",
        "Name": "An Episode",
        "RunTimeTicks": 1500 * 10_000_000,
        "UserData": {"PlaybackPositionTicks": 600 * 10_000_000},
    }
    api = ResumeApi(episode)
    # play.py reads api.http to hand the transport to the subtitle fetch;
    # nothing here exercises subtitles, so any placeholder serves.
    api.http = None
    listitem = ResumeListItem()
    resolved = []

    class Creds:
        is_logged_in = True
        device_id = "dev1"

        @classmethod
        def load(cls):
            return cls()

    class ApiFactory:
        @staticmethod
        def for_plugin(creds):
            return api

    built = {}

    def fake_build(item, server, resume_seconds=None):
        built["resume_seconds"] = resume_seconds
        return listitem

    monkeypatch.setattr(play, "Credentials", Creds)
    monkeypatch.setattr(play, "Api", ApiFactory)
    monkeypatch.setattr(play.listitems, "build", fake_build)
    monkeypatch.setattr(
        "xbmcplugin.setResolvedUrl", lambda h, ok, li: resolved.append(li)
    )

    state.clear_play_queue()
    return {
        "api": api,
        "li": listitem,
        "resolved": resolved,
        "addon": FakeAddon,
        "built": built,
    }


def run_play(params, resume):
    from kofin.plugin.router import Request

    play.play(Request("plugin://x/", 1, params, resume))


def test_resume_true_starts_at_the_server_position(resume_env):
    run_play({"id": "ep1"}, resume=True)
    assert resume_env["api"].start_ticks == [600 * 10_000_000]
    assert resume_env["built"]["resume_seconds"] == 600.0


def test_play_from_beginning_builds_the_item_without_a_resume_point(resume_env):
    # resume:false is Kodi saying "Play from beginning". It landed on the
    # server resume point anyway, because build() stamps one on everything.
    run_play({"id": "ep1"}, resume=False)
    assert resume_env["api"].start_ticks == [0]
    assert resume_env["built"]["resume_seconds"] == 0.0


def test_play_next_start_overrides_a_resume_prompt(resume_env):
    run_play({"id": "ep1", "fromstart": "1"}, resume=True)
    assert resume_env["api"].start_ticks == [0]
    assert resume_env["built"]["resume_seconds"] == 0.0


def test_explicit_start_ticks_wins_and_is_exact(resume_env):
    # A SyncPlay group start says exactly where the group timeline is; the
    # resume offset must not shift it.
    resume_env["addon"].store["resumeJumpBack"] = "-10"
    run_play({"id": "ep1", "startticks": str(300 * 10_000_000)}, resume=True)
    assert resume_env["api"].start_ticks == [300 * 10_000_000]
    assert resume_env["built"]["resume_seconds"] == 300.0


def test_resume_start_carries_the_offset(resume_env):
    resume_env["addon"].store["resumeJumpBack"] = "-10"
    run_play({"id": "ep1"}, resume=True)
    assert resume_env["api"].start_ticks == [590 * 10_000_000]
    assert resume_env["built"]["resume_seconds"] == 590.0


# Kodi seeks a library item to the bookmark in its own database and ignores
# what the resolved item says, so for a library row that bookmark is the start
# position — and it already carries the offset, applied when the sync wrote it.


def test_library_resume_starts_at_kodis_own_bookmark(resume_env, monkeypatch):
    resume_env["addon"].store["resumeJumpBack"] = "-10"
    monkeypatch.setattr(
        "kofin.core.kodirpc.resume_seconds", lambda kodi_id, media: 200.0
    )
    run_play({"id": "ep1", "dbid": "8956"}, resume=True)
    # 200, not 590: the server's position is not what Kodi is about to seek to,
    # and not 190 either -- the offset is already in the bookmark.
    assert resume_env["api"].start_ticks == [200 * 10_000_000]
    assert resume_env["built"]["resume_seconds"] == 200.0


def test_library_resume_honours_a_cleared_bookmark(resume_env, monkeypatch):
    # Kodi will start at 0 whatever the server thinks; saying otherwise reports
    # a position nothing is at.
    monkeypatch.setattr("kofin.core.kodirpc.resume_seconds", lambda kodi_id, media: 0.0)
    run_play({"id": "ep1", "dbid": "8956"}, resume=True)
    assert resume_env["api"].start_ticks == [0]
    assert resume_env["built"]["resume_seconds"] == 0.0


def test_pick_media_source_prefers_matching_id():
    sources = [{"Id": "a"}, {"Id": "b"}, {"Id": "c"}]
    assert play.pick_media_source(sources, "b")["Id"] == "b"
    assert play.pick_media_source(sources, None)["Id"] == "a"
    assert play.pick_media_source(sources, "")["Id"] == "a"
    assert play.pick_media_source(sources, "missing")["Id"] == "a"


def test_play_mediasourceid_selects_that_source(resume_env, monkeypatch):
    """Version library rows pass mediasourceid; play must use that source."""
    resume_env["api"].playback_info = lambda *a, **k: {
        "MediaSources": [
            {
                "Id": "src-primary",
                "SupportsDirectStream": True,
                "Container": "mkv",
            },
            {
                "Id": "src-dc",
                "SupportsDirectStream": True,
                "Container": "mkv",
            },
        ],
        "PlaySessionId": "ps1",
    }
    chosen = []

    def capture_stream_url(server, item, source, device_id, play_session_id):
        chosen.append(source.get("Id"))
        return ("http://s/stream", "DirectStream")

    monkeypatch.setattr(play, "stream_url", capture_stream_url)
    run_play({"id": "ep1", "mediasourceid": "src-dc"}, resume=False)
    assert chosen == ["src-dc"]


def test_play_without_mediasourceid_uses_first_source(resume_env, monkeypatch):
    resume_env["api"].playback_info = lambda *a, **k: {
        "MediaSources": [
            {"Id": "src-primary", "SupportsDirectStream": True, "Container": "mkv"},
            {"Id": "src-dc", "SupportsDirectStream": True, "Container": "mkv"},
        ],
        "PlaySessionId": "ps1",
    }
    chosen = []

    def capture_stream_url(server, item, source, device_id, play_session_id):
        chosen.append(source.get("Id"))
        return ("http://s/stream", "DirectStream")

    monkeypatch.setattr(play, "stream_url", capture_stream_url)
    run_play({"id": "ep1"}, resume=False)
    assert chosen == ["src-primary"]


def test_unreadable_row_falls_back_to_the_server_position(resume_env, monkeypatch):
    resume_env["addon"].store["resumeJumpBack"] = "-10"
    monkeypatch.setattr(
        "kofin.core.kodirpc.resume_seconds", lambda kodi_id, media: None
    )
    run_play({"id": "ep1", "dbid": "8956"}, resume=True)
    assert resume_env["api"].start_ticks == [590 * 10_000_000]


def test_non_library_resume_uses_the_server_position(resume_env, monkeypatch):
    # A plugin listing has no Kodi bookmark, so the resolved item's resume
    # point is the only one in play and the server's position is the answer.
    def explode(kodi_id, media):  # pragma: no cover - must not be reached
        raise AssertionError("no dbid, so no library row to read")

    resume_env["addon"].store["resumeJumpBack"] = "-10"
    monkeypatch.setattr("kofin.core.kodirpc.resume_seconds", explode)
    run_play({"id": "ep1"}, resume=True)
    assert resume_env["api"].start_ticks == [590 * 10_000_000]


# --- forcing the video re-encode through the whole play route -----------------


def _transcoding_source(resume_env, monkeypatch, **overrides):
    """Point the fake api at a transcode-only MediaSource."""
    api = resume_env["api"]

    def playback_info(item_id, profile, start_ticks=0, **kwargs):
        api.start_ticks.append(start_ticks)
        return {
            "MediaSources": [
                dict(
                    {
                        "Id": "src1",
                        "Bitrate": 2_231_688,
                        "TranscodingSubProtocol": "hls",
                        "TranscodingContainer": "ts",
                        "TranscodingUrl": (
                            "/videos/m1/master.m3u8?PlaySessionId=ps1"
                            "&VideoBitrate=2007688&AudioBitrate=224000"
                        ),
                    },
                    **overrides,
                )
            ],
            "PlaySessionId": "ps1",
        }

    monkeypatch.setattr(api, "playback_info", playback_info)
    return api


def test_forced_transcode_denies_the_video_stream_copy(resume_env, monkeypatch):
    """The context item's forced transcode must re-encode the video: sizing it
    to the source alone let the server copy the video and squeeze the audio."""
    _transcoding_source(resume_env, monkeypatch)
    run_play({"id": "ep1", "transcode": "1", "bitrate": "3"}, resume=False)
    assert "allowVideoStreamCopy=false" in resume_env["li"].path


def test_force_transcode_setting_denies_it_too(resume_env, monkeypatch):
    """Same for the Advanced-tab toggle, which is how the copy was observed."""
    resume_env["addon"].store["forceTranscode"] = "true"
    _transcoding_source(resume_env, monkeypatch)
    run_play({"id": "ep1"}, resume=False)
    assert "allowVideoStreamCopy=false" in resume_env["li"].path


def test_unforced_transcode_leaves_the_copy_decision_alone(resume_env, monkeypatch):
    """A transcode the server chose for itself is not a forced one: denying the
    copy there would re-encode video the profile was happy to stream."""
    _transcoding_source(resume_env, monkeypatch)
    run_play({"id": "ep1"}, resume=False)
    assert "allowVideoStreamCopy" not in resume_env["li"].path


def test_forced_transcode_spends_the_bitrate_the_user_picked(resume_env, monkeypatch):
    """The 3 Mbit/s pick is the budget, not a ceiling the source lowers.

    The source here reports 2,231,688 — sizing the transcode down to that was
    what left the video share above the source's own video bitrate, which is
    the copy Jellyfin then took.
    """
    _transcoding_source(resume_env, monkeypatch)
    run_play({"id": "ep1", "transcode": "1", "bitrate": "3"}, resume=False)
    path = resume_env["li"].path
    # audio = min(384k, 3 Mbit/s / 10) = 300k; video takes the rest.
    assert "VideoBitrate=2700000" in path
    assert "AudioBitrate=300000" in path


def test_forced_transcode_keeps_the_audio_copy_available(resume_env, monkeypatch):
    """Video only: the audio share is left to stand on its own, so audio that
    fits the budget (this source's 224k inside a 300k share) can still be
    copied."""
    _transcoding_source(resume_env, monkeypatch)
    run_play({"id": "ep1", "transcode": "1", "bitrate": "3"}, resume=False)
    path = resume_env["li"].path
    assert "enableAutoStreamCopy" not in path
    assert "allowAudioStreamCopy" not in path


def test_uncapped_forced_transcode_leaves_the_server_to_size_it(
    resume_env, monkeypatch
):
    """ "Source (original) bitrate" and an unset cap send no bitrates at all:
    with nothing to split, the server's own reservation stands and only the
    copy denial is added."""
    _transcoding_source(resume_env, monkeypatch)
    resume_env["addon"].store["forceTranscode"] = "true"
    run_play({"id": "ep1"}, resume=False)
    path = resume_env["li"].path
    # Untouched, exactly as the server wrote them.
    assert "VideoBitrate=2007688" in path
    assert "AudioBitrate=224000" in path
    assert "allowVideoStreamCopy=false" in path


def test_music_transcode_is_not_touched(resume_env, monkeypatch):
    """The flag is meaningless on the music transcoding profile, which has no
    video stream and its own bitrate."""
    resume_env["addon"].store["forceTranscode"] = "true"
    api = _transcoding_source(resume_env, monkeypatch)
    api._item = {"Id": "s1", "Type": "Audio", "Name": "A Song", "RunTimeTicks": 100}
    run_play({"id": "s1"}, resume=False)
    assert "allowVideoStreamCopy" not in resume_env["li"].path


# --- stream selection ---------------------------------------------------------
#
# The indices only bind when MediaSourceId travels with them: measured, a
# PlaybackInfo carrying AudioStreamIndex and no source id came back with the
# server's own default and no error (plan §2.6).


def test_stream_indices_travel_with_the_source_id(resume_env):
    run_play(
        {
            "id": "ep1",
            "mediasourceid": "src1",
            "audioindex": "3",
            "subtitleindex": "-1",
        },
        resume=False,
    )
    sent = resume_env["api"].kwargs[0]
    assert sent["media_source_id"] == "src1"
    assert sent["audio_index"] == 3
    assert sent["subtitle_index"] == -1


def test_a_plain_play_names_no_source_and_no_indices(resume_env):
    run_play({"id": "ep1"}, resume=False)
    sent = resume_env["api"].kwargs[0]
    assert sent["media_source_id"] is None
    assert sent["audio_index"] is None
    assert sent["subtitle_index"] is None


def test_play_state_carries_what_the_stream_menu_needs(resume_env):
    from kofin.core import state

    run_play({"id": "ep1", "dbid": "77"}, resume=False)
    queued = state.claim_play_item("")
    published = queued["Streams"]
    # Summarized, not the raw MediaStreams: this rides a window property.
    assert [stream["Index"] for stream in published["MediaStreams"]] == [1, 2]
    # The sidecar attached, so the menu can map its Jellyfin index to a Kodi one.
    assert published["Attached"] == [2]
    # The originating params, so a restart reproduces this play method.
    assert published["Request"]["dbid"] == "77"


def test_burn_subtitles_withdraws_the_image_formats(resume_env, monkeypatch):
    from kofin.core import deviceprofile

    seen = {}
    original = deviceprofile.build

    def spy(config, **kwargs):
        seen.update(kwargs)
        return original(config, **kwargs)

    monkeypatch.setattr(play.deviceprofile, "build", spy)
    run_play({"id": "ep1", "burnsubs": "1"}, resume=False)
    assert seen["burn_subtitles"] is True

    profile = original(
        deviceprofile.ProfileConfig(), force_transcode=True, burn_subtitles=True
    )
    formats = {entry["Format"] for entry in profile["SubtitleProfiles"]}
    # Withdrawing the image formats is what makes the server answer Encode
    # for a PGS track instead of handing back a 37 MB .sup (plan §2.2).
    assert "pgssub" not in formats and "dvdsub" not in formats
    assert "srt" in formats


# --- a download beats the network, online too ---------------------------------
#
# The library row of a downloaded item points at the file, so playing one from
# the library never reaches this route. Everything that plays by *id* does —
# and a SyncPlay group start has no library row to go through by construction,
# which is how a follower ended up streaming media the initiator was playing
# off its own disk.


@pytest.fixture
def downloaded_env(resume_env, monkeypatch):
    monkeypatch.setattr(play, "downloaded_file", lambda item_id: "/dl/ep1.mp4")
    return resume_env


def test_a_downloaded_item_plays_from_disk_online(downloaded_env):
    run_play({"id": "ep1"}, resume=True)
    # No PlaybackInfo at all: the resolve never asked the server for a stream.
    assert downloaded_env["api"].start_ticks == []
    assert downloaded_env["li"].path == "/dl/ep1.mp4"
    assert downloaded_env["resolved"] == [downloaded_env["li"]]


def test_the_downloaded_play_claims_the_playback(downloaded_env):
    """The claim is pushed here, not left to the service's back-fill.

    ``backfill_library_claim`` needs a Kodi database id off the
    ``Player.OnPlay`` announcement and a SyncPlay group start carries none, so
    without this the follower's playback would run unclaimed — no session, no
    reporting, no segment engine, no watched-to-end offer.
    """
    from kofin.core import state

    run_play({"id": "ep1"}, resume=False)
    queued = state.claim_play_item("")
    assert queued["Id"] == "ep1"
    assert queued["Path"] == "/dl/ep1.mp4"
    assert queued["PlayMethod"] == "DirectPlay"
    assert queued["Name"] == "An Episode"
    assert queued["Runtime"] == 1500 * 10_000_000
    assert queued["DeviceId"] == "dev1"
    # No stream menu: the download's tracks are its own, so the server's
    # MediaStreams would describe a different file.
    assert "Streams" not in queued


def test_the_downloaded_play_starts_where_the_group_is(downloaded_env):
    """A group start states its position; the resolved item has to carry it,
    or the follower starts the file at zero."""
    from kofin.core import state

    run_play({"id": "ep1", "startticks": str(471 * 10_000_000)}, resume=False)
    assert downloaded_env["built"]["resume_seconds"] == 471.0
    assert state.claim_play_item("")["CurrentPosition"] == 471.0


@pytest.mark.parametrize(
    "params",
    [
        {"transcode": "1"},
        {"bitrate": "3"},
        {"mediasourceid": "src1"},
        {"audioindex": "3"},
        {"subtitleindex": "-1"},
        {"burnsubs": "1"},
    ],
    ids=lambda params: next(iter(params)),
)
def test_a_request_naming_a_stream_still_streams(downloaded_env, params):
    """A download is one file with the tracks it was made with, so a request
    that names a source, a track or a bitrate goes to the server even with the
    file on disk — which is also what keeps the stream menu's restart
    resolving back to the server it was picked from."""
    run_play(dict(params, id="ep1"), resume=False)
    assert downloaded_env["api"].start_ticks != []
    assert downloaded_env["li"].path.startswith("http")


# --- offline behaviour (plan W2.2) -------------------------------------------


@pytest.fixture
def offline_env(tmp_path, monkeypatch):
    from tests.unit.fakes import FakeAddon, FakeWindow

    FakeAddon.store = {
        "isLoggedIn": "true",
        "accessToken": "t",
        "serverAddress": "http://s",
        "userId": "u",
        "downloadsPath": str(tmp_path / "dl"),
    }
    FakeWindow.store = {"kofin.online": "false"}  # a stated outage
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    monkeypatch.setattr("xbmcvfs.exists", lambda p: True)
    monkeypatch.setattr("xbmcvfs.translatePath", lambda p: str(p))
    from kofin.sync import db as sync_db

    sync_db.reset_overrides()
    sync_db.set_path_override("kofin", str(tmp_path / "kofin.db"))
    yield tmp_path
    sync_db.reset_overrides()


def test_offline_refuses_an_item_that_is_not_downloaded(offline_env, monkeypatch):
    """Offline this used to spend the transport's whole budget — ~8 s of
    spinner — before a generic failure (feasibility V8)."""
    from kofin.plugin import play as play_module
    from kofin.plugin.router import Request

    resolved = []
    toasts = []
    monkeypatch.setattr(
        play_module.xbmcplugin,
        "setResolvedUrl",
        lambda handle, ok, li: resolved.append(ok),
    )
    monkeypatch.setattr(play_module.toast, "show", lambda *a, **k: toasts.append(a[0]))
    built = []
    monkeypatch.setattr(
        "kofin.core.api.plugin_transport", lambda verify: built.append(1)
    )

    play_module.play(Request("plugin://x", 1, {"id": "nope"}))

    assert resolved == [False]
    assert toasts and built == []  # no transport was ever built


def test_offline_plays_an_item_that_is_downloaded(offline_env, monkeypatch):
    """A library row points at its file already; this is the kofin-listing
    path (Continue watching, a widget), where refusing to play a file on
    disk would be absurd."""
    from kofin.downloads import store
    from kofin.plugin import play as play_module
    from kofin.plugin.router import Request

    media = offline_env / "dl" / "Movies" / "M (2019)"
    media.mkdir(parents=True)
    (media / "m.mkv").write_bytes(b"x")
    store.queue(store.Download(jellyfin_id="m1", media_type="movie", queued_at=1))
    store.claim()
    store.finish("m1", "Movies/M (2019)/m.mkv", "mkv", 1)

    # Kodistubs' ListItem.getPath() is hardcoded to "", so the path is
    # captured where it is handed over instead of read back.
    built = {}

    class RecordingListItem:
        def __init__(self, path="", **kwargs):
            built["path"] = path

        def setContentLookup(self, value):
            pass

        def getVideoInfoTag(self):
            return type("Tag", (), {"setDbId": lambda self, dbid: None})()

    resolved = []
    monkeypatch.setattr(play_module.xbmcgui, "ListItem", RecordingListItem)
    monkeypatch.setattr(
        play_module.xbmcplugin,
        "setResolvedUrl",
        lambda handle, ok, li: resolved.append(ok),
    )
    monkeypatch.setattr("kofin.core.api.plugin_transport", lambda verify: 1 / 0)

    play_module.play(Request("plugin://x", 1, {"id": "m1"}))

    assert resolved == [True]
    assert built["path"].endswith("Movies/M (2019)/m.mkv")


def test_downloaded_file_answers_only_for_a_finished_download(offline_env):
    """None for every reason there is nothing to play, including a row whose
    file has gone from under it — the store is not proof the bytes are there."""
    from kofin.downloads import store
    from kofin.plugin import play as play_module

    assert play_module.downloaded_file("m1") is None  # no row at all

    store.queue(store.Download(jellyfin_id="m1", media_type="movie", queued_at=1))
    assert play_module.downloaded_file("m1") is None  # queued, not finished

    store.claim()
    media = offline_env / "dl" / "Movies" / "M (2019)"
    media.mkdir(parents=True)
    (media / "m.mkv").write_bytes(b"x")
    store.finish("m1", "Movies/M (2019)/m.mkv", "mkv", 1)
    assert play_module.downloaded_file("m1").endswith("Movies/M (2019)/m.mkv")

    (media / "m.mkv").unlink()
    assert play_module.downloaded_file("m1") is None  # the file went away
