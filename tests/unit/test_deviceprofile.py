from kofin.core import deviceprofile
from kofin.core.deviceprofile import (
    ProfileConfig,
    UNLIMITED_BITRATE,
    audio_bitrate_bps,
    build,
)
from tests.unit.fakes import FakeAddon


def audio_condition(profile):
    return profile["TranscodingProfiles"][0]["Conditions"][0]["Value"]


def test_audio_bitrate_split_scales_with_budget():
    # Unlimited leaves the configured value alone.
    assert audio_bitrate_bps(384, UNLIMITED_BITRATE) == 384_000
    assert audio_bitrate_bps(384, 0) == 384_000
    # A budget big enough for the setting keeps it.
    assert audio_bitrate_bps(384, 10_000_000) == 384_000
    # Below 10x the setting, audio scales down so video keeps 9/10.
    assert audio_bitrate_bps(384, 3_000_000) == 300_000
    assert audio_bitrate_bps(384, 500_000) == 50_000
    # A lower configured cap is never exceeded.
    assert audio_bitrate_bps(128, 10_000_000) == 128_000


def test_transcoding_profile_audio_cap_follows_budget():
    # The profile condition must agree with what play.py writes into the URL.
    assert audio_condition(build(ProfileConfig(max_bitrate_mbps=3))) == "300000"
    assert audio_condition(build(ProfileConfig(max_bitrate_mbps=10))) == "384000"


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

    # All HDR types selected + 10bit/rext allowed and music transcoding off:
    # nothing constrains direct play. The video audio-track bitrate cap lives
    # on the transcoding profiles (it must never gate direct play — a 448k ac3
    # track would otherwise transcode).
    assert profile["CodecProfiles"] == []
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


def test_eac3_direct_plays_by_default():
    # Missing from the original port; pvr.kofin has carried it since day one.
    assert "eac3" in deviceprofile.DEFAULT_AUDIO_CODECS
    assert "eac3" in build(ProfileConfig())["DirectPlayProfiles"][0]["AudioCodec"]


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


def test_fractional_bitrate_override():
    # The context 0.5/0.75 Mbit/s options must survive as integer bits/s.
    config = ProfileConfig(force_direct_play=True)
    profile = build(config, bitrate_override_mbps=0.5, force_transcode=True)
    assert profile["MaxStreamingBitrate"] == 500_000
    assert profile["DirectPlayProfiles"] == []


def test_source_bitrate_override_is_unlimited():
    # The context "Source" option (0) transcodes at the source bitrate, the
    # same result as force transcode with no cap.
    config = ProfileConfig(force_direct_play=True)
    profile = build(config, bitrate_override_mbps=0, force_transcode=True)
    assert profile["MaxStreamingBitrate"] == UNLIMITED_BITRATE
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


def test_force_remux_disables_video_direct_play():
    profile = build(ProfileConfig(force_remux=True))
    assert profile["DirectPlayProfiles"] == [{"Type": "Audio"}]


def test_video_force_settings_never_transcode_music():
    """forceRemux/forceTranscode describe the video pipe. Taking the audio
    DirectPlayProfile with them left the server no way to direct play a song:
    measured against 10.11, an mp3 came back SupportsDirectPlay=false with a
    stream.opus TranscodingUrl however musicTranscode was set."""
    for config in (
        ProfileConfig(force_remux=True),
        ProfileConfig(force_transcode=True),
        ProfileConfig(video_codecs=[]),  # nothing direct plays on the video side
    ):
        direct = build(config)["DirectPlayProfiles"]
        assert {"Type": "Audio"} in direct
        assert not [dp for dp in direct if dp["Type"] == "Video"]


def test_the_transcode_context_item_still_takes_everything():
    """A per-play forced transcode is asked for by name, on this item only."""
    assert build(ProfileConfig(), force_transcode=True)["DirectPlayProfiles"] == []


def test_av1_preferred_ordering_and_ts_lead():
    config = ProfileConfig(preferred_video="av1")
    transcoding = build(config)["TranscodingProfiles"]
    assert [tp["Container"] for tp in transcoding[:2]] == ["mp4", "ts"]
    assert transcoding[1]["VideoCodec"].startswith("hevc")


def test_av1_transcoding_leg_withdrawn_with_the_direct_play_codec():
    """Removing av1 from the codec list must remove it from *both* lists.

    A TranscodingProfile is a device statement too. Jellyfin ranks the
    transcoding profiles and puts the one holding the source codec first so it
    can stream-copy, so leaving the fMP4/av1 leg behind handed an av1 source
    straight back to a device that had just refused it — the server answered
    VideoCodec=av1 and ran `-codec:v:0 copy` (measured against 10.11).
    """
    config = ProfileConfig(video_codecs=["h264", "hevc"])
    transcoding = build(config)["TranscodingProfiles"]
    assert [tp["Container"] for tp in transcoding] == ["ts", "opus"]
    assert "av1" not in transcoding[0]["VideoCodec"]


def test_av1_transcoding_leg_survives_being_preferred_but_unlisted():
    # Preferring av1 implies the device decodes it, which is why
    # _direct_play_profiles adds it to the direct list; the transcode
    # fallback has to be able to target it for the same reason.
    config = ProfileConfig(video_codecs=["h264"], preferred_video="av1")
    transcoding = build(config)["TranscodingProfiles"]
    assert [tp["Container"] for tp in transcoding] == ["mp4", "ts", "opus"]
    assert transcoding[0]["VideoCodec"] == "av1"


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


def test_force_direct_play_is_scoped_to_video():
    """forceDirectPlay uncaps video only. Music delivery belongs to
    musicTranscode, so the music cap survives it."""
    profile = build(ProfileConfig(force_direct_play=True, music_max_bitrate_kbps=192))
    audio_caps = [cp for cp in profile["CodecProfiles"] if cp["Type"] == "Audio"]
    assert audio_caps and audio_caps[0]["Conditions"][0]["Value"] == "192000"
    # The video side is still fully uncapped, and the music transcoding
    # profile is still there for the server to fall back on.
    assert not [
        cp
        for cp in profile["CodecProfiles"]
        if cp["Conditions"][0]["Property"] in ("Width", "VideoRangeType")
    ]
    assert profile["TranscodingProfiles"][-1]["Type"] == "Audio"


def test_music_cap_only_applies_when_music_transcode_is_on(monkeypatch):
    FakeAddon.store = {"musicMaxBitrate": "320", "musicTranscodeBitrate": "128"}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)

    assert ProfileConfig.from_settings().music_max_bitrate_kbps == 0

    FakeAddon.store["musicTranscode"] = "true"
    assert ProfileConfig.from_settings().music_max_bitrate_kbps == 320


def test_streaming_cap_carries_a_fractional_setting(monkeypatch):
    """The streaming cap is a *string* setting so it can say 0.5 Mbit/s —
    Kodi's integer type cannot — and the fraction has to survive as far as
    the envelope's bits per second, not be truncated to a whole Mbit/s."""
    FakeAddon.store = {"maxStreamingBitrate": "0.75"}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)

    config = ProfileConfig.from_settings()
    assert config.max_bitrate_mbps == 0.75
    assert build(config)["MaxStreamingBitrate"] == 750000


# -- the download profile (plan W3.1/W3.2) -----------------------------------


def test_download_profile_shape():
    profile = deviceprofile.build_download(
        ProfileConfig(max_bitrate_mbps=8, max_width=1920)
    )
    assert profile["MaxStreamingBitrate"] == 8_000_000

    transcoding = profile["TranscodingProfiles"]
    assert len(transcoding) == 2  # one fMP4 video leg, one music leg
    video = transcoding[0]
    assert video["Container"] == "mp4" and video["Protocol"] == "http"
    # Preferred first, then every mp4-muxable direct-play codec; vc1 is
    # direct-playable but never a copy target.
    assert video["VideoCodec"] == "h264,hevc,av1,mpeg2video,vp9"

    widths = [
        cp
        for cp in profile["CodecProfiles"]
        if cp["Conditions"][0].get("Property") == "Width"
    ]
    assert widths and widths[0]["Conditions"][0]["Value"] == "1920"


def test_download_direct_play_keeps_containers_open():
    profile = deviceprofile.build_download(ProfileConfig())
    video = profile["DirectPlayProfiles"][0]
    assert video["Type"] == "Video"
    assert "Container" not in video  # a container must never force a transcode
    assert "vc1" in video["VideoCodec"]


def test_download_audio_direct_play_is_lossy_only():
    """SupportsDirectPlay is the whole music decision, so the audio entry
    must exclude lossless — an open profile would answer "keep the FLAC"."""
    profile = deviceprofile.build_download(ProfileConfig())
    audio = profile["DirectPlayProfiles"][-1]
    codecs = audio["AudioCodec"].split(",")
    assert "flac" not in codecs and "alac" not in codecs
    assert "mp3" in codecs and "opus" in codecs and "aac" in codecs


def test_download_copy_list_ranks_the_encoder_fallback_by_efficiency():
    """Preferred first, then hevc, then h264, then the rest: the server
    encodes to the first entry its ffmpeg can, so a stripped build without
    the preferred encoder falls back to quality-per-byte order — not the
    direct-play list's display order, which puts h264 first."""
    av1 = deviceprofile.build_download(ProfileConfig(preferred_video="av1"))
    assert av1["TranscodingProfiles"][0]["VideoCodec"] == "av1,hevc,h264,mpeg2video,vp9"
    hevc = deviceprofile.build_download(ProfileConfig(preferred_video="hevc"))
    assert (
        hevc["TranscodingProfiles"][0]["VideoCodec"] == "hevc,h264,av1,mpeg2video,vp9"
    )
    # A fallback the device cannot decode never appears at all.
    no_hevc = deviceprofile.build_download(
        ProfileConfig(preferred_video="av1", video_codecs=["av1", "h264"])
    )
    assert no_hevc["TranscodingProfiles"][0]["VideoCodec"] == "av1,h264"


def test_download_music_leg_and_cap_follow_config():
    profile = deviceprofile.build_download(
        ProfileConfig(
            music_codec="opus", music_bitrate_kbps=128, music_max_bitrate_kbps=192
        )
    )
    music = profile["TranscodingProfiles"][-1]
    assert music["Container"] == "opus" and music["Protocol"] == "http"
    caps = [
        cp
        for cp in profile["CodecProfiles"]
        if cp["Type"] == "Audio" and cp["Conditions"][0]["Property"] == "AudioBitrate"
    ]
    assert caps and caps[0]["Conditions"][0]["Value"] == "192000"


def test_for_downloads_reads_the_downloads_settings(monkeypatch):
    FakeAddon.store = {
        "directPlayVideoCodecs": "h264,hevc",
        "directPlayAudioCodecs": "aac,ac3",
        "allowedHdrTypes": "HDR10",
        "preferredVideoCodec": "hevc",
        "preferredAudioCodec": "aac",
        "maxAudioChannels": "6",
        "maxStreamingBitrate": "120",  # the *streaming* cap must not leak in
        "maxResolution": "3840",
        "downloadsMaxBitrate": "8",
        "downloadsMaxResolution": "1280",
        "downloadsMusicCodec": "opus",
        "downloadsMusicBitrate": "128",
        "downloadsMusicMaxBitrate": "192",
        "audioBitrate": "384",
    }
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    config = ProfileConfig.for_downloads()
    assert config.max_bitrate_mbps == 8
    assert config.max_width == 1280
    assert config.music_max_bitrate_kbps == 192
    assert config.video_codecs == ["h264", "hevc"]
    assert not config.force_direct_play
    assert not config.force_transcode


# --- the P1.8 identity golden -------------------------------------------------
#
# build()/build_download() canonical-JSON digests captured on the *before*
# build (5b3b3dc, pre-_envelope) over a matrix covering every leg. The
# refactor must not move a byte; a digest that changes is a finding, not a
# rename.

GOLDEN_CONFIGS = {
    "defaults": {},
    "capped": {"max_bitrate_mbps": 8, "max_width": 1920, "music_max_bitrate_kbps": 192},
    "force_direct": {"force_direct_play": True},
    "force_remux": {"force_remux": True},
    "force_transcode_cfg": {"force_transcode": True},
    "av1_preferred": {"preferred_video": "av1"},
    "no_av1": {"video_codecs": ["h264", "hevc"], "hdr_types": ["HDR10"]},
    "hevc_only_rext": {"video_codecs": ["hevc_rext", "vp9"]},
    "audio_narrow": {
        "audio_codecs": ["aac", "mp3"],
        "preferred_audio": "mp3",
        "max_channels": 2,
        "audio_bitrate_kbps": 256,
    },
    "music": {
        "music_codec": "mp3",
        "music_bitrate_kbps": 320,
        "music_max_bitrate_kbps": 320,
    },
}
GOLDEN_CALLS = {
    "plain": {},
    "override": {"bitrate_override_mbps": 0.75},
    "forced": {"force_transcode": True},
    "burn": {"burn_subtitles": True},
}
GOLDEN = {
    ("defaults", "plain"): "c68216246e4c2b00",
    ("defaults", "override"): "25d7b4b1738c42ed",
    ("defaults", "forced"): "37368a081f4a8076",
    ("defaults", "burn"): "4fd9d7ef77cbbe80",
    ("defaults", "download"): "af8f53d825e2ef92",
    ("capped", "plain"): "5ea0b39a9856a1b2",
    ("capped", "override"): "78fb533eb65c6de7",
    ("capped", "forced"): "2f07a299b3b4a40e",
    ("capped", "burn"): "c6fc0468a6991820",
    ("capped", "download"): "084a14bde7d73f7b",
    ("force_direct", "plain"): "c6103b90d63a489d",
    ("force_direct", "override"): "c6103b90d63a489d",
    ("force_direct", "forced"): "37368a081f4a8076",
    ("force_direct", "burn"): "328a810c03abedd7",
    ("force_direct", "download"): "af8f53d825e2ef92",
    ("force_remux", "plain"): "5eeef495c22c6d43",
    ("force_remux", "override"): "fa5b5024e407dc55",
    ("force_remux", "forced"): "37368a081f4a8076",
    ("force_remux", "burn"): "3b0120cee7bb47a3",
    ("force_remux", "download"): "af8f53d825e2ef92",
    ("force_transcode_cfg", "plain"): "5eeef495c22c6d43",
    ("force_transcode_cfg", "override"): "fa5b5024e407dc55",
    ("force_transcode_cfg", "forced"): "37368a081f4a8076",
    ("force_transcode_cfg", "burn"): "3b0120cee7bb47a3",
    ("force_transcode_cfg", "download"): "af8f53d825e2ef92",
    ("av1_preferred", "plain"): "eff9b6f63d1a8c0c",
    ("av1_preferred", "override"): "4f71d7470dac2d85",
    ("av1_preferred", "forced"): "83484c59c3aacd99",
    ("av1_preferred", "burn"): "64d4c26ff9bf45b5",
    ("av1_preferred", "download"): "5d5628f7d14110e1",
    ("no_av1", "plain"): "2bd2562cd93edd7f",
    ("no_av1", "override"): "7a13b4b7c9199087",
    ("no_av1", "forced"): "3c7983fb854b73ee",
    ("no_av1", "burn"): "6df8c9584ec5ea25",
    ("no_av1", "download"): "cb581e8be95aa21b",
    ("hevc_only_rext", "plain"): "34fb9703d90322b6",
    ("hevc_only_rext", "override"): "1659626f78a6b602",
    ("hevc_only_rext", "forced"): "75b8ba6c604b934a",
    ("hevc_only_rext", "burn"): "8e79239a9db8e482",
    ("hevc_only_rext", "download"): "01a96df055606043",
    ("audio_narrow", "plain"): "c41aa8ce1075d873",
    ("audio_narrow", "override"): "9ddcd8cb954ad11a",
    ("audio_narrow", "forced"): "33dd0113d264844c",
    ("audio_narrow", "burn"): "1ece55cf97aa243c",
    ("audio_narrow", "download"): "f3d092ee67b6f01a",
    ("music", "plain"): "716ca5c82c2175b9",
    ("music", "override"): "ca08694a64c08f39",
    ("music", "forced"): "e9b1ff2ee8fc46bf",
    ("music", "burn"): "f6892d429c665adc",
    ("music", "download"): "e2d895cada328016",
}


def test_the_profile_json_matches_the_pre_envelope_golden():
    import hashlib
    import json

    for (config_name, call_name), digest in GOLDEN.items():
        config = ProfileConfig(**GOLDEN_CONFIGS[config_name])
        if call_name == "download":
            document = deviceprofile.build_download(config)
        else:
            document = build(config, **GOLDEN_CALLS[call_name])
        actual = hashlib.sha256(
            json.dumps(document, sort_keys=True).encode()
        ).hexdigest()[:16]
        assert actual == digest, (config_name, call_name, actual)
