"""Playback resolve: PlaybackInfo -> stream URL -> setResolvedUrl.

The device profile decides direct play vs transcode. Optional pre-play
audio/subtitle dialogs (Transcoding → Ask for tracks) may run in this
plugin process before setResolvedUrl when the play will Transcode.
The resolved play's state is queued on kofin.play.json for the service-side
player to claim and report.
"""

from typing import Any, Dict, List, Optional, Tuple

import xbmc
import xbmcgui
import xbmcplugin

from kofin.core import (
    deviceprofile,
    kodirpc,
    playback,
    settings,
    state,
    streammaps,
    subtitles,
    toast,
)
from kofin.core.api import Api
from kofin.core.http import Http, JellyfinError
from kofin.core.log import Logger
from kofin.core.playback import (  # noqa: F401 — re-export for tests / callers
    ASSUMED_SOURCE_BITRATE,
    AUDIO_TYPES,
    HLS_MIME,
    MIME_BY_CONTAINER,
    mime_for,
    pick_media_source,
    rewrite_bitrates,
    stream_url,
    transcode_budget,
)
from kofin.core.settings import Credentials
from kofin.plugin import listitems
from kofin.plugin.router import Request

LOG = Logger(__name__)

JsonDict = Dict[str, Any]


def external_subtitles(server: str, source: JsonDict) -> List[str]:
    """Absolute DeliveryUrls for eligible text external subs.

    Kept as a pure URL listing for tests and callers that only need the
    DeliveryUrl set. Playback uses :func:`attach_text_subtitles`, which
    materialises labelled local files when possible.
    """
    return subtitles.external_subtitle_urls(server, source)


def _download_subtitle(api: Api, url: str) -> Optional[bytes]:
    try:
        return api.get_bytes(
            url,
            timeout=subtitles.SUB_DOWNLOAD_TIMEOUT,
            max_bytes=subtitles.MAX_SUB_BYTES,
            retries=1,
        )
    except Exception as error:
        LOG.debug("subtitle download failed for %s: %s", url, error)
        return None


def attach_text_subtitles(
    api: Api, source: JsonDict, play_session_id: str
) -> Tuple[List[str], JsonDict]:
    """Materialise text external subs for ``setSubtitles`` + play-state fields.

    Returns ``(paths_for_listitem, play_state_subtitle_fields)``. When the
    Playback setting is off or nothing is eligible, both are empty.
    """
    # settings.xml default is true. Empty store / unset → enabled, so unit
    # tests and first-run installs match that default (getSettingBool("") is
    # false and would otherwise disable attach silently).
    if settings.get_str("enableExternalSubs") == "false":
        return [], {}
    paths, order, local_paths = subtitles.materialize_text_subs(
        api.server,
        source,
        play_session_id,
        lambda url: _download_subtitle(api, url),
    )
    if not paths:
        # Visible when debug is off: empty attach is the #1 "subs missing" cause.
        n_sub = sum(
            1
            for stream in (source.get("MediaStreams") or [])
            if stream.get("Type") == "Subtitle"
        )
        LOG.info(
            "no external text subs attached for session %s (%d subtitle stream(s))",
            play_session_id,
            n_sub,
        )
        return [], {}
    LOG.info(
        "attached %d external text sub(s) for session %s",
        len(paths),
        play_session_id,
    )
    # SubsPaths prefers local files for PR2 basename reconcile; URL-only
    # fallbacks still appear in the listitem attach list.
    return paths, subtitles.play_state_subtitle_fields(order, local_paths or paths)


def play_state(
    item: JsonDict,
    source: JsonDict,
    url: str,
    play_method: str,
    play_session_id: str,
    device_id: str,
    start_seconds: float,
    subtitle_fields: Optional[JsonDict] = None,
    force_transcode: bool = False,
    bitrate_override_mbps: float = 0.0,
) -> JsonDict:
    payload: JsonDict = {
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
        # PR3b: mid-play restart must rebuild the same force/bitrate policy.
        "ForceTranscode": bool(force_transcode),
        "BitrateOverrideMbps": float(bitrate_override_mbps or 0.0),
    }
    # Audio/embedded maps + external attach fields for PR2 progress mapping.
    payload.update(streammaps.play_state_stream_fields(source, subtitle_fields))
    return payload


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
    audio_index: Optional[int] = None
    subtitle_index: Optional[int] = None
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

        # PR4: optional pre-play audio/sub pick when the server will Transcode.
        # Second PlaybackInfo carries the chosen indexes (planning phase).
        audio_index = source.get("DefaultAudioStreamIndex")
        subtitle_index = source.get("DefaultSubtitleStreamIndex")
        if method == "Transcode" and not is_audio:
            chosen = _preplay_stream_choice(request, item, source, config)
            if chosen is None:
                # User cancelled a dialog.
                _fail(request)
                return
            audio_index, subtitle_index = chosen
            if (
                audio_index != source.get("DefaultAudioStreamIndex")
                or subtitle_index != source.get("DefaultSubtitleStreamIndex")
                or subtitle_index == playback.SUBTITLE_OFF_INDEX
            ):
                info = api.playback_info(
                    item_id,
                    profile,
                    start_ticks=start_ticks,
                    audio_index=audio_index,
                    subtitle_index=subtitle_index,
                    media_source_id=source.get("Id"),
                )
                sources = info.get("MediaSources") or []
                if not sources:
                    raise JellyfinError("no media sources after stream select")
                source = pick_media_source(sources, source.get("Id"))
                play_session_id = info.get("PlaySessionId", "")
                url, method = stream_url(
                    api.server, item, source, creds.device_id, play_session_id
                )

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
    sub_paths, sub_fields = attach_text_subtitles(api, source, play_session_id)
    if sub_paths:
        li.setSubtitles(sub_paths)

    play_item = play_state(
        item,
        source,
        url,
        method,
        play_session_id,
        creds.device_id,
        start_ticks / 10_000_000,
        subtitle_fields=sub_fields or None,
        force_transcode=transcode or config.force_transcode,
        bitrate_override_mbps=bitrate_mbps,
    )
    # Prefer indexes chosen in pre-play / second PlaybackInfo over source defaults.
    if audio_index is not None:
        play_item["AudioStreamIndex"] = audio_index
    if subtitle_index == playback.SUBTITLE_OFF_INDEX:
        play_item["SubtitleStreamIndex"] = None
    elif subtitle_index is not None:
        play_item["SubtitleStreamIndex"] = subtitle_index
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


def _preplay_stream_choice(
    request: Request,
    item: JsonDict,
    source: JsonDict,
    config: deviceprofile.ProfileConfig,
) -> Optional[Tuple[Optional[int], Optional[int]]]:
    """Optional Dialog.select for Transcode audio/subs. None = cancelled.

    Returns ``(audio_index, subtitle_index)``. ``subtitle_index`` may be
    ``SUBTITLE_OFF_INDEX`` (-1) when the user picks No subtitles.
    """
    select_mode = settings.get_int("transcodeStreamSelect")
    # Empty store → 0 (Never), matching settings default.
    ask_audio, ask_subs = playback.needs_preplay_stream_dialog(
        play_method="Transcode",
        item_type=str(item.get("Type") or ""),
        select_mode=select_mode,
        source=source,
        allow_burned=config.allow_burned_subs,
        suppress=playback.suppress_stream_dialogs(request.params),
    )
    audio_index: Optional[int] = source.get("DefaultAudioStreamIndex")
    subtitle_index: Optional[int] = source.get("DefaultSubtitleStreamIndex")
    if not ask_audio and not ask_subs:
        return audio_index, subtitle_index

    if ask_audio:
        streams = playback.eligible_audio_streams(source)
        labels = [playback.format_stream_label(s) for s in streams]
        default = 0
        for i, stream in enumerate(streams):
            if stream.get("Index") == audio_index:
                default = i
                break
        choice = xbmcgui.Dialog().select(
            settings.localized(30137),
            labels,  # type: ignore[arg-type]
            preselect=default,
        )
        if choice < 0:
            return None
        audio_index = int(streams[choice].get("Index") or 0)

    if ask_subs:
        streams = playback.eligible_dialog_subs(
            source, allow_burned=config.allow_burned_subs
        )
        # Leading "No subtitles" → SUBTITLE_OFF_INDEX on PlaybackInfo #2.
        labels = [settings.localized(30139)] + [
            playback.format_stream_label(s) for s in streams
        ]
        default = 0  # No subtitles preselected when server default is none
        if subtitle_index is not None and subtitle_index >= 0:
            for i, stream in enumerate(streams):
                if stream.get("Index") == subtitle_index:
                    default = i + 1
                    break
        choice = xbmcgui.Dialog().select(
            settings.localized(30138),
            labels,  # type: ignore[arg-type]
            preselect=default,
        )
        if choice < 0:
            return None
        if choice == 0:
            subtitle_index = playback.SUBTITLE_OFF_INDEX
        else:
            subtitle_index = int(streams[choice - 1].get("Index") or 0)

    return audio_index, subtitle_index
