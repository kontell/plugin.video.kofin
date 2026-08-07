"""Pre-seed Kodi's texture cache with the cast thumbnails the info dialog
is about to ask for.

The info dialog renders an actor tile by handing Kodi the art URL the sync
wrote into MyVideos; Kodi then fetches, decodes, rescales and caches it on
first sight. Measured on this repo's test box: ~110 ms per actor cold against
~1 ms warm, on a library holding 19,661 actor thumb URLs of which 102 were
cached — so every first open of a film's info dialog paid its whole cast, and
paid it again for the next film.

Nothing about the URLs is wrong (they are already capped at 400 px by
``sync/fields.get_people_artwork``); the cost is that the work happens while
someone is looking at an empty tile. So this module does that work earlier, on
an idle box, through the same contract ``service/chapters.py`` uses for
chapter thumbs: the file under ``Thumbnails/`` at Kodi's CRC-named path, plus
a ``texture`` row whose empty ``imagehash``/``lasthashcheck`` marks it trusted
and a ``sizes`` row with ``size=1``. The encoder was re-verified against real
rows Kodi itself wrote for actor art before this module existed
(``test_artcache.py`` carries those vectors).

Two things it deliberately does not do. It never rewrites a URL: a changed URL
shape would only take effect for items the sync happens to touch again, so the
cache would agree with the library for some rows and not others. And it never
reverts: unlike a chapter thumb, whose key embeds a per-play session id, an
actor tile stays valid for as long as the art URL does, and Kodi's own texture
cleanup owns the eviction. "Clean databases" removes them with the rest of the
cached server art (``sync/clean.purge_server_art``).
"""

import os
import struct
import threading
from typing import Any, Dict, List, Optional, Tuple

import xbmc
import xbmcvfs

from kofin.core import settings
from kofin.core.http import Http, JellyfinError
from kofin.core.log import Logger
from kofin.sync import schema
from kofin.sync.db import Database
from kofin.sync.kodidb.texture import TextureCache, cached_rel_path

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

THUMBNAILS = "special://thumbnails/"

# The setting that turns the idle trickle on. Off by default: it is disk the
# user has not agreed to spend (see the help string's estimate).
SETTING = "precacheActorArt"

# Seconds of Kodi idle before a batch runs. Long enough that it never competes
# with someone browsing, short enough to make progress during an evening.
IDLE_SECONDS = 60

# Images per wake. Bounded so a batch can always finish promptly when
# playback starts or the service shuts down, and so the texture database is
# only held for one batch's worth of inserts at a time.
BATCH = 25

# Seconds between wakes while there is work left.
TICK_SECONDS = 15

# Short and un-retried: a cast tile that does not arrive promptly is not worth
# holding an idle-time worker for, and the next batch will pick it up again.
TIMEOUT = (3.05, 10.0)


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_SOF_MARKERS = frozenset(
    (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)
)

# Actor thumbs only. Posters and fanart are already cached as a side effect of
# browsing the library — they are on screen the moment a list is drawn — while
# cast art is the one kind nothing renders until a dialog opens.
_PENDING_QUERY = """
SELECT      DISTINCT url
FROM        art
WHERE       media_type = 'actor' AND type = 'thumb'
            AND url LIKE 'http%'
"""


def image_size(data: bytes) -> Tuple[int, int]:
    """(width, height) for a JPEG or PNG; (0, 0) when unparseable.

    The dimensions are bookkeeping in the ``sizes`` row — Kodi's lookup keys
    on ``size=1`` — so an unreadable header degrades rather than skips.
    """
    if data[:8] == _PNG_MAGIC and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    index = 2
    while index < len(data) - 9:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in _SOF_MARKERS:
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return int(width), int(height)
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        (length,) = struct.unpack(">H", data[index + 2 : index + 4])
        index += 2 + length
    return 0, 0


def extension_for(data: bytes) -> str:
    """The cache file's extension. Kodi stores a PNG source as ``.png`` and
    everything else as ``.jpg``, and the extension is part of the cachedurl
    this module writes — so it has to match what the bytes actually are."""
    return ".png" if data[:8] == _PNG_MAGIC else ".jpg"


def pending_urls(limit: int) -> List[str]:
    """The next ``limit`` actor thumb URLs the texture cache has no row for.

    Both sides are read whole and differenced here rather than paged in SQL:
    they live in different database files, so there is no join to push down,
    and a LIMIT on the candidate side alone does not advance — the first page
    of art rows is the same page next time, so once it is cached the query
    answers "nothing pending" with the other 19,000 still missing. (Measured
    exactly that way on the first live run: 186 seeded, then a claim of
    completion.) A whole-column read of both tables is a few milliseconds
    against a batch of network fetches, and it makes progress monotonic.
    """
    with Database("video") as video_db:
        video_db.cursor.execute(_PENDING_QUERY)
        candidates = [row[0] for row in video_db.cursor.fetchall() if row[0]]
    if not candidates:
        return []

    with Database("texture") as texture_db:
        texture_db.cursor.execute("SELECT url FROM texture")
        cached = {row[0] for row in texture_db.cursor.fetchall()}

    pending: List[str] = []
    for url in candidates:
        if url in cached:
            continue
        pending.append(url)
        if len(pending) >= limit:
            break
    return pending


class ActorArtCache:
    """The idle-time seeder, and the settings button's worker.

    One instance per service generation; ``start`` and ``stop`` are the whole
    interface. Nothing here raises into its caller.
    """

    def __init__(self, thumbs_dir: Optional[str] = None) -> None:
        self._thumbs_dir = thumbs_dir or xbmcvfs.translatePath(THUMBNAILS)
        self._http = Http(settings.get_bool("sslVerify"))
        self._halt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # The idle trickle and the settings button are the same work through
        # two doors, and each computes its own list — run them at once (seen
        # live) and both fetch the same images. One instance, one lock, so
        # whichever starts second waits and then reads a list the first has
        # already shortened.
        self._working = threading.Lock()

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._halt.clear()
        self._thread = threading.Thread(target=self._run, name="kofin-artcache")
        self._thread.daemon = True
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Halt and join. Bounded: a batch is BATCH short fetches, and the
        worker checks the flag between every one."""
        self._halt.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():  # pragma: no cover - watchdog logging only
                LOG.warning("actor art seeder did not stop within its deadline")
        self._http.close()

    # -- the idle trickle --------------------------------------------------------

    def _run(self) -> None:
        monitor = xbmc.Monitor()
        # Nothing to do until the user asks for it; the flag is read per wake
        # so turning it on takes effect without a service restart.
        while not monitor.abortRequested() and not self._halt.is_set():
            if monitor.waitForAbort(TICK_SECONDS):
                return
            if self._halt.is_set() or not settings.get_bool(SETTING):
                continue
            if not self._idle():
                continue
            try:
                seeded = self.seed_batch()
            except schema.SchemaError as error:
                LOG.debug("actor art seeding unavailable: %s", error)
                return
            except Exception:
                LOG.exception("actor art seeding failed")
                return
            if seeded:
                LOG.debug("seeded %d actor thumb(s)", seeded)

    def _idle(self) -> bool:
        """Whether now is a fair time to spend disk and bandwidth.

        Never during playback — the seeder must not compete with a stream for
        the same server — and only once the box has been left alone.
        """
        if xbmc.Player().isPlaying():
            return False
        return xbmc.getGlobalIdleTime() >= IDLE_SECONDS

    # -- the work --------------------------------------------------------------

    def seed_batch(self, limit: int = BATCH) -> int:
        """Seed up to ``limit`` uncached actor thumbs; returns how many landed.

        The file is written before the row is inserted, and the rows for the
        whole batch go in one transaction at the end: a row is a promise that
        the file is there, so it must never be the earlier of the two.
        """
        with self._working:
            return self._seed_batch(limit)

    def _seed_batch(self, limit: int) -> int:
        urls = pending_urls(limit)
        if not urls:
            return 0

        published: List[Tuple[str, str, int, int]] = []
        for url in urls:
            if self._halt.is_set():
                break
            data = self._download(url)
            if not data:
                continue
            relative = cached_rel_path(url, extension_for(data))
            destination = os.path.join(self._thumbs_dir, relative)
            try:
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                with open(destination, "wb") as handle:
                    handle.write(data)
            except OSError as error:
                LOG.debug("could not write %s: %s", destination, error)
                continue
            width, height = image_size(data)
            published.append((url, relative, width, height))

        if not published:
            return 0
        with Database("texture") as texture_db:
            cache = TextureCache(texture_db.cursor)
            for url, relative, width, height in published:
                cache.add(url, relative, width, height)
        return len(published)

    def seed_all(self) -> int:
        """Seed everything outstanding, for the settings button.

        Runs to exhaustion rather than one batch — the user asked for it —
        and reports the total. Shares the instance lock with the trickle, so
        the two never fetch the same image twice, and honours the same halt
        flag, so a service shutdown ends it promptly.
        """
        total = 0
        while not self._halt.is_set():
            seeded = self.seed_batch()
            if not seeded:
                break
            total += seeded
        LOG.info("actor art pre-cache: %d image(s) seeded", total)
        return total

    def _download(self, url: str) -> bytes:
        """The image bytes, or b'' for anything that went wrong.

        Anonymous, like every other art fetch: these URLs carry their own
        image tag and Jellyfin serves them without a token.
        """
        try:
            response = self._http.request("GET", url, timeout=TIMEOUT, retries=0)
        except (JellyfinError, Exception) as error:
            LOG.debug("actor art fetch failed (%s): %s", url[:80], error)
            return b""
        content: bytes = response.content or b""
        return content
