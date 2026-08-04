"""Reads of Kodi's own library state over JSON-RPC.

Both processes need these. The play route has to start where Kodi is about to
seek, and the service has to confirm a resume bookmark really is gone before
acting on the announcement that says so.

``drop_cached_texture`` is the one write: rewriting a file in place leaves
Kodi's texture cache serving the bytes it already cached, so the backdrop swap
has to invalidate the entry itself.
"""

import json
from typing import Any, Dict, Optional

import xbmc

from kofin.core.log import Logger

LOG = Logger(__name__)

# Kodi media type -> (JSON-RPC method, id parameter, result key). The three
# video types kofin syncs; songs carry no resume point.
RESUME_QUERY = {
    "movie": ("VideoLibrary.GetMovieDetails", "movieid", "moviedetails"),
    "episode": ("VideoLibrary.GetEpisodeDetails", "episodeid", "episodedetails"),
    "musicvideo": (
        "VideoLibrary.GetMusicVideoDetails",
        "musicvideoid",
        "musicvideodetails",
    ),
}


def current_subtitle() -> Optional[int]:
    """Kodi's number for the subtitle on screen, or None when none is.

    Read over JSON-RPC rather than through ``Player.getSubtitles()``, which
    answers with the track's *name*: every subtitle kofin attaches as a file is
    named "Stream (External)" — Jellyfin's delivery route has a fixed filename
    — so looking a name up in the available list finds whichever came first.
    Only the index distinguishes them.
    """
    try:
        players: Dict[str, Any] = json.loads(
            xbmc.executeJSONRPC(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "Player.GetActivePlayers",
                    }
                )
            )
        )
        active = players["result"]
        if not active:
            return None
        response: Dict[str, Any] = json.loads(
            xbmc.executeJSONRPC(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "Player.GetProperties",
                        "params": {
                            "playerid": active[0]["playerid"],
                            "properties": ["currentsubtitle", "subtitleenabled"],
                        },
                    }
                )
            )
        )
        result = response["result"]
        if not result.get("subtitleenabled"):
            return None
        index = result.get("currentsubtitle", {}).get("index")
        return int(index) if index is not None else None
    except Exception as error:
        LOG.debug("current subtitle read failed: %s", error)
        return None


def drop_cached_texture(needle: str, require: str = "") -> int:
    """Remove cached textures whose url contains ``needle`` (and ``require``,
    if given); returns how many went. Best effort — a failure only means a
    stale image.

    Kodi keys its texture cache on the source url, so overwriting a file the
    cache already holds changes nothing on screen: it re-reads only when its
    own hash check falls due, which is not on any timescale a user connects to
    the action that caused the change. Removing the row forces the next draw
    to re-cache from disk.

    Two-stage matching because the cache runs to thousands of rows on a real
    install, and Kodi's filter takes exactly one substring. ``needle`` narrows
    the query server-side; ``require`` then makes the match exact in Python.
    Both are needed: filtering on the bare filename is not ours to do — a live
    install had ``plugin.video.jellyfin`` holding its own ``fanart.png``, which
    a filename-only match would have evicted — and filtering on the addon id
    alone would take every image the addon ships. The url is percent-encoded
    by Kodi (``image://%2fhome%2f…%2fplugin.video.kofin%2f…``), so neither
    substring may span a path separator.
    """
    try:
        listed: Dict[str, Any] = json.loads(
            xbmc.executeJSONRPC(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "Textures.GetTextures",
                        "params": {
                            # Without an explicit properties list Kodi answers
                            # with bare texture ids, and ``require`` would have
                            # no url to test.
                            "properties": ["url"],
                            "filter": {
                                "field": "url",
                                "operator": "contains",
                                "value": needle,
                            },
                        },
                    }
                )
            )
        )
        textures = listed["result"].get("textures", [])
    except Exception as error:
        LOG.warning("texture lookup failed for %r: %s", needle, error)
        return 0

    removed = 0
    for texture in textures:
        if require and require not in str(texture.get("url", "")):
            continue
        try:
            xbmc.executeJSONRPC(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "Textures.RemoveTexture",
                        "params": {"textureid": int(texture["textureid"])},
                    }
                )
            )
            removed += 1
        except Exception as error:
            LOG.warning("texture removal failed for %r: %s", texture, error)
    LOG.debug("dropped %s cached texture(s) matching %r", removed, needle)
    return removed


def resume_seconds(kodi_id: int, media: str) -> Optional[float]:
    """Kodi's stored resume position for a library row.

    0.0 when the row has no bookmark — which is an answer, not a failure, and
    the callers rely on telling the two apart. None only when the row cannot
    be read at all.
    """
    query = RESUME_QUERY.get(media)
    if query is None:
        return None
    method, id_field, result_field = query
    try:
        response: Dict[str, Any] = json.loads(
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
        return float(response["result"][result_field]["resume"]["position"])
    except Exception as error:
        LOG.debug("resume read failed for %s/%s: %s", media, kodi_id, error)
        return None
