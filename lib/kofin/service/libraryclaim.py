"""Claiming library-originated playback (shell refactor P2.3).

Kodi starts a synced row on its own — a song written as a direct stream
URL, a downloaded movie or episode whose row is a local file — and nothing
queues a play state for it. ``backfill_library_claim`` builds one from the
``Player.OnPlay`` announcement: map the Kodi id back to a Jellyfin id, fetch
(or, offline, reconstruct) the item, and push the claim so the player
reports it like any other. Moved verbatim out of ``service/player.py``,
where it was self-free module code beside the player class.
"""

import json
import os
import re
from typing import Any, Dict, Optional, Tuple
from uuid import uuid4

import xbmc
import xbmcgui

from kofin.core import settings, state
from kofin.core.api import Api
from kofin.core.log import Logger
from kofin.core.segments import parse_segments

LOG = Logger(__name__)

JsonDict = Dict[str, Any]


# Kodi media types whose rows may point somewhere playback never reaches the
# play route from. Songs are written with a direct stream URL depending on
# ``musicTranscode``; movies and episodes joined with the downloads repoint
# (W1.7) — a downloaded item's row is a *local file*, so playing it from the
# library claims nothing, which left downloaded plays invisible as sessions
# (no dashboard, no progress reporting, no auto-next surface — found by the
# G13 gate). Both kinds check the play queue before claiming (see below), so
# a plugin:// play that claims the normal way is never double-claimed.
BACKFILL_MEDIA_TYPES = ("song", "movie", "episode")


# A song played from a saved playlist is a bare ``musicdb://songs/<id><ext>``
# line: nothing ever loads its music tag, so Kodi announces the playback
# without a database id. Measured on Kodi 21 — the same song started from the
# library announces ``{"id": 7005, "type": "song"}``, started from a playlist
# ``{"title": "04. Golden Earring - Radar Love", "type": "song"}``. The id is
# still in the path, which is the only thing that identifies the row.
_MUSICDB_SONG = re.compile(r"^musicdb://songs/(\d+)")


def downloaded_path(path: str) -> bool:
    """A local file under the downloads root — a repointed row's target,
    the one kind of video playback the back-fill claims for."""
    if not path or "://" in path:
        return False
    try:
        from kofin.downloads import downloads_root

        root = os.path.abspath(downloads_root())
    except Exception:  # pragma: no cover - settings unavailable
        return False
    return os.path.abspath(path).startswith(root + os.sep)


def musicdb_song_id(path: str) -> Optional[int]:
    """The Kodi song id in a ``musicdb://songs/<id><ext>`` path, or None."""
    match = _MUSICDB_SONG.match(path or "")
    return int(match.group(1)) if match else None


def mapped_jellyfin_id(kodi_id: int, media: str) -> Optional[str]:
    """The Jellyfin id kofin synced a Kodi library row from, or None if the row
    is not ours (or the mapping database cannot be read)."""
    from kofin.sync import db as sync_db
    from kofin.sync import kofindb

    try:
        with sync_db.Database("kofin") as opened:
            jellyfin_id = kofindb.JellyfinDatabase(opened.cursor).get_item_by_kodi_id(
                kodi_id, media
            )
    except Exception:
        LOG.exception("library claim lookup failed for %s/%s", media, kodi_id)
        return None

    return str(jellyfin_id) if jellyfin_id else None


# A song's Jellyfin id as it appears in whichever path Kodi is playing:
# ``<server>/Audio/<id>/stream.<ext>`` for direct rows, ``…?id=<id>`` for the
# plugin:// rows musicTranscode writes. Only used when the item carries no
# Kodi database id (playback started from kofin's own browse listing rather
# than the synced library).
_ID_IN_PATH = re.compile(r"/Audio/([0-9a-f]{32})/|[?&]id=([0-9a-f]{32})\b")


def playing_jellyfin_id(item: xbmcgui.ListItem, path: str) -> Optional[str]:
    """The Jellyfin id of the song Kodi is playing, or None if it is not ours.

    Prefers the Kodi database id, which is authoritative and cannot collide
    with foreign playback; falls back to reading the id out of the path for
    songs played from kofin's browse listing, which never get a library row.

    A playlist line carries its database id in the path rather than the tag
    (see :func:`musicdb_song_id`), so that is tried as the library id before
    the path is read for a Jellyfin one.
    """
    try:
        kodi_id = item.getMusicInfoTag().getDbId()
    except Exception:  # pragma: no cover - defensive, tag may be absent
        kodi_id = 0

    if not (kodi_id and kodi_id > 0):
        kodi_id = musicdb_song_id(path) or 0

    if kodi_id and kodi_id > 0:
        mapped = mapped_jellyfin_id(kodi_id, "song")
        if mapped:
            return mapped

    match = _ID_IN_PATH.search(path or "")
    if match:
        return match.group(1) or match.group(2)
    return None


def library_claim(jellyfin_id: str, path: str, api: Api) -> Optional[JsonDict]:
    """The play-state a library-originated playback would have queued.

    Unless ``musicTranscode`` is on, songs are written into Kodi as
    ``<server>/Audio/<id>/stream.<ext>``, so playing one from the music library
    never invokes ``mode=play`` and nothing
    claims it — the player sees a file it did not queue and reports nothing,
    which is why music never appeared on the dashboard and server playcounts
    never advanced. The fork solves it the same way (``objects/actions.py``
    ``on_play``): map the Kodi id back to a Jellyfin id, fetch the item, and
    register it so the normal reporting path takes over.

    Returns None when the server cannot be reached — genuinely foreign
    playback must stay unclaimed.
    """
    try:
        item = api.item(jellyfin_id)
    except Exception as error:
        LOG.debug("library claim fetch failed for %s: %s", jellyfin_id, error)
        return None

    sources = item.get("MediaSources") or [{}]

    return {
        "Id": item.get("Id", jellyfin_id),
        "Type": item.get("Type", ""),
        "Name": item.get("Name", ""),
        "SeriesId": item.get("SeriesId", ""),
        "Path": path,
        # Direct stream: Kodi pulls the server's URL itself, untranscoded.
        "PlayMethod": "DirectStream",
        "PlaySessionId": uuid4().hex,
        "MediaSourceId": sources[0].get("Id") or item.get("Id", jellyfin_id),
        "DeviceId": settings.get_str("deviceId"),
        "Runtime": int(item.get("RunTimeTicks") or 0),
        "AudioStreamIndex": None,
        "SubtitleStreamIndex": None,
        "CurrentPosition": 0.0,
    }


def _offline_claim(jellyfin_id: str, media: str, path: str) -> Optional[JsonDict]:
    """A claim built from local state alone (W4.7): the server is away, but
    a *downloaded* item's playback still deserves what the claim carries —
    the segment engine reading the download-time cache, position tracking,
    the watched-to-end offers. Reporting is separately gated offline, so
    the claim costs no doomed posts. None for anything not downloaded:
    genuinely foreign playback must stay unclaimed."""
    from kofin.downloads import store as downloads_store

    row = downloads_store.get(jellyfin_id)
    if row is None or row.state != downloads_store.DONE:
        return None
    kind = {"movie": "Movie", "episode": "Episode", "song": "Audio"}.get(media, "")
    name, runtime_ticks = _local_item_facts(jellyfin_id, media)
    return {
        "Id": jellyfin_id,
        "Type": kind,
        "Name": name,
        "SeriesId": row.series_id,
        "Path": path,
        "PlayMethod": "DirectPlay",
        "PlaySessionId": uuid4().hex,
        "MediaSourceId": jellyfin_id,
        "DeviceId": settings.get_str("deviceId"),
        "Runtime": runtime_ticks,
        "AudioStreamIndex": None,
        "SubtitleStreamIndex": None,
        "CurrentPosition": 0.0,
    }


def _local_item_facts(jellyfin_id: str, media: str) -> "Tuple[str, int]":
    """(name, runtime ticks) from Kodi's own rows via the mapping — the
    dialogs name the item and ``watched_to_end`` needs a runtime, and both
    must work with the server unreachable."""
    from kofin.downloads import repoint as downloads_repoint
    from kofin.sync.db import Database

    table = {"movie": "movie", "episode": "episode"}.get(media)
    if table is None:
        return "", 0
    id_column = "idMovie" if table == "movie" else "idEpisode"
    try:
        with Database("kofin") as kofin_db, Database("video") as video:
            mapping = downloads_repoint.mapping_for_on(kofin_db.cursor, jellyfin_id)
            if mapping is None:
                return "", 0
            video.cursor.execute(
                "SELECT c00 FROM %s WHERE %s = ?" % (table, id_column),
                (mapping.kodi_id,),
            )
            row = video.cursor.fetchone()
            name = str(row[0]) if row is not None and row[0] else ""
            video.cursor.execute(
                "SELECT iVideoDuration FROM streamdetails "
                "WHERE idFile = ? AND iStreamType = 0",
                (mapping.kodi_fileid,),
            )
            duration = video.cursor.fetchone()
            seconds = int(duration[0]) if duration is not None and duration[0] else 0
        return name, seconds * 10_000_000
    except Exception:  # pragma: no cover - a torn database must not stop play
        LOG.exception("local item facts unavailable for %s", jellyfin_id)
        return "", 0


def _attach_cached_segments(claim: JsonDict) -> None:
    """The download-time segment cache onto a claim (W4.7): the engine is
    armed before the first frame with no server fetch — offline's only
    source, and online it saves the checker's fallback round trip."""
    if claim.get("Type") not in ("Movie", "Episode"):
        return
    from kofin.downloads import store as downloads_store

    row = downloads_store.get(str(claim.get("Id") or ""))
    if row is None or row.state != downloads_store.DONE or not row.segments_json:
        return
    try:
        claim["Segments"] = parse_segments(json.loads(row.segments_json))
    except (ValueError, TypeError):
        LOG.debug("cached segments unreadable for %s", claim.get("Id"))


def backfill_library_claim(data: JsonDict, api: Api) -> bool:
    """Queue a claim for library playback that bypassed the play route.

    Driven by the ``Player.OnPlay`` notification; True when a claim was
    pushed. Only the media types in ``BACKFILL_MEDIA_TYPES`` qualify, and
    the queue/playing-id guard below is what keeps a ``plugin://`` play —
    which claims the normal way — from being double-claimed.

    The announcement is not required to carry the database id: playback
    started from a saved playlist never has one, and the id has to come out of
    the path instead (see :func:`musicdb_song_id`). Without that, a whole
    playlist plays unclaimed and unreported — the server's play counts stand
    still while Kodi's own keep advancing, and the next userdata sync writes
    the server's stale number back over them.
    """
    item = data.get("item") or {}
    media = item.get("type") or ""
    kodi_id = item.get("id")

    if media not in BACKFILL_MEDIA_TYPES:
        return False

    try:
        path = xbmc.Player().getPlayingFile()
    except RuntimeError:
        return False

    if not path:
        return False

    if not isinstance(kodi_id, int):
        kodi_id = musicdb_song_id(path)
    if kodi_id is None:
        return False

    jellyfin_id = mapped_jellyfin_id(kodi_id, media)
    if not jellyfin_id:
        return False

    # With ``musicTranscode`` on, songs are plugin:// rows that claim
    # themselves through the play route, and a second claim here would be left
    # in the queue for the next playback to adopt via claim_play_item's
    # oldest-entry fallback. Both orderings have to be caught: this
    # notification can land before onPlayBackStarted claims (the entry is
    # still queued) or after it (the entry is gone, but the player has
    # published what it is playing). Testing the play state rather than the
    # setting also keeps the window between flipping it and repairing the
    # library reported, where the rows are still direct URLs.
    if state.play_item_queued(path) or state.get_playing_id() == jellyfin_id:
        return False

    if state.is_offline():
        # Straight to the local claim: the server fetch below would ride
        # the transport ladder for ~30 s against a stated outage, landing
        # the claim long after the player's own claim window closed — the
        # engine never armed, measured live (W4.7).
        claim = _offline_claim(jellyfin_id, media, path)
    else:
        claim = library_claim(jellyfin_id, path, api)
        if claim is None:
            # The fetch failed some other way: a *downloaded* item still
            # claims from local state (W4.7).
            claim = _offline_claim(jellyfin_id, media, path)
    if claim is None:
        return False
    LOG.info("--> library claim %s (%s)", claim["Id"], media)
    _attach_cached_segments(claim)
    state.push_play_item(claim)
    return True
