"""The lyrics tick: keeps the skin's lyrics ladder in step with the song.

Shaped after ``segments.SegmentChecker`` -- a thread that owns nothing but the
cadence and calls one hook on the player. It exists because lyrics are a
playback-time property with no home in Kodi's music database, so the only way
to show them is to keep republishing which line is current while the song
runs.

The skin renders them, not an addon window: a Python window shown over
playback becomes the *active* window, which swallows navigation and -- fatally
-- the OSD, so a passive addon overlay is not achievable. The skin's controls
belong to the window that is already active, so they cost nothing.
"""

import threading
from typing import Any

import xbmc

from kofin.core.log import Logger

LOG = Logger(__name__)

# Matches the segment checker. Fine enough that a line change is imperceptible
# against the music, coarse enough to be free.
TICK_SECONDS = 0.25


class LyricsTicker(threading.Thread):
    """Drives the player's lyrics tick while a song with lyrics plays."""

    def __init__(self, player: Any) -> None:
        super().__init__(name="kofin-lyrics")
        self._player = player
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()
        if self.is_alive():
            self.join(timeout=5)

    def run(self) -> None:
        LOG.debug("---> lyrics ticker")
        monitor = xbmc.Monitor()
        while not self._halt.is_set() and not monitor.abortRequested():
            try:
                self._player.lyrics_tick()
            except Exception:
                LOG.exception("lyrics tick failed")
            # waitForAbort rather than Event.wait, matching the segment
            # checker: it yields to Kodi between ticks and drops out promptly
            # on shutdown instead of holding the join for a whole interval.
            if monitor.waitForAbort(TICK_SECONDS):
                break
        LOG.debug("<--- lyrics ticker")
