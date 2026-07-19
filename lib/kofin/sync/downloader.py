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
from kofin.sync.shims import LibraryExitException, stop

LOG = Logger(__name__)


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
    return api.get("/Users/%s/Items/%s/LocalTrailers" % (api.user_id, item_id))


def get_item_count(api, parent_id, item_type=None):

    url = "/Users/%s/Items" % api.user_id

    query_params = {
        "ParentId": parent_id,
        "IncludeItemTypes": item_type,
        "EnableTotalRecordCount": True,
        "LocationTypes": "FileSystem,Remote,Offline",
        "Recursive": True,
        "Limit": 1,
    }

    result = api.get(url, query_params)

    return result.get("TotalRecordCount", 1)


def get_items(api, parent_id, item_type=None, basic=False, params=None):

    query = {
        "url": "/Users/%s/Items" % api.user_id,
        "params": {
            "ParentId": parent_id,
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
        query["params"].update(params)

    for items in _get_items(api, query):
        yield items


PRUNE_PAGE_SIZE = 500


def get_id_etag_map(api, parent_id, item_types):
    """Page a library's id → (Etag, Type) map — the server side of the
    update-mode prune (phase 5, research §3 "update that works").

    Ids-only pages are cheap even at 10^5 items: Fields=Etag adds only the
    MD5(DateLastSaved) string the server computes without touching People
    or MediaStreams. Sequential paging, no restore point — the prune is
    idempotent and simply reruns after an interruption. Errors propagate to
    the caller (the library stays pending and is retried).
    """
    url = "/Users/%s/Items" % api.user_id
    params = {
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

    while True:
        if state.should_stop():
            raise LibraryExitException("Should stop flag raised, exiting...")

        page = api.get(url, dict(params, StartIndex=start, Limit=PRUNE_PAGE_SIZE))
        items = page.get("Items") or []

        for item in items:
            result[item["Id"]] = (item.get("Etag"), item.get("Type"))

        if len(items) < PRUNE_PAGE_SIZE:
            return result

        start += PRUNE_PAGE_SIZE


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
            # semaphore to avoid fetching complete library to memory
            thread_buffer = threading.Semaphore(dthreads)

            # wrapper function for api.get that uses a semaphore
            def get_wrapper(params):
                thread_buffer.acquire()
                return api.get(url, params)

            # create jobs
            jobs = [(p.submit(get_wrapper, param), param) for param in query_params]

            # Consume pages strictly in submission order: the RestorePoint may
            # only ever advance past pages that have been handed to the caller.
            # Out-of-order consumption could persist a restore point beyond
            # pages that were still in flight, and a resumed sync would then
            # skip those items entirely. The semaphore still keeps up to
            # dthreads pages buffered ahead of the consumer.
            for index, (job, param) in enumerate(jobs):
                try:
                    result = job.result() or {"Items": []}
                except Exception as error:
                    LOG.exception("Failed to retrieve page %s: %s", param, error)

                    for pending, _ in jobs:
                        if pending is not None and not pending.done():
                            pending.cancel()

                    # Unblock workers waiting on the buffer so the executor
                    # can shut down instead of deadlocking.
                    for _ in range(dthreads):
                        thread_buffer.release()

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


class GetItemWorker(threading.Thread):

    is_done = False

    def __init__(
        self,
        server,
        queue,
        output,
        error_event=None,
        userdata_ids=None,
        artwork_ids=None,
        fields=None,
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
        threading.Thread.__init__(self)

    def _flag_error(self):
        if self.error_event is not None:
            self.error_event.set()

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

                for item in result["Items"]:

                    if item["Type"] in self.output:
                        item["_userdata_changed"] = item.get("Id") in self.userdata_ids
                        if item.get("Id") in self.artwork_ids:
                            item["_artwork_only"] = True
                        self.output[item["Type"]].put(item)
            except ServerUnreachable as error:
                LOG.error("--[ server unreachable: %s ]", error)
                self._flag_error()
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
