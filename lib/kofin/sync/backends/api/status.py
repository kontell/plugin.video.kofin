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
import time
from typing import Any, Dict, List, NamedTuple, Optional

from kofin.core.log import Logger
from kofin.sync import private

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
    try:
        with open(path(), "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


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
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target + ".part", "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        os.replace(target + ".part", target)
    except OSError as error:
        # A status that cannot be written is never a reason to fail a sync.
        LOG.debug("sync status not written: %s", error)


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
    companion: Optional[str],
    text,
    kind_label,
    clock=time.localtime,
) -> List[str]:
    """The dialog's lines. ``text`` gives a string by its id and
    ``kind_label`` Kodi's own word for a kind; the whole of what is shown is
    decided here, where it can be tested without a dialog."""
    lines = []
    state = document.get("state") or text(20177)
    if document.get("since"):
        # An update that began hours ago is one that stopped.
        state = "%s (%s)" % (state, _moment(document["since"], clock))
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
    if found.enumerated:
        lines.append("%s:  %s" % (text(30865), _moment(found.enumerated, clock)))
    lines.append("%s:  %s" % (text(30866), companion or "—"))
    return [line.rstrip() for line in lines]


def _moment(stamp: float, clock) -> str:
    return time.strftime("%Y-%m-%d %H:%M", clock(stamp))


def _number(value: int) -> str:
    return "{:,}".format(int(value))
