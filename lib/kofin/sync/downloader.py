# -*- coding: utf-8 -*-
"""Server download helpers for the sync pipeline (fork ``downloader.py``
port: in-order paging with a look-ahead thread pool, plus the incremental
GetItemWorker).

Adaptations per plan §3: every helper takes the kofin ``Api`` as its first
argument instead of reaching for the fork's client singleton; the field
constants from the fork's ``jellyfin/api.py`` live here now; the dead
``validate_view``/``get_single_item`` helpers are dropped (no callers in the
fork either).
"""

import threading
import concurrent.futures
from datetime import date

import queue

from kofin.core import settings, state
from kofin.core.http import JellyfinError, ServerUnreachable
from kofin.core.log import Logger
from kofin.sync.shims import LibraryException, LibraryExitException, stop

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


def get_episode_by_show(api, show_id):

    query = {
        "url": "/Shows/%s/Episodes" % show_id,
        "params": {
            "EnableUserData": True,
            "EnableImages": True,
            "UserId": api.user_id,
            "Fields": info(),
        },
    }
    for items in _get_items(api, query):
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
    for items in _get_items(api, query):
        yield items


def get_seasons(api, show_id):
    return api.get(
        "/Shows/%s/Seasons" % show_id,
        {"UserId": api.user_id, "EnableImages": True, "Fields": info()},
    )


def get_local_trailers(api, item_id):
    return api.get("/Items/%s/LocalTrailers" % item_id, {"userId": api.user_id})


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


def get_items(api, parent_id, item_type=None, basic=False, params=None):

    query = {
        "url": "/Items",
        "params": {
            "userId": api.user_id,
            "ParentId": parent_id,
            # Load-bearing: the 3-pass tvshows walk (Series, then Season,
            # then Episode) is only three *different* queries because of
            # this. Dropping it makes every pass fetch the whole library and
            # apply the wrong writer to each item.
            "IncludeItemTypes": item_type,
            # Newest first (phase 5, sync-plan Phase 3): fresh content is
            # browsable minutes into an initial sync. SortName breaks the
            # tie so pagination stays deterministic under equal timestamps
            # (bulk imports share DateCreated); the 10.11 composite
            # DateCreated indexes make this cheap. Callers that need a
            # structural order (music) override via ``params``.
            "SortBy": "DateCreated,SortName",
            "SortOrder": "Descending,Ascending",
            "Fields": basic_info() if basic else info(),
            "CollapseBoxSetItems": False,
            "IsVirtualUnaired": False,
            "EnableTotalRecordCount": False,
            "LocationTypes": "FileSystem,Remote,Offline",
            "IsMissing": False,
            "Recursive": True,
        },
    }
    if params:
        # Directions belong to the fields they were written for: a caller that
        # names its own SortBy without a SortOrder gets a plain ascending
        # order, not the default pair's Descending-then-Ascending.
        if "SortBy" in params and "SortOrder" not in params:
            query["params"]["SortOrder"] = "Ascending"

        query["params"].update(params)

    align_sort_order(query["params"])

    for items in _get_items(api, query):
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
    params = {
        "userId": api.user_id,
        "ParentId": parent_id,
        "IncludeItemTypes": item_types,
        "SortBy": "SortName",
        "SortOrder": "Ascending",
        "Fields": basic_info(),
        "EnableUserData": False,
        "EnableImages": False,
        "EnableTotalRecordCount": False,
        "CollapseBoxSetItems": False,
        "IsVirtualUnaired": False,
        "LocationTypes": "FileSystem,Remote,Offline",
        "IsMissing": False,
        "Recursive": True,
    }

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
        {
            "userId": api.user_id,
            "ParentId": parent_id,
            "IncludeItemTypes": item_types,
            "EnableUserData": False,
            "EnableImages": False,
            "EnableTotalRecordCount": True,
            "CollapseBoxSetItems": False,
            "IsVirtualUnaired": False,
            "LocationTypes": "FileSystem,Remote,Offline",
            "IsMissing": False,
            "Recursive": True,
            "Limit": 0,
        },
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

    for items in _get_items(api, query):
        yield items


@stop
def _get_items(api, query):
    """query = {
        'url': string,
        'params': dict -- opt, include StartIndex to resume
    }
    """
    items = {"Items": [], "TotalRecordCount": 0, "RestorePoint": {}}

    limit = min(settings.get_int("limitIndex") or 50, 100)
    dthreads = settings.get_int("limitThreads") or 3

    url = query["url"]
    query.setdefault("params", {})
    params = query["params"]

    try:
        test_params = dict(params)
        test_params["Limit"] = 1
        test_params["EnableTotalRecordCount"] = True

        items["TotalRecordCount"] = api.get(url, test_params)["TotalRecordCount"]

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
        # empty library is not this path — the count query answers 200 with
        # TotalRecordCount 0. Failing keeps the library pending for retry.
        raise

    else:
        params.setdefault("StartIndex", 0)

        def get_query_params(params, start, count):
            params_copy = dict(params)
            params_copy["StartIndex"] = start
            params_copy["Limit"] = count
            return params_copy

        query_params = [
            get_query_params(params, offset, limit)
            for offset in range(params["StartIndex"], items["TotalRecordCount"], limit)
        ]

        # multiprocessing.dummy.Pool completes all requests in multiple threads but has to
        # complete all tasks before allowing any results to be processed. ThreadPoolExecutor
        # allows for completed tasks to be processed while other tasks are completed on other
        # threads. Don't be a dummy.Pool, be a ThreadPoolExecutor
        with concurrent.futures.ThreadPoolExecutor(dthreads) as p:
            # Semaphore to avoid fetching the complete library into memory,
            # deliberately deeper than the pool is wide. A permit is held from
            # the moment a worker starts a page until the consumer is done with
            # it, so a depth equal to the width let the network idle whenever
            # the writer was the faster side: the album pass drained its three
            # buffered pages in about a second and then waited ~9s for the next
            # three — measured at 26% of that pass's wall time. An extra page
            # per thread keeps ``dthreads`` fetches in flight while finished
            # pages wait their turn, and still bounds memory to
            # ``PREFETCH_PAGES * dthreads * limit`` items (600 at the
            # defaults). Consumption stays in submission order either way, so
            # the restore point is unaffected.
            thread_buffer = threading.Semaphore(dthreads * PREFETCH_PAGES)

            # wrapper function for api.get that uses a semaphore
            def get_wrapper(params):
                thread_buffer.acquire()
                return api.get(url, params)

            # create jobs
            jobs = [(p.submit(get_wrapper, param), param) for param in query_params]

            def abandon_jobs():
                """Let the executor shut down when the consumer stops early.

                Every page is submitted up front and each worker blocks on
                ``thread_buffer`` until the consumer releases a permit, so a
                consumer that stops mid-iteration strands them: the
                ``with ThreadPoolExecutor(...)`` exit calls
                ``shutdown(wait=True)``, which waits on jobs nobody will ever
                unblock, and the wait never ends. That is the multi-minute
                freeze on quitting Kodi mid-sync — a writer's ``@stop`` raises
                LibraryExitException, which closes this generator.

                Cancelling covers the pages that have not started; releasing a
                permit per job covers every worker that could still reach an
                ``acquire`` while the cancellations land. Over-releasing is
                harmless — the semaphore dies with the generator.
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
                for index, (job, param) in enumerate(jobs):
                    try:
                        result = job.result() or {"Items": []}
                    except Exception as error:
                        LOG.exception("Failed to retrieve page %s: %s", param, error)
                        # The finally below cancels the rest and unblocks the
                        # workers, so the executor can actually shut down.
                        raise

                    # free job memory
                    jobs[index] = (None, None)
                    query["params"] = param

                    # Mitigates #216 till the server validates the date provided is valid
                    if result["Items"] and result["Items"][0].get("ProductionYear"):
                        try:
                            date(result["Items"][0]["ProductionYear"], 1, 1)
                        except ValueError:
                            LOG.info(
                                "#216 mitigation triggered. Setting ProductionYear to None"
                            )
                            result["Items"][0]["ProductionYear"] = None

                    items["Items"].extend(result["Items"])
                    # Using items to return data and communicate a restore point back to the callee is
                    # a violation of the SRP. TODO: Separate responsibilities.
                    items["RestorePoint"] = query
                    yield items
                    del items["Items"][:]

                    # release the semaphore again
                    thread_buffer.release()
            finally:
                abandon_jobs()


class GetItemWorker(threading.Thread):

    is_done = False
    # Set when this worker stopped because the server was unreachable, so the
    # library can pause the spawn path instead of starting a replacement into
    # the same wall (see Library.DOWNLOAD_BACKOFF_SECONDS).
    unreachable = False

    def __init__(
        self,
        server,
        queue,
        output,
        error_event=None,
        userdata_ids=None,
        artwork_ids=None,
        fields=None,
        unapplied=None,
    ):

        # ``server`` is a per-worker Api instance (own Http session), the
        # kofin equivalent of the fork's per-thread requests.Session.
        self.server = server
        self.queue = queue
        self.output = output
        # Set when a chunk could not be downloaded, so the sync watermark is
        # not advanced past changes that were never applied.
        self.error_event = error_event
        # Ids the sync queue reported as userdata changes. Items are tagged so
        # an Etag-unchanged write can apply userdata only when it actually
        # changed, instead of on every metadata-only update. Empty (the
        # default) tags nothing, so untagged items keep applying userdata.
        self.userdata_ids = userdata_ids if userdata_ids is not None else set()
        # Ids classified image-only (tier 1): tagged so the writer applies
        # the artwork-only path instead of the full cascade.
        self.artwork_ids = artwork_ids if artwork_ids is not None else set()
        # Field set per chunk; the artwork source downloads minimal fields.
        self.fields = fields
        # Callable(item_id, reason) for items downloaded but never handed to a
        # writer, so the library can schedule a recovery prune.
        self.unapplied = unapplied
        threading.Thread.__init__(self)

    def _flag_error(self):
        if self.error_event is not None:
            self.error_event.set()

    def _flag_unapplied(self, item_id, reason):
        if self.unapplied is not None:
            self.unapplied(item_id, reason)
        else:
            LOG.warning("could not apply %s (%s)", item_id, reason)

    def _put(self, output_queue, item):
        """Hand an item to its writer, waiting when the writer is behind.

        The wait is the point: nothing else throttles this side, so without it
        a large catch-up holds every downloaded item in memory at once. It has
        to stay interruptible though — a bare blocking put is how you turn a
        slow writer into a Kodi that will not quit, the same trap the page
        pool fell into.

        ``should_stop`` is the whole test: the service sets it before joining
        the library thread on every teardown path, including a Kodi abort, and
        once the writers have exited on their own ``@stop`` nothing will ever
        drain this queue again.
        """
        while True:
            try:
                output_queue.put(item, timeout=1)
                return
            except queue.Full:
                if state.should_stop():
                    raise LibraryExitException("stopping with writer queues full")

    def run(self):
        while True:
            try:
                item_ids = self.queue.get(timeout=1)
            except queue.Empty:

                self.is_done = True
                LOG.info("--<[ q:download/%s ]", id(self))

                return

            params = {
                "Ids": ",".join(str(x) for x in item_ids),
                "Fields": self.fields or info(),
            }

            try:
                result = self.server.items(params)
                returned = set()

                for item in result["Items"]:
                    returned.add(item.get("Id"))

                    if item["Type"] in self.output:
                        item["_userdata_changed"] = item.get("Id") in self.userdata_ids
                        if item.get("Id") in self.artwork_ids:
                            item["_artwork_only"] = True
                        self._put(self.output[item["Type"]], item)
                    elif item["Type"] in NON_CONTENT_TYPES:
                        # Routine, not a failure: see NON_CONTENT_TYPES. Kept
                        # visible at debug so the feed stays traceable, but it
                        # must not reach _flag_unapplied -- flagging one costs
                        # a user-facing warning and a full prune of every
                        # library, forever, on a view folder the server
                        # touches on its own schedule.
                        LOG.debug(
                            "ignoring %s (%s is not library content)",
                            item.get("Id"),
                            item["Type"],
                        )
                    else:
                        # Downloaded, then dropped because nothing consumes
                        # this type. Never legitimate — the caller asked for
                        # these ids — and it left no trace at all, while the
                        # watermark advanced past the item regardless.
                        self._flag_unapplied(
                            item.get("Id"), "no queue for type %s" % item["Type"]
                        )

                missing = [i for i in item_ids if i not in returned]

                if missing:
                    # Usually benign: an item removed server-side between the
                    # change feed reporting it and this fetch. Logged rather
                    # than flagged for that reason — but logged, because
                    # "requested and never seen again" was previously silent.
                    LOG.warning(
                        "%s of %s requested id(s) not returned: %s",
                        len(missing),
                        len(item_ids),
                        ", ".join(str(i) for i in missing[:5]),
                    )
            except LibraryExitException:
                # Shutting down while waiting on a full writer queue. Not a
                # failure, and not an error to flag: the window is unfinished,
                # which the watermark already reflects because the drain never
                # completed.
                LOG.info("--[ download stopping: shutdown requested ]")

                break

            except ServerUnreachable as error:
                # The chunk was never fetched, so it goes back: the fork
                # dropped it on the floor here (no re-queue, no task_done) and
                # the ids were simply never written, while the spawn path
                # immediately started a replacement worker against the still
                # full queue — a permanent retry storm that ate the backlog a
                # chunk at a time (audit finding #7). Re-queued and left for
                # the backoff the spawn path now respects.
                LOG.error("--[ server unreachable: %s ]", error)
                self._flag_error()
                self.queue.put(item_ids)
                self.queue.task_done()
                self.unreachable = True
                self.is_done = True

                break

            except JellyfinError as error:
                LOG.error("--[ http error: %s ]", error)
                self._flag_error()

            except Exception as error:
                LOG.exception(error)
                self._flag_error()

            self.queue.task_done()

            if state.should_stop():
                break

        self.is_done = True
