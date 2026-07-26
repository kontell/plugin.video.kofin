"""Kodi-side watched / resume changes pushed back to Jellyfin.

Kodi's own context items — "Mark as watched", "Mark as unwatched", "Reset
resume position" — write straight into MyVideos and announce
``VideoLibrary.OnUpdate``. Nothing in kofin listened, so those actions stayed
local: the server kept the old played flag and the old resume point, and the
next userdata sync wrote the server's version back over the user's change.

Two announcement shapes carry the two actions (confirmed live on Kodi 21)::

    play count   {"item": {"id": 5910, "type": "episode"}, "playcount": 1}
    resume reset {"id": 5910, "type": "episode"}

The flat one carries identity and nothing else — Kodi only ever emits it from
its resume-bookmark delete — so the position is read back out of Kodi rather
than inferred from the shape.

No loop back from the sync: its writes go into MyVideos through SQLite, which
bypasses Kodi's announcement system entirely.
"""

import json
import queue
import threading
from typing import Any, Dict, Optional, Tuple

import xbmc

from kofin.core.api import Api
from kofin.core.log import Logger

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

UPDATE_PLAYCOUNT = "playcount"
UPDATE_RESUME = "resume"

# Kodi media types that have both a kofin.db mapping and a Jellyfin user-data
# record. Music is absent: AudioLibrary.OnUpdate is a different announcement
# and Kodi has no watched/resume UI for songs.
WATCHED_MEDIA = ("movie", "episode", "musicvideo")

# media type -> (JSON-RPC method, id parameter, result key)
RESUME_QUERY = {
    "movie": ("VideoLibrary.GetMovieDetails", "movieid", "moviedetails"),
    "episode": ("VideoLibrary.GetEpisodeDetails", "episodeid", "episodedetails"),
    "musicvideo": (
        "VideoLibrary.GetMusicVideoDetails",
        "musicvideoid",
        "musicvideodetails",
    ),
}


def parse_update(data: JsonDict) -> Optional[Tuple[str, int, str, int]]:
    """``(kind, kodi id, media type, play count)`` for an OnUpdate payload.

    ``kind`` is :data:`UPDATE_PLAYCOUNT` (the nested shape, carrying a new
    play count) or :data:`UPDATE_RESUME` (the flat shape); the play count is 0
    for the latter. None for every announcement kofin does not act on — a
    library add, an unmapped media type, a malformed payload.
    """
    item = data.get("item")
    if isinstance(item, dict):
        kodi_id, media = item.get("id"), item.get("type")
        playcount = data.get("playcount")
        if not isinstance(playcount, int):
            return None  # a library add or metadata refresh, not a watch state
        kind = UPDATE_PLAYCOUNT
    else:
        kodi_id, media = data.get("id"), data.get("type")
        playcount, kind = 0, UPDATE_RESUME
    if not isinstance(kodi_id, int) or media not in WATCHED_MEDIA:
        return None
    return kind, kodi_id, str(media), playcount


def kodi_resume_seconds(kodi_id: int, media: str) -> Optional[float]:
    """Kodi's current resume position for a library row; None if unreadable."""
    method, id_field, result_field = RESUME_QUERY[media]
    try:
        response = json.loads(
            xbmc.executeJSONRPC(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": method,
                        "params": {id_field: kodi_id, "properties": ["resume"]},
                    }
                )
            )
        )
        details = response["result"][result_field]
        return float(details["resume"]["position"])
    except Exception as error:
        LOG.debug("resume read failed for %s/%s: %s", media, kodi_id, error)
        return None


class KodiUserData:
    """Applies Kodi-side watched/resume changes to the Jellyfin server.

    Work runs on one worker thread. The announcements arrive on Kodi's
    notification thread, which must not block on HTTP, and marking a whole
    season watched fires one announcement per episode.
    """

    def __init__(self, api: Api) -> None:
        self.api = api
        self._queue: "queue.Queue[Optional[Tuple[str, int, str, int]]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def submit(self, data: JsonDict) -> None:
        """Queue an OnUpdate payload; ignores what kofin does not act on."""
        parsed = parse_update(data)
        if parsed is None:
            return
        with self._lock:
            if self._worker is None:
                # Started on the first announcement worth acting on: most
                # sessions never see one, and a parked thread that never wakes
                # still has to be joined on every service restart.
                self._worker = threading.Thread(target=self._run, name="kofin-userdata")
                self._worker.daemon = True
                self._worker.start()
        self._queue.put(parsed)

    def stop(self) -> None:
        with self._lock:
            worker = self._worker
            self._worker = None
        if worker is None:
            return
        self._queue.put(None)
        worker.join(timeout=10)
        if worker.is_alive():  # pragma: no cover - watchdog only
            LOG.warning("kodi userdata worker did not stop within deadline")

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            try:
                self._apply(*job)
            except Exception:
                LOG.exception("kodi userdata push failed")

    def _apply(self, kind: str, kodi_id: int, media: str, playcount: int) -> None:
        from kofin.service.player import mapped_jellyfin_id

        jellyfin_id = mapped_jellyfin_id(kodi_id, media)
        if not jellyfin_id:
            return  # a library row kofin did not sync

        if kind == UPDATE_PLAYCOUNT:
            played = playcount > 0
            LOG.info("--> kodi %s %s played=%s", media, kodi_id, played)
            if played:
                self.api.mark_played(jellyfin_id)
            else:
                self.api.mark_unplayed(jellyfin_id)
            return

        # Flat shape: only Kodi's resume-bookmark delete emits it, but confirm
        # the bookmark really is gone before zeroing the server's position —
        # this is the one path that can discard a resume point the user never
        # asked to lose.
        resume = kodi_resume_seconds(kodi_id, media)
        if resume is None or resume > 0:
            return
        LOG.info("--> kodi %s %s resume reset", media, kodi_id)
        self.api.set_resume_position(jellyfin_id, 0)
