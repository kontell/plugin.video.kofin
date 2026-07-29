"""Shared playback resolve helpers (stream selection PR3a).

Pure-ish URL / bitrate / source selection used by the plugin play route today
and by service-side restart later (PR3b). Stream-index mapping for remote
control lives here so the websocket handler stays enqueue-only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from kofin.core import deviceprofile, streammaps
from kofin.core.http import JellyfinError
from kofin.core.log import Logger

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
