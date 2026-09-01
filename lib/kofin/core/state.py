"""The only cross-process live state: a few window properties on window 10000.

Anything else someone wants to share between the plugin and service processes
must argue its way into this module. Two groups here are read by skin XML
rather than by kofin's other process — the context bitrates and the lyrics
overlay — because a <visible> condition and a skin control can read a window
property and nothing else.
"""

import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

import xbmcgui
import xbmcvfs

from kofin.core.urls import BASE_URL

PROP_ONLINE = "kofin.online"
PROP_PLAYING_ID = "kofin.playing.id"
PROP_SYNC_STOP = "kofin.sync.stop"
PROP_SYNC_ACTIVE = "kofin.sync.active"
# Earns its place here because addon.xml's <visible> can only test window
# properties: there is no infolabel that reads an addon setting, so hiding
# "Play with transcoding" when no bitrates are configured needs the setting
# mirrored into one.
PROP_CONTEXT_BITRATES = "kofin.context.bitrates"

# The playing item's selectable streams, and the one-word summary addon.xml
# tests to choose the stream context item's label.
#
# PROP_PLAYING_STREAMS is here because no other surface reaches: the streams
# are resolved in the *plugin* process (the play route's PlaybackInfo already
# answers them), the service claims the playback and empties the play queue,
# and the thing that needs them is a *third* process — the context item, run
# minutes later. Putting them on the play-state alone would mean they vanish
# at the moment the menu becomes reachable.
#
# PROP_PLAYING_MENU earns its place the same way PROP_CONTEXT_BITRATES does: a
# context item's <label> is a fixed string id, so an entry whose wording
# depends on what the item offers has to be three declared entries whose
# <visible> conditions are mutually exclusive — and a boolean expression can
# read a window property and nothing else. The service publishes one of
# streams.OFFER_* when it claims; "" hides all three.
PROP_PLAYING_STREAMS = "kofin.playing.streams"
PROP_PLAYING_MENU = "kofin.playing.menu"

# The additional users on this device's session, as the root listing's
# "Who's watching?" entry names them. Earns its place because the owner and
# the reader are different processes: the *service* is where the set changes
# (the picker worker, the connect-time restore) and it knows the names the
# moment it changes them, while the *plugin* renders the label on every root
# listing — which previously cost a /Sessions round trip per render, and made
# an offline root hang for that call's whole retry ladder.
PROP_WHO_NAMES = "kofin.who.names"

# Whether the addon-root "Who's watching?" / SyncPlay entries are on offer.
# Earns its place the same way PROP_CONTEXT_BITRATES does: a skin <visible>
# can only test a window property, and these two entries come and go with
# addon settings the skin cannot read (the shortlist's nobody sentinel, and
# syncPlayEnabled). Contuary's home-widget buttons hide on the same
# conditions as the root listing.
PROP_MENU_WHO = "kofin.menu.who"
PROP_MENU_SYNCPLAY = "kofin.menu.syncplay"
# The SyncPlay fine-sync session (syncplay/tempo.py): the tempo file the play
# route stamps on every direct-play video item while a group is joined, and the
# queue depth the add-on must report time behind. Set by the service at group
# join, cleared at leave; absent means plays resolve as they always did. Lives
# here because the *plugin* process resolves the play and has no other way to
# learn that the service is in a group.
PROP_SYNCPLAY_TEMPO = "kofin.syncplay.tempo"
# The public sync-session mirror (plan G2.2): what a provider add-on or a
# skin needs to offer "watch together" affordances — in-group, group name,
# member names, phase, the current item's provider:key. Cross-process shared
# live state read by add-ons that are not kofin, which is exactly this
# module's charter; deliberately NOT kofin-prefixed, because the name is part
# of the published provider contract (docs/syncplay-provider-contract.md)
# and must survive any future re-hosting of the engine.
PROP_SYNCSESSION = "syncsession.state"

# The lyrics channel. These earn their place for the same reason as
# PROP_CONTEXT_BITRATES: a skin or a script can only read window properties,
# and lyrics cannot reach one any other way -- Kodi's music database has no
# lyrics column, and an addon window cannot draw a passive overlay (it becomes
# the active window and swallows the OSD).
#
# kofin publishes and stops. It fetches the lyrics at playback start, when it
# alone knows the song's Jellyfin id, and hands them over here; rendering
# them and following the clock belong to script.kofin.lyrics (a350452 removed
# the Control.SetFocus driver the service used to run). PROP_LYRIC_JSON
# carries the timed lines; PROP_LYRIC_HAS is what a renderer shows and hides
# on; PROP_LYRIC_PATH is the address of the plugin directory that serves the
# lines as list items, and it carries the song id so that it *changes* per
# song -- a list bound to it re-reads the directory on the change, which
# Container.Refresh cannot do for a window that is not a media window.
PROP_LYRIC_HAS = "kofin.lyric.has"
PROP_LYRIC_JSON = "kofin.lyric.json"
PROP_LYRIC_PATH = "kofin.lyric.path"

# The song id is a cache-buster as much as an argument: the path has to differ
# between songs for the skin's list to notice.
LYRICS_DIRECTORY = BASE_URL + "?mode=lyrics&id=%s"

_HOME_WINDOW = 10000


def _window() -> xbmcgui.Window:
    return xbmcgui.Window(_HOME_WINDOW)


def set_online(online: bool) -> None:
    """Publish the connection state. Three-valued on purpose: "true",
    "false", and *absent* — the last meaning "nobody has said yet", which is
    every moment between Kodi starting and the service's first probe.

    Writing "false" rather than clearing is what lets a caller tell a known
    outage from an unanswered question: refusing a playback during the
    startup window because the flag had not been raised yet would be a
    false negative on the most ordinary action there is (plan W2.2).
    """
    _window().setProperty(PROP_ONLINE, "true" if online else "false")


def is_online() -> bool:
    """Whether the server is known reachable. Absent reads as not-online,
    which is what the sync-side guards want: they only run after a connect,
    so for them absence means the service went away."""
    return _window().getProperty(PROP_ONLINE) == "true"


def is_offline() -> bool:
    """Whether the server is known *un*reachable — a stated outage, not an
    unanswered question. This is the one to gate user-facing refusals on."""
    return _window().getProperty(PROP_ONLINE) == "false"


# The play queue is a directory of one file per resolved play, not a window
# property holding a list. Both processes write it — the plugin's play route
# pushes, the service's player claims — and a window property can only be
# read-modify-written, which across processes is a race with no lock available
# (audit finding #12): a claim landing inside the plugin's read/write window
# resurrects the entry it just took, and a push landing inside the service's
# loses the new one, whose playback is then never claimed or reported, while
# claim's oldest-entry fallback attributes the *wrong* item to it.
#
# One file per entry removes the shared structure: nobody rewrites anybody
# else's data, and ``os.remove`` is the claim — the filesystem guarantees
# exactly one caller succeeds, which is the mutual exclusion the property
# could not give. Names are time-ordered so "oldest" is a sort, not a
# timestamp field to trust.
PLAY_QUEUE_DIR = "special://profile/addon_data/plugin.video.kofin/playqueue"

# How long an unclaimed entry survives. A play that resolved but never reached
# the player (a failed stream, a Kodi that never started it) would otherwise
# sit there forever and be adopted by an unrelated later playback through the
# oldest-entry fallback.
PLAY_QUEUE_TTL_SECONDS = 600


def _queue_dir() -> str:
    return xbmcvfs.translatePath(PLAY_QUEUE_DIR)


def _queue_files() -> List[str]:
    """Queued entry paths, oldest first (names carry a nanosecond stamp)."""
    directory = _queue_dir()
    try:
        return sorted(
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if name.endswith(".json")
        )
    except OSError:
        return []


def _read_entry(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path) as handle:
            item = json.load(handle)
    except (OSError, ValueError):
        return None
    return item if isinstance(item, dict) else None


def push_play_item(item: Dict[str, Any]) -> None:
    """Queue a resolved play's state for the service-side player to claim."""
    directory = _queue_dir()
    try:
        os.makedirs(directory, exist_ok=True)
        _expire_play_items()
        # The stamp orders the queue; the suffix keeps two plays resolved in
        # the same nanosecond apart. Written to a temp name and renamed so a
        # reader never sees a half-written entry.
        name = "%019d-%s.json" % (time.time_ns(), uuid.uuid4().hex[:8])
        target = os.path.join(directory, name)
        temporary = target + ".tmp"
        with open(temporary, "w") as handle:
            json.dump(item, handle)
        os.replace(temporary, target)
    except OSError:
        # A queue that cannot be written costs this playback its reporting,
        # which is what the old property did on failure too. Never the play.
        pass


def claim_play_item(path: str) -> Optional[Dict[str, Any]]:
    """Take the queued entry for ``path``, or the oldest entry as fallback.

    The removal is the claim: whoever unlinks the file owns that entry, and
    a loser simply moves on to the next candidate.
    """
    files = _queue_files()
    if not files:
        return None

    matching = []
    fallback = None
    for entry_path in files:
        item = _read_entry(entry_path)
        if item is None:
            continue
        if item.get("Path") == path:
            matching.append((entry_path, item))
        elif fallback is None:
            fallback = (entry_path, item)

    for entry_path, item in matching + ([fallback] if fallback else []):
        try:
            os.remove(entry_path)
        except OSError:
            continue  # someone else claimed it first
        return item
    return None


def play_item_queued(path: str) -> bool:
    """Whether a resolved play for ``path`` is already waiting to be claimed.

    Read-only counterpart to :func:`claim_play_item`, for deciding not to
    queue a second entry for a playback the play route already handled.
    """
    return any(
        (_read_entry(entry) or {}).get("Path") == path for entry in _queue_files()
    )


def _expire_play_items() -> None:
    """Drop entries older than the TTL (see PLAY_QUEUE_TTL_SECONDS)."""
    cutoff = time.time_ns() - PLAY_QUEUE_TTL_SECONDS * 1_000_000_000
    for entry in _queue_files():
        stamp = os.path.basename(entry).split("-", 1)[0]
        if not stamp.isdigit() or int(stamp) >= cutoff:
            continue
        try:
            os.remove(entry)
        except OSError:
            pass


def clear_play_queue() -> None:
    for entry in _queue_files():
        try:
            os.remove(entry)
        except OSError:
            pass


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


def publish_playing_streams(payload: Dict[str, Any], offer: str) -> None:
    """Publish the playing item's streams and what the menu should offer.

    Written when the service claims a playback, cleared when it ends, so the
    properties' lifetime is exactly the playback's and a stale menu can never
    act on an item that stopped.
    """
    window = _window()
    window.setProperty(PROP_PLAYING_STREAMS, json.dumps(payload))
    if offer:
        window.setProperty(PROP_PLAYING_MENU, offer)
    else:
        window.clearProperty(PROP_PLAYING_MENU)


def playing_streams() -> Dict[str, Any]:
    """The published streams payload, or {} when nothing kofin owns is on."""
    raw = _window().getProperty(PROP_PLAYING_STREAMS)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def clear_playing_streams() -> None:
    window = _window()
    window.clearProperty(PROP_PLAYING_STREAMS)
    window.clearProperty(PROP_PLAYING_MENU)


def set_watching_names(names: List[str]) -> None:
    """Publish the additional-user names on this device's session.

    An empty list clears the property, which reads back as "nobody extra" —
    the base label. The service calls this wherever the set changes or is
    re-learned: the picker, the connect-time restore.
    """
    window = _window()
    if names:
        window.setProperty(PROP_WHO_NAMES, json.dumps(names))
    else:
        window.clearProperty(PROP_WHO_NAMES)


def watching_names() -> List[str]:
    """The published additional-user names, or [] when there are none."""
    raw = _window().getProperty(PROP_WHO_NAMES)
    if not raw:
        return []
    try:
        names = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(names, list):
        return []
    return [str(name) for name in names if name]


def set_menu_who(offered: bool) -> None:
    """Whether the Who's watching? root entry is on offer."""
    if offered:
        _window().setProperty(PROP_MENU_WHO, "true")
    else:
        _window().clearProperty(PROP_MENU_WHO)


def menu_who() -> bool:
    return _window().getProperty(PROP_MENU_WHO) == "true"


def publish_syncplay_tempo(payload: Dict[str, Any]) -> None:
    _window().setProperty(PROP_SYNCPLAY_TEMPO, json.dumps(payload))


def syncplay_tempo() -> Dict[str, Any]:
    """The fine-sync session the play route should stamp, or {} outside one."""
    raw = _window().getProperty(PROP_SYNCPLAY_TEMPO)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def clear_syncplay_tempo() -> None:
    _window().clearProperty(PROP_SYNCPLAY_TEMPO)


def publish_syncsession(payload: Dict[str, Any]) -> None:
    """The public sync-session state (plan G2.2), JSON on PROP_SYNCSESSION."""
    _window().setProperty(PROP_SYNCSESSION, json.dumps(payload))


def syncsession() -> Dict[str, Any]:
    raw = _window().getProperty(PROP_SYNCSESSION)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def clear_syncsession() -> None:
    _window().clearProperty(PROP_SYNCSESSION)


def set_menu_syncplay(offered: bool) -> None:
    """Whether the SyncPlay root entry is on offer."""
    if offered:
        _window().setProperty(PROP_MENU_SYNCPLAY, "true")
    else:
        _window().clearProperty(PROP_MENU_SYNCPLAY)


def menu_syncplay() -> bool:
    return _window().getProperty(PROP_MENU_SYNCPLAY) == "true"


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


def clear_lyrics() -> None:
    """Drop the overlay. The skin hides on PROP_LYRIC_HAS, so this is what
    stops lyrics outliving the song that owned them."""
    window = _window()
    window.clearProperty(PROP_LYRIC_HAS)
    window.clearProperty(PROP_LYRIC_JSON)
    window.clearProperty(PROP_LYRIC_PATH)


def clear_all(keep_stop: bool = False) -> None:
    """Drop the live state. ``keep_stop`` leaves PROP_SYNC_STOP raised.

    The teardown passes it when a sync thread outlived the wait: that flag is
    what every worker's @stop guard reads, so clearing it would un-pause a
    thread the service has already replaced. Better a stuck thread that stays
    stopped than one that resumes into a second Library.
    """
    window = _window()
    props = [
        PROP_ONLINE,
        PROP_PLAYING_ID,
        PROP_SYNC_ACTIVE,
        PROP_CONTEXT_BITRATES,
        PROP_PLAYING_STREAMS,
        PROP_PLAYING_MENU,
        PROP_WHO_NAMES,
        PROP_MENU_WHO,
        PROP_MENU_SYNCPLAY,
        PROP_SYNCPLAY_TEMPO,
        PROP_SYNCSESSION,
    ]
    if not keep_stop:
        props.append(PROP_SYNC_STOP)
    for prop in props:
        window.clearProperty(prop)
    clear_lyrics()
    clear_play_queue()
