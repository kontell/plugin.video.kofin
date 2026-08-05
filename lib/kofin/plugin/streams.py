"""Audio and subtitle selection for a playback already running.

Reached from the context menu on the played item, which the viewer gets back
to by pressing Back or Tab — fullscreen video itself has no context menu, but
leaving it does not stop playback, and the item stays focused with
``ListItem.IsPlaying`` true (docs/transcode-stream-selection-plan.md §2.7).

Two kinds of change, and the difference is the whole reason this module
exists. A subtitle Kodi already holds — anything embedded on a direct play,
anything attached as a file — switches in place, instantly. Audio on a
transcode cannot: Jellyfin bakes one audio track into the HLS output and
offers no alternates, so changing it means resolving a new stream and
restarting at the current position, which costs about five seconds. The same
goes for an image subtitle on a transcode, which can only be burned in.

No API call happens here. Everything the menus show was resolved by the play
route's own PlaybackInfo and published when the service claimed the playback,
so the menu is complete before the first frame and opening it costs nothing.
"""

from typing import Any, Dict, List, NamedTuple, Optional, Tuple, Union
from urllib.parse import parse_qsl, urlparse

import xbmc
import xbmcgui

from kofin.core import kodirpc, settings, state, streams, toast
from kofin.core.log import Logger
from kofin.plugin.listitems import plugin_url
from kofin.plugin.router import Request

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

# Kodi's own wording for the headings and for "no subtitle", so the menu reads
# like the player's rather than like an addon's.
STR_AUDIO_STREAM = 460
STR_SUBTITLES = 287
STR_NONE = 231


class Playing(NamedTuple):
    """What the picture is actually made of right now.

    Not what the playback was resolved with: Kodi's own menus move both tracks
    without kofin hearing of it, and a burned-in subtitle is not a track at all
    (see :func:`_playing`). Every menu marks its rows against this, and a
    restart reproduces it with one field replaced — which is what keeps a
    burned-in subtitle through an audio change, and the audio track through a
    burn-in.
    """

    audio: Optional[int]
    subtitle: Optional[int]
    burned: bool


def _playing(
    payload: JsonDict, media_streams: List[JsonDict], attached: List[int], method: str
) -> Playing:
    audio = payload.get("AudioStreamIndex")
    if streams.is_direct(method):
        # A transcode has one audio track by construction, so the resolved
        # index is the only answer there. Direct play is the opposite: Kodi
        # holds them all and its own menu can have moved.
        live = streams.audio_index_at(media_streams, kodirpc.current_audio())
        if live is not None:
            audio = live

    wanted = payload.get("SubtitleStreamIndex")
    if streams.burned_subtitle(media_streams, wanted, method):
        return Playing(audio, wanted, True)
    return Playing(
        audio, _current_subtitle_index(media_streams, attached, method), False
    )


def _row_of(index: Optional[int], rows: List[Optional[int]]) -> int:
    """Which row to put the cursor on, or -1 for none.

    Kodi's own stream dialogs open on the running track; marking it in the text
    and then opening on row 0 is half the job.
    """
    return rows.index(index) if index in rows else -1


def menu(request: Request) -> None:
    """``mode=streams``: the same menu, for anything driving it by URL."""
    context_menu()


def context_menu() -> None:
    """The context item: pick a stream for the running playback."""
    payload = state.playing_streams()
    if not payload:
        LOG.warning("stream menu invoked with no published playback")
        return

    media_streams: List[JsonDict] = payload.get("MediaStreams") or []
    attached: List[int] = payload.get("Attached") or []
    method = str(payload.get("PlayMethod") or "")

    audio = streams.of_type(media_streams, "Audio")
    subtitles = streams.selectable_subtitles(media_streams, attached, method)
    offer = streams.menu_offer(media_streams, attached, method)
    if offer == streams.OFFER_NONE:
        return

    # One kind on offer goes straight to its list — asking "audio or
    # subtitles?" when only one answer exists is a click for nothing. With both
    # on offer that question is worth asking, and it is the second popup the
    # single menu entry implies.
    if offer == streams.OFFER_BOTH:
        kind = xbmcgui.Dialog().contextmenu(
            [
                xbmc.getLocalizedString(STR_AUDIO_STREAM),
                xbmc.getLocalizedString(STR_SUBTITLES),
            ]
        )
        if kind < 0:
            return
        offer = streams.OFFER_AUDIO if kind == 0 else streams.OFFER_SUBTITLE

    playing = _playing(payload, media_streams, attached, method)

    if offer == streams.OFFER_AUDIO:
        _choose_audio(payload, audio, media_streams, method, playing)
    else:
        _choose_subtitle(payload, subtitles, media_streams, attached, method, playing)


# -- audio -------------------------------------------------------------------


def _choose_audio(
    payload: JsonDict,
    audio: List[JsonDict],
    media_streams: List[JsonDict],
    method: str,
    playing: Playing,
) -> None:
    # Annotated the way context.choose_bitrate is: Dialog.select takes
    # list[str | ListItem], and a bare list[str] is not that (list is invariant).
    labels: List[Union[str, xbmcgui.ListItem]] = [
        streams.label_for(stream, stream.get("Index") == playing.audio)
        for stream in audio
    ]
    index = xbmcgui.Dialog().select(
        xbmc.getLocalizedString(STR_AUDIO_STREAM),
        labels,
        preselect=_row_of(playing.audio, [stream.get("Index") for stream in audio]),
    )
    if index < 0:
        return
    chosen = audio[index]
    if chosen.get("Index") == playing.audio:
        return  # already playing; nothing worth a five-second restart

    if streams.is_direct(method):
        # The tracks are all in the stream Kodi opened, so this is a switch,
        # not a new playback. Restarting here would cost five seconds to
        # arrive at what setAudioStream does immediately.
        ordinal = streams.audio_ordinal(media_streams, chosen.get("Index"))
        if ordinal is None:
            return
        xbmc.Player().setAudioStream(ordinal)
        LOG.info("audio -> jellyfin %s (kodi %s)", chosen.get("Index"), ordinal)
        return

    _restart(payload, playing._replace(audio=chosen.get("Index")))


# -- subtitles ---------------------------------------------------------------


def _choose_subtitle(
    payload: JsonDict,
    subtitles: List[JsonDict],
    media_streams: List[JsonDict],
    attached: List[int],
    method: str,
    playing: Playing,
) -> None:
    if not subtitles:
        return
    rows: List[Tuple[Optional[JsonDict], str]] = [
        (
            None,
            streams.label_for(
                {"DisplayTitle": xbmc.getLocalizedString(STR_NONE)},
                playing.subtitle is None,
            ),
        )
    ]
    for stream in subtitles:
        active = stream.get("Index") == playing.subtitle
        label = streams.label_for(stream, active)
        if streams.needs_restart(stream, method) and not active:
            # Burn-in is not a like-for-like alternative to the others in this
            # list — it re-encodes the video and restarts playback — so the row
            # says so rather than surprising the viewer with a five-second gap.
            # The one already burned in has nothing left to warn about.
            label = "%s (%s)" % (label, settings.localized(30617))
        rows.append((stream, label))

    labels: List[Union[str, xbmcgui.ListItem]] = [label for _, label in rows]
    index = xbmcgui.Dialog().select(
        xbmc.getLocalizedString(STR_SUBTITLES),
        labels,
        preselect=_row_of(
            playing.subtitle,
            [None if stream is None else stream.get("Index") for stream, _ in rows],
        ),
    )
    if index < 0:
        return
    chosen = rows[index][0]

    if chosen is None:
        if playing.burned:
            # Nothing can turn this one off: it is in the picture, not in a
            # track. Only a stream without it is an answer.
            _restart(payload, playing._replace(subtitle=None, burned=False))
            return
        xbmc.Player().showSubtitles(False)
        LOG.info("subtitles off")
        return
    if chosen.get("Index") == playing.subtitle:
        return  # already on screen; never pay a restart to arrive where we are
    if streams.needs_restart(chosen, method):
        _restart(payload, playing._replace(subtitle=chosen.get("Index"), burned=True))
        return
    ordinal = streams.subtitle_ordinal(
        media_streams, chosen.get("Index"), attached, method
    )
    if ordinal is None:
        return
    player = xbmc.Player()
    player.setSubtitleStream(ordinal)
    player.showSubtitles(True)
    LOG.info("subtitle -> jellyfin %s (kodi %s)", chosen.get("Index"), ordinal)


def _current_subtitle_index(
    media_streams: List[JsonDict], attached: List[int], method: str
) -> Optional[int]:
    """The Jellyfin index of the subtitle Kodi is showing, or None.

    Asked of the player rather than remembered, because Kodi's own subtitle
    menu can change it behind this module's back — which on a transcode is now
    a normal thing to do, the attached tracks being switchable there too.
    """
    current = kodirpc.current_subtitle()
    if current is None:
        return None
    for stream in streams.of_type(media_streams, "Subtitle"):
        if (
            streams.subtitle_ordinal(
                media_streams, stream.get("Index"), attached, method
            )
            == current
        ):
            return stream.get("Index")
    return None


# -- restart -----------------------------------------------------------------


def _restart(payload: JsonDict, playing: Playing) -> None:
    """Resolve a new stream that is ``playing``, and pick up where we were.

    Every field is stated, not just the one the viewer changed. The server
    resolves what it is not told from the user's profile, so an audio switch
    that named only the audio track came back with the profile's subtitle —
    losing a burned-in one, or bringing back one that had been turned off — and
    a burn-in that named only the subtitle came back on the profile's audio.
    Restating all three makes a restart a change of one thing.

    The position is stated in the params rather than left to Kodi: PlayMedia's
    resume flag is gated on ``GetItemResumeInformation().isResumable``, which a
    bare plugin path never satisfies, so it downgrades itself to noresume (see
    CLAUDE.md). The transcode context item states its position for the same
    reason.

    The originating request's own params are carried forward, so a playback
    that was a forced transcode at 3 Mbit/s restarts as one — the settings
    alone would resolve it straight back to direct play.
    """
    item_id = str(payload.get("Id") or "")
    if not item_id:
        return
    position = _current_position()
    request = payload.get("Request") or {}

    params: Dict[str, str] = {"mode": "play", "id": item_id}
    for key in ("transcode", "bitrate", "dbid", "mediasourceid"):
        value = request.get(key)
        if value:
            params[key] = str(value)
    if not params.get("mediasourceid") and payload.get("MediaSourceId"):
        params["mediasourceid"] = str(payload["MediaSourceId"])
    if not params.get("dbid"):
        # Play Next builds its own URL and carries no dbid, so a restart of an
        # autoplayed episode would drop the library link. Kodi's own playing
        # path still has it.
        dbid = _playing_dbid()
        if dbid:
            params["dbid"] = dbid
    if playing.audio is not None:
        params["audioindex"] = str(playing.audio)
    # -1 is how Jellyfin is told "no subtitle", as distinct from omitting the
    # parameter and letting the user's profile choose one (plugin.play).
    params["subtitleindex"] = str(
        playing.subtitle if playing.subtitle is not None else -1
    )
    if playing.burned:
        params["burnsubs"] = "1"
    if position > 0:
        params["startticks"] = str(int(position * 10_000_000))
    else:
        params["fromstart"] = "1"

    LOG.info(
        "restarting %s at %.1fs (audio=%s subtitle=%s burn=%s)",
        item_id,
        position,
        playing.audio,
        playing.subtitle,
        playing.burned,
    )
    # Nothing here reports the stop: the restart replaces the playback, and the
    # player's own onPlayBackStopped finalize() posts the position and closes
    # the outgoing transcode session before the new one claims.
    toast.show(settings.localized(30616), time_ms=4000)
    xbmc.executebuiltin("PlayMedia(%s)" % plugin_url(params))


def _current_position() -> float:
    try:
        return max(float(xbmc.Player().getTime()), 0.0)
    except RuntimeError:  # playback ended between opening the menu and choosing
        return 0.0


def _playing_dbid() -> str:
    try:
        return dbid_from_path(xbmc.Player().getPlayingFile())
    except RuntimeError:
        return ""


def dbid_from_path(path: str) -> str:
    """The ``dbid`` a kofin play URL carries, or ''."""
    try:
        query = dict(parse_qsl(urlparse(path).query))
    except ValueError:
        return ""
    dbid = query.get("dbid", "")
    return dbid if dbid.isdigit() else ""
