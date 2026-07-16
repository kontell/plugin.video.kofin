from kofin.core import deviceprofile
from kofin.core.deviceprofile import ProfileConfig, UNLIMITED_BITRATE, build


def hdr_profiles(profile):
    return [
        cp
        for cp in profile["CodecProfiles"]
        if cp["Conditions"][0].get("Property") == "VideoRangeType"
    ]


def test_defaults_direct_play_everything():
    profile = build(ProfileConfig())
    assert profile["MaxStreamingBitrate"] == UNLIMITED_BITRATE

    direct = profile["DirectPlayProfiles"]
    assert direct[0]["Type"] == "Video"
    assert direct[0]["VideoCodec"] == "h264,hevc,av1,mpeg2video,vp9,vc1"
    assert direct[0]["AudioCodec"].startswith("aac,")
    assert direct[1] == {"Type": "Audio"}

    # All HDR types selected + 10bit/rext allowed: no video restrictions —
    # only the music direct-play cap remains a codec profile. The video
    # audio-track bitrate cap lives on the transcoding profiles (it must
    # never gate direct play — a 448k ac3 track would otherwise transcode).
    properties = [cp["Conditions"][0]["Property"] for cp in profile["CodecProfiles"]]
    assert "VideoRangeType" not in properties
    assert "VideoBitDepth" not in properties
    assert "Width" not in properties
    assert properties == ["AudioBitrate"]
    assert [cp["Type"] for cp in profile["CodecProfiles"]] == ["Audio"]
    for tp in profile["TranscodingProfiles"][:2]:
        condition = tp["Conditions"][0]
        assert condition["Property"] == "AudioBitrate"
        assert condition["Value"] == "384000"
    assert "Conditions" not in profile["TranscodingProfiles"][2]  # music

    # ts leads (preferred h264), fmp4 second, music profile last.
    transcoding = profile["TranscodingProfiles"]
    assert [tp["Container"] for tp in transcoding] == ["ts", "mp4", "opus"]
    assert transcoding[0]["VideoCodec"].startswith("h264")
    assert "av1" not in transcoding[0]["VideoCodec"]
    assert transcoding[2]["AudioCodec"] == "opus"
    assert profile["MusicStreamingTranscodingBitrate"] == 128_000


def test_bit_depth_and_profile_caps():
    config = ProfileConfig(video_codecs=["h264", "hevc"])
    codec_profiles = build(config)["CodecProfiles"]
    h264 = next(cp for cp in codec_profiles if cp.get("Codec") == "h264")
    assert h264["Conditions"][0] == {
        "Condition": "LessThanEqual",
        "Property": "VideoBitDepth",
        "Value": "8",
    }
    hevc = next(cp for cp in codec_profiles if cp.get("Codec") == "hevc")
    assert hevc["Conditions"][0]["Value"] == "main|main 10"


def test_hdr_restriction_emits_range_profiles():
    config = ProfileConfig(hdr_types=["HDR10"])
    profile = build(config)
    ranges = {cp["Codec"]: cp["Conditions"][0]["Value"] for cp in hdr_profiles(profile)}
    assert ranges["hevc"] == "SDR|HDR10|DOVIInvalid"
    assert ranges["av1"] == "SDR|HDR10|DOVIInvalid"
    assert ranges["vp9"] == "SDR|HDR10"


def test_hdr_unselected_codec_emits_nothing():
    config = ProfileConfig(video_codecs=["h264"], hdr_types=["HDR10"])
    assert hdr_profiles(build(config)) == []


def test_bitrate_cap_keeps_direct_play():
    profile = build(ProfileConfig(max_bitrate_mbps=10))
    assert profile["MaxStreamingBitrate"] == 10_000_000
    assert profile["DirectPlayProfiles"]  # VOD deviation from pvr.kofin


def test_forced_transcode_override_wins():
    config = ProfileConfig(force_direct_play=True)
    profile = build(config, bitrate_override_mbps=3, force_transcode=True)
    assert profile["MaxStreamingBitrate"] == 3_000_000
    assert profile["DirectPlayProfiles"] == []


def test_force_direct_play_wildcards():
    profile = build(ProfileConfig(force_direct_play=True, max_bitrate_mbps=10))
    assert profile["MaxStreamingBitrate"] == UNLIMITED_BITRATE
    assert profile["DirectPlayProfiles"][0]["Container"] == ""
    # Bit-depth caps still apply (they describe the decoder, not the pipe).
    config = ProfileConfig(force_direct_play=True, video_codecs=["h264"])
    properties = [
        cp["Conditions"][0]["Property"] for cp in build(config)["CodecProfiles"]
    ]
    assert "VideoBitDepth" in properties
    assert "VideoRangeType" not in properties


def test_force_remux_disables_direct_play():
    profile = build(ProfileConfig(force_remux=True))
    assert profile["DirectPlayProfiles"] == []


def test_av1_preferred_ordering_and_ts_lead():
    config = ProfileConfig(preferred_video="av1")
    transcoding = build(config)["TranscodingProfiles"]
    assert [tp["Container"] for tp in transcoding[:2]] == ["mp4", "ts"]
    assert transcoding[1]["VideoCodec"].startswith("hevc")


def test_av1_preferred_without_hevc_falls_back():
    config = ProfileConfig(preferred_video="av1", video_codecs=["av1"])
    transcoding = build(config)["TranscodingProfiles"]
    ts = next(tp for tp in transcoding if tp["Container"] == "ts")
    assert ts["VideoCodec"] == "h264"
    # Preferring av1 implies the device decodes it -> direct play includes it.
    direct = build(config)["DirectPlayProfiles"]
    assert "av1" in direct[0]["VideoCodec"]


def test_max_width_condition():
    profile = build(ProfileConfig(max_width=1920))
    width = next(
        cp
        for cp in profile["CodecProfiles"]
        if cp["Conditions"][0].get("Property") == "Width"
    )
    assert width["Conditions"][0]["Value"] == "1920"
    assert "Codec" not in width


def test_music_profile_follows_settings():
    config = ProfileConfig(music_codec="mp3", music_bitrate_kbps=320)
    profile = build(config)
    music = profile["TranscodingProfiles"][-1]
    assert music["Container"] == "mp3" and music["AudioCodec"] == "mp3"
    assert profile["MusicStreamingTranscodingBitrate"] == 320_000


def test_subtitle_profiles_cover_embed_and_external():
    subs = build(ProfileConfig())["SubtitleProfiles"]
    assert {"Format": "srt", "Method": "External"} in subs
    assert {"Format": "pgssub", "Method": "Embed"} in subs
    assert len(subs) == len(deviceprofile.SUBTITLE_FORMATS) * 2


def test_music_max_bitrate_caps_audio_direct_play():
    profile = build(ProfileConfig(music_max_bitrate_kbps=320))
    audio_caps = [
        cp
        for cp in profile["CodecProfiles"]
        if cp["Type"] == "Audio" and cp["Conditions"][0]["Property"] == "AudioBitrate"
    ]
    assert audio_caps and audio_caps[0]["Conditions"][0]["Value"] == "320000"

    unlimited = build(ProfileConfig(music_max_bitrate_kbps=0))
    assert not [cp for cp in unlimited["CodecProfiles"] if cp["Type"] == "Audio"]
