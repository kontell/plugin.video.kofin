# -*- coding: utf-8 -*-
"""Widget fingerprints: digests of what home-screen widgets render, per Kodi
database, so a refresh only fires when that state actually moved.

The pvr.kofin pattern (JellyfinRecordingManager: "hash the fields Kodi
renders") translated to the library sync. Kodi re-fetches and re-renders
every widget on each refresh builtin with no same-content check of its own,
so suppressing the no-change refreshes at the source is the only flicker
fix. The no-change class here is real: userdata echoes of this client's own
playback reporting rewrite the values Kodi already has, and Etag-matched
cycles write nothing at all.

What is hashed is the *rendered* state, not the stored one:

- ``reference`` — kofin.db's (jellyfin_id, checksum) rows per media kind.
  The checksum tracks the server's metadata/artwork state and is stamped by
  every metadata and artwork-only write, so any real change to titles, art
  or item existence moves this without hashing a single MyVideos text
  column. A re-stamp of the same value holds it still.
- ``userdata`` (video) — per item: watched flag (``playCount > 0``, the
  overlay), resume percent (whole percent — the progress bar cannot render
  finer), and the movie's set link (set widgets render membership). Raw play
  counts, timestamps and lastPlayed *values* are deliberately absent: they
  move on every playback echo while rendering nothing.
- ``ratings`` (video) — per item, the rating Kodi renders: the *default* one,
  the row the item's rating pointer names, not the whole set. A sync that
  changes a rating moves ``reference`` too, so this section exists for the
  one writer that moves nothing else — the ``preferCriticRating`` flip's
  repoint pass, which rewrites pointers and no checksums.
- ``recency`` — the *order* of the top-N ids the recently-added and (music)
  recently-played widgets show. An order-preserving timestamp bump — the
  same album played again, a lastPlayed touch — holds the digest still;
  anything that reorders or replaces rows moves it.
- ``inprogress`` (video) — the ordered id list of resumable items, the
  in-progress widgets' membership and order.
- ``downloads`` — the set of finished downloads of the kinds this database
  holds. What renders it is the downloaded badge (an ``art`` row of type
  ``kofin.downloaded``, which skins read as an overlay) and, on the music
  side, the Downloaded-music playlist's membership. It needs its own section
  because a download moves *nothing else here*: it stamps a badge, moves the
  file rows and adds a tag, and re-stamps no checksum — so a completed or
  deleted download used to sit behind an unchanged fingerprint and show up
  only when something unrelated moved. That is one refresh per download
  batch, on every skin: whether a given skin draws the badge is not
  something the addon can see, and a stale badge is a lie either way.

The maintenance contract mirrors pvr.kofin's: a field widgets render but
this module does not hash is a stale-widget bug — extend the section that
covers it, and add the case to test_widgetstate.py. The deliberate
exceptions above (raw counts/timestamps) are the point of the gate; a skin
that renders raw play counts on Home would need them added here.

Reads take no process lock: every Database connection runs WAL, so these
snapshot reads never block a mid-drain writer (and must not — they run on
the service tick). A fingerprint read racing a drain just captures the
half-written state; the drain's own completion re-arms the refresh settle,
so the final state is always fingerprinted and shown.
"""

import hashlib
from typing import Dict, Iterable, Set

from kofin.core.log import Logger
from kofin.sync import kofindb as jellyfin_db
from kofin.sync.db import Database

LOG = Logger(__name__)

# Rows per ordered projection. Matches the most a home widget row shows with
# headroom; a change past this depth is invisible on Home by definition.
TOP_N = 25

VIDEO_REFERENCE_TYPES = ("Movie", "BoxSet", "Series", "Season", "Episode", "MusicVideo")
MUSIC_REFERENCE_TYPES = ("MusicAlbum", "MusicArtist", "Audio")

# Download media types per database, in the store's own spelling. The split
# is the manager's (_mark_dirty): songs move MyMusic, everything else
# MyVideos.
VIDEO_DOWNLOAD_TYPES = ("movie", "episode")
MUSIC_DOWNLOAD_TYPES = ("song",)

# (table, id column, carries idSet, default-rating pointer column) — the video
# item tables widgets render. The pointer column is the one the matching
# ``*_view`` joins the rating table on; musicvideo has no rating at all.
_VIDEO_TABLES = (
    ("movie", "idMovie", True, "c05"),
    ("episode", "idEpisode", False, "c03"),
    ("musicvideo", "idMVideo", False, None),
)

# One scan, two digests: the rating rides along on the pass that already walks
# every row of the table rather than paying for a second one.
_VIDEO_USERDATA = """
SELECT      i.%(id)s, %(idset)s,
            COALESCE(f.playCount, 0) > 0,
            CAST(100.0 * COALESCE(b.timeInSeconds, 0)
                 / MAX(COALESCE(b.totalTimeInSeconds, 0), 1) AS INTEGER),
            %(rating)s
FROM        %(table)s i
JOIN        files f ON f.idFile = i.idFile
LEFT JOIN   bookmark b ON b.idFile = i.idFile AND b.type = 1
%(ratingjoin)s
ORDER BY    i.%(id)s
"""

# Favourites are Kodi *tags* ("Favorite movies"), so a favourite flip moves
# no column the per-row scan above reads and the fingerprint never moved --
# favourites widgets stayed stale (audit A3-M1, live S-P2.3a PARTIAL). One
# scan of the link table covers every tag-driven listing, not just favourites,
# and it is cheap: tag_link holds one row per tagged item, not per item.
_VIDEO_TAG_LINKS = """
SELECT      media_type, media_id, tag_id
FROM        tag_link
WHERE       media_type IN ('movie', 'episode', 'musicvideo', 'tvshow')
ORDER BY    media_type, media_id, tag_id
"""

_VIDEO_RECENT = """
SELECT      i.%(id)s
FROM        %(table)s i
JOIN        files f ON f.idFile = i.idFile
ORDER BY    f.dateAdded DESC, i.%(id)s DESC
LIMIT       %(limit)d
"""

_VIDEO_INPROGRESS = """
SELECT      i.%(id)s
FROM        %(table)s i
JOIN        files f ON f.idFile = i.idFile
JOIN        bookmark b ON b.idFile = i.idFile AND b.type = 1
WHERE       COALESCE(b.timeInSeconds, 0) > 0
ORDER BY    f.lastPlayed DESC, i.%(id)s DESC
LIMIT       %(limit)d
"""

_MUSIC_RECENT_ALBUMS = """
SELECT      idAlbum
FROM        album
ORDER BY    idAlbum DESC
LIMIT       %(limit)d
"""

# Kodi's recently-played albums node orders by the albums' last play, which
# direct writes keep on the song rows; only the resulting *order* is widget
# state, so only it is hashed.
_MUSIC_PLAYED_ALBUMS = """
SELECT      idAlbum
FROM        song
WHERE       COALESCE(lastplayed, '') != ''
GROUP BY    idAlbum
ORDER BY    MAX(lastplayed) DESC, idAlbum DESC
LIMIT       %(limit)d
"""


def _digest(rows: Iterable[object]) -> str:
    digest = hashlib.md5()

    for row in rows:
        digest.update(repr(row).encode("utf-8"))

    return digest.hexdigest()


def _reference_digest(types: Iterable[str]) -> str:
    """kofin.db (jellyfin_id, checksum) rows for the media kinds, sorted for
    determinism (the query has no ORDER BY, and rowid order is not stable
    across a delete-and-readd)."""
    rows: list = []

    with Database("kofin") as kofin_db:
        db = jellyfin_db.JellyfinDatabase(kofin_db.cursor)

        for jellyfin_type in types:
            rows.extend((jellyfin_type, *row) for row in db.get_checksum(jellyfin_type))

    return _digest(sorted(rows))


def _downloads_digest(media_types: Iterable[str]) -> str:
    """The finished-download set for these kinds (see the module docstring).

    Imported here rather than at module scope, like the sync's other reaches
    into the downloads package: the fingerprint runs on the service tick, and
    a profile with downloads disabled still has the table (its DDL is
    kofin.db's), so this stays a cheap read of a small table either way.
    """
    from kofin.downloads import store as downloads_store

    return _digest(downloads_store.done_signature(tuple(media_types)))


def _video_fingerprint() -> Dict[str, str]:
    userdata = hashlib.md5()
    ratings = hashlib.md5()
    recency = hashlib.md5()
    inprogress = hashlib.md5()

    with Database("video") as videodb:
        cursor = videodb.cursor

        for table, id_column, has_set, rating_column in _VIDEO_TABLES:
            params = {
                "table": table,
                "id": id_column,
                "idset": "i.idSet" if has_set else "0",
                "rating": "r.rating" if rating_column else "NULL",
                "ratingjoin": (
                    "LEFT JOIN   rating r ON r.rating_id = i.%s" % rating_column
                    if rating_column
                    else ""
                ),
                "limit": TOP_N,
            }

            cursor.execute(_VIDEO_USERDATA % params)
            for row in cursor.fetchall():
                userdata.update(repr((table, row[:4])).encode("utf-8"))
                ratings.update(repr((table, row[0], row[4])).encode("utf-8"))

            cursor.execute(_VIDEO_RECENT % params)
            recency.update(repr((table, cursor.fetchall())).encode("utf-8"))

            cursor.execute(_VIDEO_INPROGRESS % params)
            inprogress.update(repr((table, cursor.fetchall())).encode("utf-8"))

        # Outside the per-table loop: one scan covers every media type.
        cursor.execute(_VIDEO_TAG_LINKS)
        userdata.update(repr(("tags", cursor.fetchall())).encode("utf-8"))

    return {
        "reference": _reference_digest(VIDEO_REFERENCE_TYPES),
        "downloads": _downloads_digest(VIDEO_DOWNLOAD_TYPES),
        "userdata": userdata.hexdigest(),
        "ratings": ratings.hexdigest(),
        "recency": recency.hexdigest(),
        "inprogress": inprogress.hexdigest(),
    }


def _music_fingerprint() -> Dict[str, str]:
    recency = hashlib.md5()

    with Database("music") as musicdb:
        cursor = musicdb.cursor

        for query in (_MUSIC_RECENT_ALBUMS, _MUSIC_PLAYED_ALBUMS):
            cursor.execute(query % {"limit": TOP_N})
            recency.update(repr(cursor.fetchall()).encode("utf-8"))

    return {
        "reference": _reference_digest(MUSIC_REFERENCE_TYPES),
        "downloads": _downloads_digest(MUSIC_DOWNLOAD_TYPES),
        "recency": recency.hexdigest(),
    }


def fingerprint(db_file: str) -> Dict[str, str]:
    """Section digests of one Kodi database's widget-visible state.

    Raises on any database problem — the caller treats an unreadable
    fingerprint as moved, because refreshing for nothing is recoverable and
    suppressing a real change is not.
    """
    if db_file == "video":
        return _video_fingerprint()

    if db_file == "music":
        return _music_fingerprint()

    return {}


def moved_sections(stored: Dict[str, str], current: Dict[str, str]) -> Set[str]:
    """Section names that differ; every section when nothing is stored yet
    (pvr.kofin's first-poll rule: an unknown model must refresh once)."""
    if not stored:
        return set(current)

    return {name for name, digest in current.items() if stored.get(name) != digest}


# -- container scoping (widget-refresh-plan D6) --------------------------------

_VIDEO_PATH_PREFIXES = ("videodb://", "library://video")
_MUSIC_PATH_PREFIXES = ("musicdb://", "library://music")


def container_kinds(path: str) -> Set[str]:
    """Database kinds the front container renders from, by path family.

    Unknown paths (plugin listings, files views) answer both: refreshing a
    container we cannot classify keeps the old behavior, and only the two
    classifiable families are common enough to matter.
    """
    lowered = (path or "").lower()

    if lowered.startswith(_VIDEO_PATH_PREFIXES):
        return {"video"}

    if lowered.startswith(_MUSIC_PATH_PREFIXES):
        return {"music"}

    if ".xsp" in lowered:
        if "/video/" in lowered:
            return {"video"}

        if "/music/" in lowered:
            return {"music"}

    return {"video", "music"}


def container_wants_refresh(path: str, moved: Iterable[str]) -> bool:
    return bool(container_kinds(path) & set(moved))
