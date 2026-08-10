"""Embedded subtitles that arrive after the picture does.

A transcoded stream carries no subtitles, so a text track reaches Kodi as a
file the play route fetches from Jellyfin's delivery URL. The first time
anyone asks for that track the server has to extract it with ffmpeg, and
measured against 10.11.11 on a real library that takes **28 s** for a 2.4 GB
MKV, **30 s** for a 2.6 GB one and **146 s** for a 22.7 GB one. The play route
has nothing like that to spend — every second of it is a second of black
screen — so it waits only long enough to catch an already-extracted track and
hands anything slower here (``plugin/subtitles.localize``).

This worker then does what the play route could not: waits. The extraction
the play route kicked off is still running, and finishing it caches the result
server-side (measured: ~25 ms on every later request), so this is a wait on
work already in flight rather than a second cold start. When the file lands,
``Player.setSubtitles`` puts it on the running playback — verified on Omega
21.3 against a live transcode: the track appends to Kodi's subtitle list, is
selected, and renders in sync with no gap in the video. Note which
``setSubtitles`` that is: the *Player* one takes a single path and adds it to
the playback in flight, while the ListItem method of the same name takes a
list and only applies before the stream is opened. There is no ``addSubtitle``
on Player, whatever ``Player.AddSubtitle`` over JSON-RPC suggests.

Why it could not simply be waited for in the play route, and why the menu's
restart was not the answer either: both were the bug. The 8 s the play route
used to spend missed *every* first play of an unextracted track, and the
stream menu's restart-into-it resolved a new stream that ran the same doomed
8 s fetch — so the viewer picked English, watched playback restart, and got
nothing, twice, until one of the abandoned extractions happened to finish.

Bounded on purpose, in two directions. Each attempt uses the play route's own
short read budget and the loop sleeps on the cancel event between them, so a
service teardown or a stopped playback is answered within one attempt rather
than parking Kodi on ``waiting on thread`` for the length of an extraction
(CLAUDE.md's sync-thread rule applies to any thread an addon starts, daemon or
not). And the whole chase gives up at ``DEADLINE_SECONDS``: a track that has
not appeared by then is one the server cannot produce, and the stream menu
still offers it.
"""

import threading
from typing import Any, Dict, List, Optional

from kofin.core import streams
from kofin.core.http import Http
from kofin.core.log import Logger
from kofin.plugin import subtitles

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

# How long to keep asking. Sized off the worst extraction measured (146 s for
# a 22.7 GB file) with room over it; past this the answer is that the server
# is not going to produce this track.
DEADLINE_SECONDS = 300.0

# Between attempts. The gap is what bounds how long a stop waits, together
# with the per-attempt read budget — see the module docstring.
POLL_SECONDS = 8.0


def fetchable_of(item: JsonDict) -> Dict[int, streams.Attachment]:
    """Every text track this playback can be handed as a file, by index.

    Only on a transcode, and not by omission. On a direct play the container
    already holds every embedded track, so there is nothing to fetch — and a
    late ``setSubtitles`` there would land *after* the demuxed tracks, which
    is the one arrangement ``streams.subtitle_ordinal`` cannot describe (it
    puts every attached track before every demuxed one, because Kodi registers
    a ListItem's subtitle files in ``OpenInputStream`` and the container's own
    only arrive with ``OpenDemuxStream``).
    """
    if streams.is_direct(str(item.get("PlayMethod") or "")):
        return {}
    payload = item.get("Streams") or {}
    rebuilt: Dict[int, streams.Attachment] = {}
    for raw in payload.get("Fetchable") or []:
        try:
            attachment = streams.Attachment(**raw)
        except TypeError:  # pragma: no cover - a payload from another version
            LOG.debug("undecodable fetchable subtitle: %s", raw)
            continue
        rebuilt[attachment.stream_index] = attachment
    return rebuilt


def deferred_of(item: JsonDict) -> List[streams.Attachment]:
    """The tracks to chase without being asked: the one this play resolved
    with and did not get inside the play route's budget."""
    fetchable = fetchable_of(item)
    payload = item.get("Streams") or {}
    wanted = [int(index) for index in payload.get("Deferred") or []]
    return [fetchable[index] for index in wanted if index in fetchable]


class LateSubtitles:
    """One playback's outstanding subtitle fetches.

    Built per claim by the Player, exactly as ``chapters.ChapterThumbs`` is:
    ``start()`` chases on a worker thread, ``stop()`` abandons the chase at
    the next step. Nothing here raises into a player callback.

    ``requested`` says where the chase came from, and it decides only one
    thing: what happens once the file lands. An automatic chase reproduces
    what the play route would have done — ``apply_default_tracks``, the single
    rule for whether a track is merely available or actually shown. A chase
    the viewer asked for through the stream menu shows the track they picked,
    because that rule would answer with the *resolved* index instead and
    switch them straight back off it.
    """

    def __init__(
        self,
        http: Http,
        player: Any,
        item: JsonDict,
        deferred: List[streams.Attachment],
        requested: bool = False,
    ) -> None:
        self._http = http
        self._player = player
        self._item = item
        self._deferred = deferred
        self._requested = requested
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="kofin-late-subtitles")
        self._thread.daemon = True
        self._thread.start()

    def stop(self) -> None:
        self._cancel.set()

    # -- the chase -------------------------------------------------------------

    def _run(self) -> None:
        try:
            for attachment in self._deferred:
                if self._cancel.is_set():
                    return
                self._chase(attachment)
        except Exception:  # pragma: no cover - defensive
            LOG.exception("late subtitle fetch failed")

    def _chase(self, attachment: streams.Attachment) -> None:
        waited = 0.0
        while not self._cancel.is_set() and waited < DEADLINE_SECONDS:
            path = subtitles.fetch_to(self._http, attachment)
            if path:
                self._attach(attachment, path)
                return
            if self._cancel.wait(POLL_SECONDS):
                return
            waited += POLL_SECONDS
        if not self._cancel.is_set():
            LOG.info(
                "subtitle %s gave up after %.0fs; the server never produced it",
                attachment.stream_index,
                DEADLINE_SECONDS,
            )

    def _attach(self, attachment: streams.Attachment, path: str) -> None:
        """Hand the file to the running playback, if it is still that one.

        The session check is what keeps a slow fetch from landing on whatever
        is playing by the time it finishes — the stream menu's restart makes
        exactly that race, replacing the playback while this thread waits.
        """
        if self._cancel.is_set():
            return
        live = self._player.current_item()
        if live is None or live.get("PlaySessionId") != self._item.get("PlaySessionId"):
            LOG.debug(
                "subtitle %s arrived after its playback ended", attachment.stream_index
            )
            return

        self._player.setSubtitles(path)
        # Kodi appends it to the subtitle list, and on a transcode that list is
        # exactly the attached one — nothing is demuxed — so recording it in
        # the same order keeps streams.subtitle_ordinal able to answer.
        payload = live.setdefault("Streams", {})
        attached = payload.setdefault("Attached", [])
        if attachment.stream_index not in attached:
            attached.append(attachment.stream_index)
        LOG.info(
            "subtitle %s attached late (kodi %s)",
            attachment.stream_index,
            len(attached) - 1,
        )
        # Republished before anything is selected, so the stream menu marks
        # and switches the right row against the list this just extended.
        self._player.republish_streams(live)
        if not self._requested:
            # Landed the way it would have landed on time.
            self._player.apply_default_tracks()
            return
        # The viewer picked this one, so it goes on screen whatever the
        # resolved index was. setSubtitles already selected it — Kodi appends
        # and selects in one step — but saying so is what makes the state
        # right when subtitles were switched off at the time of the pick.
        ordinal = len(payload.get("Attached") or []) - 1
        self._player.setSubtitleStream(ordinal)
        self._player.showSubtitles(True)
