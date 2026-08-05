# -*- coding: utf-8 -*-
"""The schema gate: kofin only writes Kodi databases it was tested against.

Discovery lists ``special://database/`` and picks the newest file per prefix
(the fork's mechanism minus the ``UpdateLibrary()`` mtime hack). The gate then
refuses any version not in the map — write sync is disabled, one notification
is raised by the library manager, and the Library tab status line explains.
Never write blind (plan §2).

Version map: Kodi 21 (Omega) ships MyVideos131/MyMusic83; Kodi 22 (Piers)
ships MyVideos146 and, since the 147 bump mid-beta, MyVideos147 — MyMusic
stays 84 across both. Every entry is fixture-backed: the L2 writer suite runs
against a schema dump of each before a version enters the map (see
docs/myvideos147-gate.md for how 147's was established).

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

# kind -> allowed schema versions. Texture entered the map with the chapter
# thumbnail feature: Omega ships Textures13, Piers Textures14, both
# fixture-backed like the video/music legs.
SUPPORTED: Dict[str, Optional[set]] = {
    "video": {131, 146, 147},
    "music": {83, 84},
    "texture": {13, 14},
}

DATABASE_DIR = "special://database/"

# Kodi's VideoAssetType::EXTRA value per MyVideos schema version. Piers shifts
# the whole enum up by one (VERSION 0->1, EXTRA 1->2), confirmed against the
# Bravia install's seed rows (plan §7: keyed here, never inlined in a writer).
# 147 keeps Piers's numbering: the enum is untouched by that bump, which is
# data-only (docs/myvideos147-gate.md).
# A version missing from this map disables the extras pass, not the sync.
EXTRA_ITEM_TYPE: Dict[int, int] = {131: 1, 146: 2, 147: 2}

# VideoAssetTypeOwner::USER — the owner kofin stamps on videoversiontype rows
# it creates (matches what Kodi's own "convert to extra" flow writes).
VIDEO_ASSET_OWNER_USER = 2

# The chapter-thumb cache key per Textures schema version: Omega's bookmarks
# dialog requests the raw "chapter://{dynpath}/{n}" string, Piers wraps the
# same request as a canonical "image://video@{encoded}/?chapter={n}" URL
# (GUIDialogVideoBookmarks in each; bench-verified on both, see
# docs/chapter-thumbnails-feasibility.md §4). Keyed here like EXTRA_ITEM_TYPE:
# a version missing from this map disables chapter-thumb seeding, not playback.
CHAPTER_ART_WRAPPED: Dict[int, bool] = {13: False, 14: True}

# Rows Kodi itself writes at music-database creation: the "Default role" from
# MusicDatabase::CreateTables and the BLANKARTIST_* "[Missing Tag]" artist —
# the same statements as tests/fixtures/mymusic8*_seed.sql. The cleaner
# re-inserts them after its wipe: a bare DELETE of every table lands *below*
# pristine, which is the jellyfin-kodi reset bug the cleaner exists to not
# repeat (docs/clean-databases-plan.md G2). Keyed per version like
# EXTRA_ITEM_TYPE; a new music version must state its seeds here
# (test_sync_schema refuses a SUPPORTED entry without them).
_MUSIC_SEEDS: Tuple[str, ...] = (
    "INSERT INTO role (idRole, strRole) VALUES (1, 'Artist')",
    "INSERT INTO artist (idArtist, strArtist, strSortName, strMusicBrainzArtistID) "
    "VALUES (1, '[Missing Tag]', '[Missing Tag]', 'Artist Tag Missing')",
)
MUSIC_SEED_SQL: Dict[int, Tuple[str, ...]] = {83: _MUSIC_SEEDS, 84: _MUSIC_SEEDS}

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
