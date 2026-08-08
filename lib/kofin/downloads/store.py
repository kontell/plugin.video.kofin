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
from typing import Any, Dict, List, Optional, Set

from kofin.core.log import Logger
from kofin.sync.db import Database

LOG = Logger(__name__)

QUEUED = "queued"
ACTIVE = "active"
DONE = "done"
FAILED = "failed"

ORIGIN_USER = "user"

QUALITY_ORIGINAL = "original"


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
    # The files.strFilename the writers had built when the repoint captured
    # the row — restore puts it back verbatim (byte-identical, and correct
    # even for transcodes, whose local name shares nothing with the
    # server's). Re-captured whenever a writer pass rebuilds the row, so a
    # server-side rename never restores a stale URL.
    restore_filename: str = ""

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
            opened.cursor.execute(
                "UPDATE download SET state = ?, queued_at = ?, error = '', "
                "quality = ?, origin = ? WHERE jellyfin_id = ?",
                (QUEUED, now, download.quality, download.origin, download.jellyfin_id),
            )
            return True
    return False


def get(jellyfin_id: str) -> Optional[Download]:
    with Database("kofin") as opened:
        opened.cursor.execute(_SELECT + " WHERE jellyfin_id = ?", (jellyfin_id,))
        row = opened.cursor.fetchone()
    return _row_to_download(row) if row is not None else None


def claim() -> Optional[Download]:
    """Move the oldest queued row to active and return it; None when idle.

    Race-safe across worker threads without RETURNING (the deployed SQLite
    floor is not ours to raise): each candidate is taken with a guarded
    UPDATE, and a loser's zero rowcount just moves it to the next candidate.
    """
    while True:
        with Database("kofin") as opened:
            opened.cursor.execute(
                _SELECT + " WHERE state = ? ORDER BY queued_at, jellyfin_id LIMIT 1",
                (QUEUED,),
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


def set_restore_filename(jellyfin_id: str, filename: str) -> None:
    with Database("kofin") as opened:
        opened.cursor.execute(
            "UPDATE download SET restore_filename = ? WHERE jellyfin_id = ?",
            (filename, jellyfin_id),
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


def is_done(jellyfin_id: str) -> bool:
    row = get(jellyfin_id)
    return row is not None and row.state == DONE


def done_ids() -> Set[str]:
    with Database("kofin") as opened:
        opened.cursor.execute(
            "SELECT jellyfin_id FROM download WHERE state = ?", (DONE,)
        )
        fetched = opened.cursor.fetchall()
    return {row[0] for row in fetched}


def series_has_done(series_id: str) -> bool:
    """Any completed download under this show (the tvshow tag lookup)."""
    if not series_id:
        return False
    with Database("kofin") as opened:
        opened.cursor.execute(
            "SELECT 1 FROM download WHERE series_id = ? AND state = ? LIMIT 1",
            (series_id, DONE),
        )
        found = opened.cursor.fetchone()
    return found is not None


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
