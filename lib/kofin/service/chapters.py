"""Native chapter thumbnails: seed Kodi's texture cache at playback start.

Kodi's bookmarks dialog lists a direct-played file's embedded chapters but
cannot extract their thumbnails from an internet stream (``CanExtract``
refuses http), so the tiles render blank. The cache, however, is consulted
before the extractor — so at playback start this worker downloads Jellyfin's
server-extracted chapter images and publishes them into ``Textures*.db`` +
``Thumbnails/`` under the exact keys the dialog is about to request. The
stock dialog then renders them on any skin. Mechanism and cache contract are
bench-verified on both supported Kodi generations
(docs/chapter-thumbnails-feasibility.md).

The keys embed the resolved stream URL, whose ``playSessionId`` is fresh per
play — hence seeding per playback (the claimed play item carries the exact
URL) and full revert in ``finalize``. Crash leftovers are self-identifying
(every key contains this install's deviceId) and swept at service start.

Coverage is honest: a transcode's HLS stream carries no chapters and a
chapterless file lists none, so both are skipped — there is nothing to
decorate. Jellyfin's server-side "dummy chapters" never reach the player and
are likewise out of scope.
"""

import os
import threading
from typing import Any, Dict, List, Optional, Tuple

import xbmcvfs

from kofin.core.api import Api
from kofin.core.imagecache import THUMBNAILS, store_image
from kofin.core.log import Logger
from kofin.sync import schema
from kofin.sync.db import Database
from kofin.sync.kodidb.texture import TextureCache, cached_rel_path, chapter_art_key

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

# Server-side resize for the downloads. The dialog renders small tiles;
# source-resolution chapter images would be megabytes each for no gain.
IMAGE_MAX_WIDTH = 640

# Play methods whose stream is the original container, chapters included.
_DIRECT_METHODS = ("DirectPlay", "DirectStream")

_VIDEO_TYPES = ("Movie", "Episode", "MusicVideo", "Video")


def eligible(item: JsonDict) -> bool:
    """Whether a claimed play item can carry native chapter thumbs at all."""
    return bool(
        item.get("Id")
        and item.get("Path")
        and item.get("PlayMethod") in _DIRECT_METHODS
        and item.get("Type") in _VIDEO_TYPES
    )


def sweep(device_id: str, thumbs_dir: Optional[str] = None) -> int:
    """Remove chapter-thumb leftovers from crashed playbacks; returns the
    row count removed. Every kofin chapter key contains this install's
    deviceId (raw in the Omega form, percent-encoded in the Piers form, and
    SQLite's ASCII LIKE is case-insensitive), so ours are the only possible
    matches. Callers skip the sweep while a kofin playback is live — its
    entries are in use."""
    directory = thumbs_dir or xbmcvfs.translatePath(THUMBNAILS)
    patterns = (
        "chapter://%%%s%%" % device_id,
        "image://video@%%%s%%" % device_id,
    )
    removed: List[Tuple[str, str]] = []
    try:
        with Database("texture") as tex_db:
            cache = TextureCache(tex_db.cursor)
            for pattern in patterns:
                removed.extend(cache.remove_like(pattern))
    except schema.SchemaError as error:
        LOG.debug("chapter thumb sweep skipped: %s", error)
        return 0
    for _url, cachedurl in removed:
        _remove_file(os.path.join(directory, cachedurl))
    if removed:
        LOG.info("swept %d stale chapter thumb(s)", len(removed))
    return len(removed)


class ChapterThumbs:
    """One playback's seeded chapter-thumb cache entries.

    Built per claim by the Player: ``start()`` downloads and publishes on a
    worker thread, ``stop()`` reverts everything that was published — safe to
    call repeatedly and while the seeder is still running (it aborts at the
    next step and the janitor joins it before deleting). Nothing here ever
    raises into a player callback."""

    def __init__(self, api: Api, item: JsonDict, thumbs_dir: Optional[str] = None):
        self._api = api
        self._item = item
        self._thumbs_dir = thumbs_dir or xbmcvfs.translatePath(THUMBNAILS)
        self._seeded: List[Tuple[str, str]] = []  # (cache key, rel path)
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="kofin-chapter-thumbs")
        self._thread.daemon = True
        self._thread.start()

    def stop(self) -> None:
        self._cancel.set()
        janitor = threading.Thread(target=self._cleanup, name="kofin-chapter-clean")
        janitor.daemon = True
        janitor.start()

    # -- seeding ---------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._seed()
        except schema.SchemaError as error:
            LOG.debug("chapter thumbs unavailable: %s", error)
        except Exception:
            LOG.exception("chapter thumb seeding failed")

    def _seed(self) -> None:
        item = self._item
        wrapped = schema.CHAPTER_ART_WRAPPED.get(schema.check("texture"))
        if wrapped is None:  # supported db without a key style: disabled
            return
        chapters = self._api.chapters(item["Id"])
        # The image index is the chapter's position in the *full* server list
        # (Jellyfin's ChapterIndex), and Kodi numbers the same list 1-based —
        # so enumerate the whole list and skip imageless entries in place.
        tagged = [
            (position, chapter.get("ImageTag"))
            for position, chapter in enumerate(chapters)
            if chapter.get("ImageTag")
        ]
        if not tagged:
            LOG.debug("no chapter images for %s", item["Id"])
            return

        published: List[Tuple[str, str, int, int]] = []
        for position, tag in tagged:
            if self._cancel.is_set():
                break
            url = self._api.chapter_image_url(
                item["Id"], position, str(tag), IMAGE_MAX_WIDTH
            )
            try:
                data = self._api.download(url)
            except Exception as error:
                LOG.debug("chapter %d image fetch failed: %s", position, error)
                continue
            if not data:
                continue
            key = chapter_art_key(item["Path"], position + 1, wrapped)
            rel = cached_rel_path(key)
            # Recorded before the file lands so a cancellation mid-write is
            # still cleaned up; the DB row (the publish step) comes last.
            with self._lock:
                self._seeded.append((key, rel))
            destination = os.path.join(self._thumbs_dir, rel)
            width, height = store_image(data, destination)
            published.append((key, rel, width, height))

        if not published or self._cancel.is_set():
            return
        with Database("texture") as tex_db:
            cache = TextureCache(tex_db.cursor)
            for key, rel, width, height in published:
                cache.add(key, rel, width, height)
        LOG.info(
            "--> chapter thumbs %s (%d of %d)",
            item["Id"],
            len(published),
            len(chapters),
        )

    # -- cleanup ---------------------------------------------------------------

    def _cleanup(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=30)
        with self._lock:
            seeded, self._seeded = list(self._seeded), []
        if not seeded:
            return
        try:
            with Database("texture") as tex_db:
                cache = TextureCache(tex_db.cursor)
                for key, _rel in seeded:
                    cache.remove(key)
        except schema.SchemaError:
            pass
        except Exception:
            LOG.exception("chapter thumb cleanup failed")
        for _key, rel in seeded:
            _remove_file(os.path.join(self._thumbs_dir, rel))
        LOG.debug("<-- chapter thumbs (%d removed)", len(seeded))


def _remove_file(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as error:  # pragma: no cover - defensive
        LOG.debug("could not remove %s: %s", path, error)
