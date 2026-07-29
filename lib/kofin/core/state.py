"""The only cross-process live state: a few window properties on window 10000.

Anything else someone wants to share between the plugin and service processes
must argue its way into this module. Two groups here are read by skin XML
rather than by kofin's other process — the context bitrates and the lyrics
overlay — because a <visible> condition and a skin control can read a window
property and nothing else.
"""

import json
from typing import Any, Dict, List, Optional

import xbmcgui

PROP_ONLINE = "kofin.online"
PROP_PLAY_QUEUE = "kofin.play.json"
PROP_PLAYING_ID = "kofin.playing.id"
PROP_SYNC_STOP = "kofin.sync.stop"
PROP_SYNC_ACTIVE = "kofin.sync.active"
# Earns its place here because addon.xml's <visible> can only test window
# properties: there is no infolabel that reads an addon setting, so hiding
# "Play with transcoding" when no bitrates are configured needs the setting
# mirrored into one.
PROP_CONTEXT_BITRATES = "kofin.context.bitrates"

# The lyrics overlay's channel to the skin. These earn their place for the
# same reason as PROP_CONTEXT_BITRATES: a skin can only read window
# properties, and lyrics cannot reach it any other way -- Kodi's music
# database has no lyrics column, and an addon window cannot draw a passive
# overlay (it becomes the active window and swallows the OSD).
#
# The skin renders the lines in a fixedlist so Kodi animates the scroll; the
# service drives which line is current with Control.SetFocus. PROP_LYRIC_JSON
# carries the lines to the plugin process, which serves them as the list's
# directory -- the alternative was fetching them from Jellyfin a second time.
#
# PROP_LYRIC_PATH is that directory's address, and it carries the song id so
# that it *changes* per song: the skin binds its list content to this, and a
# changed path is what makes Kodi re-read the directory. Container.Refresh
# cannot do it -- the visualisation window is not a media window, so it has no
# container for that builtin to act on.
#
# PROP_LYRIC_CONTROL runs the other way: the *skin* sets it, naming the
# control to drive. So the service drives whatever id a skin declares, and
# stays silent for skins that declare nothing.
PROP_LYRIC_HAS = "kofin.lyric.has"
PROP_LYRIC_JSON = "kofin.lyric.json"
PROP_LYRIC_PATH = "kofin.lyric.path"
PROP_LYRIC_CONTROL = "kofin.lyric.control"

# The song id is a cache-buster as much as an argument: the path has to differ
# between songs for the skin's list to notice.
LYRICS_DIRECTORY = "plugin://plugin.video.kofin/?mode=lyrics&id=%s"

_HOME_WINDOW = 10000


def _window() -> xbmcgui.Window:
    return xbmcgui.Window(_HOME_WINDOW)


def set_online(online: bool) -> None:
    if online:
        _window().setProperty(PROP_ONLINE, "true")
    else:
        _window().clearProperty(PROP_ONLINE)


def is_online() -> bool:
    return _window().getProperty(PROP_ONLINE) == "true"


def push_play_item(item: Dict[str, Any]) -> None:
    """Queue a resolved play's state for the service-side player to claim."""
    window = _window()
    queue = _read_queue(window)
    queue.append(item)
    window.setProperty(PROP_PLAY_QUEUE, json.dumps(queue))


def claim_play_item(path: str) -> Optional[Dict[str, Any]]:
    """Pop the queued entry for ``path``, or the oldest entry as fallback."""
    window = _window()
    queue = _read_queue(window)
    if not queue:
        return None
    claimed = next((item for item in queue if item.get("Path") == path), queue[0])
    queue.remove(claimed)
    window.setProperty(PROP_PLAY_QUEUE, json.dumps(queue))
    return claimed


def play_item_queued(path: str) -> bool:
    """Whether a resolved play for ``path`` is already waiting to be claimed.

    Read-only counterpart to :func:`claim_play_item`, for deciding not to
    queue a second entry for a playback the play route already handled.
    """
    return any(item.get("Path") == path for item in _read_queue(_window()))


def clear_play_queue() -> None:
    _window().clearProperty(PROP_PLAY_QUEUE)


def drop_play_items_for_id(item_id: str) -> None:
    """Remove queued play-state entries for ``item_id`` (restart hygiene)."""
    if not item_id:
        return
    window = _window()
    queue = [item for item in _read_queue(window) if item.get("Id") != item_id]
    if queue:
        window.setProperty(PROP_PLAY_QUEUE, json.dumps(queue))
    else:
        window.clearProperty(PROP_PLAY_QUEUE)


def set_playing_id(item_id: str) -> None:
    _window().setProperty(PROP_PLAYING_ID, item_id)


def get_playing_id() -> str:
    return _window().getProperty(PROP_PLAYING_ID)


def clear_playing_id() -> None:
    _window().clearProperty(PROP_PLAYING_ID)


def set_should_stop(stop: bool) -> None:
    """Raised while the service shuts down so sync workers exit their loops."""
    if stop:
        _window().setProperty(PROP_SYNC_STOP, "true")
    else:
        _window().clearProperty(PROP_SYNC_STOP)


def should_stop() -> bool:
    return _window().getProperty(PROP_SYNC_STOP) == "true"


def set_sync_active(active: bool) -> None:
    if active:
        _window().setProperty(PROP_SYNC_ACTIVE, "true")
    else:
        _window().clearProperty(PROP_SYNC_ACTIVE)


def is_sync_active() -> bool:
    return _window().getProperty(PROP_SYNC_ACTIVE) == "true"


def set_context_bitrates(bitrates: str) -> None:
    """Mirror the configured context bitrates so addon.xml can hide the
    "Play with transcoding" item when the user has selected none."""
    if bitrates:
        _window().setProperty(PROP_CONTEXT_BITRATES, bitrates)
    else:
        _window().clearProperty(PROP_CONTEXT_BITRATES)


def get_context_bitrates() -> str:
    return _window().getProperty(PROP_CONTEXT_BITRATES)


def publish_lyrics(lines: List[Any], item_id: str) -> None:
    """Publish the song's lyrics for whatever is going to render them.

    ``lines`` is ``[[start_seconds_or_null, text], ...]``. The timings ride
    along because the renderer decides which line is current -- kofin's job
    ends at fetching them.
    """
    window = _window()
    window.setProperty(PROP_LYRIC_JSON, json.dumps(lines))
    window.setProperty(PROP_LYRIC_PATH, LYRICS_DIRECTORY % item_id)
    window.setProperty(PROP_LYRIC_HAS, "true")


def lyric_lines() -> List[Any]:
    """The published ``[start, text]`` pairs, or [] when there are none."""
    raw = _window().getProperty(PROP_LYRIC_JSON)
    if not raw:
        return []
    try:
        lines = json.loads(raw)
    except ValueError:
        return []
    return lines if isinstance(lines, list) else []


def lyric_texts() -> List[str]:
    """Just the text of each line, for serving the list's directory."""
    return [str(line[1]) for line in lyric_lines() if isinstance(line, list) and line]


def lyric_control_id() -> int:
    """The list control a skin has asked us to drive, or 0 if none has.

    Set by the skin's window on load and cleared on unload, so this doubles as
    "is a lyrics-capable window on screen" -- Control.SetFocus only reaches the
    active window anyway.
    """
    raw = _window().getProperty(PROP_LYRIC_CONTROL)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def clear_lyrics() -> None:
    """Drop the overlay. The skin hides on PROP_LYRIC_HAS, so this is what
    stops lyrics outliving the song that owned them."""
    window = _window()
    window.clearProperty(PROP_LYRIC_HAS)
    window.clearProperty(PROP_LYRIC_JSON)
    window.clearProperty(PROP_LYRIC_PATH)


def has_lyrics() -> bool:
    return _window().getProperty(PROP_LYRIC_HAS) == "true"


def clear_all() -> None:
    window = _window()
    for prop in (
        PROP_ONLINE,
        PROP_PLAY_QUEUE,
        PROP_PLAYING_ID,
        PROP_SYNC_STOP,
        PROP_SYNC_ACTIVE,
        PROP_CONTEXT_BITRATES,
    ):
        window.clearProperty(prop)
    clear_lyrics()


def _read_queue(window: xbmcgui.Window) -> List[Dict[str, Any]]:
    raw = window.getProperty(PROP_PLAY_QUEUE)
    if not raw:
        return []
    try:
        queue = json.loads(raw)
    except ValueError:
        return []
    return queue if isinstance(queue, list) else []
