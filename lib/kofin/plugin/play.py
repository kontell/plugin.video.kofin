"""Playback resolve: PlaybackInfo -> stream URL -> setResolvedUrl.

No interactive dialogs in this path — the device profile decides everything.
The resolved play's state is queued on kofin.play.json for the service-side
player to claim and report.
"""

import threading
from typing import Any, Dict, List, Optional, Tuple

import xbmc
import xbmcgui
import xbmcplugin

from kofin.core import deviceprofile, kodirpc, settings, state, streams, toast
from kofin.core.api import Api
from kofin.core.http import JellyfinError, plugin_transport
from kofin.core.log import Logger
from kofin.core.settings import Credentials
from kofin.plugin import listitems, subtitles
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


def deny_video_stream_copy(url: str) -> str:
    """Take the video stream copy off the table for a forced transcode.

    No bitrate can do this. Jellyfin allows the copy whenever the requested
    VideoBitrate is at or above the source's, so a budget high enough to be
    worth asking for is a budget that permits a copy -- and any budget low
    enough to forbid one is a quality choice the user did not make. Measured
    before this call existed, on an HEVC 2,000,962 + AAC 224,000 source sized
    to its own 2,231,688 total: kofin asked for 2,008,520 video, the server
    answered ``-codec:v:0 copy`` and re-encoded the audio instead, which had
    missed its share by 832 bits. Which stream got re-encoded turned on the
    source's audio share rather than on anything the user asked for.

    So the intent is stated instead of implied: ``allowVideoStreamCopy=false``
    makes ``CanStreamCopyVideo`` answer no whatever the bitrates say. Video
    only -- ``enableAutoStreamCopy=false`` would deny the audio copy too, and
    forcing a video transcode is no reason to re-encode audio that already
    fits.
    """
    base, _, query = url.partition("?")
    if not query:
        return url
    kept = [
        param
        for param in query.split("&")
        if not param.startswith("allowVideoStreamCopy=")
    ]
    kept.append("allowVideoStreamCopy=false")
    return "%s?%s" % (base, "&".join(kept))


def play_state(
    item: JsonDict,
    source: JsonDict,
    url: str,
    play_method: str,
    play_session_id: str,
    device_id: str,
    start_seconds: float,
    attached: Optional[List[int]] = None,
    request_params: Optional[Dict[str, str]] = None,
) -> JsonDict:
    return {
        "Id": item.get("Id", ""),
        "Type": item.get("Type", ""),
        # Carried so the service can name the item in a dialog after playback
        # ends, without a round trip for something it already had.
        "Name": item.get("Name", ""),
        # Same reason, and it has to travel: the dialog in question is the
        # delete offer, and the queue entry is all the service gets. The
        # server answers this per account on the fetch above, with no Fields
        # request needed (verified on 10.11).
        "CanDelete": bool(item.get("CanDelete")),
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
        # Everything the stream menu needs, resolved here because the
        # PlaybackInfo that answers it has just been made — the menu costs no
        # round trip and is complete before the first frame. The service moves
        # this to a window property when it claims the playback, since by then
        # the queue entry is gone and the context item runs in a third process.
        "Streams": {
            "MediaStreams": streams.summarize(source),
            # The setSubtitles order, which is what makes a Jellyfin index
            # translatable to a Kodi subtitle number at all.
            "Attached": list(attached or []),
            # A restart has to reproduce *this* play method, and a context
            # transcode's bitrate lives nowhere else — the settings would
            # resolve it back to direct play.
            "Request": dict(request_params or {}),
        },
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


def _stream_index(raw: Optional[str]) -> Optional[int]:
    """A stream-index play param, or None when absent or junk.

    ``-1`` is meaningful and passes through: it is how Jellyfin is told "no
    subtitle", as distinct from omitting the parameter and letting the user's
    profile choose one.
    """
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


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

    http = plugin_transport(settings.get_bool("sslVerify"))
    api = Api.from_credentials(http, creds, interactive=True)
    # The media-segments prefetch shares nothing with PlaybackInfo or the
    # subtitle fetches, so it runs beside them instead of after them and its
    # round trip leaves the resolve's critical path (perf plan W2.6). The
    # item fetch stays ahead of PlaybackInfo on the main thread: StartTimeTicks
    # needs the resume position, which in the server-resume case only the item
    # DTO knows. prefetch_segments catches its own failures (None), so the
    # join can only yield segments or the service-side fallback. Daemon and
    # bounded (interactive Api budget), so an abandoned fail path leaks
    # nothing but a finishing request.
    segments_box: List[Optional[List[JsonDict]]] = []
    segments_thread: Optional[threading.Thread] = None
    try:
        item = api.item(item_id)
        segments_thread = threading.Thread(
            target=lambda: segments_box.append(prefetch_segments(api, item)),
            name="kofin-segments-prefetch",
            daemon=True,
        )
        segments_thread.start()
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
            burn_subtitles=request.params.get("burnsubs") == "1",
        )
        # The stream indices only bind when MediaSourceId travels with them:
        # measured, a PlaybackInfo carrying AudioStreamIndex=3 and no source id
        # came back with the server's own default and no complaint. The first
        # call cannot name a source it has not seen yet, so a request that
        # selects streams states which source it is selecting them on.
        wanted_source = request.params.get("mediasourceid") or ""
        audio_index = _stream_index(request.params.get("audioindex"))
        subtitle_index = _stream_index(request.params.get("subtitleindex"))
        info = api.playback_info(
            item_id,
            profile,
            start_ticks=start_ticks,
            audio_index=audio_index,
            subtitle_index=subtitle_index,
            media_source_id=wanted_source or None,
        )
        sources = info.get("MediaSources") or []
        if not sources:
            raise JellyfinError("no media sources for %s" % item_id)
        source = pick_media_source(sources, wanted_source)
        play_session_id = info.get("PlaySessionId", "")
        url, method = stream_url(
            api.server, item, source, creds.device_id, play_session_id
        )
        is_audio = item.get("Type") in AUDIO_TYPES
        if method == "Transcode" and not is_audio:
            # The bitrate the user asked for is the budget, whether it came from
            # the context item or the setting: the server reserves its own audio
            # share off that cap, so the split is recomputed to keep audio
            # proportional to the budget rather than to the source. Nothing caps
            # this against the source bitrate — sizing a transcode down to the
            # source was only ever a way to provoke a re-encode, which
            # ``deny_video_stream_copy`` now states outright.
            #
            # Music is exempt: its transcode is already sized by
            # MusicStreamingTranscodingBitrate, and the video audio share would
            # otherwise overwrite that with an unrelated number.
            budget = int(profile["MaxStreamingBitrate"])
            if budget < deviceprofile.UNLIMITED_BITRATE:
                url = rewrite_bitrates(url, budget, config.audio_bitrate_kbps)
            if transcode or config.force_transcode:
                url = deny_video_stream_copy(url)
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
    # Free: measured identical time to first frame with 0, 2 and 20 subtitles
    # attached (4.0 s each), because Kodi fetches them only when one is
    # selected. On a transcode this is the *only* way any subtitle reaches the
    # screen — the transcoded stream carries none.
    #
    # Sidecar ones are fetched here rather than linked, because Kodi reads a
    # subtitle's language out of its filename and Jellyfin's route cannot carry
    # one (plugin/subtitles.py). That is the one place this route pays for
    # anything, which is why it is capped, short-timeout and falls back to the
    # URL.
    # Only the tracks that actually landed are attached, and only those count
    # for the ordinal mapping the stream menu reads (subtitles.localize).
    attached = streams.attached_subtitles(
        api.server, source, method, kodirpc.preferred_subtitle_language()
    )
    localized = subtitles.localize(http, attached) if attached else []
    if localized:
        li.setSubtitles([path for _attachment, path in localized])

    play_item = play_state(
        item,
        source,
        url,
        method,
        play_session_id,
        creds.device_id,
        start_ticks / 10_000_000,
        attached=[attachment.stream_index for attachment, _path in localized],
        request_params=request.params,
    )
    # Bounded: the interactive Api budget caps the fetch, so a hung join here
    # can only mean the bound itself failed — fall back rather than wait.
    if segments_thread is not None:
        segments_thread.join(timeout=15.0)
    segments = segments_box[0] if segments_box else None
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
