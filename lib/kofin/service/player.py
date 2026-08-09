"""Playback reporting and the segment engine (skip / Play Next overlay).

The player owns its progress ticker (10s cadence) — the service loop does no
playback polling. Foreign playback (anything not queued on kofin.play.json)
is ignored entirely.

The segment engine (plan §2) runs on the SegmentChecker's 0.25 s tick:
boundary-*crossing* detection on float positions (a coarse or late poll
cannot step over a segment), a pre-armed next boundary (one compare per
tick), recoverable dedup (seek out and back in re-offers), and a settle
window after our own skip seek so a lagging ``getTime()`` cannot re-trigger.
The overlay's lifetime is tick-driven — open at the crossing, auto-close
past the end, button actions on Kodi's GUI thread — no second monitor
thread. Play Next resolves the next episode up front and starts it through
kofin's own play path; no ``service.upnext`` anywhere.
"""

import json
import queue
import json
import os
import re
import threading
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set, Tuple
from uuid import uuid4

import xbmc
import xbmcgui

from kofin.core import lyrics as lyrics_render
from kofin.core import ipc, settings, state, streams, toast
from kofin.core.api import Api
from kofin.downloads import auto as downloads_auto
from kofin.core.http import JellyfinError
from kofin.core.log import Logger
from kofin.service import chapters
from kofin.service.segments import SegmentChecker, parse_segments

if TYPE_CHECKING:
    from kofin.syncplay.manager import SyncPlayManager

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

PROGRESS_INTERVAL_SECONDS = 10.0
CLAIM_TIMEOUT_SECONDS = 10.0
# Grace for a claim that arrives from the Player.OnPlay notification instead
# of the play route (see backfill_library_claim): library-originated audio,
# and downloaded video playing as the local file its row points at (W1.7).
# Only until the notification lands.
BACKFILL_GRACE_SECONDS = 3.0

# ~3 s of ticks: how long a lagging getTime() may keep reporting the pre-seek
# position after our own skip seek before we give up waiting for it.
SEEK_SETTLE_TICKS = 12

# A seek issued at t~=0 (an Intro starting at the very start) is dropped by the
# player before it is seekable, so the skip is silently lost. Each settle window
# that expires with the position still short of the target re-issues the seek,
# up to this many times, before giving up — covering ~the first few seconds of
# startup buffering. The notification fires only once the seek actually lands.
SEEK_RETRIES = 6

# A starting playback is not at its start position yet, in either direction.
# Below it: Kodi reports 0 while it seeks to the resume point, and an intro
# beginning at 0.0 fires against that phantom zero. Above it: on a transition
# (notably Play Next A->B) getTime() reports the *previous* item's position —
# which keeps advancing, so it cannot be told from real playback by stability
# alone — and Play Next fires near A's end, so that value sits far past B's
# start. The engine holds off arming until the position is within this many
# seconds of the one the play route resolved, which is generous enough for a
# resume seek snapping back to a keyframe.
FRESH_START_TOLERANCE = 30.0

# ...but not forever: a seek that never lands must not leave the engine
# disarmed for the whole item. ~10 s of ticks, then arm against whatever the
# player reports.
FRESH_START_MAX_TICKS = 40

# Autoplay starts the next episode this close to the overlay deadline, so the
# handoff lands before natural EOF tears the player down.
AUTOPLAY_MARGIN_SECONDS = 1.0

# How far into an item counts as having watched it, for the
# ``deleteAfterWatching`` offer. Jellyfin's own played threshold.
WATCHED_FRACTION = 0.9

# Item types the finished-watching delete offer applies to. A song or a music
# video reaching its end is not an invitation to delete anything.
DELETABLE_TYPES = ("Movie", "Episode")

SEGMENT_MODE_SETTINGS = {
    "Introduction": "skipIntroductionMode",
    "Credits": "skipCreditsMode",
    "Recap": "skipRecapMode",
    "Preview": "skipPreviewMode",
    "Commercial": "skipCommercialMode",
}

SKIP_LABEL_IDS = {
    "Introduction": 30481,
    "Credits": 30482,
    "Recap": 30483,
    "Preview": 30484,
    "Commercial": 30485,
}

MODE_OFF = 0
MODE_AUTO = 1
MODE_ASK = 2


# -- pure timing / decision helpers (L1-tested) -------------------------------


def crossed_into(prev: Optional[float], now: float, start: float, end: float) -> bool:
    """Whether this tick fires the ``[start, end]`` boundary.

    Inside the window always fires (catches seeks into it and late-loaded
    segments); otherwise the boundary must have been stepped over since the
    previous tick (``prev < start <= now``), so a coarse or lagging poll
    cannot silently pass a segment.
    """
    if start <= now <= end:
        return True
    return prev is not None and prev < start <= now


def safe_seek_end(
    end: Any, runtime: float, current: Optional[float], margin: float = 1.0
) -> Optional[float]:
    """EOF-clamped seek target for a segment end; None when the seek would go
    backwards or nowhere (fork ``_get_safe_seek_time`` semantics)."""
    try:
        target = max(0.0, float(end))
    except (TypeError, ValueError):
        return None
    if runtime > 0:
        cap = runtime - margin
        if cap <= 0:
            cap = runtime
        target = min(target, cap)
    if current is not None and target <= current:
        return None
    return target


def near_end_prompt_at(runtime: float, lead: float) -> float:
    """When the no-segment Play Next prompt fires; the lead is clamped so the
    prompt still appears on items shorter than the configured lead."""
    lead = min(max(lead, 0.0), runtime / 2.0)
    return runtime - lead


def watched_to_end(item: JsonDict) -> bool:
    """True when this playback got close enough to the end to call it watched.

    Not "did Kodi fire onPlayBackEnded": Play Next autoplay hands over about a
    second before natural EOF, so the episode the viewer just finished ends as
    a *stopped* playback. A share of the runtime catches both, and matches the
    threshold the server itself uses to mark an item played. The last progress
    tick can be up to ``PROGRESS_INTERVAL_SECONDS`` stale, which at this
    threshold only matters for items under ~100 s.
    """
    runtime = float(item.get("Runtime") or 0) / 10_000_000
    if runtime <= 0:
        return False
    return float(item.get("CurrentPosition") or 0) >= runtime * WATCHED_FRACTION


def plan_for_crossing(
    segment_type: str, mode: int, offer_next: bool
) -> Tuple[bool, Tuple[str, ...]]:
    """``(auto_seek, buttons)`` for a segment crossing — the §2 decision matrix.

    ``mode`` is 0 Off / 1 Auto / 2 Ask; ``offer_next`` means a Play Next is on
    offer (episode with a resolved next episode, Play Next enabled, not in a
    SyncPlay group). Only the Credits crossing ever carries Play Next.
    """
    if segment_type != "Credits":
        if mode == MODE_AUTO:
            return True, ()
        if mode == MODE_ASK:
            return False, ("skip", "close")
        return False, ()
    if mode == MODE_AUTO:
        return True, ("playnext", "close") if offer_next else ()
    if mode == MODE_ASK:
        if offer_next:
            return False, ("skip", "playnext", "close")
        return False, ("skip", "close")
    return False, ("playnext", "close") if offer_next else ()


def segments_entered_at(
    segments: List[JsonDict], position: float
) -> Set[Tuple[float, float]]:
    """The ``(start, end)`` keys of every segment ``position`` lands inside.

    Used to mark the segments a playback *started* part-way through: resuming
    into the middle of an intro must not fire that intro's skip prompt, which
    opens and auto-closes a moment later — all the viewer sees is a flash.

    Strictly past the start, because a position exactly at a segment's start
    has not entered it. Intros routinely begin at 0.0, and playing such an
    episode from the beginning is about to watch that intro from its first
    frame; offering to skip it is the entire feature.
    """
    keys = set()
    for segment in segments:
        start = float(segment["Start"])
        end = float(segment["End"])
        if start < position <= end:
            keys.add((start, end))
    return keys


def next_episode_label(episode: JsonDict) -> str:
    season = episode.get("ParentIndexNumber")
    number = episode.get("IndexNumber")
    name = episode.get("Name") or ""
    if season is not None and number is not None:
        prefix = "S%02dE%02d" % (int(season), int(number))
        return "%s. %s" % (prefix, name) if name else prefix
    return name


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


def _downloaded_path(path: str) -> bool:
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

# What lrclyrics shows as the attribution line. Setting it also tells the
# addon these lyrics did not come from one of its own scrapers.
LYRICS_SOURCE = "Jellyfin"

# The push is rejected until Kodi's current item is in place, which is not
# guaranteed at the instant the callback fires. Each miss costs a sleep, and
# the whole budget is the gap before the first frame, so this stays small.
LYRICS_PUSH_ATTEMPTS = 4
LYRICS_PUSH_RETRY_SECONDS = 0.05

# musicLyricsMode. Two ways to show the same lyrics, and they must not both
# run: with a lyrics addon installed, the skin overlay and the addon's own
# window would draw the same words twice.
LYRICS_OFF = 0
LYRICS_SKIN = 1  # publish for the skin to render (see core/state.py)
LYRICS_ADDON = 2  # hand to a lyrics addon via the playing item's music tag


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


class _Reporter(threading.Thread):
    """One FIFO worker for every server-bound playback report.

    Kodi delivers player callbacks on its announcement thread, which every
    addon shares: a network call there stalls player and monitor callbacks
    for all of them, for the transport's whole budget, whenever the server is
    away (audit finding #2). So callbacks capture their payload at event time
    — position and volume are read when the event happens, not when the
    network gets to it — and enqueue here, and this thread posts strictly in
    order: the server reads playing/progress/stopped as one session's story,
    and the single pipe is what keeps a stop from overtaking its start.
    """

    def __init__(self) -> None:
        super().__init__(name="kofin-reporter", daemon=True)
        self._jobs: "queue.Queue[Optional[Callable[[], None]]]" = queue.Queue()

    def submit(self, job: Callable[[], None]) -> None:
        self._jobs.put(job)

    def flush(self, timeout: float = 5.0) -> bool:
        """True once everything queued before this call has run (tests)."""
        done = threading.Event()
        self._jobs.put(lambda: done.set())
        return done.wait(timeout)

    def stop(self, timeout: float = 5.0) -> None:
        self._jobs.put(None)
        if self.is_alive():
            self.join(timeout=timeout)
            if self.is_alive():  # pragma: no cover - watchdog logging only
                LOG.warning("reporter did not drain within its deadline")

    def run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                break
            try:
                job()
            except Exception as error:
                LOG.warning("playback report failed: %s", error)


class Player(xbmc.Player):
    def __init__(self, api: Api) -> None:
        super().__init__()
        self.api = api
        self._item: Optional[JsonDict] = None
        self._ticker: Optional[_Ticker] = None
        self._lock = threading.Lock()
        # Driven by SyncPlay (phase 4); while True, Play Next is withheld —
        # the group queue is authoritative.
        self._syncplay_group_active = False
        # The SyncPlay manager, attached by the service when built.
        self.syncplay: Optional["SyncPlayManager"] = None
        self._checker: Optional[SegmentChecker] = None
        self._segments: List[JsonDict] = []
        self._segments_loaded = False
        self._armed_index = 0
        self._prompted: Set[Tuple[float, float]] = set()
        # Segments this playback started inside (resume point mid-segment):
        # no skip prompt for them until the position leaves them.
        self._start_inside: Set[Tuple[float, float]] = set()
        self._prev_pos: Optional[float] = None
        self._settle_target: Optional[float] = None
        self._settle_ticks = 0
        self._settle_retries = 0
        self._pending_notify: Optional[str] = None
        self._pending_jump = False
        self._fresh_start = False
        self._fresh_start_ticks = 0
        self._next_episode: Optional[JsonDict] = None
        # W4.1's one-shot: the item id whose 80% crossing already fired,
        # latched before the lookup so a failed resolve never retries.
        self._auto_next_latch = ""
        self._runtime = 0.0
        self._near_end_at: Optional[float] = None
        self._near_end_prompted = False
        self._overlay: Optional[Any] = None
        self._overlay_end = 0.0
        self._overlay_window: Optional[Tuple[float, float]] = None
        self._overlay_autoplay = False
        self._skip_target: Optional[float] = None
        self._chapter_thumbs: Optional[chapters.ChapterThumbs] = None
        self._reporter = _Reporter()
        self._reporter.start()

    # A property so joining or leaving a group also withdraws or restores the
    # stream menu, without the SyncPlay manager having to know the menu exists:
    # it writes this attribute and nothing else (syncplay/manager.py is ported
    # fork code and stays that way). Changing audio means restarting playback,
    # which in a group would desync everyone else, so the entry disappears.
    @property
    def syncplay_group_active(self) -> bool:
        return self._syncplay_group_active

    @syncplay_group_active.setter
    def syncplay_group_active(self, active: bool) -> None:
        self._syncplay_group_active = active
        item = self.current_item()
        if item is not None:
            self._publish_streams(item)

    # -- syncplay forwarding ---------------------------------------------------

    def _syncplay_event(self, name: str, *args: Any) -> None:
        """Forward a player callback to SyncPlay without ever letting it
        break regular playback reporting."""
        if self.syncplay is None:
            return
        try:
            getattr(self.syncplay, name)(*args)
        except Exception as error:
            LOG.exception("SyncPlay hook %s failed: %s", name, error)

    def current_item(self) -> Optional[JsonDict]:
        """The claimed play state of the current kofin playback (SyncPlay's
        identity source), or None for idle/foreign playback."""
        with self._lock:
            return self._item

    # -- lyrics ---------------------------------------------------------------

    def start_lyrics(self) -> None:
        """Show the playing song's Jellyfin lyrics. Never raises.

        Kodi's music database has no lyrics column, so lyrics cannot be synced
        into the library the way everything else is — they exist only on the
        playback that is running, and only while it runs. That holds whichever
        path a song was written with, so this one route covers both direct
        rows and the plugin:// rows musicTranscode writes.

        Two destinations, never both (see the LYRICS_* modes). The skin
        overlay is the seamless one; the lyrics-addon hand-off is what works
        on skins that draw nothing themselves.

        Timing only binds on the addon path: script.cu.lrclyrics searches on
        onAVStarted and memoises the result per song, and its own force
        refresh does not clear that memo, so lyrics arriving after it looked
        are ignored until the song leaves its cache. Hence this runs
        synchronously at the top of playback start, ahead of the claim, and
        the fetch behind it forfeits rather than stalls (see Api.lyrics).
        """
        mode = settings.get_int("musicLyricsMode")
        if mode == LYRICS_OFF:
            return
        try:
            self._start_lyrics(mode)
        except JellyfinError as error:
            LOG.debug("lyrics unavailable: %s", error)
        except Exception:
            LOG.exception("lyrics start failed")

    def _start_lyrics(self, mode: int) -> None:
        if not self.isPlayingAudio():
            return

        jellyfin_id = playing_jellyfin_id(self.getPlayingItem(), self.getPlayingFile())
        if jellyfin_id is None:
            return

        payload = self.api.lyrics(jellyfin_id)
        if mode == LYRICS_SKIN:
            self._publish_lyrics(payload, jellyfin_id)
        else:
            self._push_lyrics_to_tag(payload, jellyfin_id)

    def _publish_lyrics(self, payload: JsonDict, jellyfin_id: str) -> None:
        """Publish the lyrics and stop. Rendering them, and following the
        clock, belongs to script.kofin.lyrics -- kofin only ever knows how to
        fetch them at the right instant."""
        lines = lyrics_render.to_lines(payload)
        if not lines:
            return
        state.publish_lyrics([[start, text] for start, text in lines], jellyfin_id)
        LOG.info("--> lyrics %s (%d lines)", jellyfin_id, len(lines))

    def _reset_lyrics(self) -> None:
        """Release the overlay. Called from finalize, so lyrics never outlive
        the playback that fetched them — including the error and stale-play
        paths, which are the ones that would otherwise leave them stranded."""
        state.clear_lyrics()

    def _push_lyrics_to_tag(self, payload: JsonDict, jellyfin_id: str) -> None:
        text = lyrics_render.to_text(payload)
        if not text:
            return

        for attempt in range(LYRICS_PUSH_ATTEMPTS):
            if attempt:
                xbmc.sleep(int(LYRICS_PUSH_RETRY_SECONDS * 1000))
            # Built from the playing item so the path and the rest of the
            # music tag already match: Kodi accepts the update only for the
            # item it is playing, and applies the tag wholesale, so a partial
            # one would blank the now-playing display.
            push = self.getPlayingItem()
            # setInfo rather than InfoTagMusic.setLyrics: only setInfo marks
            # the tag loaded, and an unloaded tag gets re-read from the music
            # database on its way to the screen — which clears the lyrics,
            # there being no column to read them back from.
            push.setInfo("music", {"lyrics": text})
            push.setProperty("culrc.source", LYRICS_SOURCE)
            self.updateInfoTag(push)
            if xbmc.getInfoLabel("MusicPlayer.Lyrics").strip() == text.strip():
                LOG.info("--> lyrics %s (%d chars)", jellyfin_id, len(text))
                return

        LOG.debug("lyrics for %s did not land on the playing item", jellyfin_id)

    # -- kodi callbacks ------------------------------------------------------

    def onPlayBackStarted(self) -> None:
        # Before anything that can block this callback thread: a start that
        # must wait for a SyncPlay group is paused at its first instant, not
        # seconds later when the claim and round trip complete.
        self._syncplay_event("on_playback_started")
        self.finalize()  # a previous kofin play that never got its stop event
        # Ahead of the claim, and before anything else that blocks: the lyrics
        # addon searches on onAVStarted, so a round trip taken first has
        # already lost the race. After finalize only because that is what
        # releases the previous song's lyrics — it is a no-op on the normal
        # path, where the last playback stopped cleanly. See start_lyrics.
        self.start_lyrics()
        claimed = self._claim()
        if claimed is None:
            return
        with self._lock:
            self._item = claimed
        state.set_playing_id(claimed["Id"])
        self._publish_streams(claimed)
        LOG.info("--> play %s (%s)", claimed["Id"], claimed["PlayMethod"])
        self._report(self.api.session_playing, event=None)
        self._start_ticker()
        self._start_segment_engine(claimed)
        self._start_chapter_thumbs(claimed)

    def onAVStarted(self) -> None:
        """First frame rendered: the SyncPlay Ready trigger, and the earliest
        moment Kodi's stream lists are populated enough to pick from."""
        self._syncplay_event("on_avstarted")
        self.apply_default_tracks()

    def onPlayBackPaused(self) -> None:
        self._syncplay_event("on_paused")
        self._set_paused(True)

    def onPlayBackResumed(self) -> None:
        self._syncplay_event("on_resumed")
        self._set_paused(False)

    def onPlayBackSeek(self, time: int, seekOffset: int) -> None:
        self._syncplay_event("on_seek", time / 1000.0)
        if self._item is not None:
            self._update_position(time / 1000.0)
            self._report(self.api.session_progress, event="timeupdate")
            self.note_seek(time / 1000.0)

    def onPlayBackStopped(self) -> None:
        self._syncplay_event("on_stopped")
        self._finish()

    def onPlayBackEnded(self) -> None:
        self._syncplay_event("on_ended")
        self._finish()

    def onPlayBackError(self) -> None:
        self._syncplay_event("on_error")
        self.finalize()

    # -- reporting -----------------------------------------------------------

    def report_progress(self) -> None:
        """Ticker callback: refresh position and post progress."""
        if self._item is None:
            return
        try:
            self._update_position(self.getTime())
        except RuntimeError:  # nothing playing (race with stop)
            return
        self._report(self.api.session_progress, event="timeupdate")
        self._maybe_auto_next()

    def _maybe_auto_next(self) -> None:
        """W4.1: the 80% crossing of a downloaded episode queues the next
        keep-ahead. Runs on the ticker thread — the lookup is one bounded
        listing, and the ticker's next beat is ten seconds out anyway."""
        item = self._item
        if item is None or item.get("Type") != "Episode":
            return
        item_id = str(item.get("Id") or "")
        if not item_id or self._auto_next_latch == item_id:
            return
        runtime = float(item.get("Runtime") or 0) / 10_000_000
        position = float(item.get("CurrentPosition") or 0)
        if runtime <= 0 or position < runtime * downloads_auto.NEXT_TRIGGER_RATIO:
            return
        self._auto_next_latch = item_id
        try:
            downloads_auto.trigger_next(self.api, item)
        except Exception:  # pragma: no cover - never break the ticker
            LOG.exception("auto-next trigger failed for %s", item_id)

    def _finish(self) -> None:
        """A playback the viewer ended: report the stop, then make any
        finished-watching offer against what was playing.

        ``finalize`` clears ``_item``, so the item is captured first. Only the
        stop/end callbacks come through here — ``finalize``'s other caller
        (a stale play discovered at the next start) is cleanup, not a
        playback the viewer just finished.
        """
        item = self.current_item()
        self.finalize()
        if item is not None:
            if not self.offer_delete(item):
                # Never two stacked dialogs about one just-finished item:
                # the local-remove offer stands down when the server-delete
                # prompt ran (W4.5).
                self.offer_remove_download(item)

    def finalize(self) -> None:
        """Report the stop and release all playback state."""
        self._segment_reset()
        self._stop_ticker()
        self._stop_chapter_thumbs()
        self._reset_lyrics()
        self._auto_next_latch = ""
        with self._lock:
            item = self._item
            self._item = None
        if item is None:
            return
        LOG.info("<-- stop %s", item["Id"])
        stop_data = {
            "ItemId": item["Id"],
            "MediaSourceId": item["MediaSourceId"],
            "PlaySessionId": item["PlaySessionId"],
            "PositionTicks": int(item["CurrentPosition"] * 10_000_000),
        }
        close_encoding = item.get("PlayMethod") == "Transcode"
        device_id = item.get("DeviceId", "")
        play_session_id = item["PlaySessionId"]

        def post_stop() -> None:
            try:
                self.api.session_stopped(stop_data)
            except Exception as error:
                LOG.warning("stop report failed: %s", error)
                # The position is the user's viewing progress and this was
                # its only route to the server; park it for the next connect
                # rather than lose it (plan W2.4). Contained: a parking
                # failure must not skip the transcode close below.
                try:
                    from kofin.downloads import pending

                    pending.enqueue(
                        str(item["Id"]),
                        str(item.get("Type") or ""),
                        position_ticks=int(stop_data["PositionTicks"]),
                        snapshot=item.get("ServerUserData") or {},
                    )
                except Exception:  # pragma: no cover - defensive
                    LOG.exception("could not park the stop position")
            if close_encoding:
                try:
                    self.api.close_transcode(device_id, play_session_id)
                except Exception as error:
                    LOG.debug("close transcode failed: %s", error)

        self._reporter.submit(post_stop)
        state.clear_playing_id()
        state.clear_playing_streams()

    def stop_threads(self) -> None:
        """Service shutdown: stop the ticker and checker without reporting.

        Chapter thumbs are reverted too — the entries are per-play and this
        Player is their only owner, so they must not outlive it. The cost is
        a playback that survives a mid-play service restart losing its
        chapter tiles; the startup sweep would otherwise never dare touch
        them while that playback is live."""
        self._segment_reset()
        self._stop_ticker()
        self._stop_chapter_thumbs()
        self._reporter.stop()

    def submit_backfill(self, data: JsonDict) -> None:
        """Player.OnPlay's library-claim back-fill, off the notification
        thread: library_claim makes a server GET and opens kofin.db, and every
        song start with the server away blocked Kodi's notification dispatch
        for the transport's whole budget (audit finding #3). Rides the
        reporter so the claim lands before any report that follows it."""

        def run() -> None:
            try:
                backfill_library_claim(data, self.api)
            except Exception:
                LOG.exception("library claim back-fill failed")

        self._reporter.submit(run)

    # -- delete after watching -------------------------------------------------

    def offer_delete(self, item: JsonDict) -> bool:
        """Ask whether to delete an item the viewer just finished.

        Returns whether the prompt was raised. Both settings must be on: the
        Advanced tab's delete opt-in owns whether kofin deletes anything at
        all, and this is a sub-option of it.
        """
        if item.get("Type") not in DELETABLE_TYPES or not item.get("Id"):
            return False
        if not settings.get_bool("enableDelete"):
            return False
        if not settings.get_bool("deleteAfterWatching"):
            return False
        if not watched_to_end(item):
            return False
        if not item.get("CanDelete"):
            # The server's own answer for this account. Without it an account
            # with no EnableContentDeletion was asked "delete this?" after
            # every single episode, and told "Server request failed" every
            # time it said yes.
            return False
        # Off the Kodi callback thread: this dialog waits on a person, and
        # blocking that thread stalls the player callbacks behind it (Play
        # Next's handoff to the following episode arrives on it). Daemon, so a
        # prompt left open cannot hold up service shutdown.
        threading.Thread(
            target=self._delete_prompt,
            args=(item,),
            name="kofin-delete-prompt",
            daemon=True,
        ).start()
        return True

    def offer_remove_download(self, item: JsonDict) -> bool:
        """W4.5: the local sibling of ``offer_delete`` — a *user-origin*
        download watched to the end is offered for removal (Ask) or removed
        outright (Always). Auto-origin items belong to the retention sweep,
        which would otherwise race this prompt for the same file."""
        mode = settings.get_str("downloadsDeleteAfterPlay")
        if mode not in ("ask", "always"):
            return False
        if not settings.get_bool("downloadsEnabled"):
            return False
        item_id = str(item.get("Id") or "")
        if not item_id or not watched_to_end(item):
            return False
        from kofin.downloads import store as downloads_store

        row = downloads_store.get(item_id)
        if (
            row is None
            or row.state != downloads_store.DONE
            or downloads_store.is_auto_origin(row.origin)
        ):
            return False
        if mode == "always":
            LOG.info("removing watched download %s (always mode)", item_id)
            ipc.notify(ipc.DOWNLOAD_REMOVE, {"Id": item_id})
            return True
        # The same thread shape as the delete prompt: a dialog waits on a
        # person, and Kodi's callback thread must never wait with it.
        threading.Thread(
            target=self._remove_download_prompt,
            args=(item_id, str(item.get("Name") or "")),
            name="kofin-remove-download-prompt",
            daemon=True,
        ).start()
        return True

    def _remove_download_prompt(self, item_id: str, name: str) -> None:
        if not xbmcgui.Dialog().yesno(
            settings.localized(30710),  # Remove download
            settings.localized(30714) % name,
        ):
            return
        ipc.notify(ipc.DOWNLOAD_REMOVE, {"Id": item_id})

    def _delete_prompt(self, item: JsonDict) -> None:
        name = item.get("Name") or ""
        if not xbmcgui.Dialog().yesno(
            xbmc.getLocalizedString(117),  # Delete
            settings.localized(30506) % name,
        ):
            return
        try:
            self.api.delete_item(str(item["Id"]))
        except Exception as error:
            LOG.warning("delete after watching failed: %s", error)
            self._notify(settings.localized(30507), toast.ERROR)
            return
        # No refresh from here: the server's own library-changed event drives
        # the removal out of the Kodi database through the normal sync path.
        LOG.info("deleted %s after watching", item["Id"])

    # -- stream selection ------------------------------------------------------

    def _publish_streams(self, item: JsonDict) -> None:
        """Hand the claimed playback's streams to the context item.

        The play route resolved them; this moves them somewhere a later plugin
        invocation can read, and states in one word what the menu should
        offer so addon.xml can pick a label (see core/state.py). Never raises:
        a menu that fails to appear must not take the playback with it.
        """
        try:
            payload = dict(item.get("Streams") or {})
            if not payload:
                state.clear_playing_streams()
                return
            media_streams = payload.get("MediaStreams") or []
            attached = payload.get("Attached") or []
            method = str(item.get("PlayMethod") or "")
            payload["PlayMethod"] = method
            payload["Id"] = item.get("Id", "")
            payload["MediaSourceId"] = item.get("MediaSourceId", "")
            payload["AudioStreamIndex"] = item.get("AudioStreamIndex")
            # Both indices, not just audio: a burned-in subtitle is not a Kodi
            # track, so this is the only thing that tells the menu which row is
            # the one in the picture (core/streams.burned_subtitle).
            payload["SubtitleStreamIndex"] = item.get("SubtitleStreamIndex")
            offer = streams.menu_offer(media_streams, attached, method)
            if self._syncplay_group_active:
                offer = streams.OFFER_NONE
            state.publish_playing_streams(payload, offer)
        except Exception:
            LOG.exception("publishing playing streams failed")

    def apply_default_tracks(self) -> None:
        """Start on the tracks the Jellyfin *user profile* nominates.

        Kodi otherwise picks from its own language settings, so a viewer whose
        Jellyfin account asks for Japanese audio with English subtitles got
        neither. The server has already resolved both against that account and
        returns them on every MediaSource; nothing had ever applied them.

        Runs at first frame rather than at claim because Kodi's stream lists
        do not exist until then — and because running here cannot delay the
        picture, only follow it. Never raises.
        """
        if not settings.get_bool("honourJellyfinDefaultTracks"):
            return
        item = self.current_item()
        if item is None:
            return
        try:
            self._apply_default_tracks(item)
        except Exception:
            LOG.exception("applying Jellyfin default tracks failed")

    def _apply_default_tracks(self, item: JsonDict) -> None:
        payload = item.get("Streams") or {}
        media_streams = payload.get("MediaStreams") or []
        attached = payload.get("Attached") or []
        method = str(item.get("PlayMethod") or "")

        # Audio only on a direct play: a transcode carries the single track the
        # server already encoded to our request, so there is nothing to choose
        # and Kodi's only index is 0.
        if streams.is_direct(method):
            ordinal = streams.audio_ordinal(media_streams, item.get("AudioStreamIndex"))
            if ordinal is not None:
                self.setAudioStream(ordinal)
                LOG.info("--> audio track %s (Jellyfin default)", ordinal)

        wanted = item.get("SubtitleStreamIndex")
        if wanted is None or int(wanted) < 0:
            # The profile asks for no subtitle. Say so rather than leaving
            # whatever Kodi auto-selected running.
            self.showSubtitles(False)
            return
        if streams.burned_subtitle(media_streams, wanted, method):
            # It is already in the picture. Anything Kodi shows on top of it is
            # a second subtitle over the first, which is what an auto-selected
            # attached track would be here.
            self.showSubtitles(False)
            LOG.info("--> subtitle %s is burned in; Kodi's own left off", wanted)
            return
        ordinal = streams.subtitle_ordinal(media_streams, wanted, attached, method)
        if ordinal is None:
            return
        self.setSubtitleStream(ordinal)
        self.showSubtitles(True)
        LOG.info("--> subtitle track %s (Jellyfin default)", ordinal)

    # -- segment engine: lifecycle -------------------------------------------

    def _start_segment_engine(self, item: JsonDict) -> None:
        if item.get("Type") not in ("Movie", "Episode"):
            return
        segments_enabled = settings.get_bool("mediaSegmentsEnabled")
        play_next = (
            settings.get_bool("playNextEnabled") and item.get("Type") == "Episode"
        )
        if not segments_enabled and not play_next:
            return
        self._segment_reset()
        prefetched = item.get("Segments")
        if not segments_enabled:
            self._segments_loaded = True  # engine runs for Play Next only
        elif isinstance(prefetched, list):
            # Warm fetch: the play path piggybacked the segments (plan §2d),
            # so the engine is armed before the first frame renders.
            self._segments = sorted(
                (
                    segment
                    for segment in prefetched
                    if isinstance(segment, dict)
                    and segment.get("Type") in SEGMENT_MODE_SETTINGS
                    and float(segment.get("End") or 0)
                    > float(segment.get("Start") or 0)
                ),
                key=lambda segment: float(segment["Start"]),
            )
            self._segments_loaded = True
        self._fresh_start = True  # ignore a stale pre-switch position (see tick)
        self._checker = SegmentChecker(self)
        self._checker.start()

    def prepare_segment_state(self, halt: threading.Event) -> None:
        """Checker-thread setup: warm-fetch fallback and next-episode
        resolution. Ticks no-op until the segments are loaded (plan §2d).

        Every assignment re-checks ``halt`` and the claimed item: a slow
        fetch must never land its result on a playback that superseded the
        one it was started for.
        """
        item = self._item
        if item is None:
            return
        if not self._segments_loaded:
            if state.is_offline():
                # No one to ask, and the transport ladder would park this
                # thread for nothing. A downloaded play carries its cache in
                # the claim, so arriving here offline means there is none.
                self._segments = []
                self._segments_loaded = True
                return
            segments: List[JsonDict] = []
            for attempt in (1, 2):  # short bounded retry (plan §7)
                if halt.is_set():
                    return
                try:
                    segments = parse_segments(self.api.media_segments(item["Id"]))
                    break
                except Exception as error:
                    LOG.warning(
                        "media segments fetch failed (try %d): %s", attempt, error
                    )
                    if halt.wait(1.0):
                        return
            if halt.is_set() or self._item is not item:
                return
            self._segments = segments
            self._segments_loaded = True
            if segments:
                LOG.info(
                    "segments for %s: %s",
                    item["Id"],
                    [segment["Type"] for segment in segments],
                )
        if (
            item.get("Type") == "Episode"
            and item.get("SeriesId")
            and settings.get_bool("playNextEnabled")
            and not state.is_offline()  # adjacency is a server lookup
        ):
            nxt = self._resolve_next_episode(item)
            if halt.is_set() or self._item is not item:
                return
            self._next_episode = nxt

    def _resolve_next_episode(self, item: JsonDict) -> Optional[JsonDict]:
        """The episode after the playing one (fork ``next_up`` adjacency)."""
        try:
            listing = (
                self.api.adjacent_episodes(item["SeriesId"], item["Id"]).get("Items")
                or []
            )
        except Exception as error:
            LOG.warning("next episode resolution failed: %s", error)
            return None
        for index, episode in enumerate(listing):
            if episode.get("Id") == item["Id"]:
                if index + 1 < len(listing):
                    nxt: JsonDict = listing[index + 1]
                    LOG.info("next episode resolved: %s", nxt.get("Id"))
                    return nxt
                return None  # season/series finale
        return None

    def _segment_reset(self) -> None:
        self._stop_checker()
        self._close_overlay()
        self._segments = []
        self._segments_loaded = False
        self._armed_index = 0
        self._prompted = set()
        self._start_inside = set()
        self._prev_pos = None
        self._settle_target = None
        self._settle_ticks = 0
        self._pending_jump = False
        self._next_episode = None
        self._runtime = 0.0
        self._near_end_at = None
        self._near_end_prompted = False
        self._skip_target = None
        self._fresh_start = False
        self._fresh_start_ticks = 0

    def _stop_checker(self) -> None:
        checker = self._checker
        self._checker = None
        if checker is not None:
            checker.stop()

    # -- segment engine: the tick --------------------------------------------

    def segment_tick(self) -> None:
        """One 0.25 s engine step (runs on the checker thread only)."""
        if self._item is None:
            return
        try:
            now = float(self.getTime())
        except RuntimeError:  # nothing playing yet / race with stop
            return

        self._drive_overlay(now)

        if not self._segments_loaded:
            self._prev_pos = now
            return

        if self._fresh_start:
            # Hold off arming until the position getTime() reports is the one
            # this playback was resolved to start at; anything else is a
            # phantom that fires segments nobody is anywhere near.
            expected = float((self._item or {}).get("CurrentPosition") or 0.0)
            self._fresh_start_ticks += 1
            if (
                abs(now - expected) > FRESH_START_TOLERANCE
                and self._fresh_start_ticks < FRESH_START_MAX_TICKS
            ):
                self._prev_pos = now
                return
            self._fresh_start = False
            self._prev_pos = None  # no crossing credit from the stale position
            # ``expected`` (the position the play route resolved) rather than
            # ``now``: it is exact, where ``now`` carries whatever keyframe the
            # resume seek snapped back to.
            self._start_inside = segments_entered_at(self._segments, expected)

        if self._runtime <= 0:
            self._runtime = self._live_runtime()
            if self._runtime > 0:
                self._compute_near_end()

        if self._settle_target is not None:
            # Post-seek settle: a lagging getTime() must not re-trigger the
            # segment we just skipped (plan §2f).
            self._settle_ticks -= 1
            if now >= self._settle_target - 0.5:
                # The seek landed. Toast now (not before — a seek issued at
                # t~=0 can be silently dropped) and release the settle.
                if self._pending_notify is not None:
                    self._notify(settings.localized(30488) % self._pending_notify)
                    self._pending_notify = None
                self._settle_target = None
                self._resync(now)
                self._prev_pos = now
            elif self._settle_ticks <= 0:
                if self._settle_retries > 0:
                    # Seek dropped (player not yet seekable at t~=0): re-issue.
                    self._settle_retries -= 1
                    self._settle_ticks = SEEK_SETTLE_TICKS
                    LOG.info(
                        "auto-skip seek retry -> %.1f (still at %.1f)",
                        self._settle_target,
                        now,
                    )
                    self.seekTime(self._settle_target)
                else:
                    # Gave up: the skip never took, so no toast.
                    self._pending_notify = None
                    self._settle_target = None
                    self._resync(now)
                    self._prev_pos = now
            return

        if self._pending_jump:
            self._pending_jump = False
            self._resync(now)
            self._prev_pos = None  # no crossing credit across a user seek

        self._check_armed(now)
        self._check_near_end(now)
        self._prev_pos = now

    def note_seek(self, target: float) -> None:
        """Player-seek hook: user seeks re-arm the engine; the echo of our own
        skip seek (same target as the settle window) is ignored."""
        settle = self._settle_target
        if settle is not None and abs(target - settle) < 2.0:
            return
        self._settle_target = None
        self._pending_jump = True

    def _resync(self, now: float) -> None:
        """Recompute the armed boundary and the recoverable dedup after a
        position jump: only segments still containing ``now`` stay consumed."""
        segments = self._segments
        self._armed_index = next(
            (
                index
                for index, segment in enumerate(segments)
                if float(segment["End"]) >= now
            ),
            len(segments),
        )
        self._prompted = {key for key in self._prompted if key[0] <= now <= key[1]}
        self._start_inside = {
            key for key in self._start_inside if key[0] <= now <= key[1]
        }
        if self._near_end_at is not None and now < self._near_end_at:
            self._near_end_prompted = False
        # An overlay whose firing window the jump left is stale — close it
        # (the pruned dedup re-offers it on the next crossing).
        window = self._overlay_window
        if (
            self._overlay is not None
            and window is not None
            and not (window[0] <= now <= window[1])
        ):
            self._close_overlay()

    def _check_armed(self, now: float) -> None:
        """Evaluate the pre-armed boundary (one compare per tick in the steady
        state; amortized O(1) advancement past consumed segments)."""
        segments = self._segments
        prev = self._prev_pos
        index = self._armed_index
        while index < len(segments):
            segment = segments[index]
            start = float(segment["Start"])
            end = float(segment["End"])
            key = (start, end)
            if crossed_into(prev, now, start, end):
                if key not in self._prompted:
                    self._prompted.add(key)
                    self._fire_segment(segment, now)
                if now <= end:
                    break  # stay armed on this segment until we pass it
            if now > end:
                self._prompted.discard(key)  # left it: re-arm for a seek back
                self._start_inside.discard(key)
                index += 1
                continue
            break  # segment still ahead
        self._armed_index = index

    def _check_near_end(self, now: float) -> None:
        if self._near_end_at is None or self._near_end_prompted:
            return
        if self._overlay is not None:
            return  # never two overlays at once
        if crossed_into(self._prev_pos, now, self._near_end_at, self._runtime):
            self._near_end_prompted = True
            LOG.info("near-end Play Next prompt at %.1f", now)
            self._open_overlay(None, ("playnext", "close"))

    def _compute_near_end(self) -> None:
        """Arm the no-credits-segment Play Next prompt once runtime is known."""
        self._near_end_at = None
        if not self._play_next_on_offer():
            return
        if any(segment["Type"] == "Credits" for segment in self._segments):
            return  # the credits crossing owns the Play Next moment
        lead = float(settings.get_int("playNextLeadTime") or 30)
        self._near_end_at = near_end_prompt_at(self._runtime, lead)

    # -- segment engine: firing ----------------------------------------------

    def _fire_segment(self, segment: JsonDict, now: float) -> None:
        segment_type = str(segment["Type"])
        mode = self._segment_mode(segment_type)
        offer_next = segment_type == "Credits" and self._play_next_on_offer()
        auto_seek, buttons = plan_for_crossing(segment_type, mode, offer_next)
        LOG.info(
            "segment %s [%.1f-%.1f] at %.2f: mode=%d auto=%s buttons=%s",
            segment_type,
            segment["Start"],
            segment["End"],
            now,
            mode,
            auto_seek,
            buttons,
        )
        if auto_seek:
            self._auto_skip(segment, now)
        started_inside = (
            float(segment["Start"]),
            float(segment["End"]),
        ) in self._start_inside
        if started_inside or now >= float(segment["End"]) - 0.25:
            # No skip button. Either the crossing already stepped past the
            # boundary, or playback *started* inside this segment — a resume
            # point mid-intro, where the viewer asked to pick up exactly here
            # and all the prompt does is flash and auto-close. Auto-skip is
            # untouched ("always skip intros" must not lapse because a resume
            # landed in one) and a Play Next offer still stands.
            buttons = tuple(button for button in buttons if button != "skip")
        if any(button in ("skip", "playnext") for button in buttons):
            self._open_overlay(segment, buttons)

    def _auto_skip(self, segment: JsonDict, now: float) -> None:
        target = safe_seek_end(segment["End"], self._runtime_for_seek(), now)
        if target is None:
            return
        # The toast is deferred to the tick that confirms the seek landed, so a
        # dropped t~=0 seek never reports a skip that did not happen.
        self._begin_settle(target, notify=str(segment["Type"]))
        self.seekTime(target)
        LOG.info("auto-skip %s -> %.1f", segment["Type"], target)

    def _segment_mode(self, segment_type: str) -> int:
        setting_id = SEGMENT_MODE_SETTINGS.get(segment_type)
        if not setting_id or not settings.get_bool("mediaSegmentsEnabled"):
            return MODE_OFF
        return settings.get_int(setting_id)

    def _play_next_on_offer(self) -> bool:
        return (
            self._next_episode is not None
            and not self.syncplay_group_active
            and settings.get_bool("playNextEnabled")
        )

    def _begin_settle(self, target: float, notify: Optional[str] = None) -> None:
        self._settle_target = target
        self._settle_ticks = SEEK_SETTLE_TICKS
        self._settle_retries = SEEK_RETRIES
        self._pending_notify = notify

    def _live_runtime(self) -> float:
        try:
            total = float(self.getTotalTime())
            if total > 0:
                return total
        except RuntimeError:
            pass
        item = self._item
        if item is not None:
            return float(item.get("Runtime") or 0) / 10_000_000
        return 0.0

    def _runtime_for_seek(self) -> float:
        return self._runtime if self._runtime > 0 else self._live_runtime()

    # -- segment engine: the overlay -----------------------------------------

    def _open_overlay(
        self, segment: Optional[JsonDict], buttons: Tuple[str, ...]
    ) -> None:
        from kofin.plugin import skip as skip_dialog

        self._close_overlay()
        offers_next = "playnext" in buttons
        show_skip = "skip" in buttons and segment is not None

        skip_label = ""
        self._skip_target = None
        if show_skip and segment is not None:
            skip_label = settings.localized(
                SKIP_LABEL_IDS.get(str(segment["Type"]), 30481)
            )
            self._skip_target = float(segment["End"])

        next_label = settings.localized(30486) if offers_next else ""
        next_info = ""
        if offers_next and self._next_episode is not None:
            next_info = settings.localized(30489) % next_episode_label(
                self._next_episode
            )

        # A Play Next offer persists to the end of the video; a pure skip
        # overlay auto-closes past its segment end.
        if offers_next or segment is None:
            self._overlay_end = self._runtime
        else:
            self._overlay_end = float(segment["End"])
        window_start = (
            float(segment["Start"]) if segment is not None else self._near_end_at or 0.0
        )
        self._overlay_window = (window_start, self._overlay_end)
        self._overlay_autoplay = offers_next and settings.get_bool("playNextAutoplay")

        try:
            self._overlay = skip_dialog.open_overlay(
                skip_label,
                next_label,
                next_info,
                self._overlay_skip if show_skip else None,
                self._overlay_play_next if offers_next else None,
            )
        except Exception:
            LOG.exception("overlay open failed")
            self._overlay = None

    def _drive_overlay(self, now: float) -> None:
        overlay = self._overlay
        if overlay is None:
            return
        if getattr(overlay, "closed", False):
            self._overlay = None  # a button or back closed it on the GUI thread
            return
        if self._overlay_autoplay and self._overlay_end > 0:
            remaining = self._overlay_end - now
            try:
                overlay.set_countdown(max(0, int(round(remaining))))
            except Exception:
                pass
            if remaining <= AUTOPLAY_MARGIN_SECONDS:
                self._close_overlay()
                self._start_next_episode()
                return
        if 0 < self._overlay_end <= now:
            self._close_overlay()

    def _close_overlay(self) -> None:
        overlay = self._overlay
        self._overlay = None
        self._overlay_window = None
        if overlay is not None:
            try:
                overlay.close()
            except Exception:
                pass

    # Overlay button callbacks (run on Kodi's GUI thread).

    def _overlay_skip(self) -> None:
        target = self._skip_target
        if target is None:
            return
        try:
            now: Optional[float] = float(self.getTime())
        except RuntimeError:
            now = None
        seek_to = safe_seek_end(target, self._runtime_for_seek(), now)
        if seek_to is None:
            return
        self._begin_settle(seek_to)
        self.seekTime(seek_to)
        LOG.info("user skipped to %.1f", seek_to)

    def _overlay_play_next(self) -> None:
        self._start_next_episode()

    def _start_next_episode(self) -> None:
        nxt = self._next_episode
        if nxt is None or not nxt.get("Id"):
            return
        from kofin.plugin.listitems import plugin_url

        LOG.info("play next episode %s", nxt.get("Id"))
        # Play Next always starts the next episode from the beginning — never at
        # a stale server-side resume point, which would drop the viewer inside
        # the credits (skipping the outro, not the intro).
        url = plugin_url({"mode": "play", "id": str(nxt.get("Id")), "fromstart": "1"})
        xbmc.executebuiltin('PlayMedia("%s")' % url)

    def _notify(self, message: str, level: str = toast.INFO) -> None:
        toast.show(message, level, time_ms=3000)

    # -- internals -------------------------------------------------------------

    def _claim(self) -> Optional[JsonDict]:
        monitor = xbmc.Monitor()
        waited = 0.0
        while waited < CLAIM_TIMEOUT_SECONDS:
            try:
                current_file = self.getPlayingFile()
            except RuntimeError:
                current_file = ""
            if current_file:
                claimed = state.claim_play_item(current_file)
                if claimed is not None:
                    return claimed
                # Two kinds of playback never pass through the play route
                # and get their claim back-filled from the Player.OnPlay
                # notification instead, which can land after this callback:
                # songs written as direct ``<server>/Audio/<id>/`` stream
                # URLs, and downloaded video whose row is a local file
                # (W1.7). Wait a beat for the back-fill rather than calling
                # either foreign playback.
                if (
                    (self.isPlayingAudio() or _downloaded_path(current_file))
                    and waited < BACKFILL_GRACE_SECONDS
                    and not monitor.waitForAbort(0.25)
                ):
                    waited += 0.25
                    continue
                # A file is playing but nothing is queued: foreign playback.
                return None
            if monitor.waitForAbort(0.5):
                return None
            waited += 0.5
        return None

    def _set_paused(self, paused: bool) -> None:
        if self._item is None:
            return
        self._item["Paused"] = paused
        self._report(self.api.session_progress, event="pause" if paused else "unpause")

    def _update_position(self, seconds: float) -> None:
        if self._item is not None and seconds >= 0:
            self._item["CurrentPosition"] = seconds

    def _report(self, poster: Any, event: Optional[str]) -> None:
        item = self._item
        if item is None:
            return
        if state.is_offline():
            # Offline claims exist for the local machinery (W4.7); a session
            # post every ten seconds would ride the ladder to nowhere for a
            # session the server cannot see. The stop report is not gated —
            # its failure path parks the position for the replay (W2.4).
            return
        volume, muted = _volume_state()
        data: JsonDict = {
            "QueueableMediaTypes": "Video,Audio",
            "CanSeek": True,
            "ItemId": item["Id"],
            "MediaSourceId": item["MediaSourceId"],
            "PlayMethod": item["PlayMethod"],
            "PlaySessionId": item["PlaySessionId"],
            "PositionTicks": int(item["CurrentPosition"] * 10_000_000),
            "IsPaused": bool(item.get("Paused")),
            "IsMuted": muted,
            "VolumeLevel": volume,
            "AudioStreamIndex": item.get("AudioStreamIndex"),
            "SubtitleStreamIndex": item.get("SubtitleStreamIndex"),
        }
        if event:
            data["EventName"] = event
        # Payload is complete; the post itself must not run on this thread —
        # for callbacks it is Kodi's announcement thread (see _Reporter).
        self._reporter.submit(lambda: poster(data))

    def _start_ticker(self) -> None:
        self._stop_ticker()
        self._ticker = _Ticker(self)
        self._ticker.start()

    def _stop_ticker(self) -> None:
        if self._ticker is not None:
            self._ticker.stop()
            self._ticker = None

    def _start_chapter_thumbs(self, item: JsonDict) -> None:
        if not settings.get_bool("chapterImages"):
            return
        if not chapters.eligible(item):
            return
        self._stop_chapter_thumbs()  # belt: no owner leaks across claims
        self._chapter_thumbs = chapters.ChapterThumbs(self.api, item)
        self._chapter_thumbs.start()

    def _stop_chapter_thumbs(self) -> None:
        if self._chapter_thumbs is not None:
            self._chapter_thumbs.stop()
            self._chapter_thumbs = None


class _Ticker(threading.Thread):
    def __init__(self, player: Player) -> None:
        super().__init__(name="kofin-progress")
        self._player = player
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()
        if self.is_alive():
            self.join(timeout=5)

    def run(self) -> None:
        while not self._halt.wait(PROGRESS_INTERVAL_SECONDS):
            try:
                self._player.report_progress()
            except Exception as error:  # pragma: no cover - defensive
                LOG.warning("progress tick failed: %s", error)


def _volume_state() -> "tuple[int, bool]":
    try:
        response = json.loads(
            xbmc.executeJSONRPC(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "Application.GetProperties",
                        "params": {"properties": ["volume", "muted"]},
                    }
                )
            )
        )
        result = response.get("result", {})
        return int(result.get("volume", 100)), bool(result.get("muted", False))
    except Exception:
        return 100, False
