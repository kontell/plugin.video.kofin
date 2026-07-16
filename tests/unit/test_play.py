import pytest

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


def test_stream_url_unplayable_raises():
    with pytest.raises(JellyfinError):
        play.stream_url(SERVER, {"Id": "m1"}, {"Id": "s"}, "d", "p")


def test_mime_for():
    assert play.mime_for("mkv", "DirectStream") == "video/x-matroska"
    assert play.mime_for("anything", "Transcode") == play.HLS_MIME
    assert play.mime_for("unknown", "DirectStream") == ""


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
    assert context.choose_bitrate(["junk", "-5"]) == "10"  # falls back to default
