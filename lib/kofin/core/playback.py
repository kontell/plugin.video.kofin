"""Shared playback resolve helpers (stream selection PR3a).

Pure-ish URL / bitrate / source selection used by the plugin play route today
and by service-side restart later (PR3b). Stream-index mapping for remote
control lives here so the websocket handler stays enqueue-only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, TYPE_CHECKING

from kofin.core import deviceprofile, streammaps
from kofin.core.http import JellyfinError
from kofin.core.log import Logger

if TYPE_CHECKING:
    from kofin.core.api import Api

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

MIME_BY_CONTAINER = {
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "mkv": "video/x-matroska",
    "webm": "video/webm",
    "avi": "video/avi",
    "ts": "video/mp2t",
    "mpegts": "video/mp2t",
    "mov": "video/quicktime",
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "aac": "audio/aac",
    "m4a": "audio/mp4",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "wav": "audio/wav",
}
HLS_MIME = "application/x-mpegURL"

AUDIO_TYPES = frozenset({"Audio"})

# pvr.kofin's stand-in for a MediaSource that reports no bitrate.
ASSUMED_SOURCE_BITRATE = 30_000_000


def pick_media_source(
    sources: List[JsonDict], mediasource_id: Optional[str] = None
) -> JsonDict:
    """Select a MediaSource for playback.

    Library version rows pass ``mediasourceid`` so the alternate file is used;
    primary movie URLs omit it and take the first source. A missing id falls
    back to the first source (server may have reorganized sources).
    """
    if not sources:
        raise JellyfinError("no media sources")
    wanted = (mediasource_id or "").strip()
    if wanted:
        for source in sources:
            if source.get("Id") == wanted:
                return source
        LOG.warning("mediasourceid %s not in PlaybackInfo; using first source", wanted)
    return sources[0]


def stream_url(
    server: str,
    item: JsonDict,
    source: JsonDict,
    device_id: str,
    play_session_id: str,
) -> Tuple[str, str]:
    """(url, play method) for a MediaSource; raises on unplayable.

    Every stream served off the transcoding endpoint reports as Transcode,
    remux included — the same call pvr.kofin and jellyfin-kodi make. The
    dashboard's remux-vs-transcode wording comes from the server's own
    TranscodingInfo (IsVideoDirect/IsAudioDirect), not from this value, so
    reporting a stream copy as DirectStream here buys nothing and costs the
    session-close and drift-correction paths their signal.
    """
    if source.get("SupportsDirectPlay") or source.get("SupportsDirectStream"):
        kind = "Audio" if item.get("Type") in AUDIO_TYPES else "Videos"
        container = (source.get("Container") or "").split(",")[0]
        suffix = ".%s" % container if container else ""
        url = (
            "%s/%s/%s/stream%s?static=true&mediaSourceId=%s&deviceId=%s&playSessionId=%s"
            % (
                server,
                kind,
                item.get("Id", ""),
                suffix,
                source.get("Id", ""),
                device_id,
                play_session_id,
            )
        )
        return url, "DirectStream"
    transcoding_url = source.get("TranscodingUrl")
    if transcoding_url:
        return server + transcoding_url, "Transcode"
    raise JellyfinError(
        "no playable stream for %s (%s)" % (item.get("Id"), source.get("Id"))
    )


def mime_for(source: JsonDict, play_method: str) -> str:
    """The MIME type to hand Kodi for the resolved stream.

    A transcode is only HLS when the server says so. The music transcoding
    profile is plain http (``TranscodingSubProtocol: "http"``), and labelling
    that stream as an HLS playlist makes Kodi try to parse the audio as m3u8;
    it gets the transcoded container's type instead.
    """
    if play_method == "Transcode":
        if (source.get("TranscodingSubProtocol") or "hls").lower() == "hls":
            return HLS_MIME
        container = source.get("TranscodingContainer") or ""
    else:
        container = (source.get("Container") or "").split(",")[0]
    return MIME_BY_CONTAINER.get(container.lower(), "")


def transcode_budget(
    source: JsonDict, max_bitrate_bps: int, force_transcode: bool
) -> int:
    """The bits/s a transcode must fit into.

    A forced transcode caps at the source bitrate so the server actually
    re-encodes: with an unlimited budget and no direct-play profile Jellyfin
    still picks a transcoding profile but copies both codecs, which is why
    forcing a transcode appeared to do nothing. The source bitrate comes from
    the PlaybackInfo response and is never uncapped.
    """
    if not force_transcode:
        return max_bitrate_bps
    source_bps = int(source.get("Bitrate") or 0) or ASSUMED_SOURCE_BITRATE
    return min(source_bps, max_bitrate_bps)


def rewrite_bitrates(url: str, budget_bps: int, audio_cap_kbps: int) -> str:
    """Replace the server's bitrates with our split of ``budget_bps``.

    Params are spliced textually rather than re-encoded, as pvr.kofin does:
    the transcoding URL carries opaque values (PlaySessionId, api_key) that a
    parse/re-encode round trip has no business touching.
    """
    base, _, query = url.partition("?")
    if not query:
        return url
    kept = [
        param
        for param in query.split("&")
        if not param.startswith(("VideoBitrate=", "AudioBitrate="))
    ]
    audio = deviceprofile.audio_bitrate_bps(audio_cap_kbps, budget_bps)
    kept.append("VideoBitrate=%d" % max(budget_bps - audio, 0))
    kept.append("AudioBitrate=%d" % audio)
    return "%s?%s" % (base, "&".join(kept))


def format_stream_label(stream: Mapping[str, Any]) -> str:
    """Prefer server DisplayTitle; else Language - Codec Channels ch."""
    title = (stream.get("DisplayTitle") or "").strip()
    if title:
        return title
    lang = stream.get("Language") or "und"
    codec = (stream.get("Codec") or "").upper()
    ch = stream.get("Channels")
    if stream.get("Type") == "Audio" and ch:
        return "%s - %s %sch" % (lang, codec, ch)
    return "%s - %s" % (lang, codec) if codec else str(lang)


def parse_remote_stream_index(arguments: Mapping[str, Any]) -> Optional[int]:
    """Jellyfin GeneralCommand payload → MediaStream index.

    Live clients disagree on the key name; accept common aliases.
    """
    for key in (
        "Index",
        "AudioStreamIndex",
        "SubtitleStreamIndex",
        "StreamIndex",
        "SubtitleIndex",
        "AudioIndex",
    ):
        if key not in arguments or arguments[key] is None or arguments[key] == "":
            continue
        try:
            return int(arguments[key])
        except (TypeError, ValueError):
            continue
    return None


# Local apply decision for remote SetAudio/SetSubtitle (PR3a). Restart for
# TC audio is PR3b — reported as needs_restart here.
LocalStreamAction = str  # audio|subtitle|subtitle_off|refuse|needs_restart


def resolve_local_stream_switch(
    item: Mapping[str, Any],
    *,
    kind: str,
    jellyfin_index: Optional[int],
) -> Tuple[LocalStreamAction, Optional[int], str]:
    """Decide how to honour a remote stream-index command without restarting.

    Returns ``(action, kodi_index_or_none, reason)``.
    """
    if kind not in ("audio", "subtitle"):
        return "refuse", None, "unknown kind %s" % kind

    if kind == "audio":
        if jellyfin_index is None:
            return "refuse", None, "missing audio index"
        amap = streammaps.int_map(item.get("AudioMap") or {})
        if jellyfin_index not in amap:
            return "refuse", None, "audio index %s not in AudioMap" % jellyfin_index
        if item.get("PlayMethod") == "Transcode":
            # Single-rendition HLS: local setAudioStream cannot change the
            # source track. PR3b restarts PlaybackInfo with the new index.
            return (
                "needs_restart",
                amap[jellyfin_index],
                "transcode audio requires PlaybackInfo restart (PR3b)",
            )
        return "audio", amap[jellyfin_index], "ok"

    # subtitle
    if jellyfin_index is None or jellyfin_index < 0:
        return "subtitle_off", None, "disable subtitles"

    ready = bool(item.get("SubsMappingReady"))
    attach = list(item.get("SubsAttachOrder") or [])
    abs_map = streammaps.int_map(item.get("SubsMapping") or {})
    rev_abs = streammaps.reverse_map(abs_map)
    emb = streammaps.int_map(item.get("EmbeddedSubMap") or {})

    if ready and jellyfin_index in rev_abs:
        return "subtitle", rev_abs[jellyfin_index], "ok"

    if not ready:
        # External attach not reconciled yet — never guess attachment order.
        if attach and jellyfin_index in attach:
            return (
                "refuse",
                None,
                "SubsMapping not ready for external subtitle %s" % jellyfin_index,
            )
        # Demux-only / no externals: provisional embedded map is absolute.
        if not attach and jellyfin_index in emb:
            return "subtitle", emb[jellyfin_index], "embedded provisional"
        return "refuse", None, "SubsMapping not ready"

    # Ready but index missing from absolute map — try embedded provisional
    # only when that JF index is known demuxed (not external attach).
    if jellyfin_index in emb and jellyfin_index not in attach:
        # Re-seat: prefer absolute map values that equal this JF index
        for kodi_i, jf in abs_map.items():
            if jf == jellyfin_index:
                return "subtitle", kodi_i, "ok"
        return (
            "refuse",
            None,
            "embedded subtitle %s not in absolute map" % jellyfin_index,
        )

    if item.get("PlayMethod") == "Transcode":
        return (
            "needs_restart",
            None,
            "transcode subtitle %s needs restart (PR3b)" % jellyfin_index,
        )
    return "refuse", None, "subtitle index %s not mapped" % jellyfin_index


def eligible_audio_streams(source: Mapping[str, Any]) -> List[JsonDict]:
    return [
        dict(s) for s in (source.get("MediaStreams") or []) if s.get("Type") == "Audio"
    ]


def should_offer_pick_audio(item: Mapping[str, Any]) -> bool:
    """True when the local mid-play TC audio picker is useful (PR5).

    Only Transcode sessions lack demuxed multi-audio in the native OSD;
    DirectStream already exposes tracks via Kodi. Need more than one source
    audio stream to switch between.
    """
    if item.get("PlayMethod") != "Transcode":
        return False
    streams = item.get("AudioStreams") or []
    return len(streams) > 1


# Position band for corrective seek after a restart (design §6.5).
RESTART_SEEK_TOLERANCE_SECONDS = 2.0


def resolve_restart_stream(
    api: "Api",
    *,
    item_id: str,
    media_source_id: str,
    device_id: str,
    force_transcode: bool,
    bitrate_override_mbps: float,
    audio_index: Optional[int],
    subtitle_index: Optional[int],
    start_ticks: int,
) -> Tuple[str, str, JsonDict, str, JsonDict]:
    """PlaybackInfo + URL for a mid-play stream restart (PR3b).

    Returns ``(url, play_method, source, play_session_id, profile)``.
    Reuses the same profile force/bitrate and bitrate rewrite as the plugin
    play path so context force-transcode survives the restart.
    """
    config = deviceprofile.ProfileConfig.from_settings()
    profile = deviceprofile.build(
        config,
        bitrate_override_mbps=bitrate_override_mbps,
        force_transcode=force_transcode,
    )
    info = api.playback_info(
        item_id,
        profile,
        start_ticks=start_ticks,
        audio_index=audio_index,
        subtitle_index=subtitle_index,
        media_source_id=media_source_id or None,
    )
    sources = info.get("MediaSources") or []
    if not sources:
        raise JellyfinError("no media sources for restart of %s" % item_id)
    source = pick_media_source(sources, media_source_id)
    play_session_id = info.get("PlaySessionId", "")
    item_stub: JsonDict = {"Id": item_id, "Type": "Movie"}
    url, method = stream_url(api.server, item_stub, source, device_id, play_session_id)
    if method == "Transcode":
        budget = transcode_budget(
            source,
            int(profile["MaxStreamingBitrate"]),
            force_transcode or config.force_transcode,
        )
        if budget < deviceprofile.UNLIMITED_BITRATE:
            url = rewrite_bitrates(url, budget, config.audio_bitrate_kbps)
    return url, method, source, play_session_id, profile


# Image codecs that may appear in the pre-play dialog only when burn-in is on.
_IMAGE_SUB_CODECS = frozenset(
    {"pgssub", "pgs", "dvdsub", "dvbsub", "xsub", "vobsub", "sup", "hdmv_pgs_subtitle"}
)


def eligible_dialog_subs(
    source: Mapping[str, Any], *, allow_burned: bool
) -> List[JsonDict]:
    """Subtitle streams offered in the pre-play Transcode dialog (PR4)."""
    from kofin.core import subtitles as sub_mod

    out: List[JsonDict] = []
    for stream in source.get("MediaStreams") or []:
        if stream.get("Type") != "Subtitle":
            continue
        codec = sub_mod.normalize_codec(dict(stream))
        if codec in _IMAGE_SUB_CODECS:
            if allow_burned:
                out.append(dict(stream))
            continue
        if stream.get("IsTextSubtitleStream") or codec in sub_mod.TEXT_SUB_CODECS:
            out.append(dict(stream))
    return out


# transcodeStreamSelect spinner values
STREAM_SELECT_NEVER = 0
STREAM_SELECT_AUDIO_AND_SUBS = 1
STREAM_SELECT_AUDIO_ONLY = 2
STREAM_SELECT_SUBS_ONLY = 3

VIDEO_TYPES = frozenset({"Movie", "Episode", "Video", "MusicVideo"})

# Jellyfin PlaybackInfo: -1 means no subtitle track (web client convention).
SUBTITLE_OFF_INDEX = -1


def suppress_stream_dialogs(request_params: Mapping[str, Any]) -> bool:
    """True when pre-play stream dialogs must not run (SyncPlay)."""
    if str(request_params.get("syncplay") or "") == "1":
        return True
    from kofin.core import state

    return state.is_syncplay_active()


def stream_select_wants_audio(mode: int) -> bool:
    return mode in (STREAM_SELECT_AUDIO_AND_SUBS, STREAM_SELECT_AUDIO_ONLY)


def stream_select_wants_subs(mode: int) -> bool:
    return mode in (STREAM_SELECT_AUDIO_AND_SUBS, STREAM_SELECT_SUBS_ONLY)


def needs_preplay_stream_dialog(
    *,
    play_method: str,
    item_type: str,
    select_mode: int,
    source: Mapping[str, Any],
    allow_burned: bool,
    suppress: bool,
) -> Tuple[bool, bool]:
    """Return (ask_audio, ask_subs) for the pre-play Transcode dialogs."""
    if suppress or select_mode == STREAM_SELECT_NEVER:
        return False, False
    if play_method != "Transcode":
        return False, False
    if item_type not in VIDEO_TYPES:
        return False, False
    ask_audio = (
        stream_select_wants_audio(select_mode)
        and len(eligible_audio_streams(source)) > 1
    )
    ask_subs = (
        stream_select_wants_subs(select_mode)
        and len(eligible_dialog_subs(source, allow_burned=allow_burned)) > 1
    )
    return ask_audio, ask_subs
