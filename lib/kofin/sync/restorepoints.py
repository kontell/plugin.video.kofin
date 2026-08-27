"""Restore points: where an interrupted walk resumes (P2.2, pure).

A restore point is an index into a result set, kept in ``sync.json`` under
``RestorePoints`` until the walk that owns it completes. The store is the
caller's dict; nothing here touches disk or Kodi.
"""

import time
from typing import Any, Dict, List, Optional

from kofin.core.log import Logger

LOG = Logger(__name__)

# An interrupted pass retries on the resume backoff, which tops out at 30
# minutes, so a point that has not been picked up within this is not a pass
# waiting to continue, it is a corpse.
TTL = 6 * 3600


def resume_at(
    store: Dict[str, Any],
    key: str,
    fingerprint: Optional[str] = None,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """The position to resume this walk at, when it still means something.

    Nothing else expires a point, so one could outlive the pass it belonged
    to indefinitely: a movies point reading ``StartIndex: 1250`` was found
    on a live box having crossed an addon upgrade (its stored url was the
    pre-10.9 ``/Users/{id}/Items`` route), left behind because update mode
    reconciles through the prune and never runs the walk that would have
    cleared it.

    Resuming into a stale number is silent and one-directional. The walk
    sorts DateCreated descending, so N items added since the point was
    written push everything down by N: the resumed pass re-does N items it
    had already done (idempotent, harmless) and **never visits the N
    newest** -- the items a user is most likely to be waiting for. So an
    unusable point is dropped rather than trusted; a walk from zero is
    idempotent and Etag-short-circuits, which makes discarding cheap and
    resuming wrongly the only expensive option.

    Two ways to be unusable, both checked here: the query changed, so the
    number indexes a different set (``downloader.restore_fingerprint``; an
    upgrade that adds a field is the routine case), and it is too old to be
    a resume at all (``TTL``).
    """
    entry = store.get(key)

    if not entry:
        return None

    stored = entry.get("Fingerprint")

    if fingerprint is not None and stored != fingerprint:
        LOG.info(
            "--[ restore point/%s ] discarded: the query changed since it "
            "was written",
            key,
        )
        clear(store, key)

        return None

    if expired(entry, now):
        LOG.info("--[ restore point/%s ] discarded: older than %s seconds", key, TTL)
        clear(store, key)

        return None

    params: Optional[Dict[str, Any]] = entry.get("params")
    return params


def expired(entry: Dict[str, Any], now: Optional[float] = None) -> bool:
    """Whether a stored point is too old to be a resume.

    An unstamped point is expired by definition: it predates this check, so
    it is exactly the kind that has been sitting there across upgrades.
    """
    saved_at = entry.get("SavedAt")

    if not saved_at:
        return True

    try:
        age = (time.time() if now is None else now) - float(saved_at)
    except (TypeError, ValueError):
        return True

    return age > TTL


def save(
    store: Dict[str, Any],
    key: str,
    point: Dict[str, Any],
    fingerprint: Optional[str],
    now: Optional[float] = None,
) -> None:
    stamped = dict(point)
    stamped["SavedAt"] = time.time() if now is None else now
    stamped["Fingerprint"] = fingerprint
    store[key] = stamped


def clear(store: Dict[str, Any], key: str) -> None:
    store.pop(key, None)


def clear_library(store: Dict[str, Any], library_id: str) -> List[str]:
    """Drop every restore point belonging to a library.

    Update mode reconciles through the prune and never runs the walk that
    owns the point, so without this a library proven fully in sync keeps a
    position claiming it is part-way through one -- which is how the live
    one survived. Keyed by prefix because a library owns several (the
    tvshows walk keeps one slot per pass).
    """
    prefix = "%s/" % library_id
    stale = [key for key in store if key.startswith(prefix)]

    for key in stale:
        LOG.info("--[ restore point/%s ] cleared: library reconciled", key)
        clear(store, key)

    return stale
