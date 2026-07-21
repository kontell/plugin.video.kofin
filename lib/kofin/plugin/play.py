"""Playback resolve: PlaybackInfo -> stream URL -> setResolvedUrl.

No interactive dialogs in this path — the device profile decides everything.
The resolved play's state is queued on kofin.play.json for the service-side
player to claim and report.
"""

from typing import Any, Dict, List, Optional, Tuple

import xbmc
import xbmcgui
import xbmcplugin

from kofin.core import deviceprofile, settings, state
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


def stream_url(
    server: str,
    item: JsonDict,
    source: JsonDict,
    device_id: str,
    play_session_id: str,
) -> Tuple[str, str]:
    """(url, play method) for a MediaSource; raises on unplayable."""
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


def mime_for(container: str, play_method: str) -> str:
    if play_method == "Transcode":
        return HLS_MIME
    return MIME_BY_CONTAINER.get(container.lower(), "")


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
        start_ticks = 0
        if request.resume and not from_start:
            userdata = item.get("UserData") or {}
            start_ticks = int(userdata.get("PlaybackPositionTicks") or 0)
        # An explicit start position wins over resume/fromstart: SyncPlay
        # group starts say exactly where the group timeline is (plan §2).
        try:
            start_ticks = int(request.params.get("startticks") or start_ticks)
        except ValueError:
            pass

        profile = deviceprofile.build(
            deviceprofile.ProfileConfig.from_settings(),
            bitrate_override_mbps=bitrate_mbps,
            force_transcode=transcode,
        )
        info = api.playback_info(item_id, profile, start_ticks=start_ticks)
        sources = info.get("MediaSources") or []
        if not sources:
            raise JellyfinError("no media sources for %s" % item_id)
        source = sources[0]
        play_session_id = info.get("PlaySessionId", "")
        url, method = stream_url(
            api.server, item, source, creds.device_id, play_session_id
        )
    except JellyfinError as error:
        LOG.warning("play resolve failed for %s: %s", item_id, error)
        _fail(request)
        return

    LOG.info("play %s via %s", item_id, method)
    li = listitems.build(item, api.server)
    if from_start:
        # The ListItem carries the server resume point; clear it so PlayMedia
        # does not seek the fresh Play Next start back into the item.
        li.getVideoInfoTag().setResumePoint(0.0)
    # Library-item paths carry the Kodi database id (plan §2 path identity);
    # stamping it on the tag links the playback to the library row for
    # widgets invoked outside a library window.
    dbid = request.params.get("dbid", "")
    if dbid.isdigit() and item.get("Type") in ("Movie", "Episode", "MusicVideo"):
        li.getVideoInfoTag().setDbId(int(dbid))
    li.setPath(url)
    mime = mime_for((source.get("Container") or "").split(",")[0], method)
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
    xbmcgui.Dialog().notification(
        "Kofin", settings.localized(30018), xbmcgui.NOTIFICATION_ERROR, 4000, False
    )
