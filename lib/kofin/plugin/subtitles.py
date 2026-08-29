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

**Everything attached**, which is now a short list: sidecars, plus at most the
one embedded track a transcode was resolved with (``streams.attached_subtitles``
stopped attaching the rest). The old rule — sidecars fetched, embedded ones left
as URLs — assumed Kodi fetched an attached URL only when the viewer picked it.
It does not: it opens every attached subtitle while building the demuxer, and an
embedded track is extracted on demand by the server, so a slow or failing one
cost a 20-second timeout before the picture appeared.

That is also why an embedded track that cannot be fetched here is never given
its URL to Kodi: the URL is precisely what stalls. A sidecar still falls back,
because the server serves a file it already has and Kodi opening it costs
nothing. An embedded one is *deferred* instead — handed back to the caller so
the service can chase it while the picture runs (``service/latesubs.py``).

The two are on different clocks, which is why they have different budgets.
A sidecar is a file on disk and answers at once. An embedded track is an
ffmpeg extraction the server runs on demand, and measured against 10.11.11 on
a real library that is not a few seconds: **28 s** for a 2.4 GB MKV, **30 s**
for a 2.6 GB one, **146 s** for a 22.7 GB one — after which the result is
cached and the same request answers in ~25 ms. So the wait here buys the warm
case only, and every second it spends on a cold one is a second of black
screen bought for nothing: the old single 8 s budget missed *every* first play
of an unextracted track and charged the viewer 8 s to do it. Hence a short
embedded budget, and the deferral for the rest.

Abandoning the request does not abandon the work — measured, the extraction
runs to completion server-side and the next request is served from the cache
— which is what makes chasing it worthwhile rather than a second cold start.

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

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, NamedTuple, Optional, Tuple

from kofin.core import streams
from kofin.core.http import Http
from kofin.core.log import Logger

# The naming and the fetch live in core (P2.6) so the service's late chase
# labels a track exactly as this route would have; re-exported here for
# the play path and the stream menu, which reach them through this module.
from kofin.core.subtitles import (  # noqa: F401 - re-exports
    CACHE_DIR,
    DEFAULT_EXTENSION,
    EMBEDDED_TIMEOUT,
    KNOWN_EXTENSIONS,
    TIMEOUT,
    _cache_dir,
    _fetch,
    delivery_url,
    display_name,
    extension_of,
    fetch_to,
    filename_for,
    sweep,
)

LOG = Logger(__name__)

# A stop for a pathological item, not a tuning knob: a handful of sidecar files
# is the normal case, and each one is a round trip the first frame waits on.
MAX_FILES = 8


class Localized(NamedTuple):
    """What the play route got, and what it is still owed.

    ``files`` is the (attachment, path) pairs to hand ``setSubtitles``, in the
    order Kodi will list them — the Jellyfin index of each, in this order, is
    what makes an index translatable to a Kodi subtitle number at all
    (``streams.subtitle_ordinal``).

    ``deferred`` is the embedded tracks the server had not finished extracting.
    They are not failures and not losses: the service fetches them on its own
    clock and adds them to the running playback (``service/latesubs.py``).
    """

    files: List[Tuple[streams.Attachment, str]]
    deferred: List[streams.Attachment]


def localize(
    http: Http,
    attached: List[streams.Attachment],
    directory: Optional[str] = None,
) -> Localized:
    """Fetch everything attachable now, and name what has to wait.

    Order is the order in. A sidecar whose fetch failed keeps its URL and its
    place — worse-labelled, never missing. An embedded track whose fetch did
    not land is deferred rather than attached, because its URL is the thing
    that stalls Kodi.

    The fetches run concurrently (perf plan W2.6): sequentially, each cost its
    whole round trip — ~0.4 s each on LAN. The cap bounds fetch *attempts*
    rather than successes: "stop after the eighth success" would mean
    submitting a ninth only after one fails, which re-serializes exactly the
    pathological item the cap exists for. Anything past the cap that is
    embedded is deferred too, for the same reason it would have been if slow.
    """
    if not attached:
        return Localized([], [])
    path = directory or _cache_dir()
    sweep(path)

    to_fetch = attached[:MAX_FILES]
    local_paths: Dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(to_fetch))) as pool:
        futures = {
            pool.submit(_fetch, http, attachment, path): attachment
            for attachment in to_fetch
        }
        for future in as_completed(futures):
            local_paths[futures[future].stream_index] = future.result()

    files: List[Tuple[streams.Attachment, str]] = []
    deferred: List[streams.Attachment] = []
    fetched = 0
    for attachment in attached:
        local = local_paths.get(attachment.stream_index, "")
        if local:
            fetched += 1
            files.append((attachment, local))
        elif attachment.sidecar:
            files.append((attachment, attachment.url))
        else:
            LOG.info(
                "subtitle %s deferred: the server is still extracting it",
                attachment.stream_index,
            )
            deferred.append(attachment)
    if fetched:
        LOG.info("fetched %d subtitle(s) for their labels", fetched)
    return Localized(files, deferred)
