"""File layout, naming, sanitization and free-space for downloads.

Pure decisions plus one filesystem probe (:func:`free_space_ok`) — no Kodi
imports, so every rule here is table-testable. Layout per the plan's storage
decisions: human-readable, components frozen at download time, stored as
POSIX-style paths relative to the downloads root.
"""

import os
import posixpath
import re
from typing import Any, Callable, Dict

from kofin.core.log import Logger

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

# What FAT/exFAT/NTFS refuse in a name (Android SD cards are the audience,
# and every other target accepts this subset too). Windows' reserved device
# names (CON, PRN, ...) are a Windows-API restriction, not a filesystem one,
# and no target here mounts the tree through that API. Control characters
# ride along in the same class.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")

# Free space kept in reserve beyond the download itself: Kodi's own caches
# and databases live on the same volume more often than not, and filling it
# to the last byte breaks them first (Emby's reserve, same number).
FREE_SPACE_RESERVE = 2 * 1024**3

MOVIES_DIR = "Movies"
TV_DIR = "TV"


def sanitize(name: str) -> str:
    """A path component safe on every target filesystem.

    Unsafe characters are dropped rather than replaced — "Mission:
    Impossible" reads better as "Mission Impossible" than with a stand-in
    glyph — whitespace runs collapse, and FAT's trailing-dot/space rule is
    applied. Unicode passes through untouched. An empty survivor answers
    "untitled" so a caller never builds a path with a vanished component.
    """
    cleaned = _WHITESPACE.sub(" ", _UNSAFE.sub("", name)).strip().rstrip(". ")
    return cleaned or "untitled"


def filename_from_disposition(value: str, fallback: str) -> str:
    """The server's filename out of a Content-Disposition header (V1).

    Handles both the plain ``filename="..."`` parameter and the RFC 5987/2231
    ``filename*=UTF-8''...`` form via the stdlib parser. The result is taken
    as a bare basename and sanitized — a header is server input, and a path
    that walks out of the downloads tree must be impossible by construction.
    """
    from email.message import Message

    if value:
        message = Message()
        message["content-disposition"] = value
        raw = message.get_filename()
        if raw:
            # Basename first, then sanitize: dots inside a name survive
            # sanitize (only trailing ones are stripped), so the extension
            # needs no special-casing once the path parts are gone.
            name = sanitize(posixpath.basename(str(raw).replace("\\", "/")))
            if name != "untitled":
                return name
    return fallback


def item_dir(item: JsonDict) -> str:
    """The directory (relative, POSIX-style) an item's files live under.

    Movies: ``Movies/<Title (Year)>``. Episodes: ``TV/<Show>/Season NN``
    (season 0 is ``Specials``, Kodi's own convention; an episode without a
    season number sits directly under the show). Anything else raises —
    phase 1 downloads movies and episodes, and a silent default here would
    put a future type's files somewhere nobody decided.
    """
    item_type = item.get("Type", "")
    if item_type == "Movie":
        title = sanitize(str(item.get("Name") or ""))
        year = item.get("ProductionYear")
        folder = "%s (%s)" % (title, year) if year else title
        return posixpath.join(MOVIES_DIR, folder)
    if item_type == "Episode":
        show = sanitize(str(item.get("SeriesName") or ""))
        season = item.get("ParentIndexNumber")
        if season is None:
            return posixpath.join(TV_DIR, show)
        if int(season) == 0:
            return posixpath.join(TV_DIR, show, "Specials")
        return posixpath.join(TV_DIR, show, "Season %02d" % int(season))
    raise ValueError("no download layout for item type %r" % item_type)


def unique_dir(base: str, jellyfin_id: str, taken: Callable[[str], bool]) -> str:
    """``base``, or ``base [<id-prefix>]`` when another item already owns it.

    Two titles can sanitize identically ("Crash (2004)" twice, a show and
    its remake); the suffix is deterministic and applied only on actual
    collision, so the common case stays clean. ``taken`` answers whether a
    directory is already claimed by a *different* item — the store and the
    filesystem both feed it, and the caller owns that closure.
    """
    if not taken(base):
        return base
    return "%s [%s]" % (base, jellyfin_id[:8])


def free_space_ok(root: str, expected_bytes: int) -> bool:
    """Whether the volume under ``root`` fits the download plus the reserve.

    A probe that cannot answer (exotic mount, permissions) allows the
    download rather than refusing it: the write itself will fail loudly if
    space truly runs out, while a false refusal is undiagnosable.
    """
    try:
        stats = os.statvfs(root)
    except OSError as error:
        LOG.debug("free-space probe failed for %s: %s", root, error)
        return True
    free = stats.f_bavail * stats.f_frsize
    return free >= int(expected_bytes) + FREE_SPACE_RESERVE
