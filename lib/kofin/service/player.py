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
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

import xbmc
import xbmcgui

from kofin.core import settings, state
from kofin.core.api import Api
from kofin.core.log import Logger
from kofin.service.segments import SegmentChecker, parse_segments

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

PROGRESS_INTERVAL_SECONDS = 10.0
CLAIM_TIMEOUT_SECONDS = 10.0

# ~3 s of ticks: how long a lagging getTime() may keep reporting the pre-seek
# position after our own skip seek before we give up waiting for it.
SEEK_SETTLE_TICKS = 12

# A seek issued at t~=0 (an Intro starting at the very start) is dropped by the
# player before it is seekable, so the skip is silently lost. Each settle window
# that expires with the position still short of the target re-issues the seek,
# up to this many times, before giving up — covering ~the first few seconds of
# startup buffering. The notification fires only once the seek actually lands.
SEEK_RETRIES = 6

# On a playback transition (notably Play Next A->B) getTime() reports the
# *previous* item's position — which keeps advancing, so it cannot be told from
# real playback by stability alone — until Kodi switches players. Since Play Next
# always fires near A's end, that stale value sits far above the new item's
# intended start; the engine ignores positions more than this many seconds past
# the intended start until playback actually reaches the new item.
FRESH_START_MARGIN = 120.0

# Autoplay starts the next episode this close to the overlay deadline, so the
# handoff lands before natural EOF tears the player down.
AUTOPLAY_MARGIN_SECONDS = 1.0

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


def next_episode_label(episode: JsonDict) -> str:
    season = episode.get("ParentIndexNumber")
    number = episode.get("IndexNumber")
    name = episode.get("Name") or ""
    if season is not None and number is not None:
        prefix = "S%02dE%02d" % (int(season), int(number))
        return "%s. %s" % (prefix, name) if name else prefix
    return name


class Player(xbmc.Player):
    def __init__(self, api: Api) -> None:
        super().__init__()
        self.api = api
        self._item: Optional[JsonDict] = None
        self._ticker: Optional[_Ticker] = None
        self._lock = threading.Lock()
        # Set by SyncPlay (phase 4); while True, Play Next is withheld — the
        # group queue is authoritative.
        self.syncplay_group_active = False
        self._checker: Optional[SegmentChecker] = None
        self._segments: List[JsonDict] = []
        self._segments_loaded = False
        self._armed_index = 0
        self._prompted: Set[Tuple[float, float]] = set()
        self._prev_pos: Optional[float] = None
        self._settle_target: Optional[float] = None
        self._settle_ticks = 0
        self._settle_retries = 0
        self._pending_notify: Optional[str] = None
        self._pending_jump = False
        self._fresh_start = False
        self._next_episode: Optional[JsonDict] = None
        self._runtime = 0.0
        self._near_end_at: Optional[float] = None
        self._near_end_prompted = False
        self._overlay: Optional[Any] = None
        self._overlay_end = 0.0
        self._overlay_window: Optional[Tuple[float, float]] = None
        self._overlay_autoplay = False
        self._skip_target: Optional[float] = None

    # -- kodi callbacks ------------------------------------------------------

    def onPlayBackStarted(self) -> None:
        self.finalize()  # a previous kofin play that never got its stop event
        claimed = self._claim()
        if claimed is None:
            return
        with self._lock:
            self._item = claimed
        state.set_playing_id(claimed["Id"])
        LOG.info("--> play %s (%s)", claimed["Id"], claimed["PlayMethod"])
        self._report(self.api.session_playing, event=None)
        self._start_ticker()
        self._start_segment_engine(claimed)

    def onPlayBackPaused(self) -> None:
        self._set_paused(True)

    def onPlayBackResumed(self) -> None:
        self._set_paused(False)

    def onPlayBackSeek(self, time: int, seekOffset: int) -> None:
        if self._item is not None:
            self._update_position(time / 1000.0)
            self._report(self.api.session_progress, event="timeupdate")
            self.note_seek(time / 1000.0)

    def onPlayBackStopped(self) -> None:
        self.finalize()

    def onPlayBackEnded(self) -> None:
        self.finalize()

    def onPlayBackError(self) -> None:
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

    def finalize(self) -> None:
        """Report the stop and release all playback state."""
        self._segment_reset()
        self._stop_ticker()
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
        state.clear_playing_id()

    def stop_threads(self) -> None:
        """Service shutdown: stop the ticker and checker without reporting."""
        self._segment_reset()
        self._stop_ticker()

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
            # Hold off arming while getTime() still reports the previous item's
            # position (Play Next A->B): that stale value sits far past the new
            # item's intended start and would fire a phantom overlay.
            expected = float((self._item or {}).get("CurrentPosition") or 0.0)
            if now > expected + FRESH_START_MARGIN:
                self._prev_pos = now
                return
            self._fresh_start = False
            self._prev_pos = None  # no crossing credit from the stale position

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
        if now >= float(segment["End"]) - 0.25:
            # Stepped past the boundary already: a skip button would be noise,
            # but a Play Next offer still stands.
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

    def _notify(self, message: str) -> None:
        try:
            xbmcgui.Dialog().notification(
                "Kofin", message, xbmcgui.NOTIFICATION_INFO, 3000, False
            )
        except Exception:  # notifications are cosmetic
            pass

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
