"""The update-mode plan (P2.2; phase 5, research §3 "update that works").

Page a library's id+Etag set, diff it against kofin.db three ways --
missing here → fetch; stale here → remove; Etag mismatch → fetch; match →
nothing -- and hand the work to the incremental pipeline through the host
(downloads Etag-short-circuit again on write, removals route through the
SortWorker). The catch-up that runs alongside (Update = sync-queue catch-up
**plus** this) covers userdata.
"""

from typing import Any, Dict, List, Optional, Tuple

from kofin.core.log import Logger
from kofin.sync import changefeed
from kofin.sync import downloader as server
from kofin.sync.db import Database
from kofin.sync.fields import find_library, reference_checksum
from kofin.sync import kofindb as jellyfin_db
from kofin.sync.shims import localized

LOG = Logger(__name__)

PRUNE_SERVER_TYPES = {
    "movies": "Movie",
    "tvshows": "Series,Season,Episode",
    "musicvideos": "MusicVideo",
    "music": "MusicAlbum,Audio",
}


def local_reference_map(library_id, media_class):
    """{jellyfin_id: stored checksum} for everything kofin.db attributes
    to the library.

    Movies/musicvideos/music rows carry media_folder directly. TV
    children (seasons/episodes) do not — they are collected through the
    kodi-id parent chain plus the jellyfin_parent_id fallback, mirroring
    the writers' get_child walk. Checksums load once per involved
    jellyfin_type via the existing get_checksum query.

    Module-level so the divergence probe (library.py) can measure the same
    local set the prune diffs without constructing a FullSync; a probe that
    counted a different set than the prune would schedule heals the prune
    then reports nothing to do.
    """
    top_types = {
        "movies": ("Movie",),
        "tvshows": ("Series",),
        "musicvideos": ("MusicVideo",),
        # MusicArtist rows also carry media_folder but are not pruned:
        # artists are not reliably reachable via /Items under a library
        # parent, so a stale artist row lingers until Repair (rare —
        # artists rarely vanish without their albums going too).
        "music": ("MusicAlbum", "Audio"),
    }[media_class]

    checksum_types = {
        "movies": ("Movie",),
        "tvshows": ("Series", "Season", "Episode"),
        "musicvideos": ("MusicVideo",),
        "music": ("MusicAlbum", "Audio"),
    }[media_class]

    with Database("kofin") as kofin_db:
        db = jellyfin_db.JellyfinDatabase(kofin_db.cursor)

        checksums = {}
        for jellyfin_type in checksum_types:
            for row in db.get_checksum(jellyfin_type):
                checksums[row[0]] = row[1]

        ids = []
        series_ids = []

        for row in db.get_item_by_media_folder(library_id):
            if row[1] in top_types:
                ids.append(row[0])
            if row[1] == "Series":
                series_ids.append(row[0])

        if media_class == "tvshows":
            for series_id in series_ids:
                reference = db.get_item_by_id(series_id)

                if reference is None:
                    continue

                for season in db.get_item_id_by_parent_id(reference.kodi_id, "season"):
                    ids.append(season[0])

                    for episode in db.get_item_id_by_parent_id(season[1], "episode"):
                        ids.append(episode[0])

                # Episodes referencing the series directly (the writers'
                # get_child fallback arm).
                for row in db.get_media_by_parent_id(series_id):
                    ids.append(row[0])

    return {item_id: checksums.get(item_id) for item_id in dict.fromkeys(ids)}


def plan(
    api: Any, host: Any, library: Dict[str, Any], library_id: str, dialog: Any
) -> None:
    """Diff one library and enqueue the work. ``dialog`` is the progress
    bar of the pass; ``host`` takes the plan (the Library, or the tests'
    FakeHost -- the port named above ``Library.claim``)."""
    classes: Tuple[Optional[str], ...]
    if library_id.startswith("Mixed:"):
        classes = ("movies", "tvshows")
    else:
        classes = (library.get("CollectionType"),)

    missing: List[Tuple[int, str]] = []
    changed: List[str] = []
    stale: List[str] = []

    for media_class in classes:
        server_types = PRUNE_SERVER_TYPES.get(media_class or "")

        if not server_types:
            LOG.info("prune skips %s (%s)", library["Id"], media_class)
            continue

        dialog.update(
            0,
            heading="%s: %s" % ("Kofin", library["Name"]),
            message=localized(30603),
        )

        server_map = server.get_id_etag_map(api, library["Id"], server_types)
        local_map = local_reference_map(library["Id"], media_class)

        for item_id, (etag, item_type) in server_map.items():
            if item_id not in local_map:
                missing.append((changefeed.type_rank(item_type), item_id))
                continue

            # No Etag from the server (unexpected with Fields=Etag) →
            # re-fetch: the safe direction is a redundant download.
            if not etag or local_map[item_id] != reference_checksum(etag):
                changed.append(item_id)

        for item_id in local_map:
            if item_id not in server_map:
                stale.append(item_id)

    # Parent-first, by the same ranks the typed feed sorts additions by:
    # get_id_etag_map pages in SortName order, which interleaves
    # Series/Season/Episode (and MusicAlbum/Audio), so a child could be
    # downloaded and written while its parent sat in a later chunk. The
    # writers heal that by fetching the parent inside the write lock,
    # which is a fallback and not something to route work into. Stable, so
    # SortName order survives within a rank and paging stays predictable.
    missing.sort(key=lambda entry: entry[0])
    missing_ids = [item_id for _rank, item_id in missing]

    # Confirm every stale candidate by id before deleting anything. The
    # diff above infers "stale" from absence in a *filtered listing*, and
    # a listing can omit an item that is alive and well -- so the removal
    # arm, the only destructive one here, asks the server directly instead
    # of trusting the inference. See get_existing_ids.
    #
    # Failure to confirm leaves the candidate alone: the invariant is that
    # nothing is removed on an unverified id, so a confirmation that could
    # not be made must not read as "gone".
    spared: List[str] = []

    if stale:
        resolved = server.get_existing_ids(api, stale)

        if resolved:
            spared = [item_id for item_id in stale if item_id in resolved]
            stale = [item_id for item_id in stale if item_id not in resolved]

    LOG.info(
        "--[ prune/%s ] missing:%s changed:%s stale:%s spared:%s",
        library["Id"],
        len(missing_ids),
        len(changed),
        len(stale),
        len(spared),
    )

    if spared:
        # Not routine: the library listing and the reference set disagree
        # about an item -- the signature of a misattributed media_folder,
        # a series pooled under whichever library saw it first
        # (healing-loops-plan F2). Warn, then re-home instead of only
        # sparing: left alone the same ids spare and warn on every prune
        # and hold probe_divergence permanently diverged.
        LOG.warning(
            "prune/%s spared %s stale candidate(s) the server still " "resolves: %s",
            library["Id"],
            len(spared),
            ", ".join(sorted(spared)[:10]),
        )
        rehome_spared(api, spared)

    host.removed(stale)
    host.added(missing_ids)
    host.updated(changed)


def rehome_spared(api: Any, spared: List[str]) -> None:
    """Move spared references to the library the server says owns them.

    One Ancestors round trip per spared id -- rare by construction --
    re-homes it to its whitelisted ancestor view, or to NULL (the pool
    placeholder state) when no synced library owns it. Either way the
    next prune's local map matches the server's listing and the loop
    closes. Seasons and episodes are exempt: they carry no media_folder
    by design and their fate follows their series. A resolution failure
    skips the id; the next prune retries.
    """
    with Database("kofin") as jellyfindb:
        db = jellyfin_db.JellyfinDatabase(jellyfindb.cursor)

        for item_id in sorted(spared):
            if db.get_media_by_id(item_id) in ("Season", "Episode"):
                continue

            try:
                home = find_library(api, {"Id": item_id})
            except Exception as error:
                LOG.warning("could not re-home %s: %s", item_id, error)
                continue

            folder = home["Id"] if home else None
            db.update_media_folder(folder, item_id)
            LOG.warning("re-homed spared %s to %s", item_id, folder or "placeholder")
