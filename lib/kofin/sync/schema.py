# -*- coding: utf-8 -*-
"""The schema gate: kofin only writes Kodi databases it was tested against.

Discovery lists ``special://database/`` and picks the newest file per prefix
(the fork's mechanism minus the ``UpdateLibrary()`` mtime hack). The gate then
refuses any version not in the map — write sync is disabled, one notification
is raised by the library manager, and the Library tab status line explains.
Never write blind (plan §2).

Version map: Kodi 21 (Omega) ships MyVideos131/MyMusic83. Kodi 22 (Piers)
ships MyVideos146/MyMusic84, but the gate refuses those until Piers fixture
databases exist and the L2 suite runs against them (phase-2 hardening item).

Allowed module-level state: the discovery cache. Database filenames cannot
change within a Kodi process (a version bump requires a Kodi upgrade and
restart), so a stale entry is always still correct; it is exempt from the
no-module-globals rule that protects the restart path. Tests reset it via
:func:`reset_cache`.
"""

import os
import re
from typing import Dict, Optional, Tuple

import xbmcvfs

from kofin.core.log import Logger

LOG = Logger(__name__)

PREFIXES = {"video": "MyVideos", "music": "MyMusic", "texture": "Textures"}

# kind -> allowed schema versions. Texture is unversioned here because phase 2
# never writes it; the entry exists so discovery can resolve the path.
SUPPORTED: Dict[str, Optional[set]] = {
    "video": {131},
    "music": {83},
    "texture": None,
}

DATABASE_DIR = "special://database/"

# Kodi's VideoAssetType::EXTRA value per MyVideos schema version. Omega (131)
# uses 1; Piers renumbers it to 2 in migration 134 — that entry lands with the
# Piers fixtures (plan §7: keyed here, never inlined in a writer). A version
# missing from this map disables the extras pass, not the sync.
EXTRA_ITEM_TYPE: Dict[int, int] = {131: 1}

# VideoAssetTypeOwner::USER — the owner kofin stamps on videoversiontype rows
# it creates (matches what Kodi's own "convert to extra" flow writes).
VIDEO_ASSET_OWNER_USER = 2

# Jellyfin ExtraType -> the named videoversiontype for the asset row.
EXTRA_TYPE_NAMES: Dict[str, str] = {
    "BehindTheScenes": "Behind the Scenes",
    "DeletedScene": "Deleted Scene",
    "Interview": "Interview",
    "Featurette": "Featurette",
    "Short": "Short",
    "Clip": "Clip",
    "Scene": "Scene",
    "Sample": "Sample",
    "ThemeSong": "Theme Song",
    "ThemeVideo": "Theme Video",
    "Trailer": "Trailer",
}
EXTRA_TYPE_DEFAULT_NAME = "Extra"


def extra_type_name(extra_type: Optional[str]) -> str:
    """The videoversiontype name for a Jellyfin ExtraType."""
    return EXTRA_TYPE_NAMES.get(extra_type or "", EXTRA_TYPE_DEFAULT_NAME)


_cache: Dict[str, Tuple[str, int]] = {}


class SchemaError(Exception):
    """Base for schema-gate failures; carries the user-facing reason."""


class DatabaseMissing(SchemaError):
    def __init__(self, kind: str) -> None:
        super().__init__("no %s database found" % kind)
        self.kind = kind


class SchemaUnsupported(SchemaError):
    def __init__(self, kind: str, version: int) -> None:
        super().__init__("unknown %s database v%s" % (kind, version))
        self.kind = kind
        self.version = version


def reset_cache() -> None:
    _cache.clear()


def discover(kind: str) -> Tuple[str, int]:
    """(filename, version) of the newest database file for ``kind``.

    Raises :class:`DatabaseMissing` when no file matches.
    """
    if kind in _cache:
        return _cache[kind]

    prefix = PREFIXES[kind]
    pattern = re.compile(r"^%s(\d+)\.db$" % prefix)
    newest = ("", 0)

    _dirs, files = xbmcvfs.listdir(DATABASE_DIR)
    for db_file in files:
        match = pattern.match(db_file)
        if match:
            version = int(match.group(1))
            if version > newest[1]:
                newest = (db_file, version)

    if not newest[0]:
        raise DatabaseMissing(kind)

    LOG.info("discovered %s database: %s", kind, newest[0])
    _cache[kind] = newest
    return newest


def check(kind: str) -> int:
    """The discovered schema version, gated against the map.

    Raises :class:`SchemaUnsupported` for a version kofin was not tested
    against, :class:`DatabaseMissing` when discovery finds nothing.
    """
    _db_file, version = discover(kind)
    allowed = SUPPORTED[kind]

    if allowed is not None and version not in allowed:
        raise SchemaUnsupported(kind, version)

    return version


def database_path(kind: str) -> str:
    """Absolute path of the gated database file for ``kind``."""
    db_file, _version = discover(kind)
    check(kind)
    return os.path.join(xbmcvfs.translatePath(DATABASE_DIR), db_file)


def gate_status(kinds: Tuple[str, ...] = ("video", "music")) -> Optional[SchemaError]:
    """The gate failure that would disable write sync, or None when clear.

    The music gate only matters once a music library is selected (plan §4);
    callers pass the kinds their whitelist actually needs.
    """
    for kind in kinds:
        try:
            check(kind)
        except SchemaError as error:
            return error
    return None
