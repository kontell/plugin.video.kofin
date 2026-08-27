"""The worker threads of the sync pipeline (P2.3).

Four drain loops and one pager, beside each other. The three writer
workers used to be one loop copied three times; ``WriterWorker`` is that
loop once -- open the lock and both databases, build the writers for the
database, take an item at a time with a one-second timeout, dispatch it,
absorb a failure into the unapplied flag, commit every COMMIT_INTERVAL,
stop when the service says so -- and each subclass supplies only what it
dispatches. ``db_file`` and ``source`` are constructor arguments; they used
to be attached from outside after construction and read back with getattr.

``ChunkQueue`` is the download queue that knows how many *items* it holds
(a chunk is a list of ids), so nobody counts by reaching into
``queue.Queue.queue``.
"""

import queue
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from kofin.core import state
from kofin.core.http import JellyfinError, ServerUnreachable
from kofin.core.log import Logger
from kofin.sync.downloader import NON_CONTENT_TYPES, info
from kofin.sync import fields as api
from kofin.sync import kofindb as jellyfin_db
from kofin.sync import newcontent
from kofin.sync.db import Database
from kofin.sync.hooks import pipeline_hooks
from kofin.sync.shims import LibraryException, LibraryExitException
from kofin.sync.writers import Movies, TVShows, MusicVideos, Music

LOG = Logger(__name__)

COMMIT_INTERVAL = 50


class ChunkQueue(queue.Queue):
    """A queue of id chunks that keeps the count of ids in flight.

    ``pending_items`` used to sum ``len(chunk)`` over ``Queue.queue`` -- the
    internal deque, read without the mutex. The count is kept here instead,
    under the queue's own lock, and ``snapshot`` is the sanctioned read of
    what is queued (the removed-queue dedupe needs it).
    """

    def __init__(self, maxsize: int = 0) -> None:
        super().__init__(maxsize)
        self.items_pending = 0

    def _put(self, item: Any) -> None:
        super()._put(item)
        self.items_pending += self._size(item)

    def _get(self) -> Any:
        item = super()._get()
        self.items_pending -= self._size(item)
        return item

    @staticmethod
    def _size(item: Any) -> int:
        return len(item) if isinstance(item, (list, tuple, set)) else 1

    def snapshot(self) -> List[Any]:
        with self.mutex:
            return list(self.queue)


def release_worker(thread: Any) -> None:
    """Close a finished worker's HTTP session (audit finding #9).

    Each worker was handed its own Api, hence its own connection pool, and
    none of them were ever closed -- a catch-up that spawns dozens leaves
    that many idle pools until the garbage collector notices. Idempotent:
    the reaper sees a finished thread on every tick until it is pruned.
    """
    server = getattr(thread, "server", None)
    close = getattr(server, "close", None)
    if close is None or getattr(thread, "_released", False):
        return
    thread._released = True
    try:
        close()
    except Exception:
        LOG.exception("closing a worker's session failed")


class WriterWorker(threading.Thread):
    """The drain loop the three writer threads share."""

    category = ""  # the log name: updated / userdata / removed
    unapplied_label = ""  # the prefix on an unapplied report
    is_done = False

    def __init__(self, queue, lock, database, server, unapplied=None, source=None):
        self.queue = queue
        self.lock = lock
        self.db_file = database
        self.database = Database(database)
        self.server = server
        # Reports an item that never landed; the library can schedule a
        # recovery prune (see Library.flag_unapplied).
        self.unapplied = unapplied
        self.source = source
        threading.Thread.__init__(self)

    def _report_unapplied(self, item, error):
        if self.unapplied is not None:
            self.unapplied(
                item.get("Id"),
                "%s%s: %s" % (self.unapplied_label, item.get("Type"), error),
            )

    def writers(self, jellyfindb, kodidb) -> Optional[Dict[str, Any]]:
        """The writers for this database, keyed by kind; None for a database
        this worker does not know."""
        default_args = (self.server, jellyfindb, kodidb)
        hooks = pipeline_hooks()
        if kodidb.db_file == "video":
            return {
                "movies": Movies(*default_args, hooks=hooks),
                "tvshows": TVShows(*default_args, hooks=hooks),
                "musicvideos": MusicVideos(*default_args, hooks=hooks),
            }
        if kodidb.db_file == "music":
            return {"music": Music(*default_args, hooks=hooks)}
        return None

    def handle(self, item, writers) -> None:
        raise NotImplementedError

    def run(self):
        with self.lock, Database("kofin") as jellyfindb, self.database as kodidb:
            writers = self.writers(jellyfindb, kodidb)

            if writers is None:
                LOG.error(
                    '"{}" is not a valid Kodi library type.'.format(kodidb.db_file)
                )
                return

            processed = 0

            while True:
                try:
                    item = self.queue.get(timeout=1)
                except queue.Empty:
                    break

                try:
                    self.handle(item, writers)
                except LibraryException as error:
                    # Still swallowed so one bad item cannot stop the drain,
                    # but no longer forgotten: it never landed, and the
                    # watermark is about to move past it.
                    if isinstance(error, LibraryExitException):
                        self.queue.task_done()
                        break
                    LOG.warning("Ignoring exception %s", error)
                    self._report_unapplied(item, error)
                except Exception as error:
                    LOG.exception(error)
                    self._report_unapplied(item, error)

                self.queue.task_done()
                processed += 1

                if not processed % COMMIT_INTERVAL:
                    kodidb.conn.commit()
                    jellyfindb.conn.commit()

                if state.should_stop():
                    break

        LOG.info("--<[ q:%s/%s ]", self.category, id(self))
        self.is_done = True


UPDATE_DISPATCH = {
    "Movie": ("movies", "movie"),
    "BoxSet": ("movies", "boxset"),
    "Series": ("tvshows", "tvshow"),
    "Season": ("tvshows", "season"),
    "Episode": ("tvshows", "episode"),
    "MusicVideo": ("musicvideos", "musicvideo"),
    "MusicAlbum": ("music", "album"),
    "MusicArtist": ("music", "artist"),
    "Audio": ("music", "song"),
}

USERDATA_DISPATCH = {
    "Movie": ("movies", "userdata"),
    "Series": ("tvshows", "userdata"),
    "Season": ("tvshows", "userdata"),
    "Episode": ("tvshows", "userdata"),
    "MusicAlbum": ("music", "album"),
    "MusicArtist": ("music", "artist"),
    "Audio": ("music", "userdata"),
}

ARTWORK_WRITERS = {
    "Movie": "movies",
    "Series": "tvshows",
    "Season": "tvshows",
    "Episode": "tvshows",
    "MusicVideo": "musicvideos",
}


def _dispatch(table, writers, item):
    """The bound writer method for an item, or None when nothing handles
    the kind on this database."""
    entry = table.get(item["Type"])
    if entry is None:
        return None
    writer = writers.get(entry[0])
    return None if writer is None else getattr(writer, entry[1])


class UpdateWorker(WriterWorker):
    """Writes downloaded items; announces the additions."""

    category = "updated"

    def __init__(
        self,
        queue,
        notify,
        lock,
        database,
        server=None,
        notify_enabled=False,
        artwork_fallback=None,
        unapplied=None,
        source=None,
    ):
        super().__init__(
            queue, lock, database, server, unapplied=unapplied, source=source
        )
        self.notify_output = notify
        self.notify = notify_enabled
        self.artwork_fallback = artwork_fallback

    def _artwork_only(self, item, writers):
        """Apply an image-only item through the artwork-only path; fall back
        to a full re-download when it cannot be handled (unknown reference,
        unexpected payload). Returns True when the item is consumed."""
        name = ARTWORK_WRITERS.get(item["Type"])
        writer = writers.get(name) if name else None

        handled = writer is not None and api.artwork_only(
            writer, item, writer.jellyfin_db.get_item_by_id(item["Id"])
        )

        if not handled and self.artwork_fallback is not None:
            self.artwork_fallback(item["Id"])

        return True

    def handle(self, item, writers):
        LOG.debug("{} - {}".format(item["Type"], item["Name"]))

        if item.get("_artwork_only"):
            self._artwork_only(item, writers)
            return

        write = _dispatch(UPDATE_DISPATCH, writers, item)
        if write is not None:
            write(item)

        # A writer that refused this item wrote no Kodi row and no
        # kofin.db reference, so there is nothing to announce. It
        # refuses by returning early, and the return value cannot
        # carry that news -- tvshow() returns None on unchanged
        # deliberately, so full sync still walks its episodes --
        # hence the explicit set. A refusal that *raises*
        # (LibraryOrphanException) skips this block anyway.
        #
        # Not cosmetic: everything announced here also reaches
        # downloads_auto.queue_new_content, so items the writers
        # had already declined were pushing real ones out of a
        # backlog that overflowed 165 times on a live box.
        if self.notify and not any(
            item["Id"] in writer.refused for writer in writers.values()
        ):
            # What is announceable, and what it is called, is
            # newcontent's to decide; a watched item comes back
            # None here and is never reported.
            entry = newcontent.entry_for(item)

            if entry is not None:
                self.notify_output.put(entry)


class UserDataWorker(WriterWorker):
    """Applies userdata (played, favourite, resume) to existing rows."""

    category = "userdata"
    unapplied_label = "userdata "

    def handle(self, item, writers):
        write = _dispatch(USERDATA_DISPATCH, writers, item)
        if write is not None:
            write(item)


REMOVAL_WRITERS = {
    "Movie": "movies",
    "BoxSet": "movies",
    "Series": "tvshows",
    "Season": "tvshows",
    "Episode": "tvshows",
    "MusicAlbum": "music",
    "MusicArtist": "music",
    "Audio": "music",
    "MusicVideo": "musicvideos",
}


def removal_writer_for(item_type, movies, tvshows, music, musicvideos):
    """The bound ``remove`` for this kind, or None when nothing handles it."""
    writer = {
        "movies": movies,
        "tvshows": tvshows,
        "music": music,
        "musicvideos": musicvideos,
    }.get(REMOVAL_WRITERS.get(item_type or "", ""))

    return None if writer is None else writer.remove


class RemovedWorker(WriterWorker):
    """Removes items through their writers."""

    category = "removed"
    unapplied_label = "removal "

    def writers(self, jellyfindb, kodidb):
        # No hooks on removal, as before.
        default_args = (self.server, jellyfindb, kodidb)
        if kodidb.db_file == "video":
            return {
                "movies": Movies(*default_args),
                "tvshows": TVShows(*default_args),
                "musicvideos": MusicVideos(*default_args),
            }
        if kodidb.db_file == "music":
            return {"music": Music(*default_args)}
        return None

    def handle(self, item, writers):
        remove = removal_writer_for(
            item["Type"],
            writers.get("movies"),
            writers.get("tvshows"),
            writers.get("music"),
            writers.get("musicvideos"),
        )

        if remove is None:
            LOG.warning(
                "no removal writer for type %s; %s left in place",
                item["Type"],
                item["Id"],
            )
            return

        remove(item["Id"])


class SortWorker(threading.Thread):
    """Resolve removed ids to the writer queue their media type is keyed
    on, children through the parent when the row itself is gone."""

    is_done = False

    def __init__(self, queue, output, *args):
        self.queue = queue
        self.output = output
        self.args = args
        threading.Thread.__init__(self)

    def run(self):
        with Database("kofin") as jellyfindb:
            database = jellyfin_db.JellyfinDatabase(jellyfindb.cursor)

            while True:
                try:
                    item_id = self.queue.get(timeout=1)
                except queue.Empty:
                    break

                try:
                    media = database.get_media_by_id(item_id)
                    if media:
                        self.output[media].put({"Id": item_id, "Type": media})
                    else:
                        items = database.get_media_by_parent_id(item_id)

                        if not items:
                            LOG.debug(
                                "Could not find media %s in the kofin database.",
                                item_id,
                            )
                        else:
                            for item in items:
                                self.output[item[1]].put(
                                    {"Id": item[0], "Type": item[1]}
                                )
                except Exception as error:
                    LOG.exception(error)

                self.queue.task_done()

                if state.should_stop():
                    break

        LOG.info("--<[ q:sort/%s ]", id(self))
        self.is_done = True


# --- the pager (moved from downloader.py) ---------------------------------------

CHUNK_ATTEMPTS = 3


class Chunk(list):
    """A download chunk that has failed before: the ids, plus how many
    attempts they have had. A plain list everywhere else -- the queues, the
    progress counter (``len``), the request builder -- so only the retry path
    knows it exists."""

    def __init__(self, ids, attempts=0):
        super().__init__(ids)
        self.attempts = attempts


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
        source=None,
    ):

        # ``server`` is a per-worker Api instance (own Http session), the
        # kofin equivalent of the fork's per-thread requests.Session.
        self.server = server
        self.source = source
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

    def _retry_or_drop(self, item_ids, error):
        """Put a chunk that failed to download back, or give up on it.

        The fork's arm for a non-transport failure logged, flagged the
        watermark and let the chunk go: its ids were never written and,
        with the watermark held, would be re-offered by the next feed pass
        -- but a status the server keeps returning re-offered them forever,
        and a chunk that failed for a reason the feed cannot see (a bad
        item in the middle of a good chunk) never came back at all
        (docs/sync-refactor-assessment.md §3). Now the chunk goes back with
        its attempt count, this worker ends, and the spawn path starts a
        fresh one on a later tick. After CHUNK_ATTEMPTS the ids are flagged
        unapplied one by one, which is what schedules the recovery prune --
        the path that can find them however they went missing.
        """
        attempts = getattr(item_ids, "attempts", 0) + 1

        if attempts < CHUNK_ATTEMPTS:
            LOG.warning(
                "--[ download of %s id(s) failed (%s); attempt %s of %s, re-queued ]",
                len(item_ids),
                error,
                attempts,
                CHUNK_ATTEMPTS,
            )
            self.queue.put(Chunk(item_ids, attempts))
            return

        LOG.error(
            "--[ download of %s id(s) dropped after %s attempts: %s ]",
            len(item_ids),
            attempts,
            error,
        )

        for item_id in item_ids:
            self._flag_unapplied(
                item_id, "download failed %s times: %s" % (attempts, error)
            )

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
                self.queue.task_done()
                self._retry_or_drop(item_ids, error)

                break

            except Exception as error:
                LOG.exception(error)
                self._flag_error()
                self.queue.task_done()
                self._retry_or_drop(item_ids, error)

                break

            self.queue.task_done()

            if state.should_stop():
                break

        self.is_done = True
