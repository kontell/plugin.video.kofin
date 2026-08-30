"""The kofin.db ``download`` table: what is downloaded, wanted, or failed.

One row per item, keyed by jellyfin id; rows leave the table only on
remove-download. The DDL lives in :func:`kofin.sync.db.kofin_tables` so the
table exists wherever kofin.db exists; every access here goes through the
same :class:`kofin.sync.db.Database` plumbing the rest of the addon uses,
which is what lets the sync writers read download state on the connection
they already hold (plan W1.8).

State machine: queued -> active -> done | failed; failed -> queued on a
re-request (keeping ``bytes_done``, so an original resumes with a Range).
``active`` rows found at manager start are a crash's leftovers and go back
to queued (:func:`recover_interrupted`).
"""

import json
import time
from dataclasses import dataclass, fields as dataclass_fields
from typing import Any, Dict, List, Optional, Sequence

from kofin.core.log import Logger
from kofin.sync.db import Database

LOG = Logger(__name__)

QUEUED = "queued"
ACTIVE = "active"
DONE = "done"
FAILED = "failed"

ORIGIN_USER = "user"
# Automatic downloads carry "auto:<context>" — the retention sweep matches
# the prefix, never the user origin (plan W4.1/W4.2/W4.4).
ORIGIN_AUTO_PREFIX = "auto"


def is_auto_origin(origin: str) -> bool:
    return str(origin or "").startswith(ORIGIN_AUTO_PREFIX)


QUALITY_ORIGINAL = "original"
# Decided at transfer time (downloads/quality.py), recorded so retry and
# recovery know the .part is not byte-stable: a transcode restarts clean
# where an original resumes with a Range.
QUALITY_TRANSCODE = "transcode"

# The two video kinds every per-type policy branches on. "" (unknown) is
# deliberately not here — each consumer decides what unknown means.
VIDEO_MEDIA_TYPES = ("movie", "episode")


@dataclass
class Download:
    jellyfin_id: str
    media_type: str = ""
    series_id: str = ""
    state: str = QUEUED
    origin: str = ORIGIN_USER
    rel_path: str = ""
    container: str = ""
    size_expected: int = 0
    size_actual: int = 0
    quality: str = QUALITY_ORIGINAL
    bytes_done: int = 0
    userdata_json: str = ""
    queued_at: int = 0
    done_at: int = 0
    error: str = ""
    # The raw /MediaSegments body taken at download completion (W4.7):
    # parsed at claim time, where the parser lives. Empty means never
    # fetched (the online claim path falls back to its own fetch); a stored
    # '{"Items": []}' is known-empty, so nobody asks again.
    segments_json: str = ""
    # The files.strFilename the writers had built when the repoint captured
    # the row — restore puts it back verbatim (byte-identical, and correct
    # even for transcodes, whose local name shares nothing with the
    # server's). Re-captured whenever a writer pass rebuilds the row, so a
    # server-side rename never restores a stale URL.
    restore_filename: str = ""
    # The path.strPath a song sat on when the repoint captured it — the
    # music side's second half. A song's server row is referenced by nothing
    # while the download lives (MyMusic has one path row per song and the
    # repoint moves the song off it), so the row can be gone by restore
    # time, and the mapping's id alone cannot bring it back; the string can.
    # Empty on rows captured before the column existed, or on video rows,
    # whose path rows are shared and never orphaned by a repoint.
    restore_path: str = ""

    @property
    def userdata(self) -> Dict[str, Any]:
        """The server UserData snapshot taken at queue time, {} when none."""
        if not self.userdata_json:
            return {}
        try:
            parsed = json.loads(self.userdata_json)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}


_COLUMNS = tuple(field.name for field in dataclass_fields(Download))


def _row_to_download(row: Any) -> Download:
    values = {name: value for name, value in zip(_COLUMNS, row) if value is not None}
    return Download(**values)


_SELECT = "SELECT %s FROM download" % ", ".join(_COLUMNS)


def queue(download: Download) -> bool:
    """Add a download request; True when it was actually (re)queued.

    An absent row is inserted queued. A failed row is re-queued in place,
    keeping ``bytes_done`` so an original resumes where it died. A row that
    is queued, active or done is left alone — the menu should not have
    offered Download, and a double-tap must not double-fetch.
    """
    now = download.queued_at or int(time.time())
    with Database("kofin") as opened:
        opened.cursor.execute(
            "SELECT state FROM download WHERE jellyfin_id = ?",
            (download.jellyfin_id,),
        )
        existing = opened.cursor.fetchone()
        if existing is None:
            values = {name: getattr(download, name) for name in _COLUMNS}
            values.update(state=QUEUED, queued_at=now, error="")
            opened.cursor.execute(
                "INSERT INTO download (%s) VALUES (%s)"
                % (", ".join(_COLUMNS), ", ".join("?" for _ in _COLUMNS)),
                tuple(values[name] for name in _COLUMNS),
            )
            return True
        if existing[0] == FAILED:
            # A failed transcode's frozen target is worthless — nothing
            # resumes into a re-encode — and can itself be the failure (a
            # name the attempt could not put on disk), so it re-freezes
            # fresh. Originals keep theirs: that is the Range resume. The
            # CASE reads the pre-update row, as SQLite guarantees.
            opened.cursor.execute(
                "UPDATE download SET state = ?, queued_at = ?, error = '', "
                "rel_path = CASE WHEN quality = ? THEN '' ELSE rel_path END, "
                "bytes_done = CASE WHEN quality = ? THEN 0 ELSE bytes_done END, "
                "quality = ?, origin = ? WHERE jellyfin_id = ?",
                (
                    QUEUED,
                    now,
                    QUALITY_TRANSCODE,
                    QUALITY_TRANSCODE,
                    download.quality,
                    download.origin,
                    download.jellyfin_id,
                ),
            )
            return True
    return False


def get(jellyfin_id: str) -> Optional[Download]:
    with Database("kofin") as opened:
        return get_on(opened.cursor, jellyfin_id)


def get_on(cursor: Any, jellyfin_id: str) -> Optional[Download]:
    """`get` on a caller-held cursor — for the sync writers, which hold the
    kofin.db connection inside a transaction where a second connection would
    sit on the WAL write lock (plan W1.8)."""
    cursor.execute(_SELECT + " WHERE jellyfin_id = ?", (jellyfin_id,))
    row = cursor.fetchone()
    return _row_to_download(row) if row is not None else None


def is_done_on(cursor: Any, jellyfin_id: str) -> bool:
    cursor.execute(
        "SELECT 1 AS present FROM download WHERE jellyfin_id = ? AND state = ? LIMIT 1",
        (jellyfin_id, DONE),
    )
    return cursor.fetchone() is not None


def series_done_ids(series_id: str) -> List[str]:
    """Jellyfin ids of the completed downloads under a show."""
    if not series_id:
        return []
    with Database("kofin") as opened:
        opened.cursor.execute(
            "SELECT jellyfin_id FROM download WHERE series_id = ? AND state = ?",
            (series_id, DONE),
        )
        return [row[0] for row in opened.cursor.fetchall()]


def container_states(container_id: str) -> Dict[str, str]:
    """``{jellyfin_id: state}`` for every download under a container.

    Two lookups, because the table records only one parent. ``series_id`` is
    written as ``SeriesId or AlbumId`` (manager._transfer), which answers a
    Series and a MusicAlbum outright; a Season is nobody's ``series_id``, so
    its children are found through the kofin.db mapping's ``parent_id``,
    which is exactly what that column and its index are for. One connection,
    and no server — the context menu that asks this has to answer offline
    too.
    """
    if not container_id:
        return {}
    states: Dict[str, str] = {}
    with Database("kofin") as opened:
        opened.cursor.execute(
            "SELECT jellyfin_id, state FROM download WHERE series_id = ?",
            (container_id,),
        )
        states.update(opened.cursor.fetchall())
        opened.cursor.execute(
            "SELECT d.jellyfin_id, d.state FROM download d "
            "JOIN jellyfin j ON j.jellyfin_id = d.jellyfin_id "
            "WHERE j.parent_id = ?",
            (container_id,),
        )
        states.update(opened.cursor.fetchall())
    return states


def container_counts(container_id: str) -> Dict[str, int]:
    """How many downloads under a container are finished, and how many are
    still coming. What a container's context menu offers is decided from
    these two numbers — a failed row counts as neither, because the menu's
    answer to it is Download, same as for an item nobody ever asked for."""
    states = container_states(container_id)
    return {
        "done": sum(1 for state in states.values() if state == DONE),
        "pending": sum(1 for state in states.values() if state in (QUEUED, ACTIVE)),
    }


def container_done_ids(container_id: str) -> List[str]:
    """The finished downloads under a container — what "Remove download" on
    a show or an album actually removes."""
    return sorted(
        item_id
        for item_id, state in container_states(container_id).items()
        if state == DONE
    )


def container_pending_ids(container_id: str) -> List[str]:
    """The unfinished downloads under a container — what "Cancel download"
    on a show or an album actually cancels."""
    return sorted(
        item_id
        for item_id, state in container_states(container_id).items()
        if state in (QUEUED, ACTIVE)
    )


def series_done_on(cursor: Any, series_id: str) -> bool:
    if not series_id:
        return False
    cursor.execute(
        "SELECT 1 AS present FROM download WHERE series_id = ? AND state = ? LIMIT 1",
        (series_id, DONE),
    )
    return cursor.fetchone() is not None


def claim(media_types: Optional[Sequence[str]] = None) -> Optional[Download]:
    """Move the oldest queued row to active and return it; None when idle.

    Race-safe across worker threads without RETURNING (the deployed SQLite
    floor is not ours to raise): each candidate is taken with a guarded
    UPDATE, and a loser's zero rowcount just moves it to the next candidate.

    ``media_types`` scopes a worker pool to its own kind. An empty
    ``media_type`` — a row queued before the kind travelled with the id, or
    one whose sender did not know it — matches only a caller that names ""
    among its kinds, which is the video pool: unknown work must be claimed
    by somebody, and the video pool is the one whose pacing assumes an item
    might be large.
    """
    kinds = list(media_types) if media_types is not None else None
    if kinds is not None and not kinds:
        return None
    where = " WHERE state = ?"
    params: List[Any] = [QUEUED]
    if kinds is not None:
        clause = "media_type IN (%s)" % ", ".join("?" for _ in kinds)
        params.extend(kinds)
        if "" in kinds:
            # NULL is unknown too, and `IN` would never match it. The
            # dataclass writes '' so no path here produces one — but a row
            # no pool can claim is silently stuck forever, which is not a
            # bet worth taking against a column that is merely TEXT.
            clause = "(%s OR media_type IS NULL)" % clause
        where += " AND " + clause
    while True:
        with Database("kofin") as opened:
            opened.cursor.execute(
                _SELECT + where + " ORDER BY queued_at, jellyfin_id LIMIT 1",
                tuple(params),
            )
            row = opened.cursor.fetchone()
            if row is None:
                return None
            candidate = _row_to_download(row)
            opened.cursor.execute(
                "UPDATE download SET state = ? WHERE jellyfin_id = ? AND state = ?",
                (ACTIVE, candidate.jellyfin_id, QUEUED),
            )
            if opened.cursor.rowcount == 1:
                candidate.state = ACTIVE
                return candidate
        # Another worker claimed it between the SELECT and the UPDATE.


def record_progress(jellyfin_id: str, bytes_done: int) -> None:
    with Database("kofin") as opened:
        opened.cursor.execute(
            "UPDATE download SET bytes_done = ? WHERE jellyfin_id = ?",
            (int(bytes_done), jellyfin_id),
        )


def finish(
    jellyfin_id: str,
    rel_path: str,
    container: str,
    size_actual: int,
    done_at: int = 0,
) -> None:
    with Database("kofin") as opened:
        opened.cursor.execute(
            "UPDATE download SET state = ?, rel_path = ?, container = ?, "
            "size_actual = ?, bytes_done = ?, done_at = ?, error = '' "
            "WHERE jellyfin_id = ?",
            (
                DONE,
                rel_path,
                container,
                int(size_actual),
                int(size_actual),
                done_at or int(time.time()),
                jellyfin_id,
            ),
        )


def record_target(jellyfin_id: str, rel_path: str, container: str) -> None:
    """Freeze the target path the moment it is first known, so a retry or a
    restart resumes the same ``.part`` instead of re-deciding the name."""
    with Database("kofin") as opened:
        opened.cursor.execute(
            "UPDATE download SET rel_path = ?, container = ? WHERE jellyfin_id = ?",
            (rel_path, container, jellyfin_id),
        )


def record_details(
    jellyfin_id: str,
    media_type: str,
    series_id: str,
    size_expected: int,
    userdata_json: str,
    quality: str = QUALITY_ORIGINAL,
) -> None:
    """Fill what only the item DTO knows, at download time: the queue path
    carries bare ids across the IPC bus, and the worker holds the DTO (and
    the quality decision, made against the same fetch)."""
    with Database("kofin") as opened:
        opened.cursor.execute(
            "UPDATE download SET media_type = ?, series_id = ?, "
            "size_expected = ?, userdata_json = ?, quality = ? "
            "WHERE jellyfin_id = ?",
            (
                media_type,
                series_id,
                int(size_expected),
                userdata_json,
                quality,
                jellyfin_id,
            ),
        )


def set_segments(jellyfin_id: str, raw_json: str) -> None:
    with Database("kofin") as opened:
        opened.cursor.execute(
            "UPDATE download SET segments_json = ? WHERE jellyfin_id = ?",
            (raw_json, jellyfin_id),
        )


def set_restore_filename_on(cursor: Any, jellyfin_id: str, filename: str) -> None:
    cursor.execute(
        "UPDATE download SET restore_filename = ? WHERE jellyfin_id = ?",
        (filename, jellyfin_id),
    )


def set_restore_path_on(cursor: Any, jellyfin_id: str, path: str) -> None:
    cursor.execute(
        "UPDATE download SET restore_path = ? WHERE jellyfin_id = ?",
        (path, jellyfin_id),
    )


def release(jellyfin_id: str) -> None:
    """Put an active row back to queued without spending an attempt — the
    outage interruption: the worker that owns the row calls this, so it
    cannot race another claim, and ``recover_interrupted`` cannot help
    until a restart (it runs only at manager start, so a row left active
    mid-session would sit stuck until then). ``queued_at`` stays, keeping
    the row at the head of the queue for the reconnect."""
    with Database("kofin") as opened:
        opened.cursor.execute(
            "UPDATE download SET state = ? WHERE jellyfin_id = ? AND state = ?",
            (QUEUED, jellyfin_id, ACTIVE),
        )


def fail(jellyfin_id: str, error: str) -> None:
    with Database("kofin") as opened:
        opened.cursor.execute(
            "UPDATE download SET state = ?, error = ? WHERE jellyfin_id = ?",
            (FAILED, str(error)[:500], jellyfin_id),
        )


def remove(jellyfin_id: str) -> None:
    with Database("kofin") as opened:
        opened.cursor.execute(
            "DELETE FROM download WHERE jellyfin_id = ?", (jellyfin_id,)
        )


def rows(state: Optional[str] = None) -> List[Download]:
    with Database("kofin") as opened:
        if state is None:
            opened.cursor.execute(_SELECT + " ORDER BY queued_at, jellyfin_id")
        else:
            opened.cursor.execute(
                _SELECT + " WHERE state = ? ORDER BY queued_at, jellyfin_id",
                (state,),
            )
        fetched = opened.cursor.fetchall()
    return [_row_to_download(row) for row in fetched]


def done_signature(media_types: Sequence[str]) -> List[Any]:
    """``(jellyfin_id, media_type)`` for every finished download of these
    kinds, sorted — the widget fingerprint's downloads section.

    Here rather than in ``sync/widgetstate.py`` because it is a fact about
    this table, and it is the *set* that is widget state: a completed
    download stamps a badge art row and a removed one clears it, neither of
    which moves a checksum, a rating, a play count or an order — so nothing
    else the fingerprint hashes can see either happen. Only finished rows
    count: queued and active ones render nothing (the badge goes on at the
    end), and hashing them would refresh every widget at queue time for no
    visible change.
    """
    if not media_types:
        return []
    placeholders = ",".join("?" for _ in media_types)
    with Database("kofin") as opened:
        opened.cursor.execute(
            "SELECT jellyfin_id, media_type FROM download "
            "WHERE state = ? AND media_type IN (%s)" % placeholders,
            (DONE, *media_types),
        )
        return sorted(tuple(row) for row in opened.cursor.fetchall())


def is_done(jellyfin_id: str) -> bool:
    row = get(jellyfin_id)
    return row is not None and row.state == DONE


def pending_count() -> int:
    """Queued + active rows — what the progress bar calls the remainder."""
    with Database("kofin") as opened:
        opened.cursor.execute(
            "SELECT COUNT(*) FROM download WHERE state IN (?, ?)",
            (QUEUED, ACTIVE),
        )
        return int(opened.cursor.fetchone()[0])


def recover_interrupted() -> int:
    """Re-queue rows a crash left active; returns how many. Manager start.

    ``bytes_done`` survives, so a recovered original resumes with a Range
    rather than starting over; the manager deletes stale ``.part`` files for
    transcodes itself (they are not byte-stable across attempts).
    """
    with Database("kofin") as opened:
        opened.cursor.execute(
            "UPDATE download SET state = ? WHERE state = ?", (QUEUED, ACTIVE)
        )
        moved = opened.cursor.rowcount
    if moved:
        LOG.info("re-queued %d interrupted download(s)", moved)
    return int(moved)
