"""Reads of Kodi's own library state over JSON-RPC.

Both processes need these. The play route has to start where Kodi is about to
seek, and the service has to confirm a resume bookmark really is gone before
acting on the announcement that says so.

Two of these write. ``drop_cached_texture`` because rewriting a file in place
leaves Kodi's texture cache serving the bytes it already cached, so the backdrop
swap has to invalidate the entry itself; and ``stop_player`` because
``xbmc.Player.stop()`` cannot be called from a kofin thread at all (issue #155).
"""

import json
from typing import Any, Dict, List, Optional

import xbmc

from kofin.core.log import Logger

LOG = Logger(__name__)

STOP_POLL_SECONDS = 0.05

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


def _player_properties(properties: List[str]) -> Optional[Dict[str, Any]]:
    """Properties of whichever player is active, or None when none is."""
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
                        "properties": properties,
                    },
                }
            )
        )
    )
    result: Dict[str, Any] = response["result"]
    return result


def current_subtitle() -> Optional[int]:
    """Kodi's number for the subtitle on screen, or None when none is.

    Read over JSON-RPC rather than through ``Player.getSubtitles()``, which
    answers with the track's *name*: every subtitle kofin attaches as a file is
    named "Stream (External)" — Jellyfin's delivery route has a fixed filename
    — so looking a name up in the available list finds whichever came first.
    Only the index distinguishes them.
    """
    try:
        result = _player_properties(["currentsubtitle", "subtitleenabled"])
        if result is None or not result.get("subtitleenabled"):
            return None
        index = result.get("currentsubtitle", {}).get("index")
        return int(index) if index is not None else None
    except Exception as error:
        LOG.debug("current subtitle read failed: %s", error)
        return None


def current_audio() -> Optional[int]:
    """Kodi's number for the audio track being heard, or None.

    Asked for the same reason as the subtitle above: on a direct play Kodi's
    own audio menu switches tracks without kofin hearing of it, so the index
    the playback was resolved with is not necessarily the one playing, and a
    menu that marks it as current would be marking the wrong row.
    """
    try:
        result = _player_properties(["currentaudiostream"])
        if result is None:
            return None
        index = (result.get("currentaudiostream") or {}).get("index")
        return int(index) if index is not None else None
    except Exception as error:
        LOG.debug("current audio read failed: %s", error)
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


def stop_player(wait_seconds: float = 0.0) -> bool:
    """Stop whatever is playing, without holding Python's GIL while Kodi does it.

    **Never call ``xbmc.Player.stop()``.** Kodi's binding for it is
    ``SendMsg(TMSG_MEDIA_STOP)`` with no ``DelayedCallGuard``, so the calling
    thread blocks on the app thread *holding the GIL* — unlike ``playnext``,
    ``playprevious`` and ``play``, which all wrap the same send in the guard.
    The app thread's stop path ends in ``~CVideoPlayer()``, which spins until
    its outbound job queue drains, and the two jobs ahead of ``OnPlayBackStopped``
    both write MyVideos. Kodi's SQLite busy handler sleeps and retries forever,
    so if any kofin thread is holding the MyVideos write lock it can never be
    released — that thread needs the GIL this one is sitting on. Kodi is then
    wedged on a blank screen with only a force-stop to get out (issue #155;
    measured on Omega 21.3 and Piers 22.0-beta, evidence under
    ``tests/live/results/issue-155``).

    ``executeJSONRPC`` carries the guard Kodi forgot on ``stop()``, and
    ``Player.Stop`` is a ``PostMsg`` on Kodi's side, so this returns in
    microseconds and no other Python thread ever stalls behind it.

    Being asynchronous is the one behaviour change: playback is *requested* to
    stop, not stopped. Anything Kodi sequences for us needs no wait — the app
    thread runs its messages in order, so a ``player.play()`` issued afterwards
    is handled after the stop. ``wait_seconds`` is for callers that must not
    race the teardown from the Python side. Note it can only ever be a
    courtesy: ``Player.GetActivePlayers`` was measured going empty while
    ``~CVideoPlayer()`` was still running, so an empty answer means "Kodi has
    let go of the player", not "the teardown has finished".

    Returns True when a stop was asked for.
    """
    try:
        listed: Dict[str, Any] = json.loads(
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
        active = listed["result"]
    except Exception as error:
        LOG.warning("could not read the active players to stop them: %s", error)
        return False

    if not active:
        return False

    # Every active player by id, rather than a hardcoded 1: SyncPlay drives
    # music as well as video, and Player.Stop answers FailedToExecute for a
    # playerid that is not playing.
    for player in active:
        try:
            xbmc.executeJSONRPC(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "Player.Stop",
                        "params": {"playerid": int(player["playerid"])},
                    }
                )
            )
        except Exception as error:
            LOG.warning("stop failed for player %r: %s", player, error)

    if wait_seconds > 0:
        monitor = xbmc.Monitor()
        waited = 0.0
        while waited < wait_seconds:
            if monitor.waitForAbort(STOP_POLL_SECONDS):
                break
            waited += STOP_POLL_SECONDS
            try:
                still: Dict[str, Any] = json.loads(
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
                if not still["result"]:
                    break
            except Exception as error:
                LOG.debug("player poll failed while waiting for the stop: %s", error)
                break

    return True


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


def preferred_subtitle_language() -> str:
    """Kodi's configured subtitle language as an ISO 639-2 code, or ''.

    The setting holds a display name ("English"), or one of Kodi's own words:
    ``original`` and ``default`` defer to the media or the UI language, and
    ``none``/``forced_only`` mean the viewer does not want a subtitle chosen
    for them — all of which answer '' here, since none of them names a track
    to prefer.
    """
    try:
        response: Dict[str, Any] = json.loads(
            xbmc.executeJSONRPC(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "Settings.GetSettingValue",
                        "params": {"setting": "locale.subtitlelanguage"},
                    }
                )
            )
        )
        value = str(response["result"]["value"])
    except Exception as error:
        LOG.debug("subtitle language unavailable: %s", error)
        return ""
    if value.lower() in ("", "none", "forced_only", "original"):
        return ""
    if value.lower() == "default":
        return str(xbmc.getLanguage(xbmc.ISO_639_2) or "")
    return str(xbmc.convertLanguage(value, xbmc.ISO_639_2) or "")
