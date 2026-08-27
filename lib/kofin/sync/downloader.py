# -*- coding: utf-8 -*-
"""Server listing helpers for the sync pipeline (fork ``downloader.py``
port): the /Items query shapes and the pager -- in-order paging with a
look-ahead thread pool. The incremental GetItemWorker lives in workers.py.

Adaptations per plan §3: every helper takes the kofin ``Api`` as its first
argument instead of reaching for the fork's client singleton; the field
constants from the fork's ``jellyfin/api.py`` live here now; the dead
``validate_view``/``get_single_item`` helpers are dropped (no callers in the
fork either).
"""

import json
import threading
import concurrent.futures
from datetime import date

from typing import Any, Dict, List, Tuple

from kofin.core import settings, state
from kofin.core.log import Logger
from kofin.sync.shims import LibraryException, LibraryExitException, raise_if_stopping

LOG = Logger(__name__)

# Jellyfin types that are containers or metadata nodes rather than library
# content. The server broadcasts LibraryChanged for these as a matter of
# course -- writing a playlist touches the Playlists view, a scan touches the
# collection folder -- and no writer queue will ever consume one, so "nothing
# consumes this type" is the expected outcome here and not a lost item.
#
# Deliberately narrower than ``plugin.listitems.FOLDER_TYPES``, which is about
# what browses as a folder: Series/Season/BoxSet/MusicArtist/MusicAlbum are
# folders there but are synced content here, and must keep reaching
# ``_flag_unapplied`` when they fail to route.
NON_CONTENT_TYPES = frozenset(
    {
        "AggregateFolder",
        "CollectionFolder",
        "Folder",
        "Genre",
        "ManualPlaylistsFolder",
        "MusicGenre",
        "Person",
        "PhotoAlbum",
        "Playlist",
        "Studio",
        "UserRootFolder",
        "UserView",
        "Year",
    }
)


def basic_info():
    return "Etag"


def info():
    return (
        "Path,Genres,SortName,Studios,Writer,Taglines,LocalTrailerCount,"
        "OfficialRating,CumulativeRunTimeTicks,ItemCounts,"
        "Metascore,AirTime,DateCreated,People,Overview,"
        "Etag,ShortOverview,ProductionLocations,"
        "Tags,ProviderIds,ParentId,RemoteTrailers,SpecialEpisodeNumbers,"
        "MediaSources,VoteCount,RecursiveItemCount,PrimaryImageAspectRatio,"
        "SpecialFeatureCount"
    )


def music_info():
    return (
        "Etag,Genres,SortName,Studios,Writer,"
        "OfficialRating,CumulativeRunTimeTicks,Metascore,"
        "AirTime,DateCreated,MediaStreams,People,ProviderIds,Overview,ItemCounts"
    )


# Costs the server a recursive child count per item on the page. Albums have
# children, so it dominates their pages; measured on Jellyfin 10.11 against a
# real library, one page of 100 albums took 19.8s with the field and 1.6s
# without — the whole album pass, near enough. Only the Series and BrowseVideo
# object maps read it, so the music passes drop it and the video passes cannot.
_RECURSIVE_COUNT = "RecursiveItemCount"


def music_page_info():
    """``info()`` minus the field no music object maps.

    The album and song passes need the full-fidelity field set (songs read
    ``Path`` and ``MediaSources``, which ``music_info`` lacks), so this is
    ``info()`` with the one expensive field removed rather than a smaller list
    — nothing a music writer reads changes.
    """
    return ",".join(f for f in info().split(",") if f != _RECURSIVE_COUNT)


def get_movies_by_boxset(api, boxset_id):

    for items in get_items(api, boxset_id, "Movie"):
        yield items


def get_episode_by_season(api, show_id, season_id):

    query = {
        "url": "/Shows/%s/Episodes" % show_id,
        "params": {
            "SeasonId": season_id,
            "EnableUserData": True,
            "EnableImages": True,
            "UserId": api.user_id,
            "Fields": info(),
        },
    }
    limit, threads = page_settings()
    for items in _get_items(api, query, limit, threads):
        yield items


def get_seasons(api, show_id):
    return api.get(
        "/Shows/%s/Seasons" % show_id,
        {"UserId": api.user_id, "EnableImages": True, "Fields": info()},
    )


def get_local_trailers(api, item_id):
    return api.get("/Items/%s/LocalTrailers" % item_id, {"userId": api.user_id})


def library_filter(api, parent_id, item_types=None):
    """The filter every library listing shares: this parent, recursive,
    real files only (present, not virtual-unaired), sets uncollapsed.

    One spelling for the walk (build_query), the prune's id+Etag paging
    (get_id_etag_map) and its count (get_prune_count): the prune's count
    must count exactly the set the prune diffs, and the walk must page the
    set the prune will later reconcile. get_item_count is deliberately not
    on it (see get_prune_count).
    """
    return {
        "userId": api.user_id,
        "ParentId": parent_id,
        "IncludeItemTypes": item_types,
        "CollapseBoxSetItems": False,
        "IsVirtualUnaired": False,
        "LocationTypes": "FileSystem,Remote,Offline",
        "IsMissing": False,
        "Recursive": True,
    }


def page_settings():
    """The page size and the pool width, from the settings -- read by the
    callers of the pager, which itself reads nothing from Kodi."""
    return min(settings.get_int("limitIndex") or 50, 100), (
        settings.get_int("limitThreads") or 3
    )


def get_item_count(api, parent_id, item_type=None):

    url = "/Items"

    query_params = {
        "userId": api.user_id,
        "ParentId": parent_id,
        "IncludeItemTypes": item_type,
        "EnableTotalRecordCount": True,
        "LocationTypes": "FileSystem,Remote,Offline",
        "Recursive": True,
        "Limit": 1,
    }

    result = api.get(url, query_params)

    return result.get("TotalRecordCount", 1)


def align_sort_order(params):
    """Make ``SortOrder`` carry exactly as many values as ``SortBy``.

    Jellyfin 10.11 rejects a mismatched pair with an opaque 400 ("Error
    processing request"). ``get_items``' default sort is composite
    (DateCreated,SortName / Descending,Ascending), so a caller overriding
    ``SortBy`` alone left two orders against one field — which 400'd every
    album and song page of a music sync, and because the page generator
    swallowed that error the pass wrote nothing and still reported success.
    Padding here means an override can only ever specify the fields.
    """
    sort_by = params.get("SortBy")

    if not sort_by:
        return

    fields = len(str(sort_by).split(","))
    orders = str(params.get("SortOrder") or "Ascending").split(",")

    if len(orders) != fields:
        # Pad short with the last order given, truncate long.
        params["SortOrder"] = ",".join((orders + [orders[-1]] * fields)[:fields])


def build_query(api, parent_id, item_type=None, basic=False, params=None):
    """The /Items query a walk is about to page through.

    Split out of ``get_items`` so a restore point can be checked against the
    result set it claims to be an index into (``restore_fingerprint``); the
    construction below is unchanged.
    """
    query: Dict[str, Any] = {
        "url": "/Items",
        "params": library_filter(api, parent_id, item_type),
    }
    query["params"].update(
        {
            # Newest first (phase 5, sync-plan Phase 3): fresh content is
            # browsable minutes into an initial sync. SortName breaks the
            # tie so pagination stays deterministic under equal timestamps
            # (bulk imports share DateCreated); the 10.11 composite
            # DateCreated indexes make this cheap. Callers that need a
            # structural order (music) override via ``params``.
            "SortBy": "DateCreated,SortName",
            "SortOrder": "Descending,Ascending",
            "Fields": basic_info() if basic else info(),
            "EnableTotalRecordCount": False,
        }
    )
    # IncludeItemTypes is load-bearing: the 3-pass tvshows walk (Series,
    # then Season, then Episode) is only three *different* queries because
    # of it. Dropping it makes every pass fetch the whole library and apply
    # the wrong writer to each item.
    if params:
        # Directions belong to the fields they were written for: a caller that
        # names its own SortBy without a SortOrder gets a plain ascending
        # order, not the default pair's Descending-then-Ascending.
        if "SortBy" in params and "SortOrder" not in params:
            query["params"]["SortOrder"] = "Ascending"

        query["params"].update(params)

    align_sort_order(query["params"])

    return query


# Keys that say *where* in a result set a page starts rather than *which*
# result set it is. Everything else in the query defines the set itself, so a
# change to any of it invalidates a stored position.
POSITION_KEYS = frozenset({"StartIndex"})


def restore_fingerprint(api, parent_id, item_type=None, basic=False, params=None):
    """Identity of the result set a StartIndex would be an index into.

    A restore point is a position, and a position only means something in the
    set it was measured in. Fields, sort, page size and item type all define
    that set — so a walk whose query has changed in any of them (an addon
    upgrade with a new field list is the routine case) must not resume into
    the old number. Positions themselves are excluded, or a fingerprint would
    never match the page after the one that stored it.
    """
    query = build_query(api, parent_id, item_type, basic, params)
    identity = {
        key: value for key, value in query["params"].items() if key not in POSITION_KEYS
    }

    return json.dumps(identity, sort_keys=True, default=str)


def get_items(api, parent_id, item_type=None, basic=False, params=None):
    query = build_query(api, parent_id, item_type, basic, params)
    limit, threads = page_settings()

    for items in _get_items(api, query, limit, threads):
        yield items


PRUNE_PAGE_SIZE = 500

# Ids per request when confirming the prune's stale candidates (see
# get_existing_ids). Stale sets are small in normal operation -- this only
# matters when a whole library has gone -- and 100 keeps the query string
# well inside anything a reverse proxy will forward.
STALE_CONFIRM_BATCH = 100

# Pages the look-ahead pool may hold per thread (see the buffer semaphore in
# _get_items). Anything above 1 stops the writer's own work from stalling the
# next fetch.
PREFETCH_PAGES = 2


def get_id_etag_map(api, parent_id, item_types):
    """Page a library's id → (Etag, Type) map — the server side of the
    update-mode prune (phase 5, research §3 "update that works").

    Ids-only pages are cheap even at 10^5 items: Fields=Etag adds only the
    MD5(DateLastSaved) string the server computes without touching People
    or MediaStreams. Sequential paging, no restore point — the prune is
    idempotent and simply reruns after an interruption. Errors propagate to
    the caller (the library stays pending and is retried).

    Every id this function fails to return becomes ``stale`` in the prune's
    diff and is fed to the removal arm, so a truncated map is not a smaller
    answer — it is a deletion order. Two guards, both about that asymmetry:

    * Paging ends on the server's ``TotalRecordCount``, not on a short page.
      The fork's ``len(items) < Limit`` test conflates "no more records" with
      "fewer records than asked for this time", and a loaded server is
      entitled to the latter. Against a 4889-item Shows library the pages ran
      ``[500 x9, 389]`` and it never truncated in practice — the invariant is
      what makes that safe rather than lucky.
    * ``StartIndex`` advances by what the page actually carried, so a short
      page re-asks for the records it did not deliver instead of skipping them.

    Ending short of the count raises, which abandons this library's diff for
    the pass (``FullSync.<media>`` logs the LibraryException and moves on) and
    leaves the next update pass to redo it. The count is sampled on the first
    page, so a library that shrinks mid-paging can trip this benignly: that
    costs one skipped prune, where guessing costs rows.
    """
    url = "/Items"
    params = dict(
        library_filter(api, parent_id, item_types),
        SortBy="SortName",
        SortOrder="Ascending",
        Fields=basic_info(),
        EnableUserData=False,
        EnableImages=False,
        EnableTotalRecordCount=False,
    )

    result = {}
    start = 0
    total = None

    while True:
        if state.should_stop():
            raise LibraryExitException("Should stop flag raised, exiting...")

        page = api.get(
            url,
            dict(
                params,
                StartIndex=start,
                Limit=PRUNE_PAGE_SIZE,
                # Counted once, on the first page: the count is a COUNT(*) on
                # the server and the pages after it are the cheap part.
                EnableTotalRecordCount=(total is None),
            ),
        )
        items = page.get("Items") or []

        if total is None:
            total = page.get("TotalRecordCount")

        for item in items:
            result[item["Id"]] = (item.get("Etag"), item.get("Type"))

        if not items:
            # Nothing came back: either the library really is exhausted or the
            # server has stopped answering with records. The count check below
            # tells those apart -- looping again would spin forever.
            break

        start += len(items)

        if total is None:
            # No count to page against (older server, or the field switched
            # off): fall back to the fork's short-page test rather than page
            # forever. Logged because the destructive diff downstream is now
            # trusting a heuristic.
            if len(items) < PRUNE_PAGE_SIZE:
                LOG.warning(
                    "prune map for %s paged without a TotalRecordCount; "
                    "ending on a short page (%s items)",
                    parent_id,
                    len(result),
                )
                break
        elif start >= total:
            break

    if total is not None and len(result) < total:
        raise LibraryException(
            "prune map for %s truncated: %s of %s items — refusing to diff"
            % (parent_id, len(result), total)
        )

    return result


def get_prune_count(api, parent_id, item_types):
    """How many items the prune's server side would see for a library.

    Deliberately the *same* query as ``get_id_etag_map`` with Limit=0 and the
    count switched on, so the number is comparable with the local reference
    map. It exists to answer "has anything diverged" without paging the
    library: measured against a real 4889-item Shows library, the count is
    one ~50ms request where the id+Etag paging is ~12s.

    ``get_item_count`` is not reused: it omits IsVirtualUnaired/IsMissing, so
    its total counts a different set than the prune diffs, and a probe that
    disagrees with the prune schedules heals that then find nothing.
    """
    result = api.get(
        "/Items",
        dict(
            library_filter(api, parent_id, item_types),
            EnableUserData=False,
            EnableImages=False,
            EnableTotalRecordCount=True,
            Limit=0,
        ),
    )

    return result.get("TotalRecordCount")


def get_existing_ids(api, item_ids):
    """Which of ``item_ids`` the server still resolves, asked by id alone.

    The prune infers "stale" from absence: an id in kofin.db that the library
    listing did not return. That inference carries a filtered query's whole
    view of the library with it -- ``LocationTypes``, ``IsMissing``,
    ``IsVirtualUnaired``, and whichever endpoint the listing came from -- and
    the removal arm downstream is destructive. "Not in that listing" and "gone
    from the server" are different questions, and only the second one is
    grounds for deleting rows.

    So this asks the second one directly: no filters, no parent, no recursion,
    just ``Ids`` -- does the server still know this item. An id that resolves
    is not stale no matter why the listing omitted it.

    Concretely: seasons reached through ``/Shows/{id}/Seasons`` can carry a
    different id than the ``/Items`` listing reports for the same season, and
    the writers reference the former while the prune diffs the latter. Those
    ids resolve here, so they stop being removed. This is the general guard,
    though -- it does not need to know which asymmetry it is covering.

    Failures propagate: a confirmation that could not be made is not a
    confirmation, and the caller must not fall back to deleting on the
    unverified set.
    """
    found = set()
    ids = [item_id for item_id in item_ids if item_id]

    for start in range(0, len(ids), STALE_CONFIRM_BATCH):
        if state.should_stop():
            raise LibraryExitException("Should stop flag raised, exiting...")

        result = api.items(
            {
                "Ids": ",".join(ids[start : start + STALE_CONFIRM_BATCH]),
                "Fields": "Etag",
                "EnableUserData": False,
                "EnableImages": False,
                "EnableTotalRecordCount": False,
            }
        )

        for item in result.get("Items") or []:
            if item.get("Id"):
                found.add(item["Id"])

    return found


def get_artists(api, parent_id=None):

    query = {
        "url": "/Artists",
        "params": {
            "UserId": api.user_id,
            "ParentId": parent_id,
            "SortBy": "SortName",
            "SortOrder": "Ascending",
            "Fields": music_info(),
            "CollapseBoxSetItems": False,
            "IsVirtualUnaired": False,
            "EnableTotalRecordCount": False,
            "LocationTypes": "FileSystem,Remote,Offline",
            "IsMissing": False,
            "Recursive": True,
        },
    }
    limit, threads = page_settings()

    for items in _get_items(api, query, limit, threads):
        yield items


def _get_items(api, query, limit=50, threads=3, should_stop=raise_if_stopping):
    """Page one query through a thread pool; one dict per page.

    ``query`` is ``{"url": ..., "params": {...}}``; a ``StartIndex`` in the
    params is where paging resumes. ``limit`` and ``threads`` are the page
    size and the pool width -- the callers read the settings, this reads
    nothing from Kodi. ``should_stop`` is called before every page is
    handed out and raises to end the walk (the fork's ``@stop`` on this
    generator ran once, at creation, and never again).

    Each page is a fresh dict -- ``Items``, ``TotalRecordCount`` and the
    ``RestorePoint`` (the query with this page's params) -- so a consumer
    may keep a page after the next one is yielded.
    """
    url = query["url"]
    query.setdefault("params", {})
    params = query["params"]

    should_stop()

    try:
        test_params = dict(params)
        test_params["Limit"] = 1
        test_params["EnableTotalRecordCount"] = True

        total = api.get(url, test_params)["TotalRecordCount"]

    except Exception as error:
        LOG.exception(
            "Failed to retrieve the server response %s: %s params:%s",
            url,
            error,
            params,
        )
        # Raise, don't yield nothing. Swallowing here (the fork's behaviour)
        # turns a rejected query into an empty pass: the caller writes no
        # items, the library is dropped from sync.json as done and the sync
        # reports success, so a library that never landed looks synced. An
        # empty library is not this path -- the count query answers 200 with
        # TotalRecordCount 0. Failing keeps the library pending for retry.
        raise

    params.setdefault("StartIndex", 0)

    query_params = [
        dict(params, StartIndex=offset, Limit=limit)
        for offset in range(params["StartIndex"], total, limit)
    ]

    # multiprocessing.dummy.Pool completes all requests in multiple threads
    # but has to complete all tasks before allowing any results to be
    # processed. ThreadPoolExecutor allows for completed tasks to be
    # processed while other tasks are completed on other threads.
    with concurrent.futures.ThreadPoolExecutor(threads) as pool:
        # Semaphore to avoid fetching the complete library into memory,
        # deliberately deeper than the pool is wide. A permit is held from
        # the moment a worker starts a page until the consumer is done with
        # it, so a depth equal to the width let the network idle whenever
        # the writer was the faster side: the album pass drained its three
        # buffered pages in about a second and then waited ~9s for the next
        # three -- measured at 26% of that pass's wall time. An extra page
        # per thread keeps ``threads`` fetches in flight while finished
        # pages wait their turn, and still bounds memory to
        # ``PREFETCH_PAGES * threads * limit`` items (600 at the defaults).
        # Consumption stays in submission order either way, so the restore
        # point is unaffected.
        thread_buffer = threading.Semaphore(threads * PREFETCH_PAGES)

        def get_wrapper(page_params):
            thread_buffer.acquire()
            return api.get(url, page_params)

        jobs: List[Tuple[Any, Any]] = [
            (pool.submit(get_wrapper, page_params), page_params)
            for page_params in query_params
        ]

        def abandon_jobs():
            """Let the executor shut down when the consumer stops early.

            Every page is submitted up front and each worker blocks on
            ``thread_buffer`` until the consumer releases a permit, so a
            consumer that stops mid-iteration strands them: the
            ``with ThreadPoolExecutor(...)`` exit calls ``shutdown(wait=True)``,
            which waits on jobs nobody will ever unblock, and the wait never
            ends. That is the multi-minute freeze on quitting Kodi mid-sync
            -- a writer's ``@stop`` raises LibraryExitException, which closes
            this generator.

            Cancelling covers the pages that have not started; releasing a
            permit per job covers every worker that could still reach an
            ``acquire`` while the cancellations land. Over-releasing is
            harmless -- the semaphore dies with the generator.
            """
            for pending, _ in jobs:
                if pending is not None and not pending.done():
                    pending.cancel()

            for _ in jobs:
                thread_buffer.release()

        # Consume pages strictly in submission order: the RestorePoint may
        # only ever advance past pages that have been handed to the caller.
        # Out-of-order consumption could persist a restore point beyond
        # pages that were still in flight, and a resumed sync would then
        # skip those items entirely. The semaphore still bounds how far
        # ahead of the consumer the pool may run.
        try:
            for index, (job, page_params) in enumerate(jobs):
                try:
                    result = job.result() or {"Items": []}
                except Exception as error:
                    LOG.exception("Failed to retrieve page %s: %s", page_params, error)
                    # The finally below cancels the rest and unblocks the
                    # workers, so the executor can actually shut down.
                    raise

                # free job memory
                jobs[index] = (None, None)

                # Mitigates #216 till the server validates the date provided is valid
                if result["Items"] and result["Items"][0].get("ProductionYear"):
                    try:
                        date(result["Items"][0]["ProductionYear"], 1, 1)
                    except ValueError:
                        LOG.info(
                            "#216 mitigation triggered. Setting ProductionYear to None"
                        )
                        result["Items"][0]["ProductionYear"] = None

                should_stop()

                yield {
                    "Items": list(result["Items"]),
                    "TotalRecordCount": total,
                    "RestorePoint": {"url": url, "params": dict(page_params)},
                }

                # release the semaphore again
                thread_buffer.release()
        finally:
            abandon_jobs()
