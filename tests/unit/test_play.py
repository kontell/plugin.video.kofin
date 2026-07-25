import pytest

from kofin.core import deviceprofile
from kofin.core.http import JellyfinError
from kofin.plugin import play
from kofin.plugin.router import dispatch

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


def test_mime_for():
    assert play.mime_for("mkv", "DirectStream") == "video/x-matroska"
    assert play.mime_for("anything", "Transcode") == play.HLS_MIME
    assert play.mime_for("unknown", "DirectStream") == ""


def test_transcode_budget_caps_forced_transcode_at_source():
    source = {"Bitrate": 8_000_000}
    # Unlimited profile cap: the source is what bounds a forced transcode,
    # otherwise the server copies both streams and "force" does nothing.
    assert (
        play.transcode_budget(source, deviceprofile.UNLIMITED_BITRATE, True)
        == 8_000_000
    )
    # A tighter user cap still wins over the source.
    assert play.transcode_budget(source, 3_000_000, True) == 3_000_000
    # Not forced: the cap alone applies, source bitrate is irrelevant.
    assert play.transcode_budget(source, 3_000_000, False) == 3_000_000


def test_transcode_budget_without_source_bitrate():
    assert (
        play.transcode_budget({}, deviceprofile.UNLIMITED_BITRATE, True)
        == play.ASSUMED_SOURCE_BITRATE
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


def test_external_subtitles_filtering():
    source = {
        "MediaStreams": [
            {
                "Type": "Subtitle",
                "IsExternal": True,
                "DeliveryMethod": "External",
                "DeliveryUrl": "/subs/1.srt",
            },
            {"Type": "Subtitle", "IsExternal": False, "DeliveryUrl": "/subs/2.srt"},
            {"Type": "Audio", "DeliveryUrl": "/nope"},
        ]
    }
    assert play.external_subtitles(SERVER, source) == ["http://s:8096/subs/1.srt"]


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


class SegmentsStubApi:
    def __init__(self, response=None, fail=False):
        self.response = response or {"Items": []}
        self.fail = fail
        self.calls = 0

    def media_segments(self, item_id):
        self.calls += 1
        if self.fail:
            raise JellyfinError("segments down")
        return self.response


def test_prefetch_segments_warm_path(monkeypatch):
    monkeypatch.setattr("kofin.core.settings.get_bool", lambda sid: True)
    api = SegmentsStubApi(
        {"Items": [{"Type": "Intro", "StartTicks": 0, "EndTicks": 300_000_000}]}
    )
    segments = play.prefetch_segments(api, {"Id": "e1", "Type": "Episode"})
    assert segments == [{"Type": "Introduction", "Start": 0.0, "End": 30.0}]


def test_prefetch_segments_skips_non_video_and_disabled(monkeypatch):
    monkeypatch.setattr("kofin.core.settings.get_bool", lambda sid: True)
    api = SegmentsStubApi()
    assert play.prefetch_segments(api, {"Id": "a1", "Type": "Audio"}) == []
    assert api.calls == 0

    monkeypatch.setattr("kofin.core.settings.get_bool", lambda sid: False)
    assert play.prefetch_segments(api, {"Id": "e1", "Type": "Episode"}) == []
    assert api.calls == 0


def test_prefetch_segments_failure_defers_to_service(monkeypatch):
    monkeypatch.setattr("kofin.core.settings.get_bool", lambda sid: True)
    api = SegmentsStubApi(fail=True)
    assert play.prefetch_segments(api, {"Id": "e1", "Type": "Episode"}) is None


def test_router_parses_resume_argument(monkeypatch):
    seen = {}

    def fake_play(request):
        seen["resume"] = request.resume

    monkeypatch.setattr("kofin.plugin.router._handlers", lambda: {"play": fake_play})
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
        lambda sid: "Source" if sid == 30206 else "Play with transcoding",
    )
    # 0 == source, plus a fractional option; both are valid tokens now.
    assert context.choose_bitrate(["0", "0.5", "10"]) == "0"
    assert captured["labels"] == ["Source", "0.5 Mbit/s", "10 Mbit/s"]


def test_choose_bitrate_single_source_bypasses_dialog(monkeypatch):
    from kofin.plugin import context

    class ExplodingDialog:
        def select(self, *args):
            raise AssertionError("dialog should be bypassed")

    monkeypatch.setattr("xbmcgui.Dialog", ExplodingDialog)
    assert context.choose_bitrate(["0"]) == "0"
