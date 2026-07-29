"""Playback resolve: PlaybackInfo -> stream URL -> setResolvedUrl.

No interactive dialogs in this path — the device profile decides everything.
The resolved play's state is queued on kofin.play.json for the service-side
player to claim and report.
"""

from typing import Any, Dict, List, Optional, Tuple

import xbmc
import xbmcgui
import xbmcplugin

from kofin.core import deviceprofile, kodirpc, settings, state, toast
from kofin.core.api import Api
from kofin.core.http import Http, JellyfinError
from kofin.core.log import Logger
from kofin.core.settings import Credentials
from kofin.plugin import listitems
from kofin.plugin.router import Request

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


def external_subtitles(server: str, source: JsonDict) -> List[str]:
    urls = []
    for stream in source.get("MediaStreams") or []:
        if (
            stream.get("Type") == "Subtitle"
            and stream.get("IsExternal")
            and stream.get("DeliveryUrl")
            and stream.get("DeliveryMethod") == "External"
        ):
            urls.append(server + stream["DeliveryUrl"])
    return urls


def play_state(
    item: JsonDict,
    source: JsonDict,
    url: str,
    play_method: str,
    play_session_id: str,
    device_id: str,
    start_seconds: float,
) -> JsonDict:
    return {
        "Id": item.get("Id", ""),
        "Type": item.get("Type", ""),
        # Carried so the service can name the item in a dialog after playback
        # ends, without a round trip for something it already had.
        "Name": item.get("Name", ""),
        "SeriesId": item.get("SeriesId", ""),
        "Path": url,
        "PlayMethod": play_method,
        "PlaySessionId": play_session_id,
        "MediaSourceId": source.get("Id", ""),
        "DeviceId": device_id,
        "Runtime": int(source.get("RunTimeTicks") or item.get("RunTimeTicks") or 0),
        "AudioStreamIndex": source.get("DefaultAudioStreamIndex"),
        "SubtitleStreamIndex": source.get("DefaultSubtitleStreamIndex"),
        "CurrentPosition": start_seconds,
    }


def prefetch_segments(api: Api, item: JsonDict) -> Optional[List[JsonDict]]:
    """Warm the media-segments fetch on the play path (plan §2d): the parsed
    segments ride the play-state queue so the service-side checker is armed
    before the first frame, killing the t≈0 Intro race. None on failure —
    the service then falls back to its own bounded-retry fetch."""
    if item.get("Type") not in ("Movie", "Episode"):
        return []
    if not settings.get_bool("mediaSegmentsEnabled"):
        return []
    from kofin.service.segments import parse_segments

    try:
        return parse_segments(api.media_segments(item.get("Id", "")))
    except Exception as error:
        LOG.debug("segments prefetch failed for %s: %s", item.get("Id"), error)
        return None


def resume_start_ticks(item: JsonDict, dbid: str) -> int:
    """Where a resume actually starts, in ticks.

    For a library row that is the bookmark in Kodi's own database, taken
    verbatim. Kodi seeks a library item to that bookmark and ignores what the
    resolved item says — measured: with the bookmark at 200s and the resolved
    item stating 895s, playback landed at 200s — so any other answer is a
    number nothing acts on. It is also the time Kodi's resume prompt just
    quoted at the user, and the sync writes it from the server's position with
    the resume offset already applied, so applying the offset again here would
    double it.

    Everything else — a plugin listing, a row kofin cannot read — has no Kodi
    bookmark to seek to. There the resolved item's resume point is the only one
    in play, so the server's position (offset applied here) is both the answer
    and what Kodi will act on.

    A readable row whose bookmark is 0 answers 0: Kodi will start at the
    beginning whatever the server thinks, and saying so is the honest report.
    """
    media = listitems.MEDIATYPE.get(item.get("Type", ""), "")
    if dbid.isdigit() and media in kodirpc.RESUME_QUERY:
        kodi_resume = kodirpc.resume_seconds(int(dbid), media)
        if kodi_resume is not None:
            return int(kodi_resume * 10_000_000)
    position = float((item.get("UserData") or {}).get("PlaybackPositionTicks") or 0)
    return int(settings.adjusted_resume(position / 10_000_000) * 10_000_000)


def play(request: Request) -> None:
    item_id = request.params.get("id", "")
    creds = Credentials.load()
    if not creds.is_logged_in or not item_id:
        _fail(request)
        return

    transcode = request.params.get("transcode") == "1"
    try:
        bitrate_mbps = float(request.params.get("bitrate", "0"))
    except ValueError:
        bitrate_mbps = 0.0

    api = Api.from_credentials(Http(settings.get_bool("sslVerify")), creds)
    try:
        item = api.item(item_id)
        from_start = request.params.get("fromstart") == "1"
        dbid = request.params.get("dbid", "")
        start_ticks = 0
        if request.resume and not from_start:
            start_ticks = resume_start_ticks(item, dbid)
        # An explicit start position wins over resume/fromstart: SyncPlay
        # group starts say exactly where the group timeline is (plan §2).
        try:
            start_ticks = int(request.params.get("startticks") or start_ticks)
        except ValueError:
            pass

        config = deviceprofile.ProfileConfig.from_settings()
        profile = deviceprofile.build(
            config,
            bitrate_override_mbps=bitrate_mbps,
            force_transcode=transcode,
        )
        info = api.playback_info(item_id, profile, start_ticks=start_ticks)
        sources = info.get("MediaSources") or []
        if not sources:
            raise JellyfinError("no media sources for %s" % item_id)
        source = pick_media_source(sources, request.params.get("mediasourceid"))
        play_session_id = info.get("PlaySessionId", "")
        url, method = stream_url(
            api.server, item, source, creds.device_id, play_session_id
        )
        is_audio = item.get("Type") in AUDIO_TYPES
        if method == "Transcode" and not is_audio:
            # The server sizes its own VideoBitrate/AudioBitrate off the
            # profile cap alone; recompute them so a forced transcode is
            # bounded by the source and the audio share stays proportional.
            # Music is exempt: its transcode is already sized by
            # MusicStreamingTranscodingBitrate, and the video audio share
            # would otherwise overwrite that with an unrelated number.
            budget = transcode_budget(
                source,
                int(profile["MaxStreamingBitrate"]),
                transcode or config.force_transcode,
            )
            if budget < deviceprofile.UNLIMITED_BITRATE:
                url = rewrite_bitrates(url, budget, config.audio_bitrate_kbps)
    except JellyfinError as error:
        LOG.warning("play resolve failed for %s: %s", item_id, error)
        _fail(request)
        return

    LOG.info("play %s via %s", item_id, method)
    # A resume point on the *resolved* item overrides the choice the user made
    # at Kodi's resume prompt: Kodi treats a resolved item that carries one as
    # a resume regardless of the resume:true|false it just passed us, and then
    # seeks — to the item's own point, or, when that point has no time on it,
    # to the bookmark in Kodi's database. Either way "Play from beginning"
    # landed back on the resume position, because build() stamps the server's
    # position on everything it builds. Nor can the point be cleared once
    # stamped, so the play route states its start position up front and 0
    # means the item is built without one.
    li = listitems.build(item, api.server, resume_seconds=start_ticks / 10_000_000)
    # Library-item paths carry the Kodi database id (plan §2 path identity);
    # stamping it on the tag links the playback to the library row for
    # widgets invoked outside a library window.
    if dbid.isdigit() and item.get("Type") in ("Movie", "Episode", "MusicVideo"):
        li.getVideoInfoTag().setDbId(int(dbid))
    elif dbid.isdigit() and is_audio:
        # setResolvedUrl replaces the library item's music tag wholesale, so
        # the song's database id has to be restated or the link back to the
        # Kodi row is lost for anything reading the tag.
        li.getMusicInfoTag().setDbId(int(dbid), "song")
    li.setPath(url)
    mime = mime_for(source, method)
    if mime:
        li.setMimeType(mime)
    li.setContentLookup(False)
    subtitles = external_subtitles(api.server, source)
    if subtitles:
        li.setSubtitles(subtitles)

    play_item = play_state(
        item,
        source,
        url,
        method,
        play_session_id,
        creds.device_id,
        start_ticks / 10_000_000,
    )
    segments = prefetch_segments(api, item)
    if segments is not None:
        play_item["Segments"] = segments
    state.push_play_item(play_item)

    if request.handle >= 0:
        xbmcplugin.setResolvedUrl(request.handle, True, li)
    else:
        xbmc.Player().play(url, li)


def _fail(request: Request) -> None:
    if request.handle >= 0:
        xbmcplugin.setResolvedUrl(request.handle, False, xbmcgui.ListItem())
    toast.show(settings.localized(30018), toast.ERROR, time_ms=4000)
