"""Unit tests for core.playback helpers (PR3a)."""

from kofin.core import playback


def test_parse_remote_stream_index_aliases():
    assert playback.parse_remote_stream_index({"Index": 3}) == 3
    assert playback.parse_remote_stream_index({"AudioStreamIndex": "2"}) == 2
    assert playback.parse_remote_stream_index({"SubtitleStreamIndex": 0}) == 0
    assert playback.parse_remote_stream_index({}) is None
    assert playback.parse_remote_stream_index({"Index": "x"}) is None


def test_format_stream_label_prefers_display_title():
    assert (
        playback.format_stream_label(
            {"DisplayTitle": "English - DTS", "Language": "eng", "Codec": "dts"}
        )
        == "English - DTS"
    )
    assert (
        playback.format_stream_label(
            {"Type": "Audio", "Language": "jpn", "Codec": "aac", "Channels": 2}
        )
        == "jpn - AAC 2ch"
    )


def test_resolve_audio_direct_stream():
    item = {
        "PlayMethod": "DirectStream",
        "AudioMap": {"1": 0, "4": 2},
    }
    action, kodi, reason = playback.resolve_local_stream_switch(
        item, kind="audio", jellyfin_index=4
    )
    assert action == "audio"
    assert kodi == 2
    assert reason == "ok"


def test_resolve_audio_transcode_needs_restart():
    item = {"PlayMethod": "Transcode", "AudioMap": {"1": 0, "2": 1}}
    action, kodi, reason = playback.resolve_local_stream_switch(
        item, kind="audio", jellyfin_index=2
    )
    assert action == "needs_restart"
    assert kodi == 1
    assert "PR3b" in reason


def test_resolve_subtitle_ready_absolute():
    item = {
        "PlayMethod": "DirectStream",
        "SubsMappingReady": True,
        "SubsAttachOrder": [0],
        "SubsMapping": {"0": 0, "1": 5},
        "EmbeddedSubMap": {"5": 0},
    }
    action, kodi, reason = playback.resolve_local_stream_switch(
        item, kind="subtitle", jellyfin_index=0
    )
    assert action == "subtitle"
    assert kodi == 0


def test_resolve_subtitle_refuses_external_when_not_ready():
    item = {
        "PlayMethod": "DirectStream",
        "SubsMappingReady": False,
        "SubsAttachOrder": [0],
        "SubsMapping": {},
        "EmbeddedSubMap": {"5": 0},
    }
    action, kodi, reason = playback.resolve_local_stream_switch(
        item, kind="subtitle", jellyfin_index=0
    )
    assert action == "refuse"
    assert kodi is None
    assert "not ready" in reason


def test_resolve_subtitle_embedded_when_no_externals_provisional():
    item = {
        "PlayMethod": "DirectStream",
        "SubsMappingReady": False,
        "SubsAttachOrder": [],
        "SubsMapping": {},
        "EmbeddedSubMap": {"5": 0, "6": 1},
    }
    action, kodi, reason = playback.resolve_local_stream_switch(
        item, kind="subtitle", jellyfin_index=6
    )
    assert action == "subtitle"
    assert kodi == 1


def test_resolve_subtitle_off():
    item = {"PlayMethod": "DirectStream", "SubsMappingReady": True}
    action, kodi, reason = playback.resolve_local_stream_switch(
        item, kind="subtitle", jellyfin_index=-1
    )
    assert action == "subtitle_off"
    assert kodi is None


def test_pick_media_source_and_stream_url():
    sources = [
        {"Id": "a"},
        {"Id": "b", "SupportsDirectStream": True, "Container": "mkv"},
    ]
    assert playback.pick_media_source(sources, "b")["Id"] == "b"
    url, method = playback.stream_url(
        "http://s",
        {"Type": "Movie", "Id": "m1"},
        sources[1],
        "dev",
        "ps",
    )
    assert method == "DirectStream"
    assert "static=true" in url


def test_resolve_restart_stream_preserves_force_and_indexes():
    class Api:
        server = "http://s:8096"

        def playback_info(self, item_id, profile, start_ticks=0, **kwargs):
            assert kwargs.get("audio_index") == 2
            assert kwargs.get("media_source_id") == "src1"
            assert profile["DirectPlayProfiles"] == []
            return {
                "PlaySessionId": "ps-r",
                "MediaSources": [
                    {
                        "Id": "src1",
                        "Bitrate": 8_000_000,
                        "TranscodingUrl": (
                            "/v/master.m3u8?VideoBitrate=1&AudioBitrate=1"
                        ),
                        "TranscodingSubProtocol": "hls",
                    }
                ],
            }

    url, method, source, ps, profile = playback.resolve_restart_stream(
        Api(),  # type: ignore[arg-type]
        item_id="m1",
        media_source_id="src1",
        device_id="dev",
        force_transcode=True,
        bitrate_override_mbps=2.0,
        audio_index=2,
        subtitle_index=None,
        start_ticks=1_200_000_000,
    )
    assert method == "Transcode"
    assert ps == "ps-r"
    assert "VideoBitrate=" in url
    assert source["Id"] == "src1"
