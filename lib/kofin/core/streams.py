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


def attached_subtitles(
    server: str, source: JsonDict, play_method: str
) -> List[Attachment]:
    """Every subtitle to hand ``setSubtitles``, in the order it will see them.

    Two disjoint groups, and which apply turns on the play method:

    * **Sidecar** subtitles (``IsExternal``) are files beside the media, in no
      container. They are attached whatever the play method — this is the set
      kofin has always attached.
    * **Embedded** text subtitles are attached only for a transcode, because
      only then are they missing from the stream Kodi opened. Attaching them
      on direct play would list every track twice: once read out of the
      container, once fetched over HTTP.

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

    On direct play that is all of them. On a transcode it is the attached text
    ones plus the image ones, which are offered because burning them in is a
    real answer even though it costs a restart — but an embedded text subtitle
    that somehow failed to attach is dropped, since picking it would do
    nothing.
    """
    if is_direct(play_method):
        return of_type(streams, "Subtitle")
    return [
        stream
        for stream in of_type(streams, "Subtitle")
        if stream.get("Index") in attached or is_image_subtitle(stream)
    ]


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


def needs_restart(stream: JsonDict, play_method: str) -> bool:
    """Whether selecting this subtitle means resolving a new stream.

    Only one case does: an image subtitle on a transcode, which exists in the
    output solely by being burned into the video.
    """
    return not is_direct(play_method) and is_image_subtitle(stream)


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
