"""Reads of Kodi's own library state over JSON-RPC.

Both processes need these. The play route has to start where Kodi is about to
seek, and the service has to confirm a resume bookmark really is gone before
acting on the announcement that says so.

Three of these write. ``drop_cached_texture`` because rewriting a file in place
leaves Kodi's texture cache serving the bytes it already cached, so the backdrop
swap has to invalidate the entry itself; ``stop_player`` because
``xbmc.Player.stop()`` cannot be called from a kofin thread at all (issue #155);
and ``clear_resume_bookmark`` because the bookmark Kodi keeps for a plugin path
is reachable through no Python binding.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

import xbmc

from kofin.core.log import Logger

LOG = Logger(__name__)

STOP_POLL_SECONDS = 0.05


class _Failed:
    """The type of ``FAILED``; nothing else is ever this."""

    def __repr__(self) -> str:  # pragma: no cover - repr only
        return "<kodirpc.FAILED>"


# What ``call`` answers when Kodi could not be asked at all — the call
# raised, or the reply would not parse. Distinct from a method-level error
# reply (Kodi answered, refusing), which ``call`` maps to None.
FAILED: Any = _Failed()

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
    active = _active_players()
    if not isinstance(active, list) or not active:
        return None
    result = call(
        "Player.GetProperties",
        {"playerid": active[0]["playerid"], "properties": properties},
    )
    return result if isinstance(result, dict) else None


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
    result = call(
        "Textures.GetTextures",
        {
            # Without an explicit properties list Kodi answers with bare
            # texture ids, and ``require`` would have no url to test.
            "properties": ["url"],
            "filter": {"field": "url", "operator": "contains", "value": needle},
        },
    )
    if not isinstance(result, dict):
        LOG.warning("texture lookup failed for %r: %r", needle, result)
        return 0
    textures = result.get("textures", [])

    removed = 0
    for texture in textures:
        if require and require not in str(texture.get("url", "")):
            continue
        try:
            texture_id = int(texture["textureid"])
        except Exception as error:
            LOG.warning("texture removal failed for %r: %s", texture, error)
            continue
        if call("Textures.RemoveTexture", {"textureid": texture_id}) is FAILED:
            LOG.warning("texture removal failed for %r", texture)
            continue
        removed += 1
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
    active = _active_players()
    if not isinstance(active, list):
        LOG.warning("could not read the active players to stop them: %r", active)
        return False

    if not active:
        return False

    # Every active player by id, rather than a hardcoded 1: SyncPlay drives
    # music as well as video, and Player.Stop answers FailedToExecute for a
    # playerid that is not playing.
    for player in active:
        try:
            player_id = int(player["playerid"])
        except Exception as error:
            LOG.warning("stop failed for player %r: %s", player, error)
            continue
        if call("Player.Stop", {"playerid": player_id}) is FAILED:
            LOG.warning("stop failed for player %r", player)

    if wait_seconds > 0:
        monitor = xbmc.Monitor()
        waited = 0.0
        while waited < wait_seconds:
            if monitor.waitForAbort(STOP_POLL_SECONDS):
                break
            waited += STOP_POLL_SECONDS
            still = _active_players()
            if not isinstance(still, list):
                LOG.debug("player poll failed while waiting for the stop: %r", still)
                break
            if not still:
                break

    return True


def resume_player() -> bool:
    """Set every active player *playing*, by intent rather than by toggling.

    ``xbmc.Player.pause()`` is a toggle and it lands asynchronously, so
    "toggle it if it reads paused" is two races stacked: the read can predate a
    pause still in flight, and the toggle then arrives while the player is
    already moving the other way.

    That is measured, not theoretical. A group Unpause that had to align first
    left a member paused for good: the align seek resumed the player (Android's
    VideoPlayer does resume on seek), the settle loop re-paused it because it
    had been paused going in, and the toggle meant to start it read the
    not-yet-applied pause as "playing" and did nothing — so the pause landed
    last and the member sat still while the group played on
    (``docs/syncplay-drift-shakedown.md`` §11).

    ``Player.PlayPause`` takes an explicit ``play`` flag, which has no state to
    race: asking for playing while already playing is a no-op.

    This only *asks*. Confirming is the caller's job and cannot be done from
    ``speed``, which reads 1 for a player that is not advancing — the position
    sampled twice is the only proof (kodi-drive: kodi-jsonrpc). The SyncPlay
    controller's ``_resume_and_verify`` does that, and re-asks, because the
    failure being guarded is another thread's pause landing after this call.

    Returns True when the request went out (or no player was active).
    """
    active = _active_players()
    if not isinstance(active, list):
        LOG.warning("could not read the active players to resume them: %r", active)
        return False

    if not active:
        return True

    for player in active:
        try:
            player_id = int(player["playerid"])
        except Exception as error:
            LOG.warning("resume failed for player %r: %s", player, error)
            continue
        if call("Player.PlayPause", {"playerid": player_id, "play": True}) is FAILED:
            LOG.warning("resume failed for player %r", player)

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
    result = call(method, {id_field: kodi_id, "properties": ["resume"]})
    try:
        return float(result[result_field]["resume"]["position"])
    except Exception as error:
        LOG.debug("resume read failed for %s/%s: %s", media, kodi_id, error)
        return None


def resume_point(kodi_id: int, media: str) -> Optional[Tuple[float, float]]:
    """Kodi's stored ``(position, total)`` for a library row, or None.

    The same read as :func:`resume_seconds`, keeping the total the resume
    object carries beside the position: a resolved plugin item needs both to
    stamp a resume point, and offline this is the only place either number
    exists (the server's UserData is what is unreachable).
    """
    query = RESUME_QUERY.get(media)
    if query is None:
        return None
    method, id_field, result_field = query
    result = call(method, {id_field: kodi_id, "properties": ["resume"]})
    try:
        resume = result[result_field]["resume"]
        return float(resume["position"]), float(resume["total"])
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
    result = call("Settings.GetSettingValue", {"setting": "locale.subtitlelanguage"})
    if not isinstance(result, dict) or "value" not in result:
        LOG.debug("subtitle language unavailable: %r", result)
        return ""
    value = str(result["value"])
    if value.lower() in ("", "none", "forced_only", "original"):
        return ""
    if value.lower() == "default":
        return str(xbmc.getLanguage(xbmc.ISO_639_2) or "")
    return str(xbmc.convertLanguage(value, xbmc.ISO_639_2) or "")


def call(method: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """One JSON-RPC call's ``result`` — whatever shape the method answers
    with (``Settings.SetSettingValue`` answers a bare ``true``); None for a
    method-level error reply (Kodi answered, refusing); ``FAILED`` when Kodi
    could not be asked at all or answered unparseably. Collapsing those last
    two was the C3 defect — a blip read as "this Kodi lacks the setting"."""
    request: Dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        request["params"] = params
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception:
        return FAILED
    if not isinstance(response, dict):
        return FAILED
    if "error" in response:
        return None
    return response.get("result")


def _active_players() -> Any:
    """``Player.GetActivePlayers``'s list, or whatever ``call`` answered."""
    return call("Player.GetActivePlayers")


def tvshow_title(kodi_id: int) -> Optional[str]:
    """A show's title from Kodi's own library, or None when the row is gone
    or the call failed.

    JSON-RPC rather than a SELECT on MyVideos: reading the library through
    Kodi is what every process may do (writing it that way is what the
    kodi-database-writing rule forbids), and it keeps the plugin process
    out of Kodi's database files altogether.
    """
    result = call(
        "VideoLibrary.GetTVShowDetails",
        {"tvshowid": int(kodi_id), "properties": ["title"]},
    )
    details = result.get("tvshowdetails") if isinstance(result, dict) else None
    title = details.get("title") if isinstance(details, dict) else None
    return str(title) if title else None


def kodi_setting(setting_id: str) -> Any:
    """The value of one of Kodi's own settings; None where the setting does
    not exist; ``FAILED`` when Kodi could not be asked (C3).

    None is the answer only for a setting this Kodi version lacks — Omega
    has no ``videoplayer.queuetimesize`` — so a caller can branch on the
    version without asking for it, and a transient failure no longer reads
    as "this Kodi lacks the setting".
    """
    result = call("Settings.GetSettingValue", {"setting": setting_id})
    if result is FAILED:
        return FAILED
    return result.get("value") if isinstance(result, dict) else None


def set_kodi_setting(setting_id: str, value: Any) -> bool:
    """Set one of Kodi's own settings; True when Kodi accepted the value.

    Measured on 22.0-BETA1: the answer is ``{"result": true}`` — a bare
    boolean, not an object.
    """
    result = call("Settings.SetSettingValue", {"setting": setting_id, "value": value})
    return result is True


def addon_details(addon_id: str) -> Optional[Dict[str, Any]]:
    """``{"enabled": bool, "version": str}`` for an installed add-on, or None
    when it is not installed at all."""
    result = call(
        "Addons.GetAddonDetails",
        {"addonid": addon_id, "properties": ["enabled", "version"]},
    )
    addon = result.get("addon") if isinstance(result, dict) else None
    if not isinstance(addon, dict):
        return None
    return {
        "enabled": bool(addon.get("enabled")),
        "version": str(addon.get("version") or ""),
    }


def clear_resume_bookmark(path: str) -> bool:
    """Delete the resume bookmark Kodi keeps for a plugin path.

    Kodi saves a bookmark for every video it stops, keyed on the listing
    row's own path (``original_listitem_url`` for a plugin item), and reads
    it back for any row whose tag carries no resume point of its own
    (VideoUtils.cpp ``GetNonFolderItemResumeInformation``). So zeroing the
    server's position is only half of a reset: the half Kodi holds has to go
    too, or the row advertises the stale local time the moment kofin stops
    stamping the server's.

    ``Files.SetFileDetails`` is the one JSON-RPC write that reaches a plugin
    path. It insists the file exists, and ``CPluginFile::Exists`` answers
    true for any ``plugin://``; a zero position makes it clear the bookmark
    rather than write one (VideoLibrary.cpp ``UpdateResumePoint``). It adds a
    ``files`` row when none exists, which is what Kodi does after any plugin
    play, so nothing new is left behind. Verified live on Omega 21.3.

    False when Kodi refused or could not be reached; the caller has nothing
    to undo either way.
    """
    result = call(
        "Files.SetFileDetails",
        {"file": path, "media": "video", "resume": {"position": 0}},
    )
    if result is FAILED:
        LOG.debug("bookmark clear failed for %s", path)
        return False
    if result is None:
        LOG.debug("bookmark clear refused for %s", path)
        return False
    return True
