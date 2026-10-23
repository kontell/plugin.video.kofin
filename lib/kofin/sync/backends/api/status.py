"""What the sync has done and is doing, for the status dialog in the settings.

The library thread writes one small document when its state changes: what it
is doing, since when, and the names of the libraries it is listing for the
first time, which the catalogue has no row of yet. Everything else the dialog
shows is read from the catalogue at the moment it is opened, by the plugin
process that draws it, so nothing is kept up to date for a dialog nobody has
open.

The document replaces two settings the service used to write, "Synced
libraries" and "Sync status". A setting write rewrites settings.xml and
fires the settings callback of every running part of the add-on, and what
it showed was a line that could not be read once the settings were closed,
which is when a sync runs.
"""

import json
import os
import threading
import time
from typing import Any, Dict, List, NamedTuple, Optional, Union

import xbmc
import xbmcgui

from kofin.core import settings
from kofin.core.log import Logger
from kofin.sync import private
from . import capabilities, metadata, progress

LOG = Logger(__name__)

DOCUMENT = "sync-status.json"
# The kind whose rows a viewer would count for a library of each type.
COUNTED = {
    "movies": ("Movie",),
    "tvshows": ("Episode",),
    "musicvideos": ("MusicVideo",),
    "mixed": ("Movie", "Episode", "MusicVideo"),
    "music": ("Audio",),
}


def path() -> str:
    return os.path.join(private.addon_data_path(), DOCUMENT)


def read() -> Dict[str, Any]:
    """The document, as far as it has the shape ``write`` gives it. It is
    read by another process than the one that wrote it, and a field of
    another type (a hand edit, a later build's) is left out, not shown."""
    try:
        with open(path(), "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict):
        return {}
    found: Dict[str, Any] = {}
    state = document.get("state")
    if isinstance(state, str):
        found["state"] = state
    since = document.get("since")
    if isinstance(since, (int, float)) and not isinstance(since, bool):
        found["since"] = float(since)
    listing = document.get("listing")
    if isinstance(listing, list):
        found["listing"] = [name for name in listing if isinstance(name, str)]
    return found


def write(state: str, listing=()) -> None:
    """Record what the sync is doing, in the words the dialog shows, and the
    names of the libraries whose first listing is under way. Written only
    when either changes; ``since`` is when the state began."""
    current = read()
    listing = list(listing)
    same = current.get("state") == state
    if same and (current.get("listing") or []) == listing:
        return
    since = current.get("since") if same else None
    document = {"state": state, "since": since or time.time(), "listing": listing}
    target = path()
    # A path of this thread's own. Two service generations run side by side
    # for a moment, each with a library thread: on one shared ".part" the
    # first to rename took the other's file with it, and the second write
    # was lost.
    part = "%s.%d.part" % (target, threading.get_ident())
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(part, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        os.replace(part, target)
    except OSError as error:
        # A status that cannot be written is never a reason to fail a sync.
        LOG.debug("sync status not written: %s", error)
        try:
            os.remove(part)
        except OSError:
            pass


class LibraryStatus(NamedTuple):
    library: str
    name: str
    media: str
    total: int  # rows a viewer would count: movies, episodes, songs
    filed: int  # of them, in Kodi
    unfiled: int  # of them, settled without a row: Kodi cannot file them
    leaving: bool  # being removed
    stale: bool  # filed under an older scanner format (metadata.SCANNER_FORMAT)


class Snapshot(NamedTuple):
    libraries: List[LibraryStatus]
    pending: int
    failed: int
    error: str
    enumerated: float


def snapshot(store, scanner_format: int) -> Snapshot:
    """The catalogue as the dialog shows it, in a handful of queries."""
    with private.Database() as db:
        views = {
            view_id: (name, media)
            for view_id, name, media in db.cursor.execute(
                "SELECT view_id, view_name, media_type FROM view"
            ).fetchall()
        }
    whitelist = private.get_sync()["Whitelist"]
    counts = store.summary()
    leaving = store.leaving()
    libraries = []
    for library in sorted(
        set(whitelist) | set(leaving), key=lambda i: views.get(i, (i,))[0]
    ):
        name, media = views.get(library, (library, "mixed"))
        kinds = COUNTED.get(media, COUNTED["mixed"])
        total, filed, unfiled = (
            sum(counts.get((library, kind), (0, 0, 0))[part] for kind in kinds)
            for part in range(3)
        )
        libraries.append(
            LibraryStatus(
                library,
                name,
                media,
                total,
                filed,
                unfiled,
                library in leaving and library not in whitelist,
                media != "music"
                and filed > 0
                and store.scanner_format(library) < scanner_format,
            )
        )
    pending, failed, error = store.outstanding()
    return Snapshot(libraries, pending, failed, error, store.watermark()[1])


def rows(
    document: Dict[str, Any],
    found: Snapshot,
    text,
    kind_label,
    clock=time.localtime,
    features: Optional[Dict[str, bool]] = None,
) -> List[str]:
    """The dialog's lines. ``text`` gives a string by its id and
    ``kind_label`` Kodi's own word for a kind; ``features`` is the capability
    table of this Kodi (``capabilities.table``), whose off entries are named
    on one row. The whole of what is shown is decided here, where it can be
    tested without a dialog."""
    lines = []
    state = document.get("state") or text(20177)
    began = _moment(document.get("since") or 0, clock)
    if began:
        # An update that began hours ago is one that stopped.
        state = "%s (%s)" % (state, began)
    lines.append("%s:  %s" % (text(30262), state))
    # A first listing: the server is still being asked what the library has.
    known = {library.name for library in found.libraries}
    for name in document.get("listing") or []:
        if name not in known:
            lines.append("%s:  %s" % (name, text(314)))
    for library in found.libraries:
        if library.leaving:
            lines.append("%s:  %s" % (library.name, text(30408)))
            continue
        kinds = COUNTED.get(library.media, COUNTED["mixed"])
        lines.append(
            "%s:  %s / %s %s"
            % (
                library.name,
                _number(library.filed),
                _number(library.total),
                kind_label(kinds[0]) if len(kinds) == 1 else "",
            )
        )
    # A special with no episode number is the usual one: without the row
    # a library a few short of the server's count reads as a failed import.
    unfiled = sum(library.unfiled for library in found.libraries)
    if unfiled:
        lines.append("%s:  %s" % (text(30868), _number(unfiled)))
    stale = [library.name for library in found.libraries if library.stale]
    if stale:
        lines.append("%s:  %s" % (text(30867), ", ".join(stale)))
    if found.pending:
        lines.append("%s:  %s" % (text(30863), _number(found.pending)))
    if found.failed:
        line = "%s:  %s" % (text(30864), _number(found.failed))
        if found.error:
            line += " (%s)" % found.error[:120]
        lines.append(line)
    enumerated = _moment(found.enumerated, clock)
    if enumerated:
        lines.append("%s:  %s" % (text(30865), enumerated))
    if features is not None:
        # What this Kodi cannot do, by name: the one place a viewer is told,
        # since nothing logs it per item (the capability profile, CLAUDE.md).
        off = [
            text(label)
            for name, label in capabilities.FEATURE_LABELS
            if not features.get(name, False)
        ]
        if off:
            lines.append("%s:  %s" % (text(30876), ", ".join(off)))
    return [line.rstrip() for line in lines]


def show(store) -> None:
    """The report, as a dialog to read: one row per fact, the value in the
    row's own label (a select dialog's plain layout has no second field).

    It is what the catalogue and the sync's document hold at the moment it
    is opened. The first row reads them again, which is how a running import
    is watched, and it is the row under the cursor: under a cursor on the
    last row the list scrolls the sync's own line off the top as soon as
    there are more rows than fit.

    The first row is the only one that does anything. A select dialog
    closes on whichever row is picked, so a fact that was picked is shown
    again as it was, the cursor on it: picking one used to close the report,
    which read as the report having gone wrong. Back is what closes it.
    """
    heading = "%s \u2014 %s" % (settings.localized(30859), settings.localized(30862))
    lines: List[str] = []
    cursor = 0
    while True:
        if cursor == 0:
            lines = rows(
                read(),
                snapshot(store, metadata.SCANNER_FORMAT),
                progress.stage,
                progress.label,
                features=capabilities.table(),
            )
        choices: List[Union[str, xbmcgui.ListItem]] = [xbmc.getLocalizedString(184)]
        choices.extend(lines)
        cursor = xbmcgui.Dialog().select(heading, choices, preselect=cursor)
        if cursor < 0:
            return


def _moment(stamp: float, clock) -> str:
    """A time as the dialog shows it; empty for none, and for a number no
    calendar has (the clock raises for one far enough out)."""
    if not stamp:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", clock(stamp))
    except (OverflowError, OSError, ValueError):
        return ""


def _number(value: int) -> str:
    return "{:,}".format(int(value))
