"""File layout, naming, sanitization and free-space for downloads.

Pure decisions plus one filesystem probe (:func:`free_space_ok`) — no Kodi
imports, so every rule here is table-testable. Layout per the plan's storage
decisions: human-readable, components frozen at download time, stored as
POSIX-style paths relative to the downloads root.
"""

import os
import posixpath
import re
from typing import Any, Callable, Dict, Optional, Tuple

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
MUSIC_DIR = "Music"


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


def default_filename(item: JsonDict, container: str) -> str:
    """The filename when no Content-Disposition names one — every transcode,
    whose response carries no original name. Episodes lead with SxxEyy and
    songs with their track number, so a directory reads in order the way the
    server's own filenames do."""
    name = sanitize(str(item.get("Name") or item.get("Id") or "download"))
    item_type = item.get("Type", "")
    if item_type == "Episode":
        season = item.get("ParentIndexNumber")
        episode = item.get("IndexNumber")
        if season is not None and episode is not None:
            name = "S%02dE%02d %s" % (int(season), int(episode), name)
    elif item_type == "Audio":
        track = item.get("IndexNumber")
        if track is not None:
            name = "%02d %s" % (int(track), name)
    return "%s.%s" % (name, container or "bin")


def item_dirs(item: JsonDict) -> Tuple[str, Optional[str]]:
    """(owning directory, leaf subdirectory) for an item, relative, POSIX.

    Movies: ``("Movies/<Title (Year)>", None)`` — the directory belongs to
    the film alone. Episodes: ``("TV/<Show>", "Season NN")`` — the *show*
    directory is the owner and the season is a leaf inside it (season 0 is
    ``Specials``, Kodi's own convention; an episode with no season number
    sits directly under the show). The split is what keeps collision
    handling honest: a name clash is a clash between owners, and every
    episode of a show belongs under one show directory whatever its season
    (see :func:`unique_dir`). Anything else raises — phase 1 downloads
    movies and episodes, and a silent default would put a future type's
    files somewhere nobody decided.
    """
    item_type = item.get("Type", "")
    if item_type == "Movie":
        title = sanitize(str(item.get("Name") or ""))
        year = item.get("ProductionYear")
        return (
            posixpath.join(MOVIES_DIR, "%s (%s)" % (title, year) if year else title),
            None,
        )
    if item_type == "Episode":
        show = posixpath.join(TV_DIR, sanitize(str(item.get("SeriesName") or "")))
        season = item.get("ParentIndexNumber")
        if season is None:
            return show, None
        if int(season) == 0:
            return show, "Specials"
        return show, "Season %02d" % int(season)
    if item_type == "Audio":
        # ``Music/<AlbumArtist>/<Album>``, the album directory owning its
        # tracks the way a show's owns its episodes (owner = album id). The
        # artist level is plain nesting — nothing owns or uniquifies it, like
        # the ``TV/`` type directory.
        artists = item.get("AlbumArtists") or []
        artist = str(
            item.get("AlbumArtist")
            or (artists[0].get("Name") if artists else "")
            or (item.get("Artists") or [""])[0]
            or "Unknown artist"
        )
        album = str(item.get("Album") or "Unknown album")
        return (
            posixpath.join(MUSIC_DIR, sanitize(artist), sanitize(album)),
            None,
        )
    raise ValueError("no download layout for item type %r" % item_type)


def unique_dir(base: str, owner_id: str, taken: Callable[[str], bool]) -> str:
    """``base``, or ``base [<id-prefix>]`` when another *owner* holds it.

    Two titles can sanitize identically (a show and its remake, "Crash
    (2004)" twice); the suffix is deterministic and applied only on a real
    clash, so the common case stays clean. ``owner_id`` is the film's own id
    or — for an episode — its *series* id, and ``taken`` answers whether the
    directory already belongs to a different owner. Owners, not items: every
    episode of a show shares that show's directory, and a per-item test
    would suffix each sibling into a directory of its own (found live —
    four episodes of one season landed in three directories).
    """
    if not taken(base):
        return base
    return "%s [%s]" % (base, owner_id[:8])


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
