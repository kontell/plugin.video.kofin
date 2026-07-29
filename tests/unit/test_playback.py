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


def test_should_offer_pick_audio_only_multi_tc():
    multi = [
        {"Index": 1, "DisplayTitle": "Eng"},
        {"Index": 2, "DisplayTitle": "Jpn"},
    ]
    assert (
        playback.should_offer_pick_audio(
            {"PlayMethod": "Transcode", "AudioStreams": multi}
        )
        is True
    )
    assert (
        playback.should_offer_pick_audio(
            {"PlayMethod": "DirectStream", "AudioStreams": multi}
        )
        is False
    )
    assert (
        playback.should_offer_pick_audio(
            {
                "PlayMethod": "Transcode",
                "AudioStreams": [{"Index": 1, "DisplayTitle": "Eng"}],
            }
        )
        is False
    )
    assert (
        playback.should_offer_pick_audio(
            {"PlayMethod": "Transcode", "AudioStreams": []}
        )
        is False
    )


def test_suppress_stream_dialogs_syncplay_param_and_prop(monkeypatch):
    from tests.unit.fakes import FakeWindow

    FakeWindow.store = {}
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    assert playback.suppress_stream_dialogs({}) is False
    assert playback.suppress_stream_dialogs({"syncplay": "1"}) is True
    from kofin.core import state

    state.set_syncplay_active(True)
    assert playback.suppress_stream_dialogs({}) is True
    state.set_syncplay_active(False)
    assert playback.suppress_stream_dialogs({}) is False


def test_needs_preplay_stream_dialog_matrix():
    source = {
        "MediaStreams": [
            {"Type": "Audio", "Index": 1, "Language": "eng"},
            {"Type": "Audio", "Index": 2, "Language": "ita"},
            {
                "Type": "Subtitle",
                "Index": 3,
                "IsTextSubtitleStream": True,
                "Codec": "srt",
                "Language": "eng",
            },
            {
                "Type": "Subtitle",
                "Index": 4,
                "IsTextSubtitleStream": True,
                "Codec": "srt",
                "Language": "fra",
            },
        ]
    }
    assert playback.needs_preplay_stream_dialog(
        play_method="DirectStream",
        item_type="Movie",
        select_mode=playback.STREAM_SELECT_AUDIO_AND_SUBS,
        source=source,
        allow_burned=False,
        suppress=False,
    ) == (False, False)
    assert playback.needs_preplay_stream_dialog(
        play_method="Transcode",
        item_type="Movie",
        select_mode=playback.STREAM_SELECT_NEVER,
        source=source,
        allow_burned=False,
        suppress=False,
    ) == (False, False)
    assert playback.needs_preplay_stream_dialog(
        play_method="Transcode",
        item_type="Movie",
        select_mode=playback.STREAM_SELECT_AUDIO_AND_SUBS,
        source=source,
        allow_burned=False,
        suppress=False,
    ) == (True, True)
    assert playback.needs_preplay_stream_dialog(
        play_method="Transcode",
        item_type="Movie",
        select_mode=playback.STREAM_SELECT_AUDIO_ONLY,
        source=source,
        allow_burned=False,
        suppress=False,
    ) == (True, False)
    assert playback.needs_preplay_stream_dialog(
        play_method="Transcode",
        item_type="Movie",
        select_mode=playback.STREAM_SELECT_AUDIO_AND_SUBS,
        source=source,
        allow_burned=False,
        suppress=True,
    ) == (False, False)


def test_eligible_dialog_subs_text_and_burned():
    source = {
        "MediaStreams": [
            {
                "Type": "Subtitle",
                "Index": 0,
                "Codec": "subrip",
                "IsTextSubtitleStream": True,
            },
            {"Type": "Subtitle", "Index": 5, "Codec": "pgssub"},
        ]
    }
    assert len(playback.eligible_dialog_subs(source, allow_burned=False)) == 1
    assert len(playback.eligible_dialog_subs(source, allow_burned=True)) == 2


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
