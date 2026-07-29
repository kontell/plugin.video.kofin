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
import re
import threading
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4

import xbmc
import xbmcgui

from kofin.core import lyrics as lyrics_render
from kofin.core import playback, settings, state, streammaps, toast
from kofin.core.api import Api
from kofin.core.http import JellyfinError
from kofin.core.log import Logger
from kofin.service.segments import SegmentChecker, parse_segments

if TYPE_CHECKING:
    from kofin.syncplay.manager import SyncPlayManager

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

PROGRESS_INTERVAL_SECONDS = 10.0
CLAIM_TIMEOUT_SECONDS = 10.0
# Grace for a claim that arrives from the Player.OnPlay notification instead
# of the play route (see backfill_library_claim). Only library-originated
# audio needs it, and only until the notification lands.
AUDIO_BACKFILL_GRACE_SECONDS = 3.0

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


# Kodi media types whose rows may be written with a direct stream URL rather
# than a plugin:// path, so playback from the library never reaches the play
# route. Songs are written either way depending on ``musicTranscode``, which is
# why the back-fill checks the play queue before claiming (see below).
BACKFILL_MEDIA_TYPES = ("song",)


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
    """
    try:
        kodi_id = item.getMusicInfoTag().getDbId()
    except Exception:  # pragma: no cover - defensive, tag may be absent
        kodi_id = 0

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


def backfill_library_claim(data: JsonDict, api: Api) -> bool:
    """Queue a claim for library playback that bypassed the play route.

    Driven by the ``Player.OnPlay`` notification; True when a claim was
    pushed. Only the media types in ``BACKFILL_MEDIA_TYPES`` qualify — video
    always goes through ``plugin://`` and is claimed the normal way, so
    back-filling it would risk double-claiming a legitimate play.
    """
    item = data.get("item") or {}
    media = item.get("type") or ""
    kodi_id = item.get("id")

    if media not in BACKFILL_MEDIA_TYPES or not isinstance(kodi_id, int):
        return False

    try:
        path = xbmc.Player().getPlayingFile()
    except RuntimeError:
        return False

    if not path:
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

    claim = library_claim(jellyfin_id, path, api)
    if claim is None:
        return False

    LOG.info("--> library claim %s (%s)", claim["Id"], media)
    state.push_play_item(claim)
    return True


class Player(xbmc.Player):
    def __init__(self, api: Api) -> None:
        super().__init__()
        self.api = api
        self._item: Optional[JsonDict] = None
        self._ticker: Optional[_Ticker] = None
        self._lock = threading.Lock()
        # Driven by SyncPlay (phase 4); while True, Play Next is withheld —
        # the group queue is authoritative.
        self.syncplay_group_active = False
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
        self._runtime = 0.0
        self._near_end_at: Optional[float] = None
        self._near_end_prompted = False
        self._overlay: Optional[Any] = None
        self._overlay_end = 0.0
        self._overlay_window: Optional[Tuple[float, float]] = None
        self._overlay_autoplay = False
        self._skip_target: Optional[float] = None
        # Remote SetAudio/SetSubtitle (PR3a/3b): worker threads only; never the
        # websocket callback thread.
        self._stream_switch_lock = threading.Lock()
        # Mid-play PlaybackInfo restart (PR3b).
        self._stream_restart = False
        self._restart_teardown_done = False
        self._restart_target_pos: Optional[float] = None

    # -- remote stream switch (PR3a local / PR3b restart) -----------------------

    def enqueue_stream_switch(self, kind: str, jellyfin_index: Optional[int]) -> None:
        """Queue a remote stream change off the websocket thread."""
        threading.Thread(
            target=self._stream_switch_worker,
            args=(kind, jellyfin_index),
            name="kofin-stream-switch",
            daemon=True,
        ).start()

    def _stream_switch_worker(self, kind: str, jellyfin_index: Optional[int]) -> None:
        try:
            self.apply_stream_switch(kind, jellyfin_index)
        except Exception:
            LOG.exception("stream switch failed (%s %s)", kind, jellyfin_index)

    def apply_stream_switch(self, kind: str, jellyfin_index: Optional[int]) -> bool:
        """Apply a Jellyfin stream index locally or via PlaybackInfo restart."""
        with self._stream_switch_lock:
            item = self._item
            if item is None:
                LOG.info("stream switch ignored: nothing playing")
                return False
            if self.syncplay_group_active:
                LOG.info("stream switch refused: SyncPlay group active")
                toast.show(
                    "Stream changes are disabled while SyncPlay is active",
                    toast.WARNING,
                    time_ms=3000,
                )
                return False
            action, kodi_index, reason = playback.resolve_local_stream_switch(
                item, kind=kind, jellyfin_index=jellyfin_index
            )
            if action == "refuse":
                LOG.info("stream switch refused: %s", reason)
                return False
            if action == "needs_restart":
                LOG.info("stream switch restart: %s", reason)
                return self._restart_for_stream_switch(item, kind, jellyfin_index)
            try:
                if action == "audio" and kodi_index is not None:
                    self.setAudioStream(int(kodi_index))
                    item["AudioStreamIndex"] = jellyfin_index
                elif action == "subtitle" and kodi_index is not None:
                    self.setSubtitleStream(int(kodi_index))
                    self.showSubtitles(True)
                    item["SubtitleStreamIndex"] = jellyfin_index
                elif action == "subtitle_off":
                    self.showSubtitles(False)
                    item["SubtitleStreamIndex"] = None
                else:
                    LOG.info("stream switch no-op: %s", reason)
                    return False
            except RuntimeError as error:
                LOG.warning("Kodi stream apply failed: %s", error)
                return False
            LOG.info(
                "stream switch ok %s jf=%s kodi=%s", kind, jellyfin_index, kodi_index
            )
            # Progress immediately so the dashboard tracks the remote command.
            self._report(self.api.session_progress, event="timeupdate")
            return True

    def _restart_for_stream_switch(
        self, item: JsonDict, kind: str, jellyfin_index: Optional[int]
    ) -> bool:
        """Position-preserving PlaybackInfo restart (design §6.3)."""
        pos = float(item.get("CurrentPosition") or 0.0)
        try:
            pos = max(pos, float(self.getTime()))
        except RuntimeError:
            pass

        audio_index = item.get("AudioStreamIndex")
        subtitle_index = item.get("SubtitleStreamIndex")
        if kind == "audio":
            audio_index = jellyfin_index
        elif kind == "subtitle":
            subtitle_index = jellyfin_index if (jellyfin_index or 0) >= 0 else None

        force_transcode = bool(item.get("ForceTranscode"))
        bitrate_override = float(item.get("BitrateOverrideMbps") or 0.0)
        # HLS: StartTimeTicks positions the playlist; DirectStream uses client
        # seek after AV start. Always pass ticks — static ignores them for seek.
        start_ticks = int(pos * 10_000_000)

        try:
            url, method, source, play_session_id, _profile = (
                playback.resolve_restart_stream(
                    self.api,
                    item_id=str(item["Id"]),
                    media_source_id=str(item.get("MediaSourceId") or ""),
                    device_id=str(item.get("DeviceId") or ""),
                    force_transcode=force_transcode,
                    bitrate_override_mbps=bitrate_override,
                    audio_index=int(audio_index) if audio_index is not None else None,
                    subtitle_index=(
                        int(subtitle_index) if subtitle_index is not None else None
                    ),
                    start_ticks=start_ticks,
                )
            )
        except Exception as error:
            LOG.warning("stream restart resolve failed: %s", error)
            toast.show("Could not change stream", toast.ERROR, time_ms=3000)
            return False

        # Deliberate single session teardown before Player.play.
        self._stop_ticker()
        self._stream_restart = True
        self._restart_target_pos = pos
        self._teardown_session_for_restart(item, pos)
        self._restart_teardown_done = True

        from kofin.plugin import listitems, play as play_mod

        dto = item
        try:
            dto = self.api.item(str(item["Id"]))
        except Exception as error:
            LOG.debug("restart item fetch failed, using play-state: %s", error)

        sub_paths, sub_fields = play_mod.attach_text_subtitles(
            self.api, source, play_session_id
        )
        # No resume point on the listitem — seek / StartTimeTicks own position.
        li = listitems.build(dto, self.api.server, resume_seconds=0)
        li.setPath(url)
        mime = playback.mime_for(source, method)
        if mime:
            li.setMimeType(mime)
        li.setContentLookup(False)
        if sub_paths:
            li.setSubtitles(sub_paths)

        play_item = play_mod.play_state(
            dto,
            source,
            url,
            method,
            play_session_id,
            str(item.get("DeviceId") or ""),
            pos,
            subtitle_fields=sub_fields or None,
            force_transcode=force_transcode,
            bitrate_override_mbps=bitrate_override,
        )
        if audio_index is not None:
            play_item["AudioStreamIndex"] = audio_index
        if kind == "subtitle":
            play_item["SubtitleStreamIndex"] = subtitle_index
        if item.get("Segments") is not None:
            play_item["Segments"] = item.get("Segments")
        play_item["Name"] = item.get("Name") or play_item.get("Name")
        play_item["SeriesId"] = item.get("SeriesId") or play_item.get("SeriesId")

        state.drop_play_items_for_id(str(item["Id"]))
        state.push_play_item(play_item)
        with self._lock:
            self._item = None

        LOG.info(
            "stream restart play %s via %s at %.1fs (audio=%s sub=%s)",
            item["Id"],
            method,
            pos,
            audio_index,
            subtitle_index,
        )
        try:
            self.play(url, li)
        except Exception as error:
            LOG.warning("Player.play after restart failed: %s", error)
            self._stream_restart = False
            self._restart_teardown_done = False
            self._restart_target_pos = None
            return False
        return True

    def _teardown_session_for_restart(self, item: JsonDict, pos: float) -> None:
        """One session_stopped + close_transcode for the old PlaySessionId."""
        LOG.info("<-- stop (restart) %s @ %.1fs", item["Id"], pos)
        try:
            self.api.session_stopped(
                {
                    "ItemId": item["Id"],
                    "MediaSourceId": item["MediaSourceId"],
                    "PlaySessionId": item["PlaySessionId"],
                    "PositionTicks": int(pos * 10_000_000),
                }
            )
        except Exception as error:
            LOG.warning("restart stop report failed: %s", error)
        if item.get("PlayMethod") == "Transcode":
            try:
                self.api.close_transcode(item["DeviceId"], item["PlaySessionId"])
            except Exception as error:
                LOG.debug("restart close_transcode failed: %s", error)
        try:
            from kofin.core.subtitles import cleanup_session_subs

            cleanup_session_subs(item.get("PlaySessionId"))
        except Exception as error:  # pragma: no cover
            LOG.debug("restart sub cleanup failed: %s", error)

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
        was_restart = self._stream_restart
        self.finalize()  # a previous kofin play that never got its stop event
        # Ahead of the claim, and before anything else that blocks: the lyrics
        # addon searches on onAVStarted, so a round trip taken first has
        # already lost the race. After finalize only because that is what
        # releases the previous song's lyrics — it is a no-op on the normal
        # path, where the last playback stopped cleanly. See start_lyrics.
        self.start_lyrics()
        claimed = self._claim()
        if claimed is None:
            if was_restart:
                self._stream_restart = False
                self._restart_teardown_done = False
            return
        with self._lock:
            self._item = claimed
        state.set_playing_id(claimed["Id"])
        LOG.info("--> play %s (%s)", claimed["Id"], claimed["PlayMethod"])
        self._report(self.api.session_playing, event=None)
        self._start_ticker()
        self._start_segment_engine(claimed)
        if was_restart:
            # Successful claim of the restarted session.
            self._stream_restart = False
            self._restart_teardown_done = False

    def onAVStarted(self) -> None:
        """First frame rendered: the SyncPlay Ready trigger."""
        self._syncplay_event("on_avstarted")
        # PR2: absolute external-sub map + first index observation once demux
        # and setSubtitles tracks are visible to the player.
        self._reconcile_subs_mapping()
        self._observe_stream_indexes()
        self._apply_restart_position()

    def onAVChange(self) -> None:
        """Audio/subtitle/video stream changed in the player (PR2 observation)."""
        self._observe_stream_indexes()

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
        if self._stream_restart:
            # Never post PositionTicks=0 during a synthetic restart gap.
            return
        if self._item is None:
            return
        try:
            self._update_position(self.getTime())
        except RuntimeError:  # nothing playing (race with stop)
            return
        # Catch OSD stream switches if onAVChange did not fire (some builds).
        self._observe_stream_indexes()
        self._report(self.api.session_progress, event="timeupdate")

    def _finish(self) -> None:
        """A playback the viewer ended: report the stop, then make any
        finished-watching offer against what was playing.

        ``finalize`` clears ``_item``, so the item is captured first. Only the
        stop/end callbacks come through here — ``finalize``'s other caller
        (a stale play discovered at the next start) is cleanup, not a
        playback the viewer just finished.
        """
        if self._stream_restart:
            # Synthetic stop for stream restart: no delete-after-watch.
            self.finalize()
            return
        item = self.current_item()
        self.finalize()
        if item is not None:
            self.offer_delete(item)

    def finalize(self) -> None:
        """Report the stop and release all playback state."""
        self._segment_reset()
        self._stop_ticker()
        self._reset_lyrics()
        if self._stream_restart and self._restart_teardown_done:
            # Session already closed in _teardown_session_for_restart.
            with self._lock:
                self._item = None
            return
        with self._lock:
            item = self._item
            self._item = None
        if item is None:
            return
        LOG.info("<-- stop %s", item["Id"])
        try:
            self.api.session_stopped(
                {
                    "ItemId": item["Id"],
                    "MediaSourceId": item["MediaSourceId"],
                    "PlaySessionId": item["PlaySessionId"],
                    "PositionTicks": int(item["CurrentPosition"] * 10_000_000),
                }
            )
        except Exception as error:
            LOG.warning("stop report failed: %s", error)
        if item.get("PlayMethod") == "Transcode":
            try:
                self.api.close_transcode(item["DeviceId"], item["PlaySessionId"])
            except Exception as error:
                LOG.debug("close transcode failed: %s", error)
        # Drop materialised external text subs for this session (PR1).
        try:
            from kofin.core.subtitles import cleanup_session_subs

            cleanup_session_subs(item.get("PlaySessionId"))
        except Exception as error:  # pragma: no cover - defensive
            LOG.debug("subtitle session cleanup failed: %s", error)
        state.clear_playing_id()

    def _apply_restart_position(self) -> None:
        """Corrective seek after a stream restart (design §6.5)."""
        target = self._restart_target_pos
        if target is None:
            return
        self._restart_target_pos = None
        item = self._item
        if item is None:
            return
        try:
            current = float(self.getTime())
        except RuntimeError:
            return
        if abs(current - target) <= playback.RESTART_SEEK_TOLERANCE_SECONDS:
            self._update_position(current)
            return
        try:
            self.seekTime(target)
            self._update_position(target)
            LOG.info("restart seek %.1fs -> %.1fs (was %.1fs)", target, target, current)
        except RuntimeError as error:
            LOG.debug("restart seek failed: %s", error)

    def stop_threads(self) -> None:
        """Service shutdown: stop the ticker and checker without reporting."""
        self._segment_reset()
        self._stop_ticker()

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
                # Songs written into Kodi's library as direct
                # ``<server>/Audio/<id>/`` stream URLs never pass through the
                # play route, so their claim is back-filled from the
                # Player.OnPlay notification instead, which can land after
                # this callback. Wait a beat for it rather than calling it
                # foreign playback.
                if (
                    self.isPlayingAudio()
                    and waited < AUDIO_BACKFILL_GRACE_SECONDS
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

    def _reconcile_subs_mapping(self) -> None:
        """Resolve absolute Kodi subtitle indexes for external attach tracks."""
        item = self._item
        if item is None:
            return
        attach_order = item.get("SubsAttachOrder") or []
        try:
            names = list(self.getAvailableSubtitleStreams() or [])
        except RuntimeError:
            names = []
        # JSON-RPC often has richer names/paths than the Python list.
        rpc_names = _jsonrpc_subtitle_names()
        if rpc_names:
            names = rpc_names
        mapping, ready = streammaps.reconcile_subs_mapping(
            attach_order_jf=attach_order,
            subs_paths=item.get("SubsPaths") or [],
            kodi_sub_names=names,
            embedded_map_jf_to_kodi=item.get("EmbeddedSubMap") or {},
        )
        with self._lock:
            if self._item is not item:
                return
            item["SubsMapping"] = streammaps.stringify_map(mapping)
            item["SubsMappingReady"] = ready
        if ready:
            LOG.info(
                "SubsMapping ready (%d absolute entries, %d external)",
                len(mapping),
                len(attach_order),
            )
        elif attach_order:
            LOG.debug(
                "SubsMapping provisional (%d kodi names, %d external)",
                len(names),
                len(attach_order),
            )

    def _observe_stream_indexes(self) -> None:
        """Update AudioStreamIndex / SubtitleStreamIndex from the player OSD.

        Never restarts playback — progress reporting only (stream selection PR2).
        """
        item = self._item
        if item is None:
            return
        # Music has no multi-stream OSD of interest for JF MediaStream indexes.
        if item.get("Type") in ("Audio",):
            return
        state_now = _jsonrpc_current_streams()
        if state_now is None:
            # Fallback: cannot read player; leave defaults.
            return
        kodi_audio = state_now.get("audio")
        kodi_sub = state_now.get("subtitle")
        sub_on = bool(state_now.get("subtitleenabled"))
        audio_jf, sub_jf = streammaps.observe_jellyfin_indexes(
            item,
            kodi_audio=kodi_audio,
            kodi_sub=kodi_sub,
            subtitle_enabled=sub_on,
        )
        with self._lock:
            if self._item is not item:
                return
            if audio_jf is not None:
                item["AudioStreamIndex"] = audio_jf
            else:
                item["AudioStreamIndex"] = item.get("AudioStreamIndex")
            item["SubtitleStreamIndex"] = sub_jf

    def _report(self, poster: Any, event: Optional[str]) -> None:
        item = self._item
        if item is None:
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
        try:
            poster(data)
        except Exception as error:
            LOG.warning("playback report failed: %s", error)

    def _start_ticker(self) -> None:
        self._stop_ticker()
        self._ticker = _Ticker(self)
        self._ticker.start()

    def _stop_ticker(self) -> None:
        if self._ticker is not None:
            self._ticker.stop()
            self._ticker = None


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


def _active_player_id() -> Optional[int]:
    try:
        response = json.loads(
            xbmc.executeJSONRPC(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "Player.GetActivePlayers"}
                )
            )
        )
        players = response.get("result") or []
        for entry in players:
            if entry.get("type") in ("video", "audio"):
                return int(entry["playerid"])
        if players:
            return int(players[0]["playerid"])
    except Exception as error:
        LOG.debug("active player id failed: %s", error)
    return None


def _jsonrpc_current_streams() -> Optional[JsonDict]:
    """Current audio/subtitle absolute indexes from Player.GetProperties."""
    player_id = _active_player_id()
    if player_id is None:
        return None
    try:
        response = json.loads(
            xbmc.executeJSONRPC(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "Player.GetProperties",
                        "params": {
                            "playerid": player_id,
                            "properties": [
                                "currentaudiostream",
                                "currentsubtitle",
                                "subtitleenabled",
                            ],
                        },
                    }
                )
            )
        )
        result = response.get("result") or {}
    except Exception as error:
        LOG.debug("stream properties read failed: %s", error)
        return None
    audio = result.get("currentaudiostream") or {}
    sub = result.get("currentsubtitle") or {}
    audio_idx = audio.get("index") if isinstance(audio, dict) else None
    sub_idx = sub.get("index") if isinstance(sub, dict) else None
    try:
        audio_i = int(audio_idx) if audio_idx is not None else None
    except (TypeError, ValueError):
        audio_i = None
    try:
        sub_i = int(sub_idx) if sub_idx is not None else None
    except (TypeError, ValueError):
        sub_i = None
    return {
        "audio": audio_i,
        "subtitle": sub_i,
        "subtitleenabled": bool(result.get("subtitleenabled")),
    }


def _jsonrpc_subtitle_names() -> List[str]:
    """Subtitle stream names/paths for basename reconcile."""
    player_id = _active_player_id()
    if player_id is None:
        return []
    try:
        response = json.loads(
            xbmc.executeJSONRPC(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "Player.GetProperties",
                        "params": {
                            "playerid": player_id,
                            "properties": ["subtitles"],
                        },
                    }
                )
            )
        )
        subs = (response.get("result") or {}).get("subtitles") or []
    except Exception as error:
        LOG.debug("subtitle list read failed: %s", error)
        return []
    names: List[str] = []
    for entry in subs:
        if not isinstance(entry, dict):
            names.append(str(entry))
            continue
        # Prefer name; some builds put the path/filename there for externals.
        names.append(str(entry.get("name") or entry.get("language") or ""))
    return names


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
