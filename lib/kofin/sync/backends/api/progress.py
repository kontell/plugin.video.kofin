"""The library-update bar for the API build.

One background dialog for a whole update -- the enumeration, the scanner's
imports and the pass -- gated like the SQL build's on
``showLibraryUpdateProgress`` and ``syncProgressThreshold``. It paints at
most once a second and never per item: ``DialogProgressBG.update`` waits on
Kodi's app thread, and a pass that painted from every write froze the GUI
(``kodi-performance``). Kodi's own scanning dialog is shown beside it by
passing ``showdialogs`` to the scan calls, which this module also decides.
"""

import time
from typing import Optional

import xbmc
import xbmcgui

from kofin.core import settings
from kofin.core.log import Logger

LOG = Logger(__name__)

# Kodi's own strings, so the bar needs no translation of its own.
KIND_LABELS = {
    "Movie": 20342,
    "Series": 20343,
    "Season": 33054,
    "Episode": 20360,
    "MusicVideo": 20389,
    "BoxSet": 20434,
    "Audio": 134,
    "MusicAlbum": 132,
    "MusicArtist": 133,
}
CONTENT_LABELS = {"movies": 20342, "tvshows": 20343, "musicvideos": 20389, "music": 134}
SCANNING = 189  # "Scanning media information"


def label(kind_or_content: str) -> str:
    code = KIND_LABELS.get(kind_or_content) or CONTENT_LABELS.get(kind_or_content)
    return xbmc.getLocalizedString(code) if code else kind_or_content


def show_dialogs() -> bool:
    """Whether Kodi's own scanning dialog accompanies a scan.

    A build whose settings.xml lacks the setting raises ``TypeError:
    Invalid setting type`` from ``getSettingBool``; a bar is never a reason
    to fail a pass, so that reads as off.
    """
    try:
        return bool(settings.get_bool("showLibraryUpdateProgress"))
    except Exception as error:
        LOG.debug("progress setting unreadable (%s); bar off", error)
        return False


def threshold() -> int:
    try:
        return int(settings.get_int("syncProgressThreshold"))
    except Exception:
        return 0


class Progress:
    """A bar over ``total`` items of work; ``step`` counts them off."""

    INTERVAL = 1.0

    def __init__(self):
        self._dialog: Optional["xbmcgui.DialogProgressBG"] = None
        self._last = 0.0
        self.total = 0
        self.done = 0

    @property
    def open(self) -> bool:
        return self._dialog is not None

    def begin(self, total: int):
        """Size the bar; open it when the setting and the threshold allow."""
        self.total = max(0, int(total))
        self.done = 0
        if self._dialog is not None:
            return
        if not show_dialogs() or self.total <= threshold():
            return
        try:
            dialog = xbmcgui.DialogProgressBG()
            dialog.create("Kofin", settings.localized(30401))
        except Exception as error:
            LOG.debug("progress bar unavailable: %s", error)
            return
        self._dialog = dialog
        self._last = 0.0

    def step(self, kind: str, n: int = 1):
        """One more item of ``kind`` done."""
        self.done += n
        self._paint(self.counted(kind), False)

    def track(self, kind: str, done: int, total: int):
        """Enumeration progress: the total is known from the first page."""
        if not self.open:
            self.begin(total)
        self.total = max(0, int(total))
        self.done = min(int(done), self.total)
        self._paint(self.counted(kind), False)

    def note(self, message: str):
        """A phase change: painted now, whatever the last paint was."""
        self._paint(message, True)

    def phase(self, total: int, message: str):
        """A sub-phase with its own count, painted now; ``restore`` undoes it."""
        saved = (self.total, self.done)
        self.total = max(0, int(total))
        self.done = 0
        self._paint(message, True)
        return saved

    def restore(self, saved):
        self.total, self.done = saved

    def at(self, done: int, message: str):
        """Progress measured elsewhere (Kodi's own row count during a scan)."""
        self.done = max(0, min(int(done), self.total))
        self._paint(message, False)

    def counted(self, kind: str) -> str:
        return "%s %d / %d" % (label(kind), min(self.done, self.total), self.total)

    def _paint(self, message: str, force: bool):
        if self._dialog is None:
            return
        now = time.monotonic()
        if not force and now - self._last < self.INTERVAL:
            return
        self._last = now
        percent = 0
        if self.total > 0:
            percent = max(0, min(100, int(100 * self.done / self.total)))
        try:
            self._dialog.update(percent, message=message)
        except Exception as error:
            LOG.debug("progress bar update failed: %s", error)

    def close(self):
        if self._dialog is None:
            return
        try:
            self._dialog.close()
        except Exception as error:
            LOG.debug("progress bar close failed: %s", error)
        self._dialog = None
