"""Unit tests for Jellyfin ↔ Kodi stream index maps (PR2)."""

from kofin.core import streammaps

STREAMS = [
    {"Type": "Video", "Index": 0, "Codec": "hevc"},
    {
        "Type": "Audio",
        "Index": 1,
        "Language": "eng",
        "Codec": "eac3",
        "Channels": 6,
        "DisplayTitle": "English - EAC3",
    },
    {
        "Type": "Audio",
        "Index": 2,
        "Language": "jpn",
        "Codec": "aac",
        "Channels": 2,
        "DisplayTitle": "Japanese",
    },
    {
        "Type": "Subtitle",
        "Index": 3,
        "Language": "eng",
        "Codec": "subrip",
        "DeliveryMethod": "External",
        "IsTextSubtitleStream": True,
    },
    {
        "Type": "Subtitle",
        "Index": 5,
        "Language": "eng",
        "Codec": "pgssub",
        "DeliveryMethod": "Embed",
    },
    {
        "Type": "Subtitle",
        "Index": 6,
        "Language": "fra",
        "Codec": "pgssub",
        "DeliveryMethod": "Embed",
    },
]


def test_audio_map_skips_video_and_orders_by_index():
    assert streammaps.audio_map(STREAMS) == {1: 0, 2: 1}


def test_embedded_subtitle_map_excludes_external():
    assert streammaps.embedded_subtitle_map(STREAMS) == {5: 0, 6: 1}


def test_provisional_offset_externals_after_embedded():
    # 2 embedded → external attach order at absolute 2, 3
    assert streammaps.provisional_external_offset_map([3, 9], embedded_count=2) == {
        2: 3,
        3: 9,
    }


def test_reconcile_tc_externals_only():
    mapping, ready = streammaps.reconcile_subs_mapping(
        attach_order_jf=[0],
        subs_paths=["/tmp/subs/ps/00.eng.srt"],
        kodi_sub_names=["00.eng.srt"],
        embedded_map_jf_to_kodi={},
    )
    assert ready
    assert mapping == {0: 0}


def test_reconcile_embedded_plus_externals_offset():
    emb = streammaps.embedded_subtitle_map(STREAMS)
    mapping, ready = streammaps.reconcile_subs_mapping(
        attach_order_jf=[3],
        subs_paths=["/cache/00.eng.srt"],
        # Kodi lists embedded (no useful basename) then external file name.
        kodi_sub_names=["English", "French", "00.eng.srt"],
        embedded_map_jf_to_kodi=emb,
    )
    assert ready
    assert mapping[0] == 5
    assert mapping[1] == 6
    assert mapping[2] == 3


def test_reconcile_basename_preferred_over_wrong_offset_slot():
    mapping, ready = streammaps.reconcile_subs_mapping(
        attach_order_jf=[10, 11],
        subs_paths=["/a/00.eng.srt", "/a/01.swe.srt"],
        kodi_sub_names=["forced", "01.swe.srt", "00.eng.srt"],
        embedded_map_jf_to_kodi={1: 0},  # one embedded at kodi 0
    )
    assert ready
    assert mapping[0] == 1
    # Basename finds swe and eng even if order differs from attach order.
    assert mapping[1] == 11
    assert mapping[2] == 10


def test_reconcile_omega_external_label():
    """Omega names setSubtitles tracks like ``00 (External)``, not the full file."""
    mapping, ready = streammaps.reconcile_subs_mapping(
        attach_order_jf=[0],
        subs_paths=["/cache/ps/00.eng.srt"],
        kodi_sub_names=["00 (External)", "English", "French"],
        embedded_map_jf_to_kodi={5: 1, 6: 2},
    )
    assert ready
    assert mapping[0] == 0
    assert mapping[1] == 5
    assert mapping[2] == 6


def test_observe_audio_and_ready_sub():
    item = {
        "AudioStreamIndex": 1,
        "SubtitleStreamIndex": 5,
        "AudioMap": {"1": 0, "2": 1},
        "EmbeddedSubMap": {"5": 0},
        "SubsMapping": {"0": 5, "2": 3},
        "SubsMappingReady": True,
    }
    audio, sub = streammaps.observe_jellyfin_indexes(
        item, kodi_audio=1, kodi_sub=2, subtitle_enabled=True
    )
    assert audio == 2
    assert sub == 3


def test_observe_sub_off_clears_index():
    item = {
        "AudioStreamIndex": 1,
        "SubtitleStreamIndex": 3,
        "AudioMap": {"1": 0},
        "SubsMappingReady": True,
        "SubsMapping": {"0": 3},
    }
    audio, sub = streammaps.observe_jellyfin_indexes(
        item, kodi_audio=0, kodi_sub=0, subtitle_enabled=False
    )
    assert audio == 1
    assert sub is None


def test_observe_not_ready_keeps_default_for_external_slot():
    item = {
        "AudioStreamIndex": 1,
        "SubtitleStreamIndex": 3,
        "AudioMap": {"1": 0},
        "EmbeddedSubMap": {"5": 0},
        "SubsMappingReady": False,
        "SubsMapping": {},
    }
    # kodi_sub=1 would be first external under offset, but not ready → keep 3
    audio, sub = streammaps.observe_jellyfin_indexes(
        item, kodi_audio=0, kodi_sub=1, subtitle_enabled=True
    )
    assert audio == 1
    assert sub == 3
    # embedded kodi 0 still resolves
    _, sub2 = streammaps.observe_jellyfin_indexes(
        item, kodi_audio=0, kodi_sub=0, subtitle_enabled=True
    )
    assert sub2 == 5


def test_play_state_stream_fields_merge():
    source = {"MediaStreams": STREAMS}
    fields = streammaps.play_state_stream_fields(
        source,
        {
            "SubsAttachOrder": [3],
            "SubsPaths": ["/t/00.eng.srt"],
            "SubsMapping": {},
            "SubsMappingReady": False,
        },
    )
    assert fields["AudioMap"] == {"1": 0, "2": 1}
    assert fields["EmbeddedSubMap"] == {"5": 0, "6": 1}
    assert fields["SubsAttachOrder"] == [3]
    assert len(fields["AudioStreams"]) == 2
    assert len(fields["SubtitleStreams"]) == 3
