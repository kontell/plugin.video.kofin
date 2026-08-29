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
import time
from typing import Any, Dict, List, Set
from datetime import datetime, timedelta, timezone

import queue

import xbmc
import xbmcgui

from kofin.core import settings, state
from kofin.core.log import Logger
from kofin.downloads import auto as downloads_auto
from kofin.sync import changefeed
from kofin.sync import musicsources
from kofin.sync import newcontent
from kofin.sync.kodidb import Movies as KodiDb
from kofin.sync.kodidb import Music as MusicKodiDb
from kofin.sync.db import Database, get_sync
from kofin.sync import kofindb as jellyfin_db
from kofin.sync import schema
from kofin.sync.full_sync import FullSync
from kofin.sync.prune import PRUNE_SERVER_TYPES, local_reference_map
from kofin.sync.views import Views
from kofin.sync.clock import Deferred
from kofin.sync.downloader import basic_info, get_prune_count
from kofin.sync.refresh import Refresher
from kofin.sync.workers import (
    ChunkQueue,
    GetItemWorker,
    RemovedWorker,
    SortWorker,
    UpdateWorker,
    UserDataWorker,
    release_worker,
)
from kofin.sync.shims import (
    LibraryException,
    localized,
    notification,
    split_list,
    stop,
)

LOG = Logger(__name__)
# Ids-per-request chunk for incremental downloads. Deliberately independent of
# the limitIndex paging setting: paging trades progress granularity for
# round-trips, while this only trades URL length and response size.
DOWNLOAD_CHUNK = 50

# How long the spawn path stays quiet after a download worker gave up on an
# unreachable server. Long enough that a server restart or a flapping link
# does not cost a fresh 27-second retry ladder every couple of seconds, short
# enough that a recovered server is picked up without waiting for the next
# full sync cycle.
DOWNLOAD_BACKOFF_SECONDS = 60

# How many pending additions the new-content announcement keeps while it waits
# for a quiet moment (playback, or more additions still landing).
NEW_CONTENT_LIMIT = 500

# Monotonic stamp enqueue_command puts on a FastSync, and process_commands
# takes back off before dispatch. Named rather than inlined so the two halves
# cannot drift, and capitalised like the rest of the command payload keys.
FAST_SYNC_REQUESTED_AT = "RequestedAt"
TARGET_DB_VERSION = 1
# No "AlbumArtist" here or in the queue set below, unlike the fork: it is not
# a Jellyfin BaseItemKind at all. /Artists and /Artists/AlbumArtists both
# answer with Type "MusicArtist", the artist writer stamps jellyfin_type
# "MusicArtist" unconditionally, and the sync-queue plugin's classifier drops
# anything outside its own switch -- so no route could ever put an item of
# that type in a queue. The fork carried the Emby-era name through its queue
# set, both writer dispatches and its removal map, all of it dead, dispatching
# to a Music.albumartist that does not exist in either codebase.
MUSIC_QUEUES = ("Audio", "MusicArtist", "MusicAlbum")
# How often the library thread re-checks sync.json for an unfinished full
# sync, and the ceiling that interval backs off to while resuming keeps
# failing (see Library.resume_pending_libraries).
RESUME_POLL_SECONDS = 60
RESUME_POLL_MAX_SECONDS = 1800
# Ids kept for the log when items fail to apply; the count is exact, this only
# bounds how many are named.
UNAPPLIED_SAMPLE = 5
# Items a writer queue will hold before its downloader has to wait. The
# download side has no other brake: workers run until their id queue is empty,
# so a whole-library catch-up used to end up resident all at once — measured
# against a real server, roughly 54 KB per movie, 31 KB per episode and 12 KB
# per song once parsed, which is ~490 MB for the three libraries here and
# fatal on an Android box. At this bound the same catch-up peaks around 50 MB.
WRITE_QUEUE_MAX = 250
# Floor between automatic recovery prunes. A prune that itself fails to apply
# something would otherwise schedule the next one immediately, forever.
AUTO_PRUNE_MIN_SECONDS = 3600
# Ceiling for the same clock (healing-loops-plan F3): consecutive failing
# recoveries double the interval — 1h, 2h, 4h … capped here — so a
# permanently-unwritable item costs one UpdateLibrary a day at worst while
# never going silent. A recovery that applies everything resets the ladder.
AUTO_PRUNE_MAX_SECONDS = 86400
# How often managed music playlists are re-read from the server. Nothing
# pushes playlist edits: Jellyfin sends no websocket message when a playlist
# is created or a track is added to one (verified live against 10.11 — neither
# LibraryChanged nor anything else arrives), and Playlist is a
# NON_CONTENT_TYPE so the change feed never carries one either. A poll is the
# only way an edit reaches Kodi without a full sync; it costs one request plus
# one per playlist, and rewrites nothing that has not changed.
PLAYLIST_POLL_SECONDS = 900
# New-content toast display time (ms), the fork's video default. One time for
# every line: the fork's shorter music toast existed because music notified
# per song and a synced album fired a dozen of them, which aggregation ends.
NEW_CONTENT_TIME = 5000

# Companion tiers come from the change-feed ladder (phase 5, plan §2); the
# aliases keep the phase-2 names working.
TIER_KOFIN = changefeed.TIER_KOFIN
TIER_OFFICIAL = changefeed.TIER_OFFICIAL
TIER_NONE = changefeed.TIER_NONE


class Library(threading.Thread):

    started = False
    stop_thread = False
    pending_refresh = False
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
        # The one-sync-at-a-time claim (see claim): instance state,
        # so a service restart's fresh Library starts unclaimed.
        self._full_sync_lock = threading.Lock()
        self._full_sync_running = False
        self.commands: "queue.Queue[Any]" = queue.Queue()
        # When the last change-feed pass *began* (monotonic), or None. Begin
        # rather than end: the guarantee a queued FastSync is protecting is
        # that the server was asked after the event that queued it, and the
        # request goes out at the start of the pass.
        self.last_fast_sync_started = None
        self.added_queue = ChunkQueue()
        self.updated_queue = ChunkQueue()
        self.userdata_queue = ChunkQueue()
        self.removed_queue = ChunkQueue()
        # Image-only updates (tier 1): downloaded last with minimal fields,
        # written through the artwork-only path instead of the full cascade.
        self.artwork_queue = ChunkQueue()
        # Bounded: these two carry whole downloaded items, and the downloaders
        # outrun the writers by a wide margin. Deliberately not applied to the
        # other two — userdata_output is fed straight from this thread by
        # userdata(), so a full queue would block the service tick itself, and
        # removed_output holds ids.
        self.added_output = self.__new_queues__(WRITE_QUEUE_MAX)
        self.updated_output = self.__new_queues__(WRITE_QUEUE_MAX)
        self.userdata_output = self.__new_queues__()
        self.removed_output = self.__new_queues__()
        self.notify_output: "queue.Queue[Any]" = queue.Queue()
        # Announceable additions written this cycle, drained from
        # notify_output by notify_new_content and held until the cycle's
        # additions are all in (and, during playback, until it ends).
        self.new_content = []
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
        self.writer_threads: Dict[str, List[Any]] = {
            "updated": [],
            "userdata": [],
            "removed": [],
        }
        self.database_lock = threading.Lock()
        self.music_database_lock = threading.Lock()
        self.download_errors = threading.Event()
        # The clocks the tick reads, each one deferred action (sync/clock.py):
        # a moment it is due and, where it retries, a delay ladder.
        #
        # A download worker that gave up on an unreachable server holds the
        # spawn path off for a while rather than feeding it straight back
        # in (audit finding #7).
        self.download_backoff = Deferred(DOWNLOAD_BACKOFF_SECONDS)
        # The incremental sync's retry: 60 s doubling to 30 minutes.
        self.retry = Deferred(60, 1800)
        # The pending-queue check that makes an interrupted full sync
        # reconnection-proof (resume_pending_libraries); backs off while
        # resuming keeps failing.
        self.resume = Deferred(RESUME_POLL_SECONDS, RESUME_POLL_MAX_SECONDS)
        # The recovery prune's floor: consecutive failing recoveries climb
        # from an hour to a day instead of retrying hourly forever
        # (healing-loops-plan F3). recovery_pending books a retry that
        # landed inside the floor for flush_recovery_prune.
        self.recovery = Deferred(AUTO_PRUNE_MIN_SECONDS, AUTO_PRUNE_MAX_SECONDS)
        self.recovery_pending = False
        # Music playlists are re-read on this clock; nothing else reaches a
        # playlist edit (poll_music_playlists).
        self.playlist_poll = Deferred(PLAYLIST_POLL_SECONDS)
        # The widget-refresh policy: the fingerprint gate, the settle window,
        # the content probes and the skin reload (sync/refresh.py).
        self.refresher = Refresher(
            self, self.required_kinds, lambda: self.pending_refresh
        )
        # Items a drain could not apply, counted per cycle and sampled for
        # the log; what schedule_recovery_prune reads.
        self.unapplied_count = 0
        self.unapplied_sample: set = set()
        # Libraries already toasted as failed this service lifetime
        # (FullSync._notify_sync_failure: one toast per library).
        self.sync_failure_toasted: set = set()
        # The databases this cycle wrote, and the subset that took additions
        # (refresh_added publishes those early).
        self.added_databases: set = set()
        self.touched_databases: set = set()

        threading.Thread.__init__(self, name="kofin-library")

    def __new_queues__(self, maxsize=0):
        """Per-type writer queues.

        ``maxsize`` is the backpressure bound for the sets that carry whole
        downloaded items; 0 (the default) leaves a queue unbounded. Only the
        two GetItemWorker feeds are bounded — see WRITE_QUEUE_MAX.
        """
        return {
            item_type: queue.Queue(maxsize=maxsize)
            for item_type in (
                "Movie",
                "BoxSet",
                "MusicVideo",
                "Series",
                "Season",
                "Episode",
                "MusicAlbum",
                "MusicArtist",
                "Audio",
            )
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

        names = []
        with Database("kofin") as kofin_db:
            db = jellyfin_db.JellyfinDatabase(kofin_db.cursor)
            for library_id in sorted(self.whitelist()):
                view = db.get_view(library_id.replace("Mixed:", ""))
                if view:
                    names.append(view.view_name)

        # Write-on-change: every set rewrites the whole settings.xml and fires
        # onSettingsChanged, and this runs per processed command — the
        # unconditional writes raced the applier's re-read into transient
        # "failed to load addon settings" (widget-refresh-plan F9).
        if settings.get_str("syncStatus") != status:
            settings.set_str("syncStatus", status)

        joined = ", ".join(names)

        if settings.get_str("syncedLibraries") != joined:
            settings.set_str("syncedLibraries", joined)

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
        library thread so IPC handling never blocks on a sync.

        ``FastSync`` alone is stamped with the monotonic clock, because the
        queue can be serviced much later than it was written — the library
        thread may be inside a startup or a drain — and it is the one command
        that another path can satisfy on its behalf. Every other command keeps
        the plain ``(name, data)`` shape it is dispatched and asserted on.
        """
        data = dict(data or {})

        if command == "FastSync":
            data.setdefault(FAST_SYNC_REQUESTED_AT, time.monotonic())

        self.commands.put((command, data))

    def sync_music_playlists(self):
        """Rewrite managed music playlist files from the server (one-way)."""
        if not settings.get_bool("syncMusicPlaylists"):
            LOG.debug("syncMusicPlaylists off; skip SyncMusicPlaylists command")
            return
        # However this refresh was asked for, it is the poll's answer too.
        self.defer_playlist_poll()
        try:
            from kofin.sync import playlists as music_playlists

            with self.music_database_lock:
                music_playlists.refresh_with_databases(self.api)
        except Exception:
            LOG.exception("SyncMusicPlaylists failed")

    def defer_playlist_poll(self):
        """Start the poll interval again: playlists were just re-read.

        Also called by the full sync's own refresh, which runs on the sync
        thread with its own Api — without it the first tick after a sync
        re-reads every playlist for nothing.
        """
        self.playlist_poll.arm()

    def poll_music_playlists(self):
        """Re-read managed music playlists on the PLAYLIST_POLL_SECONDS clock.

        Playlist edits reach no other path (see PLAYLIST_POLL_SECONDS): before
        this, a track added on the server stayed invisible until someone ran a
        full sync. Held off while a sync cycle is in flight — the refresh reads
        song rows the drain is still writing, and would only have to run again.
        """
        if not settings.get_bool("syncMusicPlaylists"):
            return

        if self.pending_refresh or not state.is_online():
            return

        if self.playlist_poll.waiting():
            return

        # Before the refresh, not after: one that raises must not retry on
        # every two-second tick.
        self.defer_playlist_poll()
        self.sync_music_playlists()

    def reassert_music_sources(self):
        """Rewrite the per-library music ``source`` rows after a Kodi scan.

        Kodi's own scanner empties the source table on any run whose
        sources.xml disagrees with it, taking every ``album_source`` link
        with it (tgrDeleteSource) and leaving the per-library music nodes
        filtering on a name nothing carries. This is the in-session heal;
        ``check_version`` covers a scan that happened while Kodi was off.
        """
        try:
            with self.music_database_lock:
                with Database("kofin") as kofindb, Database("music") as musicdb:
                    views = jellyfin_db.JellyfinDatabase(
                        kofindb.cursor
                    ).get_views_by_media("music")

                    if not views:
                        return

                    musicsources.reassert(kofindb.cursor, musicdb.cursor, views)
        except Exception:
            LOG.exception("ReassertMusicSources failed")

    def cleanup_music_playlists(self):
        """Remove the managed ``playlists/music/Kofin/`` folder."""
        try:
            from kofin.sync import playlists as music_playlists

            music_playlists.cleanup_managed_playlists()
        except Exception:
            LOG.exception("CleanupMusicPlaylists failed")

    def repoint_ratings(self):
        """Point synced films at the rating row the user now prefers.

        The ``preferCriticRating`` flip's apply path. Both rating rows are
        written at sync time, so this fetches nothing and rewrites nothing but
        ``movie.c05`` — and only for kofin-owned films: Kodi's own scrapers
        write ``default``-typed ratings too, and which of a scraped film's
        ratings is its default is not ours to move.

        The refresh is this command's own (widget-refresh-plan D4): ratings are
        a hashed section, so it fires when a pointer actually moved and stays
        quiet when the pass was a no-op.
        """
        rating_type = "critic" if settings.get_bool("preferCriticRating") else "default"

        with Database("kofin") as kofin_db:
            db = jellyfin_db.JellyfinDatabase(kofin_db.cursor)
            movie_ids = [kodi_id for _, kodi_id in db.get_item_ids_by_media("movie")]

        if not movie_ids:
            return

        with self.database_lock:
            with Database() as videodb:
                updated = KodiDb(videodb.cursor).repoint_ratings(movie_ids, rating_type)

        # rowcount is films considered, not films moved: the UPDATE matches
        # every id it is handed, and one already on the preferred row is a
        # no-op write.
        LOG.info("--[ ratings repointed to %s over %s film(s) ]", rating_type, updated)
        self.refresh_libraries({"video"})

    def run(self):
        """Start syncing.

        There is no startup delay to honour any more. The setting existed to
        let a slow network settle before the first sync, which is a problem
        the sync should solve rather than the user: a sync that cannot reach
        the server now leaves its libraries pending and
        ``resume_pending_libraries`` picks them up on the first tick after the
        server answers. Delaying every start by a fixed guess was paying that
        cost on every boot to paper over the case where it went wrong.
        """
        LOG.info("--->[ library ]")

        try:
            startup_ok = self.startup()
        except Exception as error:
            LOG.exception(error)
            startup_ok = False

        if not startup_ok:
            self.stop_client()

        self.startup_done = True

        try:
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
        finally:
            # The bar belongs to this thread and only the drain block closes
            # it, so every exit that is not a drain used to leave it on screen
            # for the life of the Kodi process — spinner turning, percentage
            # frozen, no thread behind it. Observed at 30% for 19 minutes after
            # an addon update killed the 0.9.0 library thread mid-cycle, which
            # reads as a hung sync rather than a dead one. Kodi's script kill
            # raises abortRequested rather than killing the thread, so this
            # runs on that path too.
            self.close_progress()

        LOG.info("---<[ library ]")

    def close_progress(self):
        """Take the background progress bar down, if one is up.

        Defensive because the callers include an unwind: by the time a
        stopping thread reaches here the GUI may already be going away, and
        failing to close a dialog must never be what stops the thread from
        exiting.
        """
        dialog, self.progress_updates = self.progress_updates, None

        if dialog is None:
            return

        try:
            dialog.close()
        except Exception as error:  # pragma: no cover - defensive
            LOG.debug("progress dialog close failed: %s", error)

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

        # Music Database Migrations. Only when a music library is synced —
        # opening MyMusic otherwise would put the schema gate in front of
        # users who never asked kofin to touch their music.
        if "music" in self.required_kinds():
            with Database("kofin") as kofindb, Database("music") as musicdb:
                music_db = MusicKodiDb(musicdb.cursor)
                music_db.ensure_blank_artist()
                pruned = musicsources.prune_orphan_paths(kofindb.cursor, musicdb.cursor)
                if pruned:
                    LOG.info("pruned %s orphaned music path rows", pruned)
                # Kodi's own music scanner empties the source table whenever
                # it disagrees with sources.xml, which with an empty one it
                # always does — so the per-library music nodes come back from
                # any scan matching nothing until this runs. Startup covers a
                # scan that happened while Kodi was off; the
                # AudioLibrary.OnScanFinished command covers one in session.
                musicsources.reassert(
                    kofindb.cursor,
                    musicdb.cursor,
                    jellyfin_db.JellyfinDatabase(kofindb.cursor).get_views_by_media(
                        "music"
                    ),
                )

    @stop
    def service(self):
        """If error is encountered, it will rerun this function.
        Start new "daemon threads" to process library updates.
        (actual daemon thread is not supported in Kodi)
        """
        self.process_commands()

        for category in ("updated", "userdata", "removed"):
            for thread in self.writer_threads[category]:
                if thread.is_done:
                    release_worker(thread)

        finished = [thread for thread in self.download_threads if thread.is_done]
        for thread in finished:
            release_worker(thread)
        if any(getattr(thread, "unreachable", False) for thread in finished):
            self.download_backoff.arm()
            LOG.warning(
                "--[ downloads paused %ss: server unreachable ]",
                DOWNLOAD_BACKOFF_SECONDS,
            )
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

        self.resume_pending_libraries()

        if self.retry.due():

            self.retry.disarm()

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
            self.refresh_added()
            self.poll_music_playlists()

        # Outside the playback gate on purpose: a summary accumulated with
        # syncDuringPlay on is held while video plays, and this is the tick
        # that raises it once playback ends.
        self.notify_new_content()
        # Same shape: a first-content reload held during playback fires here,
        # and so does a drain refresh whose settle has run out.
        self.refresher.flush_pending_reload()

        settled = self.refresher.settled()

        if settled:
            self.refresh_libraries(settled)
        self.flush_recovery_prune()

        if self.pending_refresh:
            state.set_sync_active(True)

            if self.total_updates > settings.get_int("syncProgressThreshold"):
                # Everything still owed, not just what has been downloaded —
                # the "Gathering: N" count under-reported for the same reason
                # the percentage ran backwards.
                queue_size = self.pending_items()

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
                    self.progress_percent(),
                    message=message,
                )

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
                self.retry.reset()

            # After the watermark decision, not instead of it: these items
            # were downloaded fine and failed later, so re-running the feed
            # window would not offer them again. The prune is what reaches
            # them.
            self.schedule_recovery_prune()

            self.total_updates = 0
            self.class_counts = {}
            state.set_sync_active(False)

            self.close_progress()

            # Refresh whatever this cycle actually wrote — deferred behind the
            # settle so back-to-back mini-cycles cost one refresh. (Previously
            # only the video database was refreshed, so newly synced albums
            # never showed up in the music widgets until something else
            # triggered a scan.)
            self.refresher.arm(self.touched_databases)
            self.touched_databases = set()
            self.added_databases = set()

    def process_commands(self):
        """Dispatch queued IPC/service commands inside the library thread."""
        while True:
            try:
                command, data = self.commands.get_nowait()
            except queue.Empty:
                break

            # Off the payload before it is logged or dispatched: the stamp is
            # queue bookkeeping, not something a handler should ever see.
            requested_at = data.pop(FAST_SYNC_REQUESTED_AT, None)

            if requested_at is not None and self.fast_sync_started_since(requested_at):
                # A pass already asked the server for everything since the
                # watermark, and it started after this command was queued, so
                # it covers exactly what the command wanted covered. Running a
                # second one re-fetches the identical window — the watermark
                # only advances once the drain completes — and re-queues the
                # identical work list, which is what made one wake write a
                # movie and then read it back to skip it as unchanged.
                LOG.info("--[ command/FastSync ] covered by the pass in flight")
                self.commands.task_done()
                continue

            LOG.info("--[ command/%s ] %s", command, data)
            handler = self.command_handlers().get(command)

            try:
                if handler is None:
                    LOG.warning("unknown library command %s", command)
                else:
                    handler(data)
            except Exception as error:
                LOG.exception(error)

            self.update_status_strings()

            self.commands.task_done()

    def command_handlers(self):
        """The command table: one bound handler per IPC/service command."""
        return {
            "SyncLibrary": self._cmd_sync_library,
            "RemoveLibrary": self._cmd_remove_library,
            "RepairLibrary": self._cmd_repair_library,
            "UpdateLibrary": self._cmd_update_library,
            "RefreshBoxsets": lambda data: self.add_library("Boxsets:Refresh"),
            "FastSync": self._cmd_fast_sync,
            "SyncMusicPlaylists": lambda data: self.sync_music_playlists(),
            "CleanupMusicPlaylists": lambda data: self.cleanup_music_playlists(),
            "RepointRatings": lambda data: self.repoint_ratings(),
            "ReassertMusicSources": lambda data: self.reassert_music_sources(),
        }

    def _cmd_sync_library(self, data):
        if data.get("Id"):
            self.add_library(data["Id"], data.get("Update", False))

    def _remove_each(self, libraries):
        """Remove libraries in order, collecting the Kodi databases they
        wrote (from the still-present view rows). Stops at the first
        failure; the second value says whether every one went."""
        kinds: Set[str] = set()

        for lib in libraries:
            # Before the removal deletes the view row.
            kind = self._removal_kind(lib)

            if not self.remove_library(lib):
                return kinds, False

            if kind:
                kinds.add(kind)

        return kinds, True

    def _cmd_remove_library(self, data):
        if not data.get("Id"):
            return

        kinds, _ = self._remove_each(data["Id"].split(","))

        # Removal is the one write path with no other refresh owner, and it
        # must aim at the removed library's own database -- the old blanket
        # refresh aimed at video, so a removed music library lingered in the
        # music widgets indefinitely (widget-refresh-plan F5).
        if kinds:
            self.refresh_libraries(kinds)

    def _cmd_repair_library(self, data):
        if not data.get("Id"):
            return

        kinds, removed = self._remove_each(data["Id"].split(","))

        if removed and self.add_library(data["Id"]):
            self.refresher.reload_after_repair(kinds)

    def _cmd_update_library(self, data):
        ids = data.get("Id")

        if ids:
            self.add_library(ids, update=True)
            return

        whitelist = self.whitelist()

        if whitelist:
            ok = self.add_library(",".join(whitelist), update=True)

            if ok and self.retention_repair_pending:
                self.retention_repair_pending = False

                if not self.pending_refresh:
                    self.save_last_sync()

    def _cmd_fast_sync(self, data):
        if self.companion_tier != TIER_NONE:
            if not self.fast_sync():
                self.schedule_retry()

    def _removal_kind(self, library_id):
        """Which Kodi database ("video"/"music") removing this library writes,
        from the still-present view row; None when the view is unknown (the
        removal will no-op). Mixed libraries are video by definition."""
        with Database("kofin") as kofin_db:
            view = jellyfin_db.JellyfinDatabase(kofin_db.cursor).get_view(
                library_id.replace("Mixed:", "")
            )

        if view is None:
            return None

        return "music" if view.media_type == "music" else "video"

    def stop_client(self):
        self.stop_thread = True

    # What a full sync needs from this manager -- the port FullSync speaks
    # and tests/unit/synchost.py fakes: database_lock, music_database_lock,
    # claim/release, added/updated/removed, refresh_libraries,
    # stamp_watermark_if_empty, defer_playlist_poll, sync_failure_toasted.

    def claim(self):
        """Take the one-sync-at-a-time claim; False when one is already up.

        Lives here rather than on FullSync (where the fork kept it, in a
        class-level Borg dict) because the claim must die with the manager
        that owns it: a service restart builds a fresh Library, and a claim
        that outlived the old one refused every sync the new one tried.
        """
        with self._full_sync_lock:
            if self._full_sync_running:
                return False
            self._full_sync_running = True
            return True

    def release(self):
        with self._full_sync_lock:
            self._full_sync_running = False

    def enable_pending_refresh(self):
        """When there's an active thread. Let the main thread know."""
        self.pending_refresh = True
        state.set_sync_active(True)

    def progress_percent(self):
        """Share of this cycle's work already written, 0-100.

        Clamped rather than trusted: work enqueued mid-drain raises both the
        total and the pending count, and items in flight are counted in
        neither, so the raw ratio can stray outside the range without
        anything being wrong.
        """
        total = self.total_updates

        if total <= 0:
            return 0

        done = total - self.pending_items()

        return max(0, min(100, int(float(done) / float(total) * 100)))

    def pending_items(self):
        """Items enqueued this cycle that are not written yet.

        The progress denominator is ``total_updates``, which counts work at
        the moment it is *enqueued* — so the numerator has to count the same
        population. ``worker_queue_size`` does not: it sees only the output
        queues, which are empty at enqueue time because everything is still
        waiting to be downloaded. Progress therefore opened at 100% and fell
        as downloads landed, which is the percentage counting down.

        The download queues hold chunks rather than ids (``removed_queue`` is
        the exception and holds ids), so their contents are measured in items.
        Anything in flight between a download and its writer is in neither
        queue and simply reads as done a moment early; that errs upward and
        never inverts.
        """
        total = self.removed_queue.qsize()

        for work_queue in (
            self.added_queue,
            self.updated_queue,
            self.userdata_queue,
            self.artwork_queue,
        ):
            total += work_queue.items_pending

        return total + self.worker_queue_size()

    def workers_alive(self):
        """True while any thread this manager spawned is still running.

        The rebuild path is what asks. ``database_lock`` is per-instance, so a
        second Library built while the first still has writers in flight puts
        two independent locks in front of the same SQLite files — the same
        two-object-graph hazard ``_shutdown`` keeps ``PROP_SYNC_STOP`` raised
        for. The manager thread dying is therefore not on its own proof that
        the graph is finished.
        """
        threads = list(self.download_threads)

        for category in self.writer_threads:
            threads.extend(self.writer_threads[category])

        return any(thread.is_alive() for thread in threads)

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

        if self.download_backoff.waiting():
            # Still inside the pause a ServerUnreachable bought. The queues
            # keep their work; the connect probe in service/main.py is what
            # notices the server coming back, and the next tick starts fresh
            # workers on the same chunks.
            return

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
                    unapplied=self.flag_unapplied,
                    # Read back by added_downloads_pending: the added-first
                    # gate on metadata downloads keys on it.
                    source=source,
                )
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
                    unapplied=self.flag_unapplied,
                    source=source,
                )
                new_thread.start()
                LOG.info("-->[ q:%s/%s/%s ]", source, queues, id(new_thread))
                self.writer_threads["updated"].append(new_thread)
                self.touched_databases.add(db_file)

                if source == "added":
                    self.added_databases.add(db_file)

                self.enable_pending_refresh()

    def refresh_libraries(self, databases, force_reload=False):
        """Make writes made straight to Kodi's databases visible
        (refresh.Refresher.refresh: the fingerprint gate, the cheap scan,
        the first-content reload)."""
        self.refresher.refresh(databases, force_reload)

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

            new_thread = worker_class(
                queue, lock, db_file, self.api_factory(), unapplied=self.flag_unapplied
            )
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

    def notify_new_content(self):
        """Announce what this cycle added: one toast per content type.

        The fork toasted once per item, from the writer thread that wrote it —
        a queue's worth of "New Episode: ..." for a single season. Aggregation
        has to happen somewhere that can see a whole cycle, which is here, so
        the writers only report and this decides what any of it adds up to.

        Nothing about the accumulator is allowed to cost the sync: the flush
        is guarded because a message that cannot be built is a message lost,
        while an exception reaching ``service`` ends the library thread until
        Kodi restarts.
        """
        drained = []
        while True:
            try:
                drained.append(self.notify_output.get_nowait())
            except queue.Empty:
                break
        if drained:
            # W4.4/W4.6: newly added movies, episodes and albums can queue
            # their own downloads. Fed at drain time, before the toast
            # policy's holds and drops below — those pace announcements, and
            # pacing must not lose a download.
            try:
                downloads_auto.queue_new_content(self.api, drained)
            except Exception:
                LOG.exception("auto-download hook failed")
            self.new_content.extend(drained)

        if len(self.new_content) > NEW_CONTENT_LIMIT:
            # Held, not dropped, is the rule below — but a long playback plus
            # a large catch-up holds every addition of the session in memory
            # and then summarises the lot in one go (audit finding #27). The
            # oldest go: the summary names recent additions, and the count it
            # leads with is worth more than the names it can no longer fit.
            overflow = len(self.new_content) - NEW_CONTENT_LIMIT
            LOG.debug(
                "new-content backlog over %s; dropped %s oldest",
                NEW_CONTENT_LIMIT,
                overflow,
            )
            self.new_content = self.new_content[-NEW_CONTENT_LIMIT:]

        if not self.new_content:
            return

        if not settings.get_bool("notifyNewContent"):
            # Read here rather than where the entries are made, so turning it
            # off silences the cycle already in flight.
            self.new_content = []
            return

        if self.added_pending():
            # More additions still landing: the same predicate refresh_added
            # uses, so the toast arrives with the content rather than after
            # the metadata backlog queued behind it drains.
            return

        if self.player.isPlayingVideo() and not xbmc.getCondVisibility(
            "VideoPlayer.Content(livetv)"
        ):
            # Hold, do not drop. Additions only get written during playback
            # with syncDuringPlay on, and the news keeps until it ends.
            return

        pending, self.new_content = self.new_content, []

        try:
            messages = newcontent.summarize(pending)
        except Exception:
            LOG.exception("could not summarize %s new item(s)", len(pending))
            return

        if not messages:
            return

        LOG.info("--[ new content ] %s", " | ".join(messages))

        for message in messages:
            notification(message, time_ms=NEW_CONTENT_TIME)

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
            # The exception carries the *reason* ("unknown video database
            # v999"), which is a fragment, not something to hand a user on its
            # own -- wrap it in a sentence that says what it costs them.
            notification(localized(30420) % str(error), error=True)
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

            # After the catch-up, not instead of it: anything the change feed
            # was going to explain is already queued, so what the probe still
            # sees is drift the watermark cannot account for. It only
            # measures -- the UpdateLibrary it may enqueue is processed by
            # process_commands() on the first service() tick, once this
            # thread has returned and the startup FullSync is provably done.
            self.probe_divergence()
            self.probe_boxset_drift()

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
        again would cost one round trip per library).

        Deduped: two whitelisted movie libraries are one media class, and
        sending "movies,movies" only made the request longer."""
        include = []
        whitelist = self._include_libraries()

        with Database("kofin") as kofin_db:
            views = jellyfin_db.JellyfinDatabase(kofin_db.cursor).get_views()

        for view in views:
            if view.view_id in whitelist and view.media_type in changefeed.ALL_TYPES:
                if view.media_type not in include:
                    include.append(view.media_type)

        # Include boxsets if movies are synced
        if "movies" in include:
            include.append("boxsets")

        return include

    def _include_libraries(self):
        """The synced library ids, bare. This is the dimension the type
        classes cannot express: whitelisting one tvshows library while being
        able to see another used to fetch changes from both, and the client
        could only tell them apart by asking the server per item."""
        return {x.replace("Mixed:", "") for x in self.whitelist()}

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

    def fast_sync_started_since(self, moment):
        """Whether a change-feed pass began after ``moment`` (monotonic).

        False when nothing has run yet, and false for a pass *older* than the
        request — that one asked the server before the event the caller cares
        about, so it proves nothing about it.
        """
        started = self.last_fast_sync_started

        return started is not None and started > moment

    def fast_sync(self):
        """Incremental catch-up through the change-feed provider."""
        if self.changefeed is None:
            return True

        # Stamped before the request, not after it: a pass that is still in
        # flight has already asked, and that is what a queued FastSync needs.
        self.last_fast_sync_started = time.monotonic()
        last_sync = settings.get_str("lastIncrementalSync")
        include = self._include_types()
        libraries = self._include_libraries()
        LOG.info("--[ retrieve changes ] %s", last_sync)

        try:
            change_set = self.changefeed.changes(last_sync, include, libraries)
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
                    self.retention_repair_pending = True
                    self.enqueue_command("UpdateLibrary")

            plan = changefeed.build_plan(
                change_set.records,
                change_set.userdata,
                self._stored_checksums(change_set.records),
                self._known_parent_test(change_set.records),
                libraries,
            )

            if plan.skipped:
                # The tier-1 no-op class: dropped before download (S2.5's
                # 3067 fetches → 0). The request-count grep keys off this.
                LOG.info("---[ skipped unchanged:%s ]", plan.skipped)

            if plan.filtered:
                # Changes from libraries this box does not sync. Each one used
                # to cost an /Ancestors round trip and an error line before a
                # writer refused it; they cost nothing now, but say so — a
                # silently shorter work list is not the same as a small one.
                LOG.info("---[ outside synced libraries:%s ]", plan.filtered)

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

    def flag_unapplied(self, item_id, reason):
        """Record an item the pipeline could not apply. Called from workers.

        Only the download side held the watermark back, so anything that
        failed *after* a successful download — a writer raising, an item the
        server returned under a type nothing consumes — was logged and
        forgotten while the watermark advanced past it. The change feed is
        queried from that watermark, so such an item can never be offered
        again: it is lost until a human runs a library update. That is how a
        film added on the 22nd was still missing on the 25th with the
        watermark already three days past it.
        """
        self.unapplied_count += 1

        if len(self.unapplied_sample) < UNAPPLIED_SAMPLE:
            self.unapplied_sample.add(item_id)

        LOG.warning("could not apply %s (%s)", item_id, reason)

    def schedule_recovery_prune(self):
        """Recover unapplied items through the update-mode prune.

        Deliberately *not* by holding the watermark: one permanently bad item
        would then pin it forever, re-fetching the whole window on every
        retry and never converging. The prune diffs ids and Etags without
        consulting the watermark, so it finds anything missing however it went
        missing — it is what recovered the film above when run by hand.

        Bounded, never silent (healing-loops-plan F3): consecutive failing
        recoveries climb an interval ladder (AUTO_PRUNE_MIN_SECONDS doubling
        to AUTO_PRUNE_MAX_SECONDS) instead of retrying hourly forever, and a
        failure landing inside the floor books the retry for
        flush_recovery_prune rather than dropping it — which is what this
        used to do: the failed recovery's own drain always settles inside
        the floor, so a poison item was retried exactly once and then never
        again until unrelated feed activity happened to touch it.
        """
        count = self.unapplied_count
        sample = sorted(self.unapplied_sample)
        self.unapplied_count = 0
        self.unapplied_sample = set()

        if not count:
            # A drain that applied everything. With no retry owed, the
            # backlog is genuinely gone (a clean recovery lands here): reset
            # the ladder. Unrelated clean drains mid-backoff keep it.
            if not self.recovery_pending:
                self.recovery.reset()
            return

        if self.recovery.waiting():
            self.recovery_pending = True
            LOG.warning(
                "%s item(s) did not apply (%s); next recovery in at most %ss",
                count,
                ", ".join(sample),
                self.recovery.delay,
            )
            return

        LOG.warning(
            "%s item(s) did not apply (%s); scheduling a library update to recover",
            count,
            ", ".join(sample),
        )
        self._arm_recovery()

    def _arm_recovery(self):
        """Enqueue the recovery, stamp its floor, climb the ladder."""
        self.recovery_pending = False
        self.recovery.arm()
        self.recovery.escalate()
        self.enqueue_command("UpdateLibrary")

    def flush_recovery_prune(self):
        """Fire a booked recovery once its floor has passed.

        Drains are the only caller of schedule_recovery_prune, and a failed
        recovery's own drain settles inside the floor — without this tick
        hook the retry chain stalls after one attempt.
        """
        if not self.recovery_pending:
            return

        if self.recovery.waiting():
            return

        LOG.warning("recovery floor passed; scheduling the owed library update")
        self._arm_recovery()

    def sync_allowed_now(self):
        """Whether sync work may run at this moment.

        The same rule ``service()`` applies to its writers: video playback
        holds work back unless the user has said otherwise. With
        ``syncDuringPlay`` on, playback is a *good* time to probe and heal --
        the box is already awake and the user is not waiting on the library.
        """
        return (
            not self.player.isPlayingVideo()
            or settings.get_bool("syncDuringPlay")
            or xbmc.getCondVisibility("VideoPlayer.Content(livetv)")
        )

    def probe_divergence(self):
        """Compare per-library item counts against the server and heal on any
        gap.

        The catch-up passes are watermark-driven: they replay what the server
        recorded since the last sync. Nothing in them can see a hole that
        opened with no server-side record -- the two seasons kofin itself
        deleted during a prune were invisible to every one of them, and the
        watermark was long past. Counting is the cheapest question that
        notices anyway: one Limit=0 request per library, ~50ms against a
        4889-item library, against ~12s to page its ids.

        Any gap at all is a real gap, which is only true because the writers
        no longer decline to reference flat-layout ("virtual") seasons. While
        they did, a library sat permanently short by however many such
        seasons it had -- a number that moves whenever a show is added or a
        folder reorganised, so it could not be treated as a baseline either.
        If a steady non-zero gap ever reappears here it is a writer dropping
        something, not a fact of life: fix it there rather than teaching this
        to tolerate it.

        Counting is sensitive, not precise: equal counts can still hide
        compensating changes. That is the trade for a probe cheap enough to
        run every boot, and the prune it schedules does the real id-level
        diff.
        """
        if not self.sync_allowed_now():
            return

        if get_sync()["Libraries"]:
            # An unfinished full sync owns these libraries and resumes on its
            # own; mid-sync they are legitimately short and every count would
            # read as divergence.
            return

        if self.total_updates:
            # The catch-up just queued work, so the local side is mid-flight:
            # the gap the probe would measure is the work already in hand,
            # and it would read as divergence now and again as divergence
            # when it lands. The queued work is the heal.
            return

        diverged = {}

        with Database("kofin") as kofin_db:
            db = jellyfin_db.JellyfinDatabase(kofin_db.cursor)
            views = {view.view_id: view.media_type for view in db.get_views()}

        for entry in self.whitelist():
            library_id = entry.replace("Mixed:", "").replace("Boxsets:", "")
            media_class = views.get(library_id)
            item_types = PRUNE_SERVER_TYPES.get(media_class) if media_class else None

            if not item_types:
                continue

            try:
                remote = get_prune_count(self.api, library_id, item_types)
            except Exception as error:
                # Never fatal: a probe that cannot reach the server has
                # measured nothing, and the catch-up paths report their own
                # connectivity failures.
                LOG.warning("divergence probe failed for %s: %s", library_id, error)
                continue

            if remote is None:
                continue

            local = len(local_reference_map(library_id, media_class))

            if remote != local:
                diverged[library_id] = remote - local

        if not diverged:
            return

        LOG.warning(
            "divergence probe: %s; scheduling a library update to recover",
            ", ".join("%s server:%+d" % (k, v) for k, v in sorted(diverged.items())),
        )
        self.enqueue_command("UpdateLibrary")

    def probe_boxset_drift(self):
        """Schedule a boxsets pass when local set state disagrees with itself.

        The Etag gate cannot see local drift: a member removed and re-added
        arrives as a fresh movie row with no idSet while the set's Etag never
        moves (docs/boxsets-robustness-plan.md). This probe is the recurring
        eye that gap needs — pure-local, kofin.db's set references and
        boxset_state against one GROUP BY over MyVideos, no server traffic —
        so it can run on every startup tick alongside probe_divergence. Any
        disagreement enqueues the targeted boxsets pass, where the writer
        heals exactly the drifted sets and Etag-matched healthy sets stay
        skipped.

        Convergence: the walk ends by re-stamping every non-guarded set's
        state from measured reality (restamp_boxset_states), including both
        sides of a shared-member steal -- movie.idSet is single-valued, so
        the last set walked owns a shared member and the earlier owner's
        count moves *after* its own mid-walk stamp (V7,
        docs/healing-loops-plan.md). A guarded set keeps its stale or
        missing state deliberately: that is the designed retry, and this
        probe re-scheduling its walk is the retry's clock, not a loop bug.
        Members outside the synced libraries count into neither side. One
        walk per disturbance; a probe->walk->probe loop cannot form.
        """
        if not self.sync_allowed_now():
            return

        if get_sync()["Libraries"]:
            # An unfinished full sync owns the field, and its queue may well
            # include the boxsets pass this probe would schedule.
            return

        if self.total_updates:
            # Catch-up work in flight: boxset writes may be queued, and half
            # of them would read as drift now and heal on their own.
            return

        with Database("kofin") as kofin_db:
            db = jellyfin_db.JellyfinDatabase(kofin_db.cursor)

            if not db.get_views_by_media("boxsets"):
                # No collections view: nothing can have synced, and the pass
                # this probe schedules would have nothing to walk.
                return

            references = list(db.get_item_ids_by_media("set"))

            if not references:
                return

            states = dict(db.get_boxset_states())

        with self.database_lock:
            with Database() as videodb:
                kodi = KodiDb(videodb.cursor)
                set_rows = set(kodi.get_boxset_ids())
                counts = kodi.get_boxset_movie_counts()

        drifted = []

        for jellyfin_id, kodi_id in references:
            stored = states.get(jellyfin_id)

            if (
                kodi_id not in set_rows
                or stored is None
                or stored != counts.get(kodi_id, 0)
            ):
                drifted.append(jellyfin_id)

        if not drifted:
            return

        LOG.warning(
            "boxset drift probe: %s of %s set(s) unhealthy (%s); "
            "scheduling a boxsets pass to heal",
            len(drifted),
            len(references),
            ", ".join(drifted[:5]),
        )
        self.enqueue_command("SyncLibrary", {"Id": "Boxsets:"})

    def resume_pending_libraries(self):
        """Re-enter a full sync that sync.json still lists as unfinished.

        ``startup`` resumes the pending queue exactly once, when the library
        thread starts. If the server was unreachable at that moment — or went
        away mid-sync — the entry stays in the queue and nothing looked at it
        again until Kodi restarted. That is the reported behaviour: an
        interrupted sync comes back after a restart but not after a
        reconnection.

        Polling sync.json is what makes this reconnection-proof. An
        offline->online edge does exist (``service.main._go_offline`` lowers
        the flag on a confirmed outage, ``_connect`` raises it back), but it
        is the wrong trigger for this thread: the same outage tears this
        manager down (``sync.shims.stop``) and the edge builds its
        *replacement*, and the flag can also be lowered by an older
        generation's teardown under a live connection (``_republish_online``).
        A periodic check needs none of that to be true — it simply succeeds on
        the first tick after the server answers again.
        """
        if self.resume.waiting():
            return

        # A sync already under way owns the queue: starting another here would
        # only be refused at the claim (FullSync.__enter__), and the one in
        # flight will drain what this poll would have picked up.
        if state.is_sync_active() or not state.is_online():
            self._schedule_resume()
            return

        try:
            pending = get_sync()["Libraries"]
        except Exception as error:
            LOG.exception(error)
            self._schedule_resume(failed=True)
            return

        if not pending:
            self._schedule_resume()
            return

        LOG.info("--[ resume ] %s library(s) still pending", len(pending))

        # Same entry point startup() uses. It swallows and reports its own
        # failures, so a still-unreachable server simply leaves the queue
        # alone and we back off before looking again — resuming on a fixed
        # interval would re-toast "Resuming interrupted library sync" every
        # minute for as long as the server stayed down.
        resumed = self.add_library(None)
        self._schedule_resume(failed=not resumed)

        if resumed:
            self.update_status_strings()

    def _schedule_resume(self, failed=False):
        """Arm the next pending-queue check; back off while resuming fails."""
        if failed:
            self.resume.escalate()
        else:
            self.resume.reset()

        self.resume.arm()

    def schedule_retry(self):
        """Retry the incremental sync later, with exponential backoff.

        Silent, like the other self-healing paths: the backoff resolves the
        failure without the user doing anything, so a toast only invites
        worry about work already in hand. The warning below is the record.
        """
        self.retry.arm()
        LOG.warning("Sync incomplete, retrying in %s seconds", self.retry.delay)
        self.retry.escalate()

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
            assert self.changefeed is not None
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
            with FullSync(self, self.api) as sync:
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

                if media in ("MusicAlbum", "MusicArtist"):
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

        queued = set(self.removed_queue.snapshot())
        count = 0

        for item in data:

            if item in queued:
                continue

            queued.add(item)
            self.removed_queue.put(item)
            count += 1

        self.total_updates += count
        LOG.info("---[ removed:%s ]", count)
