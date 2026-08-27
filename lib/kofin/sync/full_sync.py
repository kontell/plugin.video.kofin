# -*- coding: utf-8 -*-
"""Full-sync passes (fork ``full_sync.py`` port): initial sync with
restore-point resume, update (catch-up + prune) and repair modes, boxsets,
library removal.

Adaptations per plan §3: RestorePoints and resume-without-modal are kept;
the first-run selection dialog and ``LibrarySyncLaterException`` are gone
(selection lives in the settings dialog); ``enableMusic`` auto-flip dropped
(derived from the whitelist); no modal dialogs from service threads —
failures notify; the advancedsettings.xml check is detection-only and runs
at service start (kodisetup), not here.
"""

from contextlib import contextmanager
import datetime
import time
from typing import Any, Dict, List, Optional, Tuple

import xbmc

from kofin.core import settings, state
from kofin.core.http import HttpError
from kofin.core.log import Logger
from kofin.sync import changefeed
from kofin.sync import downloader as server
from kofin.sync import musicsources
from kofin.sync.fields import find_library, reference_checksum
from kofin.sync.kodidb import Music as MusicKodiDb
from kofin.sync.writers import Movies, TVShows, MusicVideos, Music
from kofin.sync.writers.movies import (
    BOXSET_GUARDED,
    BOXSET_HEALED,
    BOXSET_UNCHANGED,
    BOXSET_WRITTEN,
)
from kofin.sync.db import Database, get_sync, save_sync
from kofin.sync import kofindb as jellyfin_db
from kofin.sync.shims import (
    LibraryException,
    LibraryExitException,
    LibraryOrphanException,
    localized,
    notification,
    progress,
)

LOG = Logger(__name__)

# How long a restore point stays usable. An interrupted pass is retried on the
# resume backoff (60s doubling to 30 minutes), so anything that has gone
# unclaimed for hours is not a pass waiting to continue — it is a leftover, and
# the result set it indexes has had time to move underneath it.
RESTORE_POINT_TTL = 6 * 3600

# Server-side item types the update-mode prune diffs per library class
# (phase 5). Boxsets keep their own refresh path; MusicArtist is deliberately
# absent — see _local_reference_map.
PRUNE_SERVER_TYPES = {
    "movies": "Movie",
    "tvshows": "Series,Season,Episode",
    "musicvideos": "MusicVideo",
    "music": "MusicAlbum,Audio",
}


def split_libraries(libraries, media_type_for):
    """Partition sync-list entries into (video, music), preserving order
    within each class. Music writes a different SQLite file than the video
    types, so a sync only needs to refresh the databases it actually wrote.
    Boxsets and mixed libraries are video by definition.
    """
    video, music = [], []

    for entry in libraries:
        if (
            not entry.startswith(("Boxsets:", "Mixed:"))
            and media_type_for(entry) == "music"
        ):
            music.append(entry)
        else:
            video.append(entry)

    return video, music


def local_reference_map(library_id, media_class):
    """{jellyfin_id: stored checksum} for everything kofin.db attributes
    to the library.

    Movies/musicvideos/music rows carry media_folder directly. TV
    children (seasons/episodes) do not — they are collected through the
    kodi-id parent chain plus the jellyfin_parent_id fallback, mirroring
    the writers' get_child walk. Checksums load once per involved
    jellyfin_type via the existing get_checksum query.

    Module-level so the divergence probe can measure the same local set the
    prune diffs without constructing a FullSync: that is a Borg and raises
    "Sync is already running" whenever one is in flight, which is precisely
    when a probe must stay out of the way rather than throw. A probe that
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


class FullSync(object):
    """This should be called like a context.
    i.e. with FullSync(library, server) as sync:
        sync.libraries()
    """

    # The fork made this a Borg (one dict shared by every instance) to enforce
    # "only one sync at a time". kofin cannot: the service rebuilds its object
    # graph in-process on restart, and class-level state outlives that — an
    # orphaned sync left running=True and the *new* Library's startup then
    # refused to sync at all until Kodi itself restarted, while the dict also
    # pinned the old Library's queues, threads and Api forever. The guard now
    # belongs to the Library the sync runs for, so it dies with it.
    # Loaded by libraries()/remove_library() before anything reads it; the
    # fork's class-level None was a Borg leftover and is gone.
    sync: Dict[str, Any]
    update_library = False

    def __init__(self, library, server):
        """You can call all big syncing methods here.
        Initial, update, repair, remove.
        """
        self.library = library
        self.server = server
        self._claimed = False
        # Set by begin_walk, stamped onto every point that walk saves.
        self._restore_fingerprint = None

        if library is not None and not library.claim_full_sync():
            # Deviation from the fork: a refusal, not a failure — the sync
            # already under way is fine and is what the user wanted.
            notification(localized(30410), warning=True)

            raise Exception("Sync is already running.")

        self._claimed = library is not None

    def __enter__(self):
        """Do everything we need before the sync"""
        LOG.info("-->[ fullsync ]")

        # No screensaver/idle-shutdown fiddling any more: the screensaver
        # never pauses sync (verified live, docs/widget-refresh-plan.md F9),
        # and an interrupted sync resumes from sync.json, so there is nothing
        # here worth overwriting a user setting to protect.
        state.set_sync_active(True)

        return self

    def release(self):
        """Give the library's sync claim back. Idempotent: __exit__ calls it,
        and so does the constructor's failure path by never having claimed."""
        if self._claimed and self.library is not None:
            self.library.release_full_sync()
            self._claimed = False

    def libraries(self, libraries=None, update=False):
        """Map the syncing process and start the sync. Ensure only one sync is running."""
        self.update_library = update
        self.sync = get_sync()

        if libraries:
            # Can be a single ID or a comma separated list
            libraries = libraries.split(",")
            for library_id in libraries:
                # Look up library in local kofin database
                library = self.get_library(library_id)

                if library:
                    if library.media_type == "mixed":
                        self.sync["Libraries"].append("Mixed:%s" % library_id)
                        # Include boxsets library
                        libraries_rows = self.get_libraries()
                        boxsets = [
                            row.view_id
                            for row in libraries_rows
                            if row.media_type == "boxsets"
                        ]
                        if boxsets:
                            self.sync["Libraries"].append("Boxsets:%s" % boxsets[0])
                    elif library.media_type == "movies":
                        self.sync["Libraries"].append(library_id)
                        # Include boxsets library
                        libraries_rows = self.get_libraries()
                        boxsets = [
                            row.view_id
                            for row in libraries_rows
                            if row.media_type == "boxsets"
                        ]
                        # Verify we're only trying to sync boxsets once
                        if boxsets and boxsets[0] not in self.sync["Libraries"]:
                            self.sync["Libraries"].append("Boxsets:%s" % boxsets[0])
                    else:
                        # Only called if the library isn't already known about
                        self.sync["Libraries"].append(library_id)
                else:
                    self.sync["Libraries"].append(library_id)
        else:
            self.mapping()

        # A queue that crashed mid-run resumes with its unfinished tail, and
        # repeated crashes re-append the same ids; syncing a library twice is
        # wasted work, so collapse duplicates while preserving order.
        self.sync["Libraries"] = list(dict.fromkeys(self.sync["Libraries"]))

        if self.sync["Libraries"]:
            self.start()

    def get_libraries(self):
        with Database("kofin") as kofin_db:
            return jellyfin_db.JellyfinDatabase(kofin_db.cursor).get_views()

    def get_library(self, library_id):
        with Database("kofin") as kofin_db:
            return jellyfin_db.JellyfinDatabase(kofin_db.cursor).get_view(library_id)

    def mapping(self):
        """Resume a previously interrupted sync, if any.

        The fork also offered the first-run library selection modal here;
        in kofin the whitelist only arrives from the settings dialog, so an
        empty pending list means there is nothing to do.
        """
        if self.sync["Libraries"]:
            # Resume automatically: a modal prompt at startup blocks an
            # unattended HTPC forever. Starting over remains available via
            # the repair option in the settings dialog.
            LOG.info(
                "Resuming interrupted sync of %s libraries",
                len(self.sync["Libraries"]),
            )
            notification(localized(30404))

        save_sync(self.sync)

    def start(self):
        """Main sync process."""
        LOG.info("starting sync with %s", self.sync["Libraries"])
        save_sync(self.sync)
        start_time = datetime.datetime.now()

        # Watermark-at-start (phase 5, plan §2): the very first sync stamps
        # the watermark before paging begins, so the first catch-up replays
        # the sync window. Full syncs never advance the watermark at their
        # end — that jumped it past pending queue records for other
        # libraries; the incremental path is the sole owner.
        self.library.stamp_watermark_if_empty()

        libraries = list(self.sync["Libraries"])
        failures: List[Exception] = []

        self.process_libraries(libraries, failures)
        save_sync(self.sync)

        # Refresh the databases this sync actually wrote. Refreshing only video
        # left a freshly synced music library invisible in the music widgets.
        # Update mode refreshes nothing here: it only *plans* (the prune hands
        # every write to the incremental pipeline), so the refresh belongs to
        # the drain that lands the work — refreshing at plan time re-rendered
        # every widget for rows that had not changed yet, doubling the update
        # command's cost for nothing (widget-refresh-plan F2/D4).
        #
        # Before the failure re-raise, not after: a run that lost one library
        # still wrote the others, and this is the refresh that shows them --
        # ``libraries`` is the whole queue, so a database a failed library
        # half-wrote is covered too, fingerprint-gated like the rest. The
        # per-library publish in process_libraries must not stand in for it:
        # that one carries no force_reload, and force_reload is the point.
        if not self.update_library:
            synced_video, synced_music = split_libraries(libraries, self._media_type)
            databases = set()

            if synced_video:
                databases.add("video")

            if synced_music:
                databases.add("music")

            # force_reload: the end of a full sync is the one moment kofin
            # knows every selected library has landed, and the first-content
            # probes cannot be trusted to notice — see refresh_libraries.
            self.library.refresh_libraries(databases, force_reload=True)

        if failures:
            # After the refresh, before the completion toast: what synced is
            # visible, and a partial run never claims success.
            raise failures[0]

        elapsed = datetime.datetime.now() - start_time

        # Music playlists are files, not MyMusic rows — refresh after a
        # successful library pass when the setting is on. Soft-fail so a
        # playlist error never fails the music library sync itself.
        self._maybe_refresh_music_playlists()

        if self.update_library:
            # Update mode only *plans*: prune diffs the library and hands the
            # work to the incremental pipeline, which has its own progress bar
            # and drain. Announcing a finished sync here claimed a 22k-item
            # backlog was already written, seconds after queueing it.
            LOG.info("Update pass planned in: %s", str(elapsed).split(".")[0])
        else:
            notification(
                "%s %s" % (localized(30409), str(elapsed).split(".")[0]),
            )
            LOG.info("Full sync completed in: %s", str(elapsed).split(".")[0])

    def _maybe_refresh_music_playlists(self):
        """Rewrite ``playlists/music/Kofin/*.m3u8`` from the server (one-way)."""
        if not settings.get_bool("syncMusicPlaylists"):
            return
        try:
            from kofin.sync import playlists as music_playlists

            with self.library.music_database_lock:
                music_playlists.refresh_with_databases(self.server)
            self.library.defer_playlist_poll()
        except Exception:
            LOG.exception("music playlist refresh failed (library sync kept)")

    def process_libraries(self, libraries, failures):
        """Process libraries in order, recording completion after each.

        Failures are collected for the caller to re-raise, so one bad library
        does not abandon the rest: a library that fails stays in the pending
        queue (process_library saved it there before raising) and the resume
        backoff owns its retry, while the libraries after it sync now rather
        than after that retry lands. The ``try`` sits inside the loop for that
        reason -- around it, the first failure ended the walk and the list
        never held more than one entry.

        A LibraryExitException is not a library failure: Kodi is quitting,
        the service is stopping, or the server has gone away, and every
        remaining library would raise the same. It abandons the rest.
        """
        for position, library in enumerate(libraries):

            try:
                synced = self.process_library(library)
            except LibraryExitException:
                raise
            except Exception as error:
                failures.append(error)
                continue

            if (
                synced
                and not library.startswith("Boxsets:")
                and library not in self.sync["Whitelist"]
            ):
                self.sync["Whitelist"].append(library)

            if library in self.sync["Libraries"]:
                self.sync["Libraries"].remove(library)

            save_sync(self.sync)

            # The last library is left to the end-of-sync refresh in start(),
            # which runs whether or not a failure is on the list.
            if synced and position < len(libraries) - 1:
                self._publish_library(library)

    def _publish_library(self, library):
        """Make a finished library visible before the rest of the sync runs.

        A full sync is one unit across every selected library, and the only
        refresh used to be the end-of-sync one, so a first sync showed an
        empty home screen until the *last* library finished. That is a few
        seconds on a desktop and 43 minutes on a Pi 3B (movies were complete
        and browsable 8 minutes in, with Home still reading "empty"), because
        Kodi raises no library-change event for direct SQLite writes.

        Skipped for the final library: the end-of-sync refresh in ``start()``
        covers it -- failure or not, it runs before the re-raise -- and firing
        both would pay for two scans and two vacuums to show the same rows.

        Cheap when there is nothing to say. ``refresh_libraries`` is
        fingerprint-gated, so a library that changed nothing a widget renders
        is suppressed there rather than here, and the first-content reload is
        gated per kind on Kodi's own cached bools — so the expensive part, a
        ``ReloadSkin()``, still happens at most once per media kind however
        many libraries are selected.
        """
        if self.update_library:
            # Update mode only plans; the drain that lands the work owns its
            # own refresh (the same reason sync() skips it).
            return

        video, music = split_libraries([library], self._media_type)
        databases = set()

        if video:
            databases.add("video")

        if music:
            databases.add("music")

        self.library.refresh_libraries(databases)

    def _media_type(self, library_id):
        view = self.get_library(library_id)

        return view.media_type if view else None

    def begin_walk(self, key, parent_id, item_type=None, basic=False, params=None):
        """Fingerprint the walk about to run; return where to resume it.

        One call rather than two so the fingerprint a point is *checked*
        against and the one it is *stamped* with can never drift apart: both
        come from the arguments the caller is about to hand ``get_items``.
        """
        self._restore_fingerprint = server.restore_fingerprint(
            self.server, parent_id, item_type, basic, params
        )

        return self.get_restore_point(key, self._restore_fingerprint)

    def get_restore_point(self, key, fingerprint=None):
        """The position to resume this walk at, when it still means something.

        A restore point is an index into a result set, and it survives in
        sync.json until the walk that owns it completes. Nothing else expired
        it, so one could outlive the pass it belonged to indefinitely: a
        movies point reading ``StartIndex: 1250`` was found on a live box
        having crossed an addon upgrade (its stored url was the pre-10.9
        ``/Users/{id}/Items`` route), left behind because update mode
        reconciles through ``prune`` and never runs the walk that would have
        cleared it.

        Resuming into a stale number is silent and one-directional. The walk
        sorts DateCreated descending, so N items added since the point was
        written push everything down by N: the resumed pass re-does N items
        it had already done (idempotent, harmless) and **never visits the N
        newest** — the items a user is most likely to be waiting for. So an
        unusable point is dropped rather than trusted; a walk from zero is
        idempotent and Etag-short-circuits, which makes discarding cheap and
        resuming wrongly the only expensive option.

        Two ways to be unusable, both checked here:

        * the query changed, so the number indexes a different set
          (``downloader.restore_fingerprint``) — an upgrade that adds a field
          is the routine case, and this box hit exactly that;
        * it is too old to be a resume at all. An interrupted pass retries on
          the resume backoff, which tops out at 30 minutes, so a point that
          has not been picked up within ``RESTORE_POINT_TTL`` is not a pass
          waiting to continue, it is a corpse.
        """
        entry = self.sync["RestorePoints"].get(key)

        if not entry:
            return None

        stored = entry.get("Fingerprint")

        if fingerprint is not None and stored != fingerprint:
            LOG.info(
                "--[ restore point/%s ] discarded: the query changed since it "
                "was written",
                key,
            )
            self.clear_restore_point(key)

            return None

        if self._restore_point_expired(entry):
            LOG.info(
                "--[ restore point/%s ] discarded: older than %s seconds",
                key,
                RESTORE_POINT_TTL,
            )
            self.clear_restore_point(key)

            return None

        return entry.get("params")

    def _restore_point_expired(self, entry):
        """Whether a stored point is too old to be a resume.

        An unstamped point is expired by definition: it predates this check,
        so it is exactly the kind that has been sitting there across upgrades.
        """
        saved_at = entry.get("SavedAt")

        if not saved_at:
            return True

        try:
            age = time.time() - float(saved_at)
        except (TypeError, ValueError):
            return True

        return age > RESTORE_POINT_TTL

    def set_restore_point(self, key, restore_point):
        stamped = dict(restore_point)
        stamped["SavedAt"] = time.time()
        stamped["Fingerprint"] = self._restore_fingerprint
        self.sync["RestorePoints"][key] = stamped

    def clear_restore_point(self, key):
        self.sync["RestorePoints"].pop(key, None)

    def clear_library_restore_points(self, library_id):
        """Drop every restore point belonging to a library.

        Update mode reconciles through ``prune`` and never runs the walk that
        owns the point, so without this a library proven fully in sync keeps a
        position claiming it is part-way through one — which is how the live
        one survived. Keyed by prefix because a library owns several (the
        tvshows walk keeps one slot per pass).
        """
        prefix = "%s/" % library_id
        stale = [key for key in self.sync["RestorePoints"] if key.startswith(prefix)]

        for key in stale:
            LOG.info("--[ restore point/%s ] cleared: library reconciled", key)
            self.clear_restore_point(key)

    def process_library(self, library_id):
        """Add a library by its id. Create a node and a playlist whenever appropriate.

        Returns True when the library was processed (and may be whitelisted),
        False when it was dropped because the server no longer has it.
        """
        media = {
            "movies": self.movies,
            "musicvideos": self.musicvideos,
            "tvshows": self.tvshows,
            "music": self.music,
        }
        try:
            if library_id.startswith("Boxsets:"):
                boxset_library = {}

                # Initial library sync is 'Boxsets:'
                # Refresh from the settings dialog is 'Boxsets:Refresh'
                # Incremental syncs are 'Boxsets:$library_id'
                sync_id = library_id.split(":")[1]

                if not sync_id or sync_id == "Refresh":
                    libraries = self.get_libraries()
                else:
                    _lib = self.get_library(sync_id)
                    libraries = [_lib] if _lib else []

                for entry in libraries:
                    if entry.media_type == "boxsets":
                        boxset_library = {"Id": entry.view_id, "Name": entry.view_name}
                        break

                if boxset_library:
                    if sync_id == "Refresh":
                        self.refresh_boxsets(boxset_library)
                    else:
                        self.boxsets(boxset_library)

                return True

            try:
                library = self.server.item(library_id.replace("Mixed:", ""))
            except HttpError as error:
                # Deleted server-side while queued. Dropping it here (instead
                # of raising) keeps a dead id from wedging every future sync
                # run on the same 404.
                if error.status != 404:
                    raise
                LOG.warning(
                    "library %s is gone from the server; dropped from the sync queue",
                    library_id,
                )
                return False

            if self.update_library:
                # Update mode is the ids+Etag prune (phase 5, research §3
                # "update that works"): plan the diff, enqueue the work
                # through the incremental pipeline — no full walk.
                self.prune(library, library_id)
                # The prune reconciled the library, so any position left over
                # from an interrupted walk is answered — and because update
                # mode never runs the walk that owns it, this is the only
                # thing that ever sweeps it.
                self.clear_library_restore_points(library["Id"])

                return True

            if library_id.startswith("Mixed:"):
                for mixed in ("movies", "tvshows"):
                    # Each pass keeps its own restore point slot.
                    media[mixed](library)
            else:
                media[library["CollectionType"]](library)
            return True
        except LibraryException as error:
            if isinstance(error, LibraryExitException):
                save_sync(self.sync)
                raise

            # A non-exit LibraryException is a pass-level failure: per-item
            # conditions (orphans, items deleted mid-page) are absorbed one
            # level down in apply_or_skip, so what reaches here is the likes
            # of the prune-map truncation guard (downloader.get_id_etag_map).
            # The fork swallowed these and reported success — the entry left
            # sync.json with the library half-written and nothing owing a
            # retry (healing-loops-plan F3). Fail like any other error: the
            # entry stays queued and the resume backoff owns the retry.
            self._notify_sync_failure(library_id)
            LOG.error("library %s failed: %s", library_id, error)
            save_sync(self.sync)

            raise

        except Exception as error:
            self._notify_sync_failure(library_id)

            LOG.error("full sync exited unexpectedly")
            LOG.exception(error)

            save_sync(self.sync)

            raise

    def _notify_sync_failure(self, library_id):
        """One failure toast per library per service lifetime.

        A failing library retries on the resume backoff (60s doubling to 30
        minutes, reset each boot); toasting every attempt turns one dead
        library into a nag loop (healing-loops-plan F3). The log still
        carries every attempt.
        """
        toasted = getattr(self.library, "sync_failure_toasted", None)

        if toasted is None or library_id not in toasted:
            if toasted is not None:
                toasted.add(library_id)

            notification(localized(30406), error=True)

    @contextmanager
    def video_database_locks(self):
        with self.library.database_lock:
            # kofin.db outermost, so the Kodi database commits first at block
            # exit: a failed Kodi commit must not leave the mapping claiming
            # rows MyVideos never got (audit finding #17) — those short-circuit
            # every later Etag-gated walk. The periodic in-pass commits already
            # go Kodi-first.
            with Database("kofin") as jellyfindb:
                with Database() as videodb:
                    yield videodb, jellyfindb

    @contextmanager
    def _held_connections(self):
        """Connections held across a pass; the writer lock and the commits
        are per page (phase 5, sync-plan Phase 3): realtime writers
        interleave exactly as before, only the per-page open/close churn is
        gone. Yields the per-page scope ``_walk`` enters for each page."""
        with Database("kofin") as jellyfindb, Database() as videodb:

            @contextmanager
            def page():
                with self.library.database_lock:
                    yield videodb, jellyfindb
                    videodb.conn.commit()
                    jellyfindb.conn.commit()

            yield page

    def _walk(
        self,
        library,
        item_type,
        restore_key,
        writer,
        apply,
        describe,
        dialog,
        heading,
        page,
        params=None,
    ):
        """The one library walk: page the server from the restore point,
        enter ``page`` for each page (the lock and the connections, one of
        the two shapes above), construct the writer, stamp the restore
        point, write every item through ``apply_or_skip``, and let the page
        scope commit.

        This used to exist four times -- movies, each tvshows pass,
        musicvideos and boxsets -- and only the tvshows copy had grown the
        mid-page-404 skip, so a movie deleted after it was paged aborted its
        whole library (docs/sync-refactor-assessment.md §3). Now every walk
        skips it. The restore key, the writer, the per-item call and the
        label are what a caller supplies; the mechanics are here once.

        Returns ``(resumed, skipped, results)``: whether the walk resumed a
        restore point, the ids ``apply_or_skip`` declined, and a list of
        ``(item, value)`` for every item applied, ``value`` being what
        ``apply`` returned -- the boxsets walk reads its outcome codes from
        it, the others ignore it.
        """
        restore_point = self.begin_walk(
            restore_key, library["Id"], item_type, False, params
        )
        resumed = restore_point is not None
        skipped = []
        results = []

        for items in server.get_items(
            self.server,
            library["Id"],
            item_type,
            False,
            restore_point or params,
        ):

            with page() as (videodb, jellyfindb):
                obj = writer(jellyfindb, videodb)

                self.set_restore_point(restore_key, items["RestorePoint"])
                start_index = items["RestorePoint"]["params"]["StartIndex"]
                total = float(items["TotalRecordCount"])

                for index, item in enumerate(items["Items"]):

                    dialog.update(
                        int((float(start_index + index) / total) * 100),
                        heading=heading,
                        message=describe(item),
                    )
                    captured = {}

                    def run(obj, item):
                        captured["value"] = apply(obj, item)

                    if self.apply_or_skip(run, obj, item, item_type):
                        results.append((item, captured.get("value")))
                    else:
                        skipped.append(item.get("Id"))

        if skipped:
            # Never let a partial pass look complete in the log.
            LOG.warning(
                "--[ %s pass: %d not applied, skipped ]",
                item_type,
                len(skipped),
            )

        return resumed, skipped, results

    @staticmethod
    def _heading(library):
        return "%s: %s" % ("Kofin", library["Name"])

    @progress()
    def movies(self, library, dialog):
        """Process movies from a single library."""
        restore_key = "%s/movies" % library["Id"]

        with self._held_connections() as page:
            self._walk(
                library,
                "Movie",
                restore_key,
                lambda jellyfindb, videodb: Movies(
                    self.server, jellyfindb, videodb, library
                ),
                lambda obj, movie: obj.movie(movie),
                lambda movie: movie["Name"],
                dialog,
                self._heading(library),
                page,
            )

        self.clear_restore_point(restore_key)

    def apply_or_skip(self, apply, obj, item, item_type):
        """Write one item; True on success, False if it could not be applied.

        The library-level drop in ``process_library``, one level down. An
        item deleted after it was paged 404s on the child fetches its writer
        makes (a show's ``/Seasons``), and an unguarded raise aborts the
        whole library — wedging every future run on the same dead id, which
        surfaces as a sync-failed toast on every service start. Anything but
        a 404 is a real failure and still stops the pass.

        A child whose parent cannot be resolved is the same shape of problem:
        one item's business, not the library's. The passes run parents before
        children, so reaching this means the parent is genuinely unavailable —
        the item is skipped and the pass carries on. In the service the same
        raise is what flags an item unapplied; here the caller's skip count
        already says a pass did not fully land.
        """
        try:
            apply(obj, item)
            return True
        except LibraryOrphanException as error:
            LOG.warning(
                "%s %s could not be applied: %s", item_type, item.get("Id"), error
            )
            return False
        except HttpError as error:
            if error.status != 404:
                raise
            LOG.warning(
                "%s %s is gone from the server; skipped",
                item_type,
                item.get("Id"),
            )
            return False

    @progress()
    def tvshows(self, library, dialog):
        """Process tvshows, seasons and episodes from a single library.

        Three per-library passes (phase 5, sync-plan P5) instead of one
        episode request per show: Series pages, then Season pages, then
        Episode pages -- parents land before children by construction, and
        a 500-show library costs pages, not 500+ requests. Restore points
        are per pass and cleared together at the end: an interruption
        resumes inside the pass it happened in, completed passes re-do only
        their final page (writes are idempotent and Etag-short-circuited).
        A pre-phase-5 pending ``{lib}/tvshows`` key simply restarts the
        library's passes; it is cleared alongside.
        """

        def child_label(item):
            return "%s / %s" % (item.get("SeriesName") or "", item.get("Name"))

        passes = (
            (
                "Series",
                "series",
                lambda obj, show: obj.tvshow(show),
                lambda s: s["Name"],
            ),
            ("Season", "seasons", lambda obj, season: obj.season(season), child_label),
            (
                "Episode",
                "episodes",
                lambda obj, episode: (
                    obj.episode(episode) if episode.get("Path") else None
                ),
                child_label,
            ),
        )

        with self._held_connections() as page:
            for item_type, key_suffix, apply, describe in passes:
                self._walk(
                    library,
                    item_type,
                    "%s/tvshows-%s" % (library["Id"], key_suffix),
                    lambda jellyfindb, videodb: TVShows(
                        self.server, jellyfindb, videodb, library, True
                    ),
                    apply,
                    describe,
                    dialog,
                    self._heading(library),
                    page,
                )

        for key_suffix in ("series", "seasons", "episodes"):
            self.clear_restore_point("%s/tvshows-%s" % (library["Id"], key_suffix))
        # Legacy single-pass key from a pre-phase-5 interrupted sync.
        self.clear_restore_point("%s/tvshows" % library["Id"])

    @progress()
    def musicvideos(self, library, dialog):
        """Process musicvideos from a single library."""
        restore_key = "%s/musicvideos" % library["Id"]

        with self._held_connections() as page:
            self._walk(
                library,
                "MusicVideo",
                restore_key,
                lambda jellyfindb, videodb: MusicVideos(
                    self.server, jellyfindb, videodb, library
                ),
                lambda obj, mvideo: obj.musicvideo(mvideo),
                lambda mvideo: mvideo["Name"],
                dialog,
                self._heading(library),
                page,
            )

        self.clear_restore_point(restore_key)

    @progress()
    def music(self, library, dialog):
        """Process artists, album, songs from a single library."""
        with self.library.music_database_lock:
            # kofin.db outermost for the same commit-order reason as
            # video_database_locks.
            with Database("kofin") as jellyfindb:
                with Database("music") as musicdb:
                    obj = Music(self.server, jellyfindb, musicdb, library)

                    library_id = library["Id"]

                    total_items = server.get_item_count(
                        self.server, library_id, "MusicArtist,MusicAlbum,Audio"
                    )
                    count = 0

                    """
                    Music database syncing.  Artists must be in the database
                    before albums, albums before songs.  Pulls batches of items
                    in sizes of setting "Paging - Max items".  'artists',
                    'albums', and 'songs' are generators containing a dict of
                    api responses
                    """
                    artists = server.get_artists(self.server, library_id)
                    for batch in artists:
                        for item in batch["Items"]:
                            LOG.debug("Artist: {}".format(item.get("Name")))
                            percent = int((float(count) / float(total_items)) * 100)
                            dialog.update(
                                percent,
                                heading="%s: %s" % ("Kofin", library["Name"]),
                                message="Artist: {}".format(item.get("Name")),
                            )
                            obj.artist(item)
                            count += 1

                    # Sort pairs are spelled in full: get_items' default is
                    # composite, and overriding SortBy alone left a mismatched
                    # SortOrder that Jellyfin 10.11 answers with a 400 (see
                    # downloader.align_sort_order). SortName breaks the tie so
                    # StartIndex paging stays deterministic under equal album
                    # artists, exactly as the video default does.
                    albums = server.get_items(
                        self.server,
                        library_id,
                        item_type="MusicAlbum",
                        params={
                            "SortBy": "AlbumArtist,SortName",
                            "SortOrder": "Ascending,Ascending",
                            "Fields": server.music_page_info(),
                        },
                    )
                    for batch in albums:
                        for item in batch["Items"]:
                            LOG.debug("Album: {}".format(item.get("Name")))
                            percent = int((float(count) / float(total_items)) * 100)
                            dialog.update(
                                percent,
                                heading="%s: %s" % ("Kofin", library["Name"]),
                                message="Album: {} - {}".format(
                                    item.get("AlbumArtist", ""), item.get("Name")
                                ),
                            )
                            obj.album(item)
                            count += 1

                    # Album is in the song key so tracks arrive grouped by
                    # album: songs whose album is somehow not in kodi yet
                    # create it on demand (song_add), and grouping means that
                    # costs one lookup per album instead of one per track.
                    songs = server.get_items(
                        self.server,
                        library_id,
                        item_type="Audio",
                        params={
                            "SortBy": "AlbumArtist,Album,SortName",
                            "SortOrder": "Ascending,Ascending,Ascending",
                            "Fields": server.music_page_info(),
                        },
                    )
                    for batch in songs:
                        for item in batch["Items"]:
                            LOG.debug("Song: {}".format(item.get("Name")))
                            percent = int((float(count) / float(total_items)) * 100)
                            dialog.update(
                                percent,
                                heading="%s: %s" % ("Kofin", library["Name"]),
                                message="Track: {} - {}".format(
                                    item.get("AlbumArtist", ""), item.get("Name")
                                ),
                            )
                            obj.song(item)
                            count += 1

                    # The writers link each album to its library's source as
                    # they go, but only when they actually rewrite something
                    # — check_unchanged returns before the hook. This closes
                    # the walk over what an unchanged pass skipped, and heals
                    # a source table Kodi's own scanner emptied (it runs
                    # DELETE FROM source whenever it disagrees with
                    # sources.xml, which with an empty one it always does).
                    musicsources.reassert(
                        jellyfindb.cursor,
                        musicdb.cursor,
                        obj.music_views(),
                    )

    @progress()
    def prune(self, library, library_id, dialog):
        """Update-mode pass (phase 5, research §3 "update that works"):
        page the library's id+Etag set, diff against kofin.db three ways —
        missing here → fetch; stale here → remove; Etag mismatch → fetch;
        match → nothing — and enqueue the work through the incremental
        pipeline (downloads Etag-short-circuit again on write, removals
        route through the SortWorker). The catch-up that runs alongside
        (Update = sync-queue catch-up **plus** this prune) covers userdata.
        """
        classes: Tuple[Optional[str], ...]
        if library_id.startswith("Mixed:"):
            classes = ("movies", "tvshows")
        else:
            classes = (library.get("CollectionType"),)

        missing = []
        changed = []
        stale = []

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

            server_map = server.get_id_etag_map(
                self.server, library["Id"], server_types
            )
            local_map = self._local_reference_map(library["Id"], media_class)

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
        spared = []

        if stale:
            resolved = server.get_existing_ids(self.server, stale)

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
                "prune/%s spared %s stale candidate(s) the server still "
                "resolves: %s",
                library["Id"],
                len(spared),
                ", ".join(sorted(spared)[:10]),
            )
            self._rehome_spared(spared)

        self.library.removed(stale)
        self.library.added(missing_ids)
        self.library.updated(changed)

    def _rehome_spared(self, spared):
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
                    home = find_library(self.server, {"Id": item_id})
                except Exception as error:
                    LOG.warning("could not re-home %s: %s", item_id, error)
                    continue

                folder = home["Id"] if home else None
                db.update_media_folder(folder, item_id)
                LOG.warning(
                    "re-homed spared %s to %s", item_id, folder or "placeholder"
                )

    def _local_reference_map(self, library_id, media_class):
        """Instance view of the module-level
        :func:`local_reference_map` -- see there."""
        return local_reference_map(library_id, media_class)

    @progress(30407)
    def boxsets(self, library, dialog=None):
        """Process all boxsets.

        Beyond the fork (docs/boxsets-robustness-plan.md): the walk asks the
        server for ChildCount — the unlink guard's server signal, measured
        harmless at set counts and deliberately not added to the shared
        info() field list — tallies per-set outcomes into one summary line,
        sweeps references the server listing no longer contains, and ends by
        re-stamping every non-guarded set's state from measured reality
        (shared members drift the mid-walk stamps; healing-loops-plan F1).
        """
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
        resumed, skipped, results = self._walk(
            library,
            "BoxSet",
            restore_key,
            lambda jellyfindb, videodb: Movies(
                self.server, jellyfindb, videodb, library
            ),
            lambda obj, boxset: obj.boxset(boxset),
            lambda boxset: boxset["Name"],
            dialog,
            "%s: %s" % ("Kofin", localized(30407)),
            self.video_database_locks,
            params=boxset_params,
        )

        # Every set the listing carried counts as walked, skipped ones
        # included: the sweep below treats absence from ``walked`` as
        # deletion, and a set that 404'd mid-page is gone from the server
        # anyway -- the next fresh walk sweeps its reference.
        walked = {item["Id"] for item, _ in results} | set(skipped)
        guarded_ids = set()

        for boxset, outcome in results:
            if outcome in stats:
                stats[outcome] += 1

            if outcome == BOXSET_GUARDED:
                guarded_ids.add(boxset["Id"])

        self.clear_restore_point(restore_key)

        # A resumed walk never listed its earlier pages, so only a fresh,
        # complete walk may treat absence from the listing as deletion.
        swept = 0 if resumed else self.sweep_stale_boxsets(walked)

        # Walk-end restamp (docs/healing-loops-plan.md F1): after the sweep,
        # so measured state covers exactly the references that survived. It
        # runs on resumed walks too -- it is measurement, not deletion, so
        # the fresh-start gate above does not apply.
        with self.video_database_locks() as (videodb, jellyfindb):
            Movies(self.server, jellyfindb, videodb).restamp_boxset_states(guarded_ids)

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

    def sweep_stale_boxsets(self, walked):
        """Remove set references the server listing no longer contains.

        The walk is the same listing the writes came from, so a reference
        absent from it is a set deleted server-side with no record to say so
        — the prune never covers boxsets, and without a change-feed Removed
        record such a set was a ghost forever. An empty listing against
        existing references is not a deletion order (permission and filter
        failures look exactly like it): skip and warn, mirroring the prune's
        get_existing_ids philosophy.
        """
        with self.video_database_locks() as (videodb, jellyfindb):
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

            obj = Movies(self.server, jellyfindb, videodb)

            for item_id in stale:
                obj.remove(item_id)

        LOG.info("swept %s stale boxset(s): %s", len(stale), ", ".join(stale[:5]))

        return len(stale)

    def refresh_boxsets(self, library):
        """Delete all existing boxsets and re-add."""
        with self.video_database_locks() as (videodb, jellyfindb):
            db = jellyfin_db.JellyfinDatabase(jellyfindb.cursor)
            before = len(db.get_items_by_media("set"))
            obj = Movies(self.server, jellyfindb, videodb, library)
            obj.boxsets_reset()

        LOG.info("refresh boxsets: reset %s set(s), re-adding", before)
        self.boxsets(library)

    @progress(30408)
    def remove_library(self, library_id, dialog):
        """Remove library by their id from the Kodi database."""
        with Database("kofin") as jellyfindb:

            db = jellyfin_db.JellyfinDatabase(jellyfindb.cursor)
            library = db.get_view(library_id.replace("Mixed:", ""))

            if library is None:
                LOG.info("Library %s is already removed", library_id)

                return

            items = db.get_item_by_media_folder(library_id.replace("Mixed:", ""))
            media = "music" if library.media_type == "music" else "video"

            # A music library's `source` row is not one of its items, so it
            # outlives an item-less removal: the gate has to open for the
            # media type, not just for the count. Every album's own
            # album_source link goes with the album (tgrDeleteAlbum), and
            # what tgrDeleteSource does not cover is exactly this row.
            if items or library.media_type == "music":
                with (
                    self.library.music_database_lock
                    if media == "music"
                    else self.library.database_lock
                ):
                    with Database(media) as kodidb:

                        count = 0

                        if library.media_type == "mixed":

                            movies = [x for x in items if x[1] == "Movie"]
                            tvshows = [x for x in items if x[1] == "Series"]

                            obj = Movies(
                                self.server, jellyfindb, kodidb, library
                            ).remove

                            for item in movies:

                                obj(item[0])
                                dialog.update(
                                    int((float(count) / float(len(items)) * 100)),
                                    heading="%s: %s" % ("Kofin", library.view_name),
                                )
                                count += 1

                            obj = TVShows(
                                self.server, jellyfindb, kodidb, library
                            ).remove

                            for item in tvshows:

                                obj(item[0])
                                dialog.update(
                                    int((float(count) / float(len(items)) * 100)),
                                    heading="%s: %s" % ("Kofin", library.view_name),
                                )
                                count += 1
                        else:
                            default_args = (self.server, jellyfindb, kodidb)
                            for item in items:
                                if item[1] in ("Series", "Season", "Episode"):
                                    TVShows(*default_args).remove(item[0])
                                elif item[1] in ("Movie", "BoxSet"):
                                    Movies(*default_args).remove(item[0])
                                elif item[1] in (
                                    "MusicAlbum",
                                    "MusicArtist",
                                    "Audio",
                                ):
                                    Music(*default_args).remove(item[0])
                                elif item[1] == "MusicVideo":
                                    MusicVideos(*default_args).remove(item[0])

                                dialog.update(
                                    int((float(count) / float(len(items)) * 100)),
                                    heading="%s: %s" % ("Kofin", library.view_name),
                                )
                                count += 1

                        if library.media_type == "music":
                            MusicKodiDb(kodidb.cursor).delete_source_for(
                                library_id.replace("Mixed:", "")
                            )

        self.sync = get_sync()

        if library_id in self.sync["Whitelist"]:
            self.sync["Whitelist"].remove(library_id)

        elif "Mixed:%s" % library_id in self.sync["Whitelist"]:
            self.sync["Whitelist"].remove("Mixed:%s" % library_id)

        save_sync(self.sync)

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exiting sync"""
        self.release()
        state.set_sync_active(False)

        LOG.info("--<[ fullsync ]")
