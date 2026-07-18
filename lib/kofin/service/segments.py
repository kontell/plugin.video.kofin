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
from typing import Any, Dict, List, Optional

import xbmc

from kofin.core.log import Logger

LOG = Logger(__name__)

TICK_SECONDS = 0.25

# Jellyfin MediaSegmentType -> the per-type identity used by settings and
# labels (fork naming kept: Introduction/Credits/Recap/Preview/Commercial).
SEGMENT_TYPES = {
    "Intro": "Introduction",
    "Outro": "Credits",
    "Recap": "Recap",
    "Preview": "Preview",
    "Commercial": "Commercial",
}


def parse_segments(response: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sorted ``[{Type, Start, End}]`` (seconds) from a /MediaSegments body."""
    segments: List[Dict[str, Any]] = []
    for item in (response or {}).get("Items") or []:
        segment_type = SEGMENT_TYPES.get(item.get("Type", ""))
        if not segment_type:
            continue
        start = float(item.get("StartTicks") or 0) / 10_000_000
        end = float(item.get("EndTicks") or 0) / 10_000_000
        if end <= start:
            continue
        segments.append({"Type": segment_type, "Start": start, "End": end})
    segments.sort(key=lambda segment: float(segment["Start"]))
    return segments


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
