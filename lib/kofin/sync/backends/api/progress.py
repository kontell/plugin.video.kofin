"""The library-update bar for the API build.

One background dialog for a whole update -- the enumeration, the scanner's
imports and the pass -- gated like the SQL build's on
``showLibraryUpdateProgress`` and ``syncProgressThreshold``. It paints at
most once a second and never per item: ``DialogProgressBG.update`` waits on
Kodi's app thread, and a pass that painted from every write froze the GUI
(``kodi-performance``). Kodi's own scanning dialog is shown beside it by
passing ``showdialogs`` to the scan calls, which this module also decides.

What a viewer asked for -- a library selected, updated, repaired or taken
out -- is *attended*: the bar is up from the request to the end of the work,
whatever the threshold, and every stretch that counts nothing says what it
is doing. Unattended, the bar was made by the first page of the first
listing and closed between the listing and the pass, so a selection was
followed by seconds of nothing, the pass by more, and a removal by nothing
at all for as long as it took.
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
PREPARING = 314  # "Preparing..."
SAVING = 33008  # "Saving"
WAITING = 20186  # "Please wait"
REMOVING = 30408  # kofin's own: "Removing library"


def stage(code: int) -> str:
    """The text of a stage that counts nothing: one of kofin's own strings
    (30000 to 30999) or one of Kodi's."""
    if 30000 <= code < 31000:
        return str(settings.localized(code))
    return str(xbmc.getLocalizedString(code))


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
        self.attended = False

    @property
    def open(self) -> bool:
        return self._dialog is not None

    def attend(self):
        """A viewer asked for what follows: the bar is up from now, whatever
        the threshold, and stays up until ``finish``."""
        self.attended = True
        self.total = self.done = 0
        if self._dialog is None:
            self._open(stage(PREPARING))
        else:
            self.note(stage(PREPARING))

    def begin(self, total: int):
        """Size the bar; open it when the setting and the threshold allow."""
        self.total = max(0, int(total))
        self.done = 0
        if self._dialog is not None:
            return
        if not self.attended and self.total <= threshold():
            return
        self._open(settings.localized(30401))

    def _open(self, message: str):
        if not show_dialogs():
            return
        try:
            dialog = xbmcgui.DialogProgressBG()
            dialog.create("Kofin", message)
        except Exception as error:
            LOG.debug("progress bar unavailable: %s", error)
            return
        self._dialog = dialog
        self._last = 0.0
        # What the bar showed and when, for a report that nothing seemed to
        # be happening: at debug level, once a second at most.
        LOG.debug("bar up: %s", message)

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

    def asking(self, kind: str):
        """The kind whose first page is being asked for: named at once, with
        nothing counted. Until that page came back the bar went on showing
        the finished count of the kind before."""
        self.total = self.done = 0
        self._paint(label(kind), True)

    def note(self, message: str):
        """A phase change: painted now, whatever the last paint was."""
        self._paint(message, True)

    def working(self, code: int = PREPARING):
        """A stretch that counts nothing: the bar says so and goes back to
        its start, in place of the last count of what came before."""
        self.total = self.done = 0
        self._paint(stage(code), True)

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
        else:
            LOG.debug("bar: %d %% %s", percent, message)

    def close(self):
        """The end of one stretch of work. An attended bar stays up: more of
        what the viewer asked for follows, and ``finish`` takes it down."""
        if self.attended:
            return
        self._shut()

    def finish(self):
        """The end of what the viewer asked for."""
        self.attended = False
        self._shut()

    def _shut(self):
        if self._dialog is None:
            return
        try:
            self._dialog.close()
        except Exception as error:
            LOG.debug("progress bar close failed: %s", error)
        self._dialog = None
        LOG.debug("bar down")
