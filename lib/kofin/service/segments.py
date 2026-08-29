"""Media-segment checker: the playback tick behind segment skipping.

Fork ``segments.py`` port, retimed per plan §2 (timing robustness): the fork
ticked at 1 Hz on ``int(getTime())`` and could step over short or late-loaded
segments entirely; kofin ticks at 0.25 s on ``float`` positions and the player
detects boundary *crossings*, so a coarse or late poll cannot lose a segment.
The checker stays decoupled from the player: it calls two hooks (prepare once,
tick repeatedly) and never blocks on dialogs — the tick itself opens and
closes the overlay, so there is no second monitor thread to hang shutdown on
(the fork's ``_monitor_skip_dialog`` defect).
"""

import threading
from typing import Any, Dict, List, Optional, Protocol, Set, Tuple

import xbmc

from kofin.core import settings, state, toast
from kofin.core.api import Api
from kofin.core.log import Logger
from kofin.core.segments import parse_segments

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

TICK_SECONDS = 0.25

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

# A skip prompt hides part-way through its segment (``skipPromptHidePercent``)
# rather than sitting over the opening moment of the content the viewer just
# chose to watch — but never before it can be read and pressed. Eight seconds
# is the Jellyfin ecosystem's own figure, agreed independently by jellyfin-web
# (skipsegment.ts setTimeout), jellyfin-androidtv (AskToSkipAutoHideDuration)
# and the intro-skipper plugin (SkipbuttonHideDelay).
MIN_PROMPT_SECONDS = 8.0

# Segments too short to act on: a prompt that flashes is worse than none, and a
# sub-second seek is indistinguishable from a stutter. Both figures are
# jellyfin-web's and jellyfin-androidtv's, which agree exactly.
MIN_ASK_SECONDS = 3.0
MIN_AUTO_SKIP_SECONDS = 1.0
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


def format_span(seconds: float) -> str:
    """``M:SS`` for a segment length, as the skip button renders it."""
    whole = max(0, int(round(seconds)))
    return "%d:%02d" % (whole // 60, whole % 60)


def prompt_hide_at(
    start: float,
    end: float,
    percent: float,
    now: float,
    minimum: float = MIN_PROMPT_SECONDS,
) -> float:
    """When a skip prompt hides: a share of its segment, but never so soon the
    viewer cannot read and press it, and never past the segment end.

    The floor is measured from ``now`` rather than ``start``, so a segment the
    engine entered late (a fetch that landed after the boundary) still gets its
    full dwell instead of a prompt that opens already expired.
    """
    deadline = start + max(0.0, end - start) * (percent / 100.0)
    floor = max(start, now) + minimum
    return min(end, max(deadline, floor))


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


class SegmentChecker(threading.Thread):
    """Drives the player's segment tick at 0.25 s while playback runs."""

    def __init__(self, player: Any) -> None:
        super().__init__(name="kofin-segments")
        self._player = player
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()
        if self.is_alive():
            self.join(timeout=5)

    def run(self) -> None:
        LOG.debug("---> segment checker")
        monitor = xbmc.Monitor()
        try:
            # Warm-fetch fallback + next-episode resolution; the first arm is
            # gated on this completing (plan §2d — the t≈0 Intro race).
            self._player.prepare_segment_state(self._halt)
        except Exception:
            LOG.exception("segment state preparation failed")
        while not self._halt.is_set() and not monitor.abortRequested():
            try:
                self._player.segment_tick()
            except Exception:
                LOG.exception("segment tick failed")
            # Kodi-aware wait, not threading.Event.wait: while the overlay is
            # open the checker must yield to Kodi between ticks so the window's
            # onClick/onAction callbacks are serviced (upstream's skip button
            # works for exactly this reason — its monitor loop pumps
            # waitForAbort; a plain Event.wait never lets Kodi deliver them).
            if monitor.waitForAbort(TICK_SECONDS):
                break
        LOG.debug("<--- segment checker")


class EnginePlayer(Protocol):
    """What the engine needs from the player that owns it (P2.3): the
    position and runtime Kodi reports, the seek, the claimed item, and the
    SyncPlay flag that withholds Play Next."""

    def getTime(self) -> float: ...

    def getTotalTime(self) -> float: ...

    def seekTime(self, seconds: float) -> None: ...

    def current_item(self) -> Optional[JsonDict]: ...

    @property
    def syncplay_group_active(self) -> bool: ...


class SegmentEngine:
    """The segment engine (plan §2), split out of the player (P2.3).

    Runs on the SegmentChecker's 0.25 s tick: boundary-*crossing* detection
    on float positions (a coarse or late poll cannot step over a segment),
    a pre-armed next boundary (one compare per tick), recoverable dedup
    (seek out and back in re-offers), and a settle window after our own
    skip seek so a lagging ``getTime()`` cannot re-trigger. The overlay's
    lifetime is tick-driven — open at the crossing, auto-close past the
    end, button actions on Kodi's GUI thread — no second monitor thread.
    Play Next resolves the next episode up front and starts it through
    kofin's own play path. One engine per player, reset per playback: the
    state below is exactly what the player used to carry.
    """

    def __init__(self, player: EnginePlayer, api: Api) -> None:
        self.player = player
        self.api = api
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
        self._overlay_hide_at = 0.0
        self._overlay_window: Optional[Tuple[float, float]] = None
        self._overlay_autoplay = False
        self._skip_target: Optional[float] = None

    def start(self, item: JsonDict) -> None:
        """Arm for a claimed playback (the checker thread is started here)."""
        if item.get("Type") not in ("Movie", "Episode"):
            return
        segments_enabled = settings.get_bool("mediaSegmentsEnabled")
        play_next = (
            settings.get_bool("playNextEnabled") and item.get("Type") == "Episode"
        )
        if not segments_enabled and not play_next:
            return
        self.reset()
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
        item = self.player.current_item()
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
            if halt.is_set() or self.player.current_item() is not item:
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
            if halt.is_set() or self.player.current_item() is not item:
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

    def reset(self) -> None:
        """Stop the checker, close the overlay, forget the item."""
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
        if self.player.current_item() is None:
            return
        try:
            now = float(self.player.getTime())
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
            expected = float(
                (self.player.current_item() or {}).get("CurrentPosition") or 0.0
            )
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
                    self.player.seekTime(self._settle_target)
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
            self._open_overlay(None, ("playnext", "close"), now)

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
        span = float(segment["End"]) - float(segment["Start"])
        if auto_seek and span < MIN_AUTO_SKIP_SECONDS:
            LOG.debug("segment too short to seek past (%.2fs)", span)
        elif auto_seek:
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
        elif span < MIN_ASK_SECONDS:
            # Likewise for a segment nobody could read the prompt for, let
            # alone press it — the dwell floor would only hold a flash on
            # screen longer than the thing it offers to skip.
            LOG.debug("segment too short to offer (%.2fs)", span)
            buttons = tuple(button for button in buttons if button != "skip")
        if any(button in ("skip", "playnext") for button in buttons):
            self._open_overlay(segment, buttons, now)

    def _auto_skip(self, segment: JsonDict, now: float) -> None:
        target = safe_seek_end(segment["End"], self._runtime_for_seek(), now)
        if target is None:
            return
        # The toast is deferred to the tick that confirms the seek landed, so a
        # dropped t~=0 seek never reports a skip that did not happen.
        self._begin_settle(target, notify=str(segment["Type"]))
        self.player.seekTime(target)
        LOG.info("auto-skip %s -> %.1f", segment["Type"], target)

    def _segment_mode(self, segment_type: str) -> int:
        setting_id = SEGMENT_MODE_SETTINGS.get(segment_type)
        if not setting_id or not settings.get_bool("mediaSegmentsEnabled"):
            return MODE_OFF
        return settings.get_int(setting_id)

    def _play_next_on_offer(self) -> bool:
        return (
            self._next_episode is not None
            and not self.player.syncplay_group_active
            and settings.get_bool("playNextEnabled")
        )

    def _begin_settle(self, target: float, notify: Optional[str] = None) -> None:
        self._settle_target = target
        self._settle_ticks = SEEK_SETTLE_TICKS
        self._settle_retries = SEEK_RETRIES
        self._pending_notify = notify

    def _live_runtime(self) -> float:
        try:
            total = float(self.player.getTotalTime())
            if total > 0:
                return total
        except RuntimeError:
            pass
        item = self.player.current_item()
        if item is not None:
            return float(item.get("Runtime") or 0) / 10_000_000
        return 0.0

    def _runtime_for_seek(self) -> float:
        return self._runtime if self._runtime > 0 else self._live_runtime()

    # -- segment engine: the overlay -----------------------------------------

    def _open_overlay(
        self, segment: Optional[JsonDict], buttons: Tuple[str, ...], now: float
    ) -> None:
        from kofin.service import skip as skip_dialog

        self._close_overlay()
        offers_next = "playnext" in buttons
        show_skip = "skip" in buttons and segment is not None

        skip_label = ""
        skip_duration = ""
        self._skip_target = None
        if show_skip and segment is not None:
            skip_label = settings.localized(
                SKIP_LABEL_IDS.get(str(segment["Type"]), 30481)
            )
            self._skip_target = float(segment["End"])
            # What pressing the button actually saves, which is not the segment
            # span when the engine entered the segment late.
            skip_duration = format_span(
                float(segment["End"]) - max(float(segment["Start"]), now)
            )

        next_label = settings.localized(30486) if offers_next else ""
        next_info = ""
        if offers_next and self._next_episode is not None:
            next_info = settings.localized(30489) % next_episode_label(
                self._next_episode
            )

        # Two deadlines, deliberately separate. ``_overlay_end`` is how long the
        # offer stands — a Play Next runs to the end of the video, and it is
        # what autoplay counts down to. ``_overlay_hide_at`` is when the window
        # closes, which for a segment prompt is part-way through the segment so
        # it does not sit over the opening moment of the content.
        if offers_next or segment is None:
            self._overlay_end = self._runtime
        else:
            self._overlay_end = float(segment["End"])
        self._overlay_autoplay = offers_next and settings.get_bool("playNextAutoplay")
        if segment is None or self._overlay_autoplay:
            # The near-end prompt has no segment to take a share of; and an
            # armed autoplay must keep its countdown on screen, because the
            # countdown *is* the warning — hiding it while still handing over
            # would take the warning away and act anyway.
            self._overlay_hide_at = self._overlay_end
        else:
            self._overlay_hide_at = prompt_hide_at(
                float(segment["Start"]),
                float(segment["End"]),
                float(settings.get_int("skipPromptHidePercent") or 100),
                now,
            )
        window_start = (
            float(segment["Start"]) if segment is not None else self._near_end_at or 0.0
        )
        self._overlay_window = (window_start, self._overlay_hide_at)

        try:
            self._overlay = skip_dialog.open_overlay(
                skip_label,
                skip_duration,
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
        if 0 < self._overlay_hide_at <= now:
            self._close_overlay()

    def _close_overlay(self) -> None:
        overlay = self._overlay
        self._overlay = None
        self._overlay_window = None
        self._overlay_hide_at = 0.0
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
            now: Optional[float] = float(self.player.getTime())
        except RuntimeError:
            now = None
        seek_to = safe_seek_end(target, self._runtime_for_seek(), now)
        if seek_to is None:
            return
        self._begin_settle(seek_to)
        self.player.seekTime(seek_to)
        LOG.info("user skipped to %.1f", seek_to)

    def _overlay_play_next(self) -> None:
        self._start_next_episode()

    def _start_next_episode(self) -> None:
        nxt = self._next_episode
        if nxt is None or not nxt.get("Id"):
            return
        from kofin.core.urls import plugin_url

        LOG.info("play next episode %s", nxt.get("Id"))
        # Play Next always starts the next episode from the beginning — never at
        # a stale server-side resume point, which would drop the viewer inside
        # the credits (skipping the outro, not the intro).
        url = plugin_url({"mode": "play", "id": str(nxt.get("Id")), "fromstart": "1"})
        xbmc.executebuiltin('PlayMedia("%s")' % url)

    def _notify(self, message: str, level: str = toast.INFO) -> None:
        toast.show(message, level, time_ms=3000)

    # -- internals -------------------------------------------------------------
