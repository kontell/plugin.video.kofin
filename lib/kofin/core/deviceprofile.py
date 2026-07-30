"""Jellyfin device profile, ported from pvr.kofin's BuildDeviceProfile.

Deliberate deviation from the C++ source: pvr.kofin clears DirectPlayProfiles
whenever a bitrate cap is set, because live-TV direct play ignores
MaxStreamingBitrate server-side. For VOD the server honors the cap in its
direct-play decision, so kofin keeps direct play available and lets the
server gate it per item.

``forceDirectPlay`` is scoped to video. It suppresses every video-side
constraint (HDR, width, streaming bitrate), but music delivery belongs to
``musicTranscode`` and its own cap, so forcing direct play for video does not
silently uncap music.

The same scoping holds the other way: ``forceRemux`` and ``forceTranscode``
withdraw the *video* DirectPlayProfile, never the audio one. A profile with no
audio DirectPlayProfile makes the server re-encode every song no matter what
the music settings say.
"""

from typing import Any, Dict, List, Optional

from kofin.core import settings

JsonDict = Dict[str, Any]

UNLIMITED_BITRATE = 1_000_000_000

ALL_HDR_TYPES = [
    "HDR10",
    "HLG",
    "HDR10Plus",
    "DOVI",
    "DOVIWithHDR10",
    "DOVIWithHLG",
    "DOVIWithSDR",
    "DOVIWithEL",
    "DOVIWithHDR10Plus",
    "DOVIWithELHDR10Plus",
]
VP9_HDR_TYPES = ["HDR10", "HLG", "HDR10Plus"]

SUBTITLE_FORMATS = ["srt", "ass", "sub", "ssa", "smi", "pgssub", "dvdsub", "pgs"]
# The same list without the image formats. Offering only these is how a
# burn-in is requested — see _subtitle_profiles.
TEXT_SUBTITLE_FORMATS = ["srt", "ass", "sub", "ssa", "smi"]


DEFAULT_VIDEO_CODECS = [
    "h264",
    "h264_10bit",
    "hevc",
    "hevc_rext",
    "av1",
    "mpeg2video",
    "vp9",
    "vc1",
]
DEFAULT_AUDIO_CODECS = ["aac", "mp2", "mp3", "ac3", "eac3", "opus", "flac", "dts"]


def audio_bitrate_bps(audio_cap_kbps: int, budget_bps: int) -> int:
    """The audio allowance for a transcode confined to ``budget_bps``.

    pvr.kofin splits the budget rather than reserving a fixed share, because
    a fixed reservation pushes VideoBitrate negative whenever the budget is
    below it — a 0.5 Mbit/s context transcode, or force-transcoding a source
    that reports less than the reservation — and the server rejects a
    negative bitrate. Deviation from the C++ source, which hardcodes 384k and
    divides by 8: kofin has a configurable transcode audio bitrate, so the
    share is min(setting, budget/10). An unlimited budget carves nothing off
    and leaves the setting to stand on its own.
    """
    cap = max(audio_cap_kbps, 0) * 1000
    if budget_bps <= 0 or budget_bps >= UNLIMITED_BITRATE:
        return cap
    return min(cap, budget_bps // 10)


class ProfileConfig:
    """The transcoding settings snapshot a profile is built from."""

    def __init__(
        self,
        force_direct_play: bool = False,
        force_remux: bool = False,
        force_transcode: bool = False,
        video_codecs: Optional[List[str]] = None,
        audio_codecs: Optional[List[str]] = None,
        hdr_types: Optional[List[str]] = None,
        preferred_video: str = "h264",
        preferred_audio: str = "aac",
        max_channels: int = 6,
        max_bitrate_mbps: int = 0,
        max_width: int = 0,
        audio_bitrate_kbps: int = 384,
        music_codec: str = "opus",
        music_bitrate_kbps: int = 128,
        music_max_bitrate_kbps: int = 0,
    ) -> None:
        self.force_direct_play = force_direct_play
        self.force_remux = force_remux
        self.force_transcode = force_transcode
        self.video_codecs = list(
            DEFAULT_VIDEO_CODECS if video_codecs is None else video_codecs
        )
        self.audio_codecs = list(
            DEFAULT_AUDIO_CODECS if audio_codecs is None else audio_codecs
        )
        self.hdr_types = list(ALL_HDR_TYPES if hdr_types is None else hdr_types)
        self.preferred_video = preferred_video
        self.preferred_audio = preferred_audio
        self.max_channels = max_channels
        self.max_bitrate_mbps = max_bitrate_mbps
        self.max_width = max_width
        self.audio_bitrate_kbps = audio_bitrate_kbps
        self.music_codec = music_codec
        self.music_bitrate_kbps = music_bitrate_kbps
        self.music_max_bitrate_kbps = music_max_bitrate_kbps

    @classmethod
    def from_settings(cls) -> "ProfileConfig":
        return cls(
            force_direct_play=settings.get_bool("forceDirectPlay"),
            force_remux=settings.get_bool("forceRemux"),
            force_transcode=settings.get_bool("forceTranscode"),
            video_codecs=settings.get_list("directPlayVideoCodecs"),
            audio_codecs=settings.get_list("directPlayAudioCodecs"),
            hdr_types=settings.get_list("allowedHdrTypes"),
            preferred_video=settings.get_str("preferredVideoCodec") or "h264",
            preferred_audio=settings.get_str("preferredAudioCodec") or "aac",
            max_channels=settings.get_int("maxAudioChannels") or 6,
            max_bitrate_mbps=settings.get_int("maxStreamingBitrate"),
            max_width=settings.get_int("maxResolution"),
            audio_bitrate_kbps=settings.get_int("audioBitrate") or 384,
            music_codec=settings.get_str("musicTranscodeCodec") or "opus",
            music_bitrate_kbps=settings.get_int("musicTranscodeBitrate") or 128,
            # ``musicTranscode`` owns the whole music group: with it off the
            # cap must not apply, or a value left behind from an earlier
            # session would still force transcodes on the paths that do reach
            # PlaybackInfo (browsing the server directly). The transcoding
            # profile itself stays either way — it is the server's fallback
            # when a container cannot be direct played at all.
            music_max_bitrate_kbps=(
                settings.get_int("musicMaxBitrate")
                if settings.get_bool("musicTranscode")
                else 0
            ),
        )


def build(
    config: ProfileConfig,
    bitrate_override_mbps: float = 0,
    force_transcode: bool = False,
    burn_subtitles: bool = False,
) -> JsonDict:
    """The DeviceProfile JSON for PlaybackInfo requests.

    ``bitrate_override_mbps``/``force_transcode`` implement the transcode
    context item: a forced transcode at a chosen bitrate for this play only.
    The override may be fractional (0.5/0.75 Mbit/s) or 0, which — like force
    transcode itself — means the source bitrate (unlimited).

    ``burn_subtitles`` withdraws the image formats from SubtitleProfiles for
    this play only, which is what makes the server answer ``Encode`` for a
    PGS/DVDSUB track instead of ``External``. It is the single case where kofin
    asks for burn-in: an image subtitle chosen on a transcode, where the
    stream carries no subtitles and the ``.sup`` the External delivery would
    serve is neither small enough to fetch nor something Kodi can render.
    """
    force_direct = config.force_direct_play and not force_transcode
    bitrate_mbps = bitrate_override_mbps or config.max_bitrate_mbps
    if force_direct or bitrate_mbps <= 0:
        max_bitrate = UNLIMITED_BITRATE
    else:
        max_bitrate = int(bitrate_mbps * 1_000_000)

    audio_codecs = _preferred_first(config.audio_codecs, config.preferred_audio)
    tokens = set(config.video_codecs)
    h264 = "h264" in tokens
    h264_10bit = "h264_10bit" in tokens
    hevc = "hevc" in tokens
    hevc_rext = "hevc_rext" in tokens

    video_codecs: List[str] = []
    if h264 or h264_10bit:
        video_codecs.append("h264")
    if hevc or hevc_rext:
        video_codecs.append("hevc")
    for token in config.video_codecs:  # keep the configured order for the rest
        if token not in ("h264", "h264_10bit", "hevc", "hevc_rext"):
            video_codecs.append(token)

    profile: JsonDict = {
        "Name": "Kodi",
        "MaxStreamingBitrate": max_bitrate,
        "MaxStaticBitrate": max_bitrate,
        "MusicStreamingTranscodingBitrate": config.music_bitrate_kbps * 1000,
        "TimelineOffsetSeconds": 5,
        "TranscodingProfiles": _transcoding_profiles(
            config, audio_codecs, video_codecs, max_bitrate
        ),
        "DirectPlayProfiles": _direct_play_profiles(
            config, audio_codecs, video_codecs, force_direct, force_transcode
        ),
        "CodecProfiles": _codec_profiles(
            config, force_direct, h264, h264_10bit, hevc, hevc_rext, tokens
        ),
        "SubtitleProfiles": _subtitle_profiles(burn_subtitles),
    }
    return profile


def _subtitle_profiles(burn_subtitles: bool) -> List[JsonDict]:
    """Declaring a format here is what keeps the server from burning it in.

    With the image formats present the server answers ``External`` for every
    subtitle, embedded PGS included — which is why kofin has never burned
    anything in. Withdrawing them is therefore an explicit request for
    ``Encode``, not a tidy-up; the text formats stay either way so a text
    subtitle chosen alongside is still delivered as a file.
    """
    formats = TEXT_SUBTITLE_FORMATS if burn_subtitles else SUBTITLE_FORMATS
    return [
        {"Format": fmt, "Method": method}
        for fmt in formats
        for method in ("Embed", "External")
    ]


def _preferred_first(codecs: List[str], preferred: str) -> List[str]:
    ordered = [preferred]
    ordered.extend(codec for codec in codecs if codec != preferred)
    return ordered


def _transcoding_profiles(
    config: ProfileConfig,
    audio_codecs: List[str],
    video_codecs: List[str],
    max_bitrate: int,
) -> List[JsonDict]:
    # TS codec list: everything except av1 (which can't ride MPEG-TS and gets
    # its own fMP4 profile). The lead codec is the forced-transcode target:
    # hevc when av1 is preferred, otherwise the preferred codec.
    hevc_in_list = "hevc" in video_codecs
    ts_lead = (
        "hevc"
        if config.preferred_video == "av1" and hevc_in_list
        else config.preferred_video
    )
    ts_codecs = [ts_lead]
    ts_codecs.extend(
        codec for codec in video_codecs if codec != ts_lead and codec != "av1"
    )
    if ts_lead == "av1":  # preferred av1 but no hevc available
        ts_codecs = [codec for codec in ts_codecs if codec != "av1"] or ["h264"]

    common = {
        "Type": "Video",
        "AudioCodec": ",".join(audio_codecs),
        "Context": "Streaming",
        "Protocol": "hls",
        "MaxAudioChannels": str(config.max_channels),
        "MinSegments": "1",
        "BreakOnNonKeyFrames": True,
    }
    audio_bps = audio_bitrate_bps(config.audio_bitrate_kbps, max_bitrate)
    if audio_bps > 0:
        # Output constraint for transcodes only — never a direct-play gate.
        # Capped against the streaming budget so the profile agrees with the
        # split ``plugin/play.py`` writes into the transcoding URL.
        common["Conditions"] = [
            {
                "Condition": "LessThanEqual",
                "Property": "AudioBitrate",
                "Value": str(audio_bps),
                "IsRequired": False,
            }
        ]
    fmp4: JsonDict = dict(common, Container="mp4", VideoCodec="av1")
    ts: JsonDict = dict(common, Container="ts", VideoCodec=",".join(ts_codecs))

    music: JsonDict = {
        "Type": "Audio",
        "Container": config.music_codec,
        "AudioCodec": config.music_codec,
        "Context": "Streaming",
        "Protocol": "http",
        "MaxAudioChannels": "2",
    }

    video_profiles = [fmp4, ts] if config.preferred_video == "av1" else [ts, fmp4]
    return video_profiles + [music]


def _direct_play_profiles(
    config: ProfileConfig,
    audio_codecs: List[str],
    video_codecs: List[str],
    force_direct: bool,
    force_transcode: bool,
) -> List[JsonDict]:
    if force_direct:
        return [
            {"Type": "Video", "Container": "", "VideoCodec": "", "AudioCodec": ""},
            {"Type": "Audio"},
        ]
    if force_transcode:
        # This play only, and the user asked for it by name (the transcode
        # context item), so it takes the audio entry with it.
        return []

    # Scoped to video, like forceDirectPlay above: forceRemux/forceTranscode
    # and the direct-play codec list all describe the *video* pipe. Dropping
    # the audio entry with them left no audio DirectPlayProfile at all, so the
    # server re-encoded every song — measured against 10.11: an mp3 came back
    # SupportsDirectPlay=false with a stream.opus TranscodingUrl, whatever
    # musicTranscode and the music bitrate cap said. Music delivery is owned
    # by musicTranscode and its cap, which gate direct play through the audio
    # CodecProfile in _codec_profiles.
    audio = [{"Type": "Audio"}]

    if config.force_remux or config.force_transcode or not video_codecs:
        return audio

    direct_video = list(video_codecs)
    if config.preferred_video not in direct_video:
        # Preferring a codec implies the device decodes it.
        direct_video.append(config.preferred_video)
    return [
        {
            "Type": "Video",
            "VideoCodec": ",".join(direct_video),
            "AudioCodec": ",".join(audio_codecs),
        }
    ] + audio


def _codec_profiles(
    config: ProfileConfig,
    force_direct: bool,
    h264: bool,
    h264_10bit: bool,
    hevc: bool,
    hevc_rext: bool,
    tokens: "set[str]",
) -> List[JsonDict]:
    profiles: List[JsonDict] = []

    if h264 and not h264_10bit:
        profiles.append(_codec_condition("h264", "LessThanEqual", "VideoBitDepth", "8"))
    if hevc and not hevc_rext:
        profiles.append(
            _codec_condition("hevc", "EqualsAny", "VideoProfile", "main|main 10")
        )

    if not force_direct:
        selected = [hdr for hdr in config.hdr_types if hdr in ALL_HDR_TYPES]
        profiles.extend(
            _hdr_profile(codec, capability, can_dovi, selected)
            for codec, capability, can_dovi, present in (
                ("hevc", ALL_HDR_TYPES, True, hevc or hevc_rext),
                ("av1", ALL_HDR_TYPES, True, "av1" in tokens),
                ("vp9", VP9_HDR_TYPES, False, "vp9" in tokens),
            )
            if present and set(capability) - set(selected)
        )

        if config.max_width > 0:
            profiles.append(
                {
                    "Type": "Video",
                    "Conditions": [
                        {
                            "Condition": "LessThanEqual",
                            "Property": "Width",
                            "Value": str(config.max_width),
                            "IsRequired": False,
                        }
                    ],
                }
            )

    # Outside the force-direct-play block on purpose: that toggle is scoped to
    # video. Music delivery is owned by ``musicTranscode`` and its cap, so a
    # user who forces direct play for video can still have the server hold
    # music to a bitrate.
    if config.music_max_bitrate_kbps > 0:
        profiles.append(
            {
                "Type": "Audio",
                "Conditions": [
                    {
                        "Condition": "LessThanEqual",
                        "Property": "AudioBitrate",
                        "Value": str(config.music_max_bitrate_kbps * 1000),
                        "IsRequired": False,
                    }
                ],
            }
        )

    return profiles


def _codec_condition(codec: str, condition: str, prop: str, value: str) -> JsonDict:
    return {
        "Type": "Video",
        "Codec": codec,
        "Conditions": [{"Condition": condition, "Property": prop, "Value": value}],
    }


def _hdr_profile(
    codec: str, capability: List[str], can_dovi: bool, selected: List[str]
) -> JsonDict:
    # SDR is always allowed and must lead; iterate the canonical order for a
    # deterministic value string.
    value = "SDR"
    for hdr_type in ALL_HDR_TYPES:
        if hdr_type in capability and hdr_type in selected:
            value += "|" + hdr_type
    if can_dovi and "HDR10" in selected:
        value += "|DOVIInvalid"  # invalid DV is served as its HDR10 base layer
    profile = _codec_condition(codec, "EqualsAny", "VideoRangeType", value)
    profile["Conditions"][0]["IsRequired"] = False
    return profile
