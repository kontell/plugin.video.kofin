"""Chapter images for the native chapters dialog, through the companion
(docs/companion-addon-plan.md, section 6).

Kodi's chapters dialog sets every tile's art to a key built from the
resolved stream URL and resolves it through the texture database only, and
its own extractor refuses an http source, so the server's chapter images
reach the dialog only as texture rows under those keys. The rows are the
companion's to write; this module does the server side at the play claim:
it downloads the images the server has for the chapters the player lists
into a staging directory in kofin's profile, publishes a ``chapter_art``
request naming the exact dynpath and the files, confirms the seeding
through ``Textures.GetTextures``, and withdraws the request when the play
ends. The companion computes the keys, copies the files into the cache and
writes the rows; absent, nothing is downloaded and the gap is logged once
per generation.
"""

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import xbmc

from kofin.core import kodirpc, state
from kofin.core.api import Api
from kofin.core.imagecache import image_size
from kofin.core.log import Logger
from kofin.sync import private
from . import companion

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

STAGE = os.path.join("companion", "chapters")
# Server-side resize for the downloads: the dialog renders small tiles.
IMAGE_MAX_WIDTH = 640
# How long a seeding or a removal is waited for before the log says so.
ACK_TIMEOUT = 12.0
ACK_POLL = 0.5

_DIRECT_METHODS = ("DirectPlay", "DirectStream")
_VIDEO_TYPES = ("Movie", "Episode", "MusicVideo", "Video")


def eligible(item: JsonDict) -> bool:
    """A direct play of a video: a transcode's HLS stream carries no
    chapters, and a downloaded file is Kodi's own to extract from."""
    return bool(
        item.get("Id")
        and item.get("Path")
        and item.get("PlayMethod") in _DIRECT_METHODS
        and item.get("Type") in _VIDEO_TYPES
        and str(item.get("Path")).startswith(("http://", "https://"))
    )


def stage_dir() -> str:
    return os.path.join(private.addon_data_path(), STAGE)


def request_id(item_id: str, path: str, claim: str = "") -> str:
    """The id of one claim's request, which is also its staging directory.

    ``claim`` is a token of the claim's own. Without it a replay of the
    same item had the id, and so the directory, of the play before it,
    whose janitor thread was still on its way to remove both."""
    key = json.dumps(
        [companion.CHAPTER_ART, item_id, path, claim], separators=(",", ":")
    )
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def clear_at_start() -> bool:
    """Withdraw what a crash left and remove its staged images, at a
    generation start where nothing of kofin's is playing.

    Not while something is: service generations overlap (Kodi starts the
    replacement while the old one is still in its run loop) and
    ``kofin.playing.id`` is shared between them, so a start with it set may
    be under a playback the other generation claimed. Its request and its
    staged tiles are in use, nothing would restage them, and that
    generation's own stop withdraws them. A request a crash left then
    waits for the next quiet start, as the SQL build's sweep does
    (``Service._start_chapter_sweep``). Returns whether it cleared."""
    if state.get_playing_id():
        LOG.debug("chapter art clear deferred: playback live")
        return False
    try:
        companion.publish_live(companion.CHAPTER_ART, [])
    except Exception:
        LOG.exception("chapter art requests not cleared")
    shutil.rmtree(stage_dir(), ignore_errors=True)
    return True


def seeded_count(item_id: str) -> Optional[int]:
    """How many chapter keys of this item Kodi's texture cache holds, or
    None when the read failed."""
    try:
        reply = kodirpc.call(
            "Textures.GetTextures",
            {
                "properties": ["url"],
                "filter": {
                    "and": [
                        {"field": "url", "operator": "contains", "value": item_id},
                        {"field": "url", "operator": "contains", "value": "chapter="},
                    ]
                },
            },
        )
    except Exception:
        return None
    textures = reply.get("textures") if isinstance(reply, dict) else None
    return len(textures) if isinstance(textures, list) else None


def player_chapter_count() -> int:
    try:
        return int(xbmc.getInfoLabel("Player.ChapterCount") or 0)
    except (TypeError, ValueError):
        return 0


class ChapterArt:
    """One playback's chapter art request: ``start()`` downloads and
    publishes on a worker thread, ``stop()`` withdraws and cleans up on a
    janitor thread. Safe to call repeatedly; nothing here raises into a
    player callback."""

    def __init__(self, api: Api, item: JsonDict) -> None:
        self._api = api
        self._item = item
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # This claim's request and staging directory, and nobody else's:
        # stop() returns before the janitor has run, and the next claim is
        # constructed straight after it.
        self._id = request_id(item["Id"], item["Path"], uuid.uuid4().hex)
        self._directory = os.path.join(stage_dir(), self._id)
        self._published = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="kofin-chapter-art")
        self._thread.daemon = True
        self._thread.start()

    def stop(self) -> None:
        self._cancel.set()
        janitor = threading.Thread(target=self._cleanup, name="kofin-chapter-clean")
        janitor.daemon = True
        janitor.start()

    # -- the request ----------------------------------------------------------

    def _run(self) -> None:
        try:
            self._publish()
        except Exception:
            LOG.exception("chapter art request failed")

    def _publish(self) -> None:
        item = self._item
        if companion.present() is None:
            LOG.info(
                "no %s: the server's chapter images stay off the chapters dialog",
                companion.ADDON_ID,
            )
            return
        began = time.monotonic()
        chapters = self._api.chapters(item["Id"])
        count = player_chapter_count()
        tagged = [
            (position, chapter.get("ImageTag"))
            for position, chapter in enumerate(chapters)
            if chapter.get("ImageTag") and (not count or position < count)
        ]
        if not tagged:
            LOG.debug("no chapter images for %s", item["Id"])
            return
        entries = self._stage(tagged)
        if not entries or self._cancel.is_set():
            shutil.rmtree(self._directory, ignore_errors=True)
            return
        request = {
            "id": self._id,
            "kind": companion.CHAPTER_ART,
            "library": "",
            "item": item["Id"],
            "dynpath": item["Path"],
            "session": str(item.get("PlaySessionId") or ""),
            "chapters": entries,
        }
        companion.put_live(request)
        self._published = True
        seeded = self._await(len(entries))
        if seeded == len(entries):
            LOG.info(
                "--> chapter art %s: %d of %d seeded by the companion in %.1f s",
                item["Id"],
                seeded,
                len(chapters),
                time.monotonic() - began,
            )
        else:
            LOG.info(
                "--> chapter art %s: %s of %d seeded after %.0f s",
                item["Id"],
                "none" if not seeded else seeded,
                len(entries),
                ACK_TIMEOUT,
            )

    def _stage(self, tagged: List[Tuple[int, Any]]) -> List[JsonDict]:
        os.makedirs(self._directory, exist_ok=True)
        entries: List[JsonDict] = []
        for position, tag in tagged:
            if self._cancel.is_set():
                break
            url = self._api.chapter_image_url(
                self._item["Id"], position, str(tag), IMAGE_MAX_WIDTH
            )
            try:
                data = self._api.download(url)
            except Exception as error:
                LOG.debug("chapter %d image fetch failed: %s", position, error)
                continue
            if not data:
                continue
            target = os.path.join(self._directory, "%d.jpg" % (position + 1))
            with open(target, "wb") as handle:
                handle.write(data)
            width, height = image_size(data)
            entries.append(
                {"n": position + 1, "file": target, "width": width, "height": height}
            )
        return entries

    def _await(self, wanted: int) -> int:
        """Poll the texture cache until it holds ``wanted`` keys of the item
        (or none, when ``wanted`` is 0) or the timeout passes."""
        deadline = time.monotonic() + ACK_TIMEOUT
        seen = 0
        while True:
            count = seeded_count(self._item["Id"])
            if count is not None:
                seen = count
                if seen == wanted:
                    return seen
            if time.monotonic() >= deadline or self._cancel.is_set() and wanted:
                return seen
            time.sleep(ACK_POLL)

    # -- the withdrawal -------------------------------------------------------

    def _cleanup(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=30)
        if self._published:
            try:
                # By id: a newer claim's request, this item's or another's,
                # is not this claim's to withdraw.
                companion.drop_live(self._id)
                left = self._await(0)
                LOG.debug(
                    "<-- chapter art %s withdrawn (%d row(s) left)",
                    self._item["Id"],
                    left,
                )
            except Exception:
                LOG.exception("chapter art request not withdrawn")
        shutil.rmtree(self._directory, ignore_errors=True)
