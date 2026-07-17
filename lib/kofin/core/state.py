"""The only cross-process live state: three window properties on window 10000.

Anything else someone wants to share between the plugin and service processes
must argue its way into this module.
"""

import json
from typing import Any, Dict, List, Optional

import xbmcgui

PROP_ONLINE = "kofin.online"
PROP_PLAY_QUEUE = "kofin.play.json"
PROP_PLAYING_ID = "kofin.playing.id"
PROP_SYNC_STOP = "kofin.sync.stop"
PROP_SYNC_ACTIVE = "kofin.sync.active"

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


def clear_play_queue() -> None:
    _window().clearProperty(PROP_PLAY_QUEUE)


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


def clear_all() -> None:
    window = _window()
    for prop in (
        PROP_ONLINE,
        PROP_PLAY_QUEUE,
        PROP_PLAYING_ID,
        PROP_SYNC_STOP,
        PROP_SYNC_ACTIVE,
    ):
        window.clearProperty(prop)


def _read_queue(window: xbmcgui.Window) -> List[Dict[str, Any]]:
    raw = window.getProperty(PROP_PLAY_QUEUE)
    if not raw:
        return []
    try:
        queue = json.loads(raw)
    except ValueError:
        return []
    return queue if isinstance(queue, list) else []
