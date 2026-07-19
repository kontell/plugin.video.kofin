# -*- coding: utf-8 -*-
"""The sync orchestrator (fork ``library.py`` port): startup, fast sync,
priority queues, worker threads, watermark honesty, degrade-not-die and
retry scheduling.

Adaptations per plan §3: the fork's ``helper.event``/window-prop plumbing is
replaced with kofin state props and a same-process command queue (the
service's ``onNotification`` must never block on a sync); clients come from
an Api factory so every worker owns its HTTP session; first-run modals are
gone — the library whitelist arrives from the settings dialog; the companion
tier probe is ``server_time()`` and feeds the Library-tab status line.
The queue/worker/priority logic is the fork's, byte for byte where possible.
"""

import threading
from datetime import datetime, timedelta, timezone

import queue

import xbmc
import xbmcgui

from kofin.core import settings, state
from kofin.core.log import Logger
from kofin.sync import changefeed
from kofin.sync.writers import Movies, TVShows, MusicVideos, Music
from kofin.sync.kodidb import Movies as KodiDb
from kofin.sync.db import Database, get_sync, save_sync
from kofin.sync import kofindb as jellyfin_db
from kofin.sync import schema
from kofin.sync.full_sync import FullSync
from kofin.sync.views import Views
from kofin.sync.downloader import GetItemWorker, basic_info
from kofin.sync import fields as api
from kofin.sync.shims import (
    LibraryException,
    LibraryExitException,
    get_screensaver,
    localized,
    notification,
    set_screensaver,
    split_list,
    stop,
)

LOG = Logger(__name__)
# Ids-per-request chunk for incremental downloads. Deliberately independent of
# the limitIndex paging setting: paging trades progress granularity for
# round-trips, while this only trades URL length and response size.
DOWNLOAD_CHUNK = 50
TARGET_DB_VERSION = 1
MUSIC_QUEUES = ("Audio", "MusicArtist", "AlbumArtist", "MusicAlbum")
# Writers commit every N items: kofin.db is shared by the writers of every
# category, and sqlite allows only one open write transaction per file — an
# unbounded drain-long transaction would block another writer past its busy
# timeout.
COMMIT_INTERVAL = 50
# Queue backlog above which the background progress dialog appears.
PROGRESS_DISPLAY = 50
# Notification display times (ms), fork defaults.
NEW_VIDEO_TIME = 5000
NEW_MUSIC_TIME = 2000

# Companion tiers come from the change-feed ladder (phase 5, plan §2); the
# aliases keep the phase-2 names working.
TIER_KOFIN = changefeed.TIER_KOFIN
TIER_OFFICIAL = changefeed.TIER_OFFICIAL
TIER_NONE = changefeed.TIER_NONE


class Library(threading.Thread):

    started = False
    stop_thread = False
    suspend = False
    pending_refresh = False
    screensaver = None
    progress_updates = None
    total_updates = 0

    def __init__(self, api, player, api_factory):

        self.api = api
        self.player = player
        # One Api (own HTTP session) per worker thread.
        self.api_factory = api_factory
        self.dthreads = settings.get_int("limitThreads") or 3
        self.monitor = xbmc.Monitor()
        self.companion_tier = TIER_NONE
        # The change-feed provider behind companion_tier (None on tier none).
        self.changefeed = None
        # Envelope of the last catch-up response: watermark + retention facts.
        # Consumed (once) by save_last_sync so a later realtime drain cannot
        # rewind the watermark to a stale query time.
        self.last_envelope = None
        # Held True from retention-overrun detection until the targeted
        # update pass completes; the watermark must not advance in between
        # (plan §2 retention overrun).
        self.retention_repair_pending = False
        self.startup_done = False
        self.commands = queue.Queue()
        self.added_queue = queue.Queue()
        self.updated_queue = queue.Queue()
        self.userdata_queue = queue.Queue()
        self.removed_queue = queue.Queue()
        # Image-only updates (tier 1): downloaded last with minimal fields,
        # written through the artwork-only path instead of the full cascade.
        self.artwork_queue = queue.Queue()
        self.added_output = self.__new_queues__()
        self.updated_output = self.__new_queues__()
        self.userdata_output = self.__new_queues__()
        self.removed_output = self.__new_queues__()
        self.notify_output = queue.Queue()
        # Ids the last incremental sync reported as userdata changes; used to
        # tag downloaded items so an Etag-unchanged write applies userdata only
        # when it changed. Empty outside the incremental path (full sync tags
        # nothing and keeps applying userdata).
        self.userdata_changed_ids = set()
        # Ids routed to the artwork-only class this cycle: downloads are
        # tagged so the writer applies art tables + checksum only. The
        # UpdateWorker fallback discards an id before re-queueing it for the
        # full path, so the retry downloads untagged.
        self.artwork_only_ids = set()
        # Per-class counts of the current catch-up for the progress dialog
        # ("New: 12 | Updates: 340"), so a metadata backlog is visibly not
        # blocking new content (sync-plan §3).
        self.class_counts = {}

        self.jellyfin_threads = []
        self.download_threads = []
        self.notify_threads = []
        self.writer_threads = {"updated": [], "userdata": [], "removed": []}
        self.database_lock = threading.Lock()
        self.music_database_lock = threading.Lock()
        self.download_errors = threading.Event()
        self.retry_at = None
        self.retry_delay = 60
        # Kodi databases ("video"/"music") that new content landed in, and that
        # anything at all was written to. Kodi is not told about writes made
        # straight to its SQLite files, so widgets only refresh when we say so.
        self.added_databases = set()
        self.touched_databases = set()

        threading.Thread.__init__(self, name="kofin-library")

    def __new_queues__(self):
        return {
            "Movie": queue.Queue(),
            "BoxSet": queue.Queue(),
            "MusicVideo": queue.Queue(),
            "Series": queue.Queue(),
            "Season": queue.Queue(),
            "Episode": queue.Queue(),
            "MusicAlbum": queue.Queue(),
            "MusicArtist": queue.Queue(),
            "AlbumArtist": queue.Queue(),
            "Audio": queue.Queue(),
        }

    # -- whitelist/status helpers (kofin-side plumbing) ------------------------

    def whitelist(self):
        return get_sync()["Whitelist"]

    def required_kinds(self):
        """Kodi database kinds the current whitelist writes to. The music
        gate only engages once a music library is selected (plan §4)."""
        kinds = {"video"}
        whitelist = [x.replace("Mixed:", "") for x in self.whitelist()]

        with Database("kofin") as kofin_db:
            views = jellyfin_db.JellyfinDatabase(kofin_db.cursor).get_views()

        for view in views:
            if view.view_id in whitelist and view.media_type == "music":
                kinds.add("music")

        return tuple(sorted(kinds))

    def update_status_strings(self):
        """Maintain the Library tab's read-only labels (plan §4)."""
        failure = schema.gate_status(self.required_kinds())

        if failure is not None:
            status = localized(30413) % getattr(failure, "version", 0)
        elif self.companion_tier == TIER_KOFIN:
            status = localized(30600)
        elif self.companion_tier == TIER_OFFICIAL:
            status = localized(30411)
        else:
            status = localized(30412)

        settings.set_str("syncStatus", status)

        names = []
        with Database("kofin") as kofin_db:
            db = jellyfin_db.JellyfinDatabase(kofin_db.cursor)
            for library_id in sorted(self.whitelist()):
                view = db.get_view(library_id.replace("Mixed:", ""))
                if view:
                    names.append(view.view_name)
        settings.set_str("syncedLibraries", ", ".join(names))

    def detect_companion(self):
        """The tier ladder (plan §2): KofinSyncQueue → official KodiSyncQueue
        → none. The provider instance is what fast_sync consumes."""
        self.changefeed = changefeed.detect(self.api)
        self.companion_tier = (
            self.changefeed.tier if self.changefeed is not None else TIER_NONE
        )

        return self.companion_tier

    def enqueue_command(self, command, data=None):
        """Called from the service's notification thread; processed in the
        library thread so IPC handling never blocks on a sync."""
        self.commands.put((command, data or {}))

    def run(self):

        LOG.info("--->[ library ]")

        delay = settings.get_int("startupDelay")
        if delay and self.monitor.waitForAbort(delay):
            return

        try:
            startup_ok = self.startup()
        except Exception as error:
            LOG.exception(error)
            startup_ok = False

        if not startup_ok:
            self.stop_client()

        self.startup_done = True

        while not self.stop_thread:

            try:
                self.service()
            except LibraryException as error:
                LOG.warning(error)
                break
            except Exception as error:
                LOG.exception(error)

                break

            if self.monitor.waitForAbort(2):
                break

        LOG.info("---<[ library ]")

    def test_databases(self):
        """Open the gated databases to prove the files exist and pass the
        schema gate; raises SchemaError otherwise."""
        for kind in self.required_kinds():
            with Database(kind):
                pass

    def check_version(self):
        """
        Checks database version and triggers any required data migrations
        """
        with Database("kofin") as kofin_db:
            db = jellyfin_db.JellyfinDatabase(kofin_db.cursor)
            db_version = db.get_version()

            if not db_version:
                # Make sure we always have a version in the database
                db.add_version((TARGET_DB_VERSION))

        # Video Database Migrations
        with Database("video") as videodb:
            vid_db = KodiDb(videodb.cursor)
            if vid_db.migrations():
                LOG.info("changes detected, reloading skin")
                xbmc.executebuiltin("UpdateLibrary(video)")
                xbmc.executebuiltin("ReloadSkin()")

    @stop
    def service(self):
        """If error is encountered, it will rerun this function.
        Start new "daemon threads" to process library updates.
        (actual daemon thread is not supported in Kodi)
        """
        self.process_commands()

        self.download_threads = [
            thread for thread in self.download_threads if not thread.is_done
        ]
        self.writer_threads["updated"] = [
            thread for thread in self.writer_threads["updated"] if not thread.is_done
        ]
        self.writer_threads["userdata"] = [
            thread for thread in self.writer_threads["userdata"] if not thread.is_done
        ]
        self.writer_threads["removed"] = [
            thread for thread in self.writer_threads["removed"] if not thread.is_done
        ]

        if self.retry_at is not None and datetime.now() >= self.retry_at:

            self.retry_at = None

            if state.is_online():
                LOG.info("--[ sync retry ]")

                if not self.fast_sync():
                    self.schedule_retry()
            else:
                self.schedule_retry()

        if (
            not self.player.isPlayingVideo()
            or settings.get_bool("syncDuringPlay")
            or xbmc.getCondVisibility("VideoPlayer.Content(livetv)")
        ):

            self.worker_downloads()
            self.worker_sort()

            self.worker_updates()
            self.worker_userdata()
            self.worker_remove()
            self.worker_notify()
            self.refresh_added()

        if self.pending_refresh:
            state.set_sync_active(True)

            if self.total_updates > PROGRESS_DISPLAY:
                queue_size = self.worker_queue_size()

                # Per-class counts (sync-plan §3): a large metadata backlog
                # is visibly not blocking new content.
                if self.class_counts:
                    message = localized(30602) % (
                        self.class_counts.get("new", 0),
                        self.class_counts.get("updates", 0),
                        self.class_counts.get("userdata", 0),
                    )
                elif queue_size:
                    message = "%s: %s" % (localized(30401), queue_size)
                else:
                    message = localized(30401)

                if self.progress_updates is None:

                    self.progress_updates = xbmcgui.DialogProgressBG()
                    self.progress_updates.create("Kofin", localized(30401))

                self.progress_updates.update(
                    int(
                        (
                            float(self.total_updates - queue_size)
                            / float(self.total_updates)
                        )
                        * 100
                    ),
                    message=message,
                )

            if not settings.get_bool("dbSyncScreensaver") and self.screensaver is None:

                xbmc.executebuiltin("InhibitIdleShutdown(true)")
                self.screensaver = get_screensaver()
                set_screensaver(value="")

        if (
            self.pending_refresh
            and not self.download_threads
            and not self.writer_threads["updated"]
            and not self.writer_threads["userdata"]
            and not self.writer_threads["removed"]
            and not self.added_queue.qsize()
            and not self.updated_queue.qsize()
            and not self.userdata_queue.qsize()
            and not self.removed_queue.qsize()
            and not self.artwork_queue.qsize()
            and not self.worker_queue_size()
        ):
            self.pending_refresh = False

            if self.download_errors.is_set():
                # Something failed to download this cycle. Keep the old
                # watermark so the next sync re-covers the window (writes are
                # idempotent, and unchanged items short-circuit on the Etag),
                # and retry with backoff.
                self.download_errors.clear()
                self.schedule_retry()
            else:
                self.save_last_sync()
                self.retry_delay = 60

            self.total_updates = 0
            self.class_counts = {}
            state.set_sync_active(False)

            if self.progress_updates:

                self.progress_updates.close()
                self.progress_updates = None

            if (
                not settings.get_bool("dbSyncScreensaver")
                and self.screensaver is not None
            ):

                xbmc.executebuiltin("InhibitIdleShutdown(false)")
                set_screensaver(value=self.screensaver)
                self.screensaver = None

            # Refresh whatever this cycle actually wrote. Previously only the
            # video database was refreshed, so newly synced albums never showed
            # up in the music widgets until something else triggered a scan.
            self.refresh_libraries(self.touched_databases)
            self.touched_databases = set()
            self.added_databases = set()

    def process_commands(self):
        """Dispatch queued IPC/service commands inside the library thread."""
        while True:
            try:
                command, data = self.commands.get_nowait()
            except queue.Empty:
                break

            LOG.info("--[ command/%s ] %s", command, data)

            try:
                if command == "SyncLibrary":
                    if data.get("Id"):
                        self.add_library(data["Id"], data.get("Update", False))
                elif command == "RemoveLibrary":
                    if data.get("Id"):
                        for lib in data["Id"].split(","):
                            if not self.remove_library(lib):
                                break
                elif command == "RepairLibrary":
                    if data.get("Id"):
                        libraries = data["Id"].split(",")

                        for lib in libraries:
                            if not self.remove_library(lib):
                                break
                        else:
                            self.add_library(data["Id"])
                elif command == "UpdateLibrary":
                    whitelist = self.whitelist()
                    if whitelist:
                        ok = self.add_library(",".join(whitelist), update=True)

                        if ok and self.retention_repair_pending:
                            # The targeted pass has planned/enqueued the
                            # heal; release the watermark hold. With work
                            # still queued the drain-success path saves as
                            # usual; on a clean tree nothing will drain, so
                            # save here — the prune verified everything.
                            self.retention_repair_pending = False

                            if not self.pending_refresh:
                                self.save_last_sync()
                elif command == "RefreshBoxsets":
                    self.add_library("Boxsets:Refresh")
                elif command == "FastSync":
                    if self.companion_tier != TIER_NONE:
                        if not self.fast_sync():
                            self.schedule_retry()
                else:
                    LOG.warning("unknown library command %s", command)
            except Exception as error:
                LOG.exception(error)

            self.update_status_strings()
            # Widget refresh policy (fork e4f8dc3f): refresh only when a
            # media window is up, never UpdateLibrary().
            self.refresh_libraries(self.touched_databases or {"video"})

            self.commands.task_done()

    def stop_client(self):
        self.stop_thread = True

    def enable_pending_refresh(self):
        """When there's an active thread. Let the main thread know."""
        self.pending_refresh = True
        state.set_sync_active(True)

    def worker_queue_size(self):
        """Get how many items are queued up for worker threads."""
        total = 0

        for queues in self.added_output:
            total += self.added_output[queues].qsize()

        for queues in self.updated_output:
            total += self.updated_output[queues].qsize()

        for queues in self.userdata_output:
            total += self.userdata_output[queues].qsize()

        for queues in self.removed_output:
            total += self.removed_output[queues].qsize()

        return total

    def added_downloads_pending(self):
        """Whether new content is still being fetched from the server.

        Gates metadata *downloads* only: once the additions are in hand there
        is no reason to leave download threads idle while they are written.
        """
        if self.added_queue.qsize():
            return True

        return any(
            not thread.is_done and getattr(thread, "source", None) == "added"
            for thread in self.download_threads
        )

    def added_pending(self):
        """Whether added-items work is still in flight: queued for download,
        downloading, waiting for a writer, or being written.

        Gates metadata *writes*, so new content always reaches the Kodi
        database first, and drives the refresh that makes it visible.
        """
        if self.added_downloads_pending():
            return True

        if any(self.added_output[queues].qsize() for queues in self.added_output):
            return True

        return any(
            not thread.is_done and getattr(thread, "source", None) == "added"
            for thread in self.writer_threads["updated"]
        )

    def worker_downloads(self):
        """Get items from jellyfin and place them in the appropriate queues.

        Strict priority: new content first, then userdata download fallbacks;
        metadata-only updates wait until every addition has been written, so
        a large metadata backlog can never delay new content.
        """
        sources = [
            ("added", self.added_queue, self.added_output),
            ("userdata", self.userdata_queue, self.userdata_output),
        ]

        if not self.added_downloads_pending():
            sources.append(("updated", self.updated_queue, self.updated_output))

            if not self.updated_queue.qsize():
                # Image-only updates are pure polish: they download last,
                # with minimal fields, into the updated outputs (the tag on
                # each item routes it to the artwork-only write).
                sources.append(("artwork", self.artwork_queue, self.updated_output))

        for source, work_queue, output in sources:
            if work_queue.qsize() and len(self.download_threads) < self.dthreads:

                new_thread = GetItemWorker(
                    self.api_factory(),
                    work_queue,
                    output,
                    self.download_errors,
                    self.userdata_changed_ids,
                    artwork_ids=self.artwork_only_ids,
                    fields=basic_info() if source == "artwork" else None,
                )
                new_thread.source = source
                new_thread.start()
                LOG.info("-->[ q:download/%s/%s ]", source, id(new_thread))
                self.download_threads.append(new_thread)

    def worker_sort(self):
        """Get items based on the local jellyfin database and place item in appropriate queues."""
        if self.removed_queue.qsize() and len(self.jellyfin_threads) < 2:

            new_thread = SortWorker(self.removed_queue, self.removed_output)
            new_thread.start()
            LOG.info("-->[ q:sort/%s ]", id(new_thread))

    def worker_updates(self):
        """Update items in the Kodi database.

        Added items are always written before metadata-only updates. Only
        additions notify the user as new content.
        """
        output_sets = [("added", self.added_output)]

        if not self.added_pending():
            output_sets.append(("updated", self.updated_output))

        for source, output in output_sets:
            for queues in output:
                queue = output[queues]

                if not queue.qsize():
                    continue

                if queues in MUSIC_QUEUES:
                    lock, db_file = self.music_database_lock, "music"
                else:
                    lock, db_file = self.database_lock, "video"

                if self.writer_busy("updated", db_file):
                    continue

                new_thread = UpdateWorker(
                    queue,
                    self.notify_output,
                    lock,
                    db_file,
                    self.api_factory(),
                    notify_enabled=source == "added",
                    artwork_fallback=self.requeue_full,
                )
                new_thread.db_file = db_file
                new_thread.source = source
                new_thread.start()
                LOG.info("-->[ q:%s/%s/%s ]", source, queues, id(new_thread))
                self.writer_threads["updated"].append(new_thread)
                self.touched_databases.add(db_file)

                if source == "added":
                    self.added_databases.add(db_file)

                self.enable_pending_refresh()

    def refresh_libraries(self, databases):
        """Make writes made straight to Kodi's databases visible.

        Kodi raises no library-change event for direct SQLite writes, so a list
        currently showing the affected library does not pick them up on its own,
        and ``Library.HasContent`` stays cached — on a first sync into an empty
        library the home screen keeps saying "Your library is currently empty"
        until something resets it.

        ``UpdateLibrary(video)`` is that reset, and it is cheap *by
        construction*: every path the video writers create carries
        ``noUpdate=1`` (see ``update_path_movie_obj`` and friends), and
        ``CVideoDatabase::GetPaths()`` skips noUpdate paths when collecting what
        to scan. The scan therefore walks nothing and finishes immediately, but
        still fires the scan-finished event that clears Kodi's cached library
        bools (``CVideoInfoScanner`` → ``ResetLibraryBools``). Upstream relies
        on exactly this, at the end of a full sync and each sync cycle.

        ``UpdateLibrary(music)`` is a different animal and is never called: the
        music writer's ``update_path`` sets only ``strPath``, leaving noUpdate
        unset, so a music scan probes every song's remote
        ``http://<server>/Audio/<id>/`` path (~21k requests, ~3 min on the real
        library) and overlapping scans have crashed Kodi
        (``CMusicLibraryQueue::StopLibraryScanning`` → ``CGUITextureGLES::Draw``,
        SIGBUS on Android — fork commit e4f8dc3f). Music gets the container
        refresh only.
        """
        if not databases:
            return

        if "video" in databases:
            # Catch the empty -> non-empty transition before the scan clears
            # Kodi's cache, because the scan alone is not enough: see
            # _video_content_hidden().
            rebuild_home = self._video_content_hidden()

            xbmc.executebuiltin("UpdateLibrary(video)")

            if rebuild_home:
                # Let the (no-op) scan finish so Library.HasContent is true
                # again before the skin rebuilds its windows against it.
                self.monitor.waitForAbort(2)
                LOG.info("first video content synced; reloading skin for home widgets")
                xbmc.executebuiltin("ReloadSkin()")

        if xbmc.getCondVisibility("Window.IsMedia"):
            xbmc.executebuiltin("Container.Refresh")

    def _video_content_hidden(self):
        """Whether Kodi still believes the video library is empty while rows
        actually exist — the state where the home screen reads "Your library is
        currently empty".

        ``UpdateLibrary(video)`` fixes the *section*, by resetting the cached
        ``Library.HasContent`` bools. It cannot fix the *widget rows*: those are
        ``videodb://`` containers populated when the Home window was built, and
        Kodi keeps Home alive, so a container built against an empty library
        stays empty — navigating away and back does not rebuild it. Only
        recreating the windows does, which is what ``ReloadSkin()`` is for
        (upstream pairs the two the same way after its database migrations).

        Testing the stale state itself, rather than remembering a "first sync"
        flag, keeps this self-limiting: once the reload has happened the cache
        is correct and this returns False forever after. It also stays False on
        a profile whose library was already populated at startup, which is the
        normal case — the reload only ever costs the very first sync.
        """
        if (
            xbmc.getCondVisibility("Library.HasContent(Movies)")
            or xbmc.getCondVisibility("Library.HasContent(TVShows)")
            or xbmc.getCondVisibility("Library.HasContent(MusicVideos)")
        ):
            return False

        try:
            with Database("video") as videodb:
                for table in ("movie", "tvshow", "musicvideo"):
                    videodb.cursor.execute("SELECT 1 FROM %s LIMIT 1" % table)
                    if videodb.cursor.fetchone():
                        return True
        except Exception:
            LOG.exception("could not determine video library content state")

        return False

    def metadata_pending(self):
        """Whether metadata-only updates are still queued or being written."""
        if self.updated_queue.qsize() or self.artwork_queue.qsize():
            return True

        if any(self.updated_output[queues].qsize() for queues in self.updated_output):
            return True

        return any(
            not thread.is_done and getattr(thread, "source", None) == "updated"
            for thread in self.writer_threads["updated"]
        )

    def refresh_added(self):
        """Make new content visible as soon as it has been written, instead of
        waiting for the metadata backlog queued behind it to drain.

        Without this the ordering work is invisible: the end-of-cycle refresh
        is the only thing that updates the widgets, so a large metadata backlog
        hides new content for as long as it takes to write.
        """
        if not self.added_databases or self.added_pending():
            return

        if not self.metadata_pending():
            # Nothing queued behind it: the end-of-cycle refresh is moments
            # away, and each refresh costs a Kodi scan plus a vacuum
            # ("Compressing database"). Let that one do the work.
            return

        databases = self.added_databases
        self.added_databases = set()

        # Each refresh makes Kodi scan and then vacuum ("Compressing database").
        # Drop these from the end-of-cycle refresh: writers that run after this
        # point put their database back, so it is only refreshed twice when
        # there was actually more to show.
        self.touched_databases -= databases

        LOG.info("--[ new content visible: %s ]", ", ".join(sorted(databases)))
        self.refresh_libraries(databases)

    def writer_busy(self, category, db_file):
        """Whether the category already has a live writer.

        One writer at a time: the video and music writers share kofin.db,
        and sqlite allows only one open write transaction per file.
        """
        return bool(self.writer_threads[category])

    def start_writers(self, category, worker_class):
        """Start a writer per output queue of the category."""
        output = getattr(self, "%s_output" % category)

        for queues in output:
            queue = output[queues]

            if not queue.qsize():
                continue

            if queues in MUSIC_QUEUES:
                lock, db_file = self.music_database_lock, "music"
            else:
                lock, db_file = self.database_lock, "video"

            if self.writer_busy(category, db_file):
                continue

            new_thread = worker_class(queue, lock, db_file, self.api_factory())
            new_thread.db_file = db_file
            new_thread.start()
            LOG.info("-->[ q:%s/%s/%s ]", category, queues, id(new_thread))
            self.writer_threads[category].append(new_thread)
            self.touched_databases.add(db_file)
            self.enable_pending_refresh()

    def worker_userdata(self):
        """Update userdata in the Kodi database."""
        self.start_writers("userdata", UserDataWorker)

    def worker_remove(self):
        """Remove items from the Kodi database."""
        self.start_writers("removed", RemovedWorker)

    def worker_notify(self):
        """Notify the user of new additions."""
        if self.notify_output.qsize() and not len(self.notify_threads):

            new_thread = NotifyWorker(self.notify_output, self.player)
            new_thread.start()
            LOG.info("-->[ q:notify/%s ]", id(new_thread))
            self.notify_threads.append(new_thread)

    def startup(self):
        """Run at startup.
        Check databases (schema gate), resume pending syncs, probe the
        companion plugin, run the incremental catch-up.

        The fork's first-run selection modal is gone: the whitelist only ever
        changes through the settings dialog, so an empty whitelist simply
        means nothing to sync yet.
        """
        try:
            self.test_databases()
        except schema.SchemaError as error:
            # Never write blind (plan §2): unknown Kodi database disables
            # write sync; realtime browsing keeps working.
            LOG.error("schema gate: %s", error)
            notification(str(error), error=True)
            self.update_status_strings()
            return False

        self.check_version()

        Views(self.api).get_views()
        Views(self.api).get_nodes()

        self.detect_companion()
        self.update_status_strings()

        try:
            if get_sync()["Libraries"]:

                try:
                    with FullSync(self, self.api) as sync:
                        sync.libraries()

                    Views(self.api).get_nodes()
                except Exception as error:
                    LOG.exception(error)

            if self.whitelist() and self.companion_tier != TIER_NONE:

                if self.fast_sync():
                    LOG.info("--<[ retrieve changes ]")
                else:
                    # Stay alive: realtime events still flow, and the
                    # catch-up window is retried with backoff instead of
                    # killing the library thread until Kodi restarts.
                    LOG.error("Failed to retrieve latest updates")
                    self.schedule_retry()

            self.update_status_strings()

            return True

        except LibraryException as error:
            LOG.error(error)

        except Exception as error:
            LOG.exception(error)

        return False

    def _include_types(self):
        """Media-type classes of the synced libraries, from the local view
        table (stored by Views().get_views() at startup; asking the server
        again would cost one round trip per library)."""
        include = []
        whitelist = [x.replace("Mixed:", "") for x in self.whitelist()]

        with Database("kofin") as kofin_db:
            views = jellyfin_db.JellyfinDatabase(kofin_db.cursor).get_views()

        for view in views:
            if view.view_id in whitelist and view.media_type in changefeed.ALL_TYPES:
                include.append(view.media_type)

        # Include boxsets if movies are synced
        if "movies" in include:
            include.append("boxsets")

        return include

    def _stored_checksums(self, records):
        """Stored reference checksums for the record ids that carry an Etag,
        keyed by jellyfin id — the lookup side of skip-before-download.
        Loaded per jellyfin_type via the existing get_checksum query; empty
        when no record carries an Etag (tier 2)."""
        types = {r.item_type for r in records if r.etag and r.item_type}

        if not types:
            return {}

        checksums = {}
        with Database("kofin") as kofin_db:
            db = jellyfin_db.JellyfinDatabase(kofin_db.cursor)

            for jellyfin_type in sorted(types):
                for row in db.get_checksum(jellyfin_type):
                    checksums[row[0]] = row[1]

        return checksums

    def _known_parent_test(self, records):
        """A predicate over the parent ids referenced by added child records:
        True when kofin.db already tracks the id (one connection, indexed
        lookups; the planner calls it per candidate)."""
        candidates = changefeed.parent_candidates(records)
        known = set()

        if candidates:
            with Database("kofin") as kofin_db:
                db = jellyfin_db.JellyfinDatabase(kofin_db.cursor)

                for candidate in candidates:
                    if db.get_item_by_id(candidate) is not None:
                        known.add(candidate)

        return lambda item_id: item_id in known

    def fast_sync(self):
        """Incremental catch-up through the change-feed provider."""
        if self.changefeed is None:
            return True

        last_sync = settings.get_str("lastIncrementalSync")
        include = self._include_types()
        LOG.info("--[ retrieve changes ] %s", last_sync)

        try:
            change_set = self.changefeed.changes(last_sync, include)
            self.last_envelope = change_set.envelope

            if changefeed.retention_overrun(
                last_sync, change_set.envelope.retention_cutoff
            ):
                # The server's queue no longer reaches our watermark: records
                # in the gap are gone. Process what we got (idempotent), then
                # heal with a targeted update pass; the watermark holds until
                # it completes (sync-plan R5 — no more silent loss).
                if not self.retention_repair_pending:
                    LOG.warning(
                        "sync queue retention exceeded (watermark %s < cutoff %s); "
                        "scheduling a library update",
                        last_sync,
                        change_set.envelope.retention_cutoff,
                    )
                    notification(localized(30601))
                    self.retention_repair_pending = True
                    self.enqueue_command("UpdateLibrary")

            plan = changefeed.build_plan(
                change_set.records,
                change_set.userdata,
                self._stored_checksums(change_set.records),
                self._known_parent_test(change_set.records),
            )

            total = (
                len(plan.added)
                + len(plan.updated)
                + len(plan.artwork)
                + len(plan.userdata)
            )

            if settings.get_bool("syncNotification") and total > (
                settings.get_int("syncNotificationCount") or 1000
            ):
                # Informational only, never a modal: a prompt here would block
                # unattended boxes, and skipping the batch would permanently
                # lose those changes once the watermark advanced.
                notification(localized(30402) % total)

            if plan.skipped:
                # The tier-1 no-op class: dropped before download (S2.5's
                # 3067 fetches → 0). The request-count grep keys off this.
                LOG.info("---[ skipped unchanged:%s ]", plan.skipped)

            self.userdata_changed_ids = plan.userdata_changed_ids
            self.artwork_only_ids = set(plan.artwork)
            self.class_counts = {
                "new": len(plan.added),
                "updates": len(plan.updated) + len(plan.artwork),
                "userdata": len(plan.userdata),
            }

            # Priority order: userdata and removals are cheap and local,
            # new content downloads next, metadata updates after, image-only
            # artwork touches last.
            self.userdata(plan.userdata)
            self.removed(plan.removed)
            self.added(plan.added)
            self.updated(plan.updated)
            self.artwork(plan.artwork)

        except Exception as error:
            LOG.exception(error)

            return False

        return True

    def schedule_retry(self):
        """Retry the incremental sync later, with exponential backoff.

        Notifies once per failure episode (on the first schedule, not on
        every backoff step).
        """
        if self.retry_delay == 60:
            notification(localized(30403))

        self.retry_at = datetime.now() + timedelta(seconds=self.retry_delay)
        LOG.warning("Sync incomplete, retrying in %s seconds", self.retry_delay)
        self.retry_delay = min(self.retry_delay * 2, 1800)

    def save_last_sync(self):
        """Advance the incremental watermark, preferring the server clock.

        Tier 1 uses the feed envelope's ServerTime — the clock at *query*
        time, sampled by the same response, so no extra round trip and no
        skew fudge. Tier 2 keeps the fork-faithful shape: the envelope
        (GetServerDateTime moved from drain to query time) minus the 2-minute
        tolerance, or a fresh GetServerDateTime on envelope-less drains
        (realtime cycles), exactly as before. The envelope is consumed once
        so a later drain can never rewind the watermark to a stale sample.

        While a retention repair is pending the watermark must not move at
        all — the gap before the cutoff is only covered once the targeted
        update pass completes.
        """
        if self.retention_repair_pending:
            LOG.info("--[ sync watermark held: retention repair pending ]")
            return

        envelope, self.last_envelope = self.last_envelope, None
        time_now = None

        if envelope is not None and envelope.server_time:
            time_now = self._naive_utc(envelope.server_time)
        elif self.companion_tier == TIER_KOFIN:
            try:
                server_now = self.changefeed.server_now()
                if server_now:
                    time_now = self._naive_utc(server_now)
            except Exception as error:
                LOG.warning(error)
                LOG.warning("Failed to fetch server time, falling back to client time.")
        elif self.companion_tier == TIER_OFFICIAL:
            try:
                raw = self.api.server_time().get("ServerDateTime", "")
                time_now = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
            except Exception as error:
                LOG.warning(error)
                LOG.warning("Failed to parse server time, falling back to client time.")

        if time_now is None:
            time_now = datetime.now(timezone.utc).replace(tzinfo=None)

        if self.companion_tier != TIER_KOFIN:
            # Add some tolerance in case time is out of sync with server
            time_now -= timedelta(minutes=2)

        last_sync = time_now.strftime("%Y-%m-%dT%H:%M:%SZ")
        settings.set_str("lastIncrementalSync", last_sync)
        LOG.info("--[ sync/%s ]", last_sync)

    @staticmethod
    def _naive_utc(unix):
        """Unix seconds → naive UTC datetime (the watermark's internal shape)."""
        return datetime.fromtimestamp(unix, tz=timezone.utc).replace(tzinfo=None)

    def stamp_watermark_if_empty(self):
        """Watermark-at-start (plan §2): the very first full sync stamps the
        watermark with the server clock *before* paging begins, so the first
        catch-up replays everything that changed during the sync — the Etag
        skip makes the replay nearly free. Full syncs never advance the
        watermark at their end (that jumped it past pending queue records
        for other libraries); the incremental path is the sole owner.
        """
        if settings.get_str("lastIncrementalSync"):
            return

        server_now = None

        if self.changefeed is not None:
            try:
                server_now = self.changefeed.server_now()
            except Exception as error:
                LOG.warning("server clock unavailable for the start stamp: %s", error)

        if server_now:
            stamp = changefeed.unix_to_watermark(server_now)
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        settings.set_str("lastIncrementalSync", stamp)
        LOG.info("--[ watermark stamped at full-sync start/%s ]", stamp)

    def add_library(self, library_id, update=False):

        try:
            with FullSync(self, server=self.api) as sync:
                sync.libraries(library_id, update)
        except Exception as error:
            LOG.exception(error)

            return False

        Views(self.api).get_nodes()

        return True

    def remove_library(self, library_id):

        try:
            with FullSync(self, self.api) as sync:
                sync.remove_library(library_id)

            Views().remove_library(library_id)
        except Exception as error:
            LOG.exception(error)

            return False

        Views(self.api).get_views()
        Views(self.api).get_nodes()

        return True

    def userdata(self, data):
        """Apply userdata changes.

        The payload entries (UserItemDataDto) carry everything the userdata
        writers need, so feed them straight into the writer queues instead of
        re-downloading the full items. Music albums and artists are the
        exception: their handlers run the full album/artist writers, which
        need complete items, so those still go through the download queue.
        """
        if not data:
            return

        fallback = []
        count = 0

        with Database("kofin") as kofin_db:
            db = jellyfin_db.JellyfinDatabase(kofin_db.cursor)

            for dto in data:
                item_id = dto.get("ItemId")
                media = db.get_media_by_id(item_id)

                if media is None:
                    LOG.debug("Skipping userdata for untracked item %s", item_id)
                    continue

                if media in ("MusicAlbum", "MusicArtist", "AlbumArtist"):
                    fallback.append(item_id)
                    count += 1
                elif media in self.userdata_output:
                    # Minimal item shape understood by the *UserData mappings.
                    self.userdata_output[media].put(
                        {"Id": item_id, "Type": media, "Name": None, "UserData": dto}
                    )
                    count += 1

        for chunk in split_list(fallback, DOWNLOAD_CHUNK):
            self.userdata_queue.put(chunk)

        self.total_updates += count
        LOG.info("---[ userdata:%s/%s ]", count, len(data))

    def _enqueue_downloads(self, work_queue, data, label):
        if not data:
            return

        for item in split_list(data, DOWNLOAD_CHUNK):
            work_queue.put(item)

        self.total_updates += len(data)
        LOG.info("---[ %s:%s ]", label, len(data))

    def added(self, data):
        """Add item_id to the added queue, downloaded ahead of updates."""
        self._enqueue_downloads(self.added_queue, data, "added")

    def updated(self, data):
        """Add item_id to updated queue."""
        self._enqueue_downloads(self.updated_queue, data, "updated")

    def artwork(self, data):
        """Add item_id to the artwork queue (image-only updates, tier 1):
        downloaded after everything else, minimal fields, artwork-only
        write."""
        self._enqueue_downloads(self.artwork_queue, data, "artwork")

    def requeue_full(self, item_id):
        """Fall an artwork-only item back to the full update path (called
        from a writer thread; Queue.put is thread-safe). The id is untagged
        first so the re-download takes the normal cascade."""
        self.artwork_only_ids.discard(item_id)
        self.updated_queue.put([item_id])
        self.total_updates += 1
        LOG.info("---[ artwork fallback -> full update: %s ]", item_id)

    def removed(self, data):
        """Add item_id to removed queue."""
        if not data:
            return

        queued = set(self.removed_queue.queue)
        count = 0

        for item in data:

            if item in queued:
                continue

            queued.add(item)
            self.removed_queue.put(item)
            count += 1

        self.total_updates += count
        LOG.info("---[ removed:%s ]", count)


class UpdateWorker(threading.Thread):

    is_done = False

    def __init__(
        self,
        queue,
        notify,
        lock,
        database,
        server=None,
        notify_enabled=False,
        artwork_fallback=None,
        *args,
    ):
        self.queue = queue
        self.notify_output = notify
        self.notify = notify_enabled and settings.get_bool("syncNotification")
        self.lock = lock
        self.database = Database(database)
        self.args = args
        self.server = server
        # Callable(item_id) that re-queues an item for the full update path
        # when the artwork-only write cannot handle it (phase 5).
        self.artwork_fallback = artwork_fallback
        threading.Thread.__init__(self)

    def _artwork_only(self, item, writers):
        """Apply an image-only item through the artwork-only path; fall back
        to a full re-download when it cannot be handled (unknown reference,
        unexpected payload). Returns True when the item is consumed."""
        writer = writers.get(item["Type"])

        handled = writer is not None and api.artwork_only(
            writer, item, writer.jellyfin_db.get_item_by_id(item["Id"])
        )

        if not handled and self.artwork_fallback is not None:
            self.artwork_fallback(item["Id"])

        return True

    def run(self):
        with self.lock, self.database as kodidb, Database("kofin") as jellyfindb:
            default_args = (self.server, jellyfindb, kodidb)
            artwork_writers = {}
            if kodidb.db_file == "video":
                movies = Movies(*default_args)
                tvshows = TVShows(*default_args)
                musicvideos = MusicVideos(*default_args)
                artwork_writers = {
                    "Movie": movies,
                    "Series": tvshows,
                    "Season": tvshows,
                    "Episode": tvshows,
                    "MusicVideo": musicvideos,
                }
            elif kodidb.db_file == "music":
                music = Music(*default_args)
            else:
                # this should not happen
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
                    LOG.debug("{} - {}".format(item["Type"], item["Name"]))
                    if item.get("_artwork_only"):
                        self._artwork_only(item, artwork_writers)
                    elif item["Type"] == "Movie":
                        movies.movie(item)
                    elif item["Type"] == "BoxSet":
                        movies.boxset(item)
                    elif item["Type"] == "Series":
                        tvshows.tvshow(item)
                    elif item["Type"] == "Season":
                        tvshows.season(item)
                    elif item["Type"] == "Episode":
                        tvshows.episode(item)
                    elif item["Type"] == "MusicVideo":
                        musicvideos.musicvideo(item)
                    elif item["Type"] == "MusicAlbum":
                        music.album(item)
                    elif item["Type"] == "MusicArtist":
                        music.artist(item)
                    elif item["Type"] == "AlbumArtist":
                        music.albumartist(item)
                    elif item["Type"] == "Audio":
                        music.song(item)

                    if self.notify:
                        self.notify_output.put(
                            (item["Type"], api.API(item).get_naming())
                        )
                except LibraryException as error:
                    # TODO: Fixme; We're catching all LibraryException here,
                    # but silently ignoring any that isn't the exit condition.
                    # Investigate what would be appropriate behavior here.
                    if isinstance(error, LibraryExitException):
                        break
                    LOG.warning("Ignoring exception %s", error)
                except Exception as error:
                    LOG.exception(error)

                self.queue.task_done()
                processed += 1

                if not processed % COMMIT_INTERVAL:
                    kodidb.conn.commit()
                    jellyfindb.conn.commit()

                if state.should_stop():
                    break

        LOG.info("--<[ q:updated/%s ]", id(self))
        self.is_done = True


class UserDataWorker(threading.Thread):

    is_done = False

    def __init__(self, queue, lock, database, server):

        self.queue = queue
        self.lock = lock
        self.database = Database(database)
        self.server = server

        threading.Thread.__init__(self)

    def run(self):

        with self.lock, self.database as kodidb, Database("kofin") as jellyfindb:
            default_args = (self.server, jellyfindb, kodidb)
            if kodidb.db_file == "video":
                movies = Movies(*default_args)
                tvshows = TVShows(*default_args)
            elif kodidb.db_file == "music":
                music = Music(*default_args)
            else:
                # this should not happen
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
                    if item["Type"] == "Movie":
                        movies.userdata(item)
                    elif item["Type"] in ["Series", "Season", "Episode"]:
                        tvshows.userdata(item)
                    elif item["Type"] == "MusicAlbum":
                        music.album(item)
                    elif item["Type"] == "MusicArtist":
                        music.artist(item)
                    elif item["Type"] == "AlbumArtist":
                        music.albumartist(item)
                    elif item["Type"] == "Audio":
                        music.userdata(item)
                except LibraryException as error:
                    # TODO: Fixme; We're catching all LibraryException here,
                    # but silently ignoring any that isn't the exit condition.
                    # Investigate what would be appropriate behavior here.
                    if isinstance(error, LibraryExitException):
                        break
                    LOG.warning("Ignoring exception %s", error)
                except Exception as error:
                    LOG.exception(error)

                self.queue.task_done()
                processed += 1

                if not processed % COMMIT_INTERVAL:
                    kodidb.conn.commit()
                    jellyfindb.conn.commit()

                if state.should_stop():
                    break

        LOG.info("--<[ q:userdata/%s ]", id(self))
        self.is_done = True


class SortWorker(threading.Thread):

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
                            LOG.info(
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


class RemovedWorker(threading.Thread):

    is_done = False

    def __init__(self, queue, lock, database, server):

        self.queue = queue
        self.lock = lock
        self.database = Database(database)
        self.server = server
        threading.Thread.__init__(self)

    def run(self):

        with self.lock, self.database as kodidb, Database("kofin") as jellyfindb:
            default_args = (self.server, jellyfindb, kodidb)
            if kodidb.db_file == "video":
                movies = Movies(*default_args)
                tvshows = TVShows(*default_args)
                musicvideos = MusicVideos(*default_args)
            elif kodidb.db_file == "music":
                music = Music(*default_args)
            else:
                # this should not happen
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

                if item["Type"] == "Movie":
                    obj = movies.remove
                elif item["Type"] in ["Series", "Season", "Episode"]:
                    obj = tvshows.remove
                elif item["Type"] in [
                    "MusicAlbum",
                    "MusicArtist",
                    "AlbumArtist",
                    "Audio",
                ]:
                    obj = music.remove
                elif item["Type"] == "MusicVideo":
                    obj = musicvideos.remove

                try:
                    obj(item["Id"])
                except LibraryException as error:
                    # TODO: Fixme; We're catching all LibraryException here,
                    # but silently ignoring any that isn't the exit condition.
                    # Investigate what would be appropriate behavior here.
                    if isinstance(error, LibraryExitException):
                        break
                    LOG.warning("Ignoring exception %s", error)
                except Exception as error:
                    LOG.exception(error)
                finally:
                    self.queue.task_done()

                processed += 1

                if not processed % COMMIT_INTERVAL:
                    kodidb.conn.commit()
                    jellyfindb.conn.commit()

                if state.should_stop():
                    break

        LOG.info("--<[ q:removed/%s ]", id(self))
        self.is_done = True


class NotifyWorker(threading.Thread):

    is_done = False

    def __init__(self, queue, player):

        self.queue = queue
        self.video_time = NEW_VIDEO_TIME
        self.music_time = NEW_MUSIC_TIME
        self.player = player
        threading.Thread.__init__(self)

    def run(self):

        while True:

            try:
                item = self.queue.get(timeout=3)
            except queue.Empty:
                break

            time = self.music_time if item[0] == "Audio" else self.video_time

            if time and (
                not self.player.isPlayingVideo()
                or xbmc.getCondVisibility("VideoPlayer.Content(livetv)")
            ):
                notification(
                    "%s %s: %s" % (localized(30405), item[0], item[1]),
                    time_ms=time,
                )

            self.queue.task_done()

            if state.should_stop():
                break

        LOG.info("--<[ q:notify/%s ]", id(self))
        self.is_done = True
