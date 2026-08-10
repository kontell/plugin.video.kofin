"""Jellyfin MediaStreams <-> Kodi stream ordinals, and the stream-menu model.

Jellyfin numbers every stream of a MediaSource in one sequence — video, audio,
subtitle, and any sidecar subtitle files the server found beside the media.
Kodi numbers each *kind* separately, and counts only what is in the stream it
actually opened. Translating between the two is this module's whole job, and
it is pure so the mapping can be tested without a player.

Three facts shape it, all measured (docs/transcode-stream-selection-plan.md §2):

* A Jellyfin transcode carries exactly one audio track. The chosen
  ``AudioStreamIndex`` is baked into the HLS URL and no alternate renditions
  are offered, so audio can only change by resolving a new stream.
* A transcode carries no subtitles either. Text subtitles reach Kodi as
  attached external URLs instead, which is why ``attached_subtitles`` exists
  and why the subtitle ordinal depends on the play method.
* Direct play is the opposite: the container already holds every embedded
  stream, in Jellyfin's own index order, so there the ordinal is just the
  position within its kind.
"""

from typing import Any, Dict, List, NamedTuple, Optional

JsonDict = Dict[str, Any]


class Attachment(NamedTuple):
    """One subtitle to hand ``setSubtitles``, and what it takes to name it.

    ``url`` is where the server serves it and is always a working answer.
    The rest is for :mod:`kofin.plugin.subtitles`, which gives the sidecar
    ones a local filename Kodi can read a language out of — Jellyfin's
    delivery route cannot carry one (``Stream.eng.srt`` is a 400).
    """

    stream_index: int
    url: str
    sidecar: bool
    language: str
    title: str
    forced: bool


# The play methods whose stream *is* the original file, so Kodi reads the
# embedded tracks straight out of it. Everything else is a transcode, which
# arrives as one audio track and no subtitles.
DIRECT_METHODS = frozenset({"DirectPlay", "DirectStream"})

# What the menu needs off a MediaStream. Deliberately small: this rides a
# window property between two processes, and a full MediaStreams array for an
# item with fifty subtitle tracks is a lot of JSON to move on every play.
_SUMMARY_FIELDS = (
    "Index",
    "Type",
    "Codec",
    "Language",
    "DisplayTitle",
    "IsDefault",
    "IsForced",
    "IsExternal",
    "IsTextSubtitleStream",
    # One short string per stream, and it is the only record of a burned-in
    # subtitle there is — see burned_subtitle().
    "DeliveryMethod",
)

# What the context item's <visible> tests to pick its label. Published as a
# window property because a boolean expression cannot read anything else.
OFFER_NONE = ""
OFFER_AUDIO = "audio"
OFFER_SUBTITLE = "subtitle"
OFFER_BOTH = "both"


def is_direct(play_method: str) -> bool:
    return play_method in DIRECT_METHODS


def summarize(source: JsonDict) -> List[JsonDict]:
    """The MediaStreams reduced to what a menu and the mapping need."""
    summary = []
    for stream in source.get("MediaStreams") or []:
        if stream.get("Type") not in ("Audio", "Subtitle"):
            continue
        summary.append({key: stream.get(key) for key in _SUMMARY_FIELDS})
    return summary


def of_type(streams: List[JsonDict], kind: str) -> List[JsonDict]:
    return [stream for stream in streams if stream.get("Type") == kind]


def preferred_embedded(
    media_streams: List[JsonDict], selected: Optional[int], language: str
) -> Optional[int]:
    """Which embedded text track to attach on a transcode.

    The server's own choice when it made one. When it made none — common, and
    what a film with twenty tracks and no default looks like — the viewer
    still expects the subtitle they always get, so: a forced track first
    (that is what forced means), then the first track in their configured
    subtitle language. Failing both, none: attaching an arbitrary one would
    be guessing, and the menu is right there.

    Text only, and not by omission — an image subtitle cannot be attached at
    all. Kodi will not render a standalone PGS/DVDSUB, and the server hands
    one over as a raw dump (measured at 37 MB and 67 s for a single track);
    those reach the screen only by being burned in.
    """
    text_tracks = [
        stream
        for stream in of_type(media_streams, "Subtitle")
        if not is_image_subtitle(stream) and not stream.get("IsExternal")
    ]
    if selected is not None and any(
        stream.get("Index") == selected for stream in text_tracks
    ):
        return selected
    for stream in text_tracks:
        if stream.get("IsForced"):
            index: Optional[int] = stream.get("Index")
            return index
    if language:
        for stream in text_tracks:
            if str(stream.get("Language") or "").lower() == language.lower():
                matched: Optional[int] = stream.get("Index")
                return matched
    return None


def attached_subtitles(
    server: str, source: JsonDict, play_method: str, language: str = ""
) -> List[Attachment]:
    """Every subtitle to hand ``setSubtitles``, in the order it will see them.

    Two disjoint groups, and which apply turns on the play method:

    * **Sidecar** subtitles (``IsExternal``) are files beside the media, in no
      container. They are attached whatever the play method — this is the set
      kofin has always attached. They cost the server nothing to serve: the
      file already exists.
    * **Embedded** text subtitles are missing from a transcoded stream, so one
      is attached for it — *the one this playback was resolved with*, and no
      others. Attaching them on direct play would list every track twice: once
      read out of the container, once fetched over HTTP.

    Only one, because each embedded track is extracted on demand by the
    server, and Kodi opens every attached subtitle when it builds the demuxer
    rather than when one is picked. A track the server cannot produce costs
    Kodi a 20-second timeout before it moves to the next, so a film with
    several of them stalls for a minute or more before the picture settles —
    measured on a real library, where one track ground for 48 s and then
    answered 400. The rest stay reachable through the stream menu, which
    restarts into them (``needs_restart``), the same way an image subtitle
    has always worked on a transcode.

    Which one is :func:`preferred_embedded`'s answer; ``language`` is the
    viewer's configured subtitle language, passed in rather than read here so
    this module stays free of Kodi imports and testable without a player.

    Embedded *image* subtitles (PGS/DVDSUB) are never attached. The server
    serves them as a raw ``.sup`` — measured at 37 MB and 67 s of extraction
    for one track — and Kodi cannot render a standalone one anyway. On a
    transcode they are reachable only by burning them in (see
    ``deviceprofile.build(burn_subtitles=True)``); on direct play they are
    already in the container.

    Order is the order Kodi will list them in, which is what makes
    ``subtitle_ordinal`` able to answer at all.
    """
    attached: List[Attachment] = []
    direct = is_direct(play_method)
    # The one embedded track worth extracting up front, if any.
    wanted = (
        None
        if direct
        else preferred_embedded(
            source.get("MediaStreams") or [],
            source.get("DefaultSubtitleStreamIndex"),
            language,
        )
    )
    for stream in source.get("MediaStreams") or []:
        if stream.get("Type") != "Subtitle":
            continue
        if stream.get("DeliveryMethod") != "External":
            continue
        url = stream.get("DeliveryUrl")
        index = stream.get("Index")
        if not url or index is None:
            continue
        sidecar = bool(stream.get("IsExternal"))
        if not sidecar and (direct or not stream.get("IsTextSubtitleStream")):
            continue
        if not sidecar and index != wanted:
            continue  # see the docstring: one extraction, not a queue of them
        attached.append(
            Attachment(
                stream_index=int(index),
                url=server + url,
                sidecar=sidecar,
                language=str(stream.get("Language") or ""),
                title=str(stream.get("Title") or ""),
                forced=bool(stream.get("IsForced")),
            )
        )
    return attached


def fetchable_subtitles(server: str, source: JsonDict) -> List[Attachment]:
    """Every embedded text track the server can hand over as a file.

    A superset of what :func:`attached_subtitles` attaches — that one picks the
    single track worth extracting before the first frame, this one describes
    all of them, so a track chosen later can be fetched without resolving a
    new stream. It is what makes the stream menu's text rows free: the service
    fetches the chosen index and adds it to the running playback rather than
    restarting into it (``service/latesubs.py``).

    Sidecars are absent because they are never missing: they cost the server
    nothing and are attached on every play. Image subtitles are absent because
    a file is no use for one — Kodi cannot render a standalone PGS/DVDSUB, and
    a transcode can only put it on screen by burning it in.
    """
    fetchable: List[Attachment] = []
    for stream in source.get("MediaStreams") or []:
        if stream.get("Type") != "Subtitle":
            continue
        if stream.get("DeliveryMethod") != "External":
            continue
        if stream.get("IsExternal") or not stream.get("IsTextSubtitleStream"):
            continue
        url = stream.get("DeliveryUrl")
        index = stream.get("Index")
        if not url or index is None:
            continue
        fetchable.append(
            Attachment(
                stream_index=int(index),
                url=server + url,
                sidecar=False,
                language=str(stream.get("Language") or ""),
                title=str(stream.get("Title") or ""),
                forced=bool(stream.get("IsForced")),
            )
        )
    return fetchable


def audio_ordinal(streams: List[JsonDict], index: Optional[int]) -> Optional[int]:
    """Kodi's audio-stream number for a Jellyfin index, or None.

    Only meaningful for a direct play: a transcode has a single audio track,
    which is Kodi 0 whatever index the server encoded into it.
    """
    if index is None:
        return None
    for ordinal, stream in enumerate(of_type(streams, "Audio")):
        if stream.get("Index") == index:
            return ordinal
    return None


def audio_index_at(streams: List[JsonDict], ordinal: Optional[int]) -> Optional[int]:
    """``audio_ordinal`` inverted: the Jellyfin index of Kodi's nth track.

    What the player answers has to be translatable back, because on a direct
    play Kodi's own audio menu can change the track without kofin hearing of
    it, and the published index is then the one the playback *started* on
    rather than the one being heard.
    """
    tracks = of_type(streams, "Audio")
    if ordinal is None or ordinal < 0 or ordinal >= len(tracks):
        return None
    return tracks[ordinal].get("Index")


def burned_subtitle(
    streams: List[JsonDict], selected: Optional[int], play_method: str
) -> bool:
    """Whether the subtitle this playback was resolved with is in the picture.

    A burned-in subtitle is not a track. It is pixels in the video, so no
    question put to the player reports it — ``currentsubtitle`` answers with
    whatever else is loaded, or nothing — and the only record of it is the
    server's own answer: on a transcode whose profile withdrew the image
    formats, the stream comes back ``DeliveryMethod: Encode``.

    ``selected`` is therefore load-bearing, not a convenience. Measured against
    10.11: a burn profile flips *every* image subtitle to ``Encode``, not only
    the one requested — twenty of them on one film — so the delivery method
    alone identifies a set of candidates, and only the index the playback was
    resolved with says which of them is the one on screen.
    """
    if selected is None or is_direct(play_method):
        return False
    for stream in of_type(streams, "Subtitle"):
        if stream.get("Index") == selected:
            return stream.get("DeliveryMethod") == "Encode"
    return False


def subtitle_ordinal(
    streams: List[JsonDict],
    index: Optional[int],
    attached: List[int],
    play_method: str,
) -> Optional[int]:
    """Kodi's subtitle number for a Jellyfin index, or None if unreachable.

    Kodi lists the ones ``setSubtitles`` attached *first*, in the order they
    were passed, then the ones it demuxed out of the stream, in their container
    order. The attached ones lead because Kodi registers a ListItem's subtitle
    files during ``OpenInputStream``, which runs before ``OpenDemuxStream``
    adds the container's own tracks. So on a transcode — where nothing is
    demuxed — the attached list *is* the whole list, and on direct play the
    embedded tracks start after it.

    The other order looks equally plausible and is wrong in a way nothing
    reports: every embedded track shifts by the sidecar count, so the viewer
    silently gets a neighbouring language.
    """
    if index is None:
        return None
    if index in attached:
        return attached.index(index)
    embedded = _embedded_subtitles(streams) if is_direct(play_method) else []
    for ordinal, stream in enumerate(embedded):
        if stream.get("Index") == index:
            return len(attached) + ordinal
    return None


def _embedded_subtitles(streams: List[JsonDict]) -> List[JsonDict]:
    return [
        stream
        for stream in of_type(streams, "Subtitle")
        if not stream.get("IsExternal")
    ]


def selectable_subtitles(
    streams: List[JsonDict], attached: List[int], play_method: str
) -> List[JsonDict]:
    """The subtitle streams the menu can actually put on screen.

    All of them, on either play method — what differs is the cost, which
    :func:`needs_fetch` and :func:`burns_in` answer and the menu labels. On a
    transcode only one embedded track is attached (see
    :func:`attached_subtitles`); another text track is fetched onto the
    running playback, and an image one can only be burned into a new stream.
    """
    return of_type(streams, "Subtitle")


def is_image_subtitle(stream: JsonDict) -> bool:
    """Whether a subtitle stream is pictures rather than text.

    Trusts ``IsTextSubtitleStream`` when the server sent it and falls back to
    the codec, because the flag is absent on some older payloads and getting
    this wrong means offering a track that cannot be shown.
    """
    text = stream.get("IsTextSubtitleStream")
    if text is not None:
        return not text
    codec = str(stream.get("Codec") or "").lower()
    return codec in ("pgssub", "pgs", "dvdsub", "dvbsub", "vobsub", "sub")


def burns_in(stream: JsonDict, play_method: str) -> bool:
    """Whether selecting this subtitle can only be answered by burning it in.

    An image subtitle on a transcode, and nothing else. The distinction is not
    cosmetic: a *text* subtitle that is not among the attached ones costs a
    restart too (:func:`needs_restart`), but that restart fetches it as a file
    — so treating the two alike labelled a plain SRT "burned in" and, worse,
    made the restart ask the server for a burn-in it never wanted
    (``deviceprofile.build(burn_subtitles=True)``, reached through the play
    route's ``burnsubs`` param).
    """
    return not is_direct(play_method) and is_image_subtitle(stream)


def needs_fetch(
    stream: JsonDict, play_method: str, attached: Optional[List[int]] = None
) -> bool:
    """Whether this subtitle has to be fetched from the server before it shows.

    A text track on a transcode that is not among the attached ones. The
    transcoded stream carries no subtitles of its own and the play route
    extracts only the one it resolved with (:func:`attached_subtitles`), so
    the rest arrive as files on demand — which the service does against the
    running playback, with no new stream and no gap.

    Never on a direct play: the container holds every track. Never for an
    image subtitle either: a file is no use for one, and
    :func:`burns_in` is that case.

    ``attached`` is optional so a caller that only cares about the burn-in
    case can leave it out; omitting it treats every text subtitle as already
    on hand.
    """
    if is_direct(play_method) or is_image_subtitle(stream) or attached is None:
        return False
    return stream.get("Index") not in attached


def menu_offer(streams: List[JsonDict], attached: List[int], play_method: str) -> str:
    """What the context item should offer — the token addon.xml tests.

    Audio counts only when there is something to switch *to*; a lone track is
    not a choice. Subtitles count whenever one can be shown, because "off" is
    always the other option.
    """
    audio = len(of_type(streams, "Audio")) > 1
    subtitle = bool(selectable_subtitles(streams, attached, play_method))
    if audio and subtitle:
        return OFFER_BOTH
    if audio:
        return OFFER_AUDIO
    if subtitle:
        return OFFER_SUBTITLE
    return OFFER_NONE


def label_for(stream: JsonDict, active: bool) -> str:
    """A menu row: the server's own wording, marked when it is the live one.

    ``DisplayTitle`` is what the Jellyfin UI shows and already folds in
    language, codec, channel layout and the default/forced flags, so there is
    nothing to rebuild here. Falls back through language to the codec for a
    stream the server described with none of it.
    """
    import xbmc

    title = str(stream.get("DisplayTitle") or "").strip()
    if not title:
        title = str(stream.get("Language") or stream.get("Codec") or "").strip()
    if not title:
        title = str(stream.get("Type") or "")
    if active:
        return "%s %s" % (title, xbmc.getLocalizedString(461))  # [active]
    return title
