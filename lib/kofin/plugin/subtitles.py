"""Sidecar subtitles as local files, so Kodi's own menu can name them.

Kodi reads a subtitle's language and label out of its **filename**, and
Jellyfin's delivery route has a fixed one: every attached track arrives as
``Stream.<codec>``. Worse, that stem matches the video stream's own
(``/Videos/<id>/stream.mkv``), so Kodi strips it as a redundant prefix and is
left with nothing at all — measured on Omega 21.3, a real sidecar SRT listed as
``name: "(External)", language: ""``.

The filename cannot be fixed at the source. Both escapes were tried against
10.11.11 and refused: ``Stream.eng.srt`` is a 400 (the format is a route
constraint) and ``English.eng.srt`` a 404. So the file has to be local, and
this module is the only thing in kofin that fetches media content on the play
path — hence the care about cost.

**Sidecars only.** They are files the server found beside the media, and there
are rarely more than a handful. The other attached set — embedded text tracks
delivered as files on a transcode — can be fifty on one film, and paying for
fifty fetches before ``setResolvedUrl`` is exactly the startup delay the design
forbids (docs/transcode-stream-selection-plan.md §2.4 measured attaching URLs
at 0 s). Those keep their URL, and kofin's own stream dialog is what names them.

What Kodi does with a name was measured, not guessed (all on Omega 21.3, real
tracks added to a running playback):

| filename | Kodi's answer |
|---|---|
| ``English.eng.srt`` | ``name "English (External)", language "eng"`` |
| ``Commentary.eng.forced.srt`` | ``name "Commentary (External)"``, forced flag set |
| ``eng.srt`` | language ``eng``, **no name** |
| ``English SDH.eng.sdh.srt`` | ``sdh`` is not a flag — it leaks into the name |

So: a name, the language, and ``forced`` when it applies. Nothing else, because
anything Kodi does not parse ends up rendered.
"""

import os
from typing import List, Optional

import xbmc
import xbmcvfs

from kofin.core import streams
from kofin.core.http import Http
from kofin.core.log import Logger

LOG = Logger(__name__)

# Everything written here belongs to the playback being resolved right now, and
# is swept before the next one writes its own (see ``localize``).
CACHE_DIR = "special://temp/kofin/subtitles"

# A stop for a pathological item, not a tuning knob: a handful of sidecar files
# is the normal case, and each one is a round trip the first frame waits on.
MAX_FILES = 8

# Short, and no retries: a subtitle that does not arrive promptly is not worth
# delaying the picture for. The URL is still attached in its place.
TIMEOUT = (3.05, 8.0)

# Extensions Kodi recognises as subtitles. A URL already ending in one of these
# is left alone; anything else is asked for as .srt, which the server converts
# to (the extension in the delivery route selects the output format).
KNOWN_EXTENSIONS = frozenset(
    {"srt", "ass", "ssa", "sub", "smi", "vtt", "txt", "utf", "utf8", "idx", "aqt"}
)
DEFAULT_EXTENSION = "srt"

# Filesystem-hostile characters, plus the dot: Kodi tokenises the filename on
# separators, so a dot inside the name would read as another token.
_UNSAFE = str.maketrans({ch: "_" for ch in '/\\<>:"|?*.\x00'})


def _last_segment(url: str) -> str:
    """The filename part of a URL — no query, no directories.

    Everything here works on this rather than on the whole URL, because a dot
    is common in a host name and rare in a subtitle route: ``http://s.co/Stream``
    has no extension at all, and treating one as if it did rewrote the host.
    """
    return url.split("?", 1)[0].rsplit("/", 1)[-1]


def extension_of(url: str) -> str:
    """The subtitle extension a URL asks the server for, or ''."""
    name = _last_segment(url)
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def delivery_url(url: str) -> str:
    """The URL to fetch, with an extension Kodi has a parser for.

    Jellyfin names the file after the *codec*, so a SubRip track arrives as
    ``Stream.subrip`` — which is not in Kodi's subtitle extension list, and is
    not something its parser factory recognises either. Asking the same route
    for ``.srt`` returns the same cues (verified: 128,891 bytes as
    ``application/x-subrip``). A URL already asking for a format Kodi knows —
    ``.ass`` keeps its styling, ``.vtt`` its own parser — is left alone.
    """
    extension = extension_of(url)
    if extension in KNOWN_EXTENSIONS:
        return url
    path, sep, query = url.partition("?")
    name = _last_segment(url)
    stem = name.rsplit(".", 1)[0] if "." in name else name
    base = path[: len(path) - len(name)]
    return "%s%s.%s%s%s" % (base, stem, DEFAULT_EXTENSION, sep, query)


def display_name(attachment: streams.Attachment) -> str:
    """What Kodi should call the track, as the first part of the filename.

    The server's own ``Title`` when it has one — that is a label a person
    wrote, like "Signs & Songs" — and otherwise the language spelled out, which
    is what the viewer is choosing between. ``convertLanguage`` is Kodi's own
    table, so the wording matches the rest of its UI.
    """
    if attachment.title:
        return attachment.title
    if attachment.language:
        spelled = xbmc.convertLanguage(attachment.language, xbmc.ENGLISH_NAME)
        if spelled:
            return spelled
        return attachment.language
    return "Subtitle"


def filename_for(attachment: streams.Attachment) -> str:
    """``<name>.<language>[.forced].<ext>`` — the shape Kodi reads back."""
    parts = [display_name(attachment).translate(_UNSAFE).strip() or "Subtitle"]
    if attachment.language:
        parts.append(attachment.language.translate(_UNSAFE))
    if attachment.forced:
        parts.append("forced")
    parts.append(extension_of(delivery_url(attachment.url)) or DEFAULT_EXTENSION)
    return ".".join(parts)


def _cache_dir() -> str:
    return xbmcvfs.translatePath(CACHE_DIR)


def sweep(directory: Optional[str] = None) -> int:
    """Empty the cache. Returns how many files went.

    Called before each play writes its own, which bounds the directory at one
    playback's worth. Best effort throughout: a file the outgoing playback
    still holds open is a failed unlink on some platforms and a stale file
    here, neither of which is worth failing a playback over.
    """
    path = directory or _cache_dir()
    if not os.path.isdir(path):
        return 0
    removed = 0
    for name in os.listdir(path):
        try:
            os.remove(os.path.join(path, name))
            removed += 1
        except OSError as error:
            LOG.debug("could not sweep %s: %s", name, error)
    return removed


def localize(
    http: Http,
    attached: List[streams.Attachment],
    directory: Optional[str] = None,
) -> List[str]:
    """The paths to hand ``setSubtitles``, sidecars fetched to local files.

    The order is the order in, and that is load-bearing: it is what makes a
    Jellyfin index translatable to a Kodi subtitle number at all
    (``streams.subtitle_ordinal``). A fetch that fails contributes its URL, so
    the track is still there and still in position — worse-labelled, never
    missing.
    """
    if not attached:
        return []
    path = directory or _cache_dir()
    sweep(path)

    urls: List[str] = []
    fetched = 0
    for attachment in attached:
        if not attachment.sidecar or fetched >= MAX_FILES:
            urls.append(attachment.url)
            continue
        local = _fetch(http, attachment, path)
        if local:
            fetched += 1
            urls.append(local)
        else:
            urls.append(attachment.url)
    if fetched:
        LOG.info("fetched %d sidecar subtitle(s) for their labels", fetched)
    return urls


def _fetch(http: Http, attachment: streams.Attachment, directory: str) -> str:
    """One subtitle to a named local file, or '' if anything at all went wrong."""
    try:
        if not os.path.isdir(directory):
            os.makedirs(directory)
        response = http.request(
            "GET", delivery_url(attachment.url), timeout=TIMEOUT, retries=0
        )
        target = os.path.join(directory, filename_for(attachment))
        with open(target, "wb") as handle:
            handle.write(response.content)
        return target
    except Exception as error:
        # Never raises, and deliberately catches everything: a subtitle that
        # did not arrive costs its label and the URL goes on in its place. It
        # must not cost the playback.
        LOG.warning("subtitle %s not fetched (%s)", attachment.stream_index, error)
        return ""
