# -*- coding: utf-8 -*-
"""Kodi-database repoint for downloaded items (docs/offline-downloads-plan.md W1.7).

New strict code beside the transplanted writers, in this directory because it
writes MyVideos the way everything here does: explicit columns, cursor in, no
policy. The policy — which item, which directories, when the restore filename
is captured — lives in :mod:`kofin.downloads.repoint`.

The shape is Emby-for-Kodi's, proven mandatory by feasibility V5: the file
row's ``idPath`` moves to a real directory row and ``strFilename`` becomes
the bare basename, so Kodi's save-state resolves back to the row it played —
the one-column shortcut left playback working while watched/resume landed on
a freshly inserted shadow row. ``idFile`` never changes, which is what keeps
bookmarks and play counts attached across repoint and restore (V4).

Episodes mirror their location in ``episode.c18``/``c19`` (FullFilePath and
idPath — the writers populate both on every pass, so repoint and restore
must too). The directory rows above an episode carry the same scraper stamps
the writers put on the show's plugin path row (``tvshows``/``metadata.local``
/``noUpdate``/``useFolderNames``): the info dialog resolves a scraper for the
item's path and silently loses cast, tags, country, director and writer
without them (the CLAUDE.md constraint). For local paths Kodi's parent walk
is string-wise and actually works — the very thing ``plugin://`` paths never
had — so the stamp on the show directory covers every file below it; movies
carry theirs on the title directory, mirroring the writers' per-library row.
"""

from typing import List, Optional, Tuple

import sqlite3

from kofin.core.log import Logger
from kofin.sync.kodidb.kodi import Kodi

LOG = Logger(__name__)

GET_FILE_LOCATION = """
SELECT      idPath, strFilename
FROM        files
WHERE       idFile = ?
"""

# dateAdded deliberately untouched: the repoint owns the location columns and
# nothing else, and the writers' own update_file keeps stamping the rest.
SET_FILE_LOCATION = """
UPDATE      files
SET         idPath = ?, strFilename = ?
WHERE       idFile = ?
"""

SET_EPISODE_LOCATION = """
UPDATE      episode
SET         c18 = ?, c19 = ?
WHERE       idEpisode = ?
"""

PATH_HAS_FILES = """
SELECT      1
FROM        files
WHERE       idPath = ?
LIMIT       1
"""

PATH_HAS_CHILDREN = """
SELECT      1
FROM        path
WHERE       idParentPath = ?
LIMIT       1
"""

DELETE_PATH = """
DELETE FROM path
WHERE       idPath = ?
"""


def with_sep(directory: str) -> str:
    """Kodi path rows always carry a trailing separator; POSIX-only, like
    every target this addon runs on."""
    return directory if directory.endswith("/") else directory + "/"


class Downloads(Kodi):
    def __init__(self, cursor: "sqlite3.Cursor") -> None:
        self.cursor = cursor
        Kodi.__init__(self)

    def file_location(self, file_id: int) -> Optional[Tuple[int, str]]:
        self.cursor.execute(GET_FILE_LOCATION, (file_id,))
        row = self.cursor.fetchone()
        if row is None:
            return None
        return int(row[0]), str(row[1] or "")

    def set_file_location(self, file_id: int, path_id: int, filename: str) -> None:
        self.cursor.execute(SET_FILE_LOCATION, (path_id, filename, file_id))

    def set_episode_location(
        self, episode_id: int, full_path: str, path_id: int
    ) -> None:
        self.cursor.execute(SET_EPISODE_LOCATION, (full_path, path_id, episode_id))

    def ensure_movie_paths(self, type_dir: str, title_dir: str) -> int:
        """Get-or-create ``<root>/Movies/`` and the title directory; returns
        the title row id (the file's target). Stamps mirror the writers'
        movie path row (``QU.update_path_movie_obj``): content ``movies``,
        ``metadata.local``, noUpdate, no useFolderNames."""
        type_id = int(self.add_path(with_sep(type_dir)))
        title_id = int(self.add_path(with_sep(title_dir)))
        self.update_path(
            with_sep(title_dir), "movies", "metadata.local", 1, None, title_id
        )
        self.update_path_parent_id(title_id, type_id)
        return title_id

    def ensure_episode_paths(
        self, type_dir: str, show_dir: str, season_dir: Optional[str]
    ) -> int:
        """Get-or-create ``<root>/TV/``, the show directory and the season
        directory; returns the file's target row id. The show row carries the
        writers' show-row stamps (``QU.update_path_tvshow_obj``), the season
        row stays bare with a parent link — the string-wise parent walk finds
        the show row's scraper from anywhere below it."""
        type_id = int(self.add_path(with_sep(type_dir)))
        show_id = int(self.add_path(with_sep(show_dir)))
        self.update_path(with_sep(show_dir), "tvshows", "metadata.local", 1, 1, show_id)
        self.update_path_parent_id(show_id, type_id)
        if season_dir is None:
            return show_id
        season_id = int(self.add_path(with_sep(season_dir)))
        self.update_path_parent_id(season_id, show_id)
        return season_id

    def remove_tag_when_orphaned(
        self, tag: str, media_id: int, media_type: str
    ) -> None:
        """``remove_tag`` plus tag-row cleanup: Kodi's base helper unlinks and
        leaves the tag row for CleanDatabase to sweep someday, which fails the
        L2 zero-trace invariant — a fully removed download must leave the
        database byte-identical, orphan tag rows included."""
        self.remove_tag(tag, media_id, media_type)
        self.cursor.execute(
            "DELETE FROM tag WHERE name = ? COLLATE NOCASE "
            "AND NOT EXISTS (SELECT 1 FROM tag_link WHERE tag_link.tag_id = tag.tag_id)",
            (tag,),
        )

    def prune_paths(self, directories: List[str]) -> int:
        """Delete each directory's row when nothing references it; returns
        how many went. Callers pass bottom-up order (season before show
        before type), so a parent emptied by an earlier deletion in the same
        call is caught by its own turn. A row keeping files — a sibling
        episode still downloaded — or child path rows survives untouched."""
        removed = 0
        for directory in directories:
            path_id = self.get_path(with_sep(directory))
            if path_id is None:
                continue
            self.cursor.execute(PATH_HAS_FILES, (path_id,))
            if self.cursor.fetchone() is not None:
                continue
            self.cursor.execute(PATH_HAS_CHILDREN, (path_id,))
            if self.cursor.fetchone() is not None:
                continue
            self.cursor.execute(DELETE_PATH, (path_id,))
            removed += 1
        return removed
