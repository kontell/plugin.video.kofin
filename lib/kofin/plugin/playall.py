"""Play all / Shuffle for a music container (mode=playall).

Kodi has no play for a plugin *folder* — ``VIDEO_UTILS::IsItemPlayable`` and
``MUSIC_UTILS::IsItemPlayable`` both stop at ``IsPlugin()`` for folders — so
an album, artist, genre or playlist row from a kofin listing had no way to be
played as a whole: the viewer opened it and started a track, which in the
video window plays that track alone. This route expands the container
server-side, in the order the container deserves, and hands Kodi a music
playlist of plugin items. Each entry still resolves through ``mode=play``, so
reporting, downloads-first and the stream menu apply per track exactly as
they do for a track Kodi queued itself (docs/dynamic-libraries-plan.md W4).

Music only, by decision: seasons and series keep Kodi's own behaviour.
"""

import random
from typing import Any, Dict, List, Optional

import xbmc

from kofin.core import settings, toast
from kofin.core.api import Api
from kofin.core.http import JellyfinError, plugin_transport
from kofin.core.log import Logger
from kofin.core.settings import Credentials
from kofin.plugin import listitems
from kofin.plugin.context import stop_current_playback
from kofin.plugin.router import Request

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

# What a queue may hold. Past this a "play all" is a library dump, and the
# plugin process builds a ListItem per row before anything plays.
QUEUE_LIMIT = 500

# The order each container deserves. An album by disc and track; an artist by
# year, then album, then disc and track; a genre by album artist first so the
# albums come out whole. A playlist keeps the server's order (below).
TRACK_ORDER = "ParentIndexNumber,IndexNumber,SortName"
SORT_BY = {
    "MusicAlbum": TRACK_ORDER,
    "MusicArtist": "ProductionYear,Album," + TRACK_ORDER,
    "MusicGenre": "AlbumArtist,Album," + TRACK_ORDER,
}

# What listitems._fill_music reads beyond the fields every DTO carries. The
# queue is built from these rows before the first note, so the payload is the
# wait.
TRACK_FIELDS = "Genres"

# Playlist pages: the server's own maximum is larger, but a page is also the
# unit a half-finished expansion can stop at.
PAGE_SIZE = 100


def _api() -> Optional[Api]:
    creds = Credentials.load()
    if not creds.is_logged_in:
        return None
    return Api.from_credentials(
        plugin_transport(settings.get_bool("sslVerify")), creds, interactive=True
    )


def expand(api: Api, item: JsonDict, shuffle: bool) -> List[JsonDict]:
    """The tracks under a container, in playing order; [] for anything else."""
    item_type = item.get("Type", "")
    item_id = item.get("Id", "")
    if item_type == "Playlist":
        return _playlist_tracks(api, item_id, shuffle)
    if item_type not in SORT_BY:
        LOG.warning("play all: %s is a %s, which has no tracks", item_id, item_type)
        return []

    query: JsonDict = {
        "IncludeItemTypes": "Audio",
        "Recursive": True,
        "Fields": TRACK_FIELDS,
        "ImageTypeLimit": 1,
        "Limit": QUEUE_LIMIT,
        "EnableTotalRecordCount": True,
        "SortBy": "Random" if shuffle else SORT_BY[item_type],
    }
    if item_type == "MusicArtist":
        # ArtistIds, not ParentId: an artist is a link target, not a folder,
        # and albums an artist merely appears on still count (the download
        # expansion says the same).
        query["ArtistIds"] = item_id
    elif item_type == "MusicGenre":
        query["GenreIds"] = item_id
    else:
        query["ParentId"] = item_id

    body = api.items(query)
    rows = list(body.get("Items") or [])
    total = int(body.get("TotalRecordCount") or 0)
    if total > len(rows):
        LOG.info(
            "play all: %s holds %d tracks; queuing the first %d",
            item_id,
            total,
            len(rows),
        )
    return rows


def _playlist_tracks(api: Api, playlist_id: str, shuffle: bool) -> List[JsonDict]:
    """A playlist's audio entries in the server's order, shuffled here if
    asked: the playlist route has no SortBy, and the order is the playlist."""
    tracks: List[JsonDict] = []
    start = 0
    while len(tracks) < QUEUE_LIMIT:
        body = api.playlist_items(
            playlist_id,
            start_index=start,
            limit=min(PAGE_SIZE, QUEUE_LIMIT - len(tracks)),
            fields=TRACK_FIELDS,
        )
        rows = body.get("Items") or []
        if not rows:
            break
        tracks.extend(row for row in rows if row.get("Type") == "Audio")
        start += len(rows)
        if start >= int(body.get("TotalRecordCount") or 0):
            break
    if shuffle:
        random.shuffle(tracks)
    return tracks


def queue_and_play(tracks: List[JsonDict], server: str) -> None:
    """Hand Kodi a music playlist of the tracks and start it.

    An explicit *music* playlist: the queue Kodi builds for a kofin listing
    itself lands on the video playlist, because ``<provides>video audio
    </provides>`` makes ``CGUIViewStateFromItems`` pick video last. Harmless
    there, wrong here. Playback is stopped first for the same reason the
    transcode item does it — a new plugin play handed to Kodi while
    something else is still playing loses a race with the outgoing stop
    (context.stop_current_playback).
    """
    playlist = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
    playlist.clear()
    for track in tracks:
        playlist.add(listitems.path_for(track), listitems.build(track, server))
    stop_current_playback()
    xbmc.Player().play(playlist)


def play_all(request: Request) -> None:
    item_id = request.params.get("id", "")
    shuffle = request.params.get("shuffle") == "1"
    if not item_id:
        return
    api = _api()
    if api is None:
        return
    try:
        item = api.item(item_id)
        tracks = expand(api, item, shuffle)
    except JellyfinError as error:
        LOG.warning("play all failed for %s: %s", item_id, error)
        toast.show(settings.localized(30018), toast.ERROR, time_ms=4000)
        return
    if not tracks:
        LOG.info("play all: nothing playable under %s", item_id)
        return
    LOG.info(
        "play all: %d tracks under %s (%s)",
        len(tracks),
        item_id,
        "shuffled" if shuffle else "in order",
    )
    queue_and_play(tracks, api.server)
