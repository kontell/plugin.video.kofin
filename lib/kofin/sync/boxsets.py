"""The boxsets pass (P2.2): walk, sweep, restamp, refresh.

Beyond the fork (docs/boxsets-robustness-plan.md): the walk asks the
server for ChildCount -- the unlink guard's server signal, measured
harmless at set counts and deliberately not added to the shared info()
field list -- tallies per-set outcomes into one summary line, sweeps
references the server listing no longer contains, and ends by re-stamping
every non-guarded set's state from measured reality (shared members drift
the mid-walk stamps; healing-loops-plan F1).

Each function takes the running ``FullSync``: the walk, the locks and the
restore points are its, and the pass is one caller of them.
"""

from typing import Any, Dict, Set

from kofin.core.log import Logger
from kofin.sync import downloader as server
from kofin.sync import kofindb as jellyfin_db
from kofin.sync.shims import localized
from kofin.sync.writers import Movies
from kofin.sync.writers.movies import (
    BOXSET_GUARDED,
    BOXSET_HEALED,
    BOXSET_UNCHANGED,
    BOXSET_WRITTEN,
)

LOG = Logger(__name__)


def walk(sync: Any, library: Dict[str, Any], dialog: Any) -> None:
    """Process all boxsets of the boxsets library."""
    restore_key = "%s/boxsets" % library["Id"]
    boxset_params = {"Fields": "%s,ChildCount" % server.info()}
    stats = {
        BOXSET_UNCHANGED: 0,
        BOXSET_WRITTEN: 0,
        BOXSET_HEALED: 0,
        BOXSET_GUARDED: 0,
    }

    # Lock first, fresh connections per page, commit at page exit --
    # kofin.db outermost so MyVideos commits first (video_database_locks).
    resumed, skipped, results = sync._walk(
        library,
        "BoxSet",
        restore_key,
        lambda jellyfindb, videodb: Movies(sync.server, jellyfindb, videodb, library),
        lambda obj, boxset: obj.boxset(boxset),
        lambda boxset: boxset["Name"],
        dialog,
        "%s: %s" % ("Kofin", localized(30407)),
        sync.video_database_locks,
        params=boxset_params,
    )

    # Every set the listing carried counts as walked, skipped ones
    # included: the sweep below treats absence from ``walked`` as
    # deletion, and a set that 404'd mid-page is gone from the server
    # anyway -- the next fresh walk sweeps its reference.
    walked = {item["Id"] for item, _ in results} | set(skipped)
    guarded_ids: Set[str] = set()

    for boxset, outcome in results:
        if outcome in stats:
            stats[outcome] += 1

        if outcome == BOXSET_GUARDED:
            guarded_ids.add(boxset["Id"])

    sync.clear_restore_point(restore_key)

    # A resumed walk never listed its earlier pages, so only a fresh,
    # complete walk may treat absence from the listing as deletion.
    swept = 0 if resumed else sync.sweep_stale_boxsets(walked)

    # Walk-end restamp (docs/healing-loops-plan.md F1): after the sweep,
    # so measured state covers exactly the references that survived. It
    # runs on resumed walks too -- it is measurement, not deletion, so
    # the fresh-start gate above does not apply.
    with sync.video_database_locks() as (videodb, jellyfindb):
        Movies(sync.server, jellyfindb, videodb).restamp_boxset_states(guarded_ids)

    LOG.info(
        "boxsets: %s checked (%s unchanged, %s written, %s healed, "
        "%s guarded, %s swept)",
        len(walked),
        stats[BOXSET_UNCHANGED],
        stats[BOXSET_WRITTEN],
        stats[BOXSET_HEALED],
        stats[BOXSET_GUARDED],
        swept,
    )


def sweep_stale(sync: Any, walked: Set[str]) -> int:
    """Remove set references the server listing no longer contains.

    The walk is the same listing the writes came from, so a reference
    absent from it is a set deleted server-side with no record to say so
    -- the prune never covers boxsets, and without a change-feed Removed
    record such a set was a ghost forever. An empty listing against
    existing references is not a deletion order (permission and filter
    failures look exactly like it): skip and warn, mirroring the prune's
    get_existing_ids philosophy.
    """
    with sync.video_database_locks() as (videodb, jellyfindb):
        db = jellyfin_db.JellyfinDatabase(jellyfindb.cursor)
        known = [row[0] for row in db.get_items_by_media("set")]
        stale = [item_id for item_id in known if item_id not in walked]

        if not walked and known:
            LOG.warning(
                "boxsets walk listed no sets while %s are referenced; "
                "skipping the sweep (an empty listing is not a deletion "
                "order)",
                len(known),
            )
            return 0

        if not stale:
            return 0

        obj = Movies(sync.server, jellyfindb, videodb)

        for item_id in stale:
            obj.remove(item_id)

    LOG.info("swept %s stale boxset(s): %s", len(stale), ", ".join(stale[:5]))

    return len(stale)


def refresh(sync: Any, library: Dict[str, Any]) -> None:
    """Delete all existing boxsets and re-add."""
    with sync.video_database_locks() as (videodb, jellyfindb):
        db = jellyfin_db.JellyfinDatabase(jellyfindb.cursor)
        before = len(db.get_items_by_media("set"))
        obj = Movies(sync.server, jellyfindb, videodb, library)
        obj.boxsets_reset()

    LOG.info("refresh boxsets: reset %s set(s), re-adding", before)
    sync.boxsets(library)
