# -*- coding: utf-8 -*-
"""Full-sync passes (fork ``full_sync.py`` port): initial sync with
restore-point resume, update (catch-up + prune) and repair modes, boxsets,
library removal.

P2.2 split: the restore points (``restorepoints``), the update-mode plan
(``prune``), the boxsets pass (``boxsets``) and the removal (``removal``)
are modules of their own; what a sync needs from the Library it runs for is
the ``SyncHost`` port (``host``). This class keeps the library queue, the
per-library dispatch, the one walk every video pass runs through, and the
locks and connections it hands out.

Adaptations per plan §3: RestorePoints and resume-without-modal are kept;
the first-run selection dialog and ``LibrarySyncLaterException`` are gone
(selection lives in the settings dialog); ``enableMusic`` auto-flip dropped
(derived from the whitelist); no modal dialogs from service threads —
failures notify; the advancedsettings.xml check is detection-only and runs
at service start (kodisetup), not here.
"""

from contextlib import contextmanager
import datetime
from typing import Any, Dict, List, Optional, Tuple

import xbmc

from kofin.core import settings, state
from kofin.core.http import HttpError
from kofin.core.log import Logger
from kofin.sync import boxsets, prune, removal, restorepoints
from kofin.sync import downloader as server
from kofin.sync import musicsources
from kofin.sync.hooks import pipeline_hooks
from kofin.sync.writers import Movies, TVShows, MusicVideos, Music
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

    def __init__(self, host, server, loader=None, saver=None):
        """``host`` is the SyncHost of the Library this sync runs for (None
        for the direct-call paths tests use); ``loader``/``saver`` read and
        write sync.json (``get_sync``/``save_sync`` unless injected).

        Construction claims nothing: the one-sync-at-a-time claim is taken
        by ``__enter__``, so a FullSync that is only ever called directly
        (the prune planner, a boxsets walk in a test) never holds one.
        """
        self.host = host
        self.server = server
        self._load = loader or get_sync
        self._save = saver or save_sync
        self._claimed = False
        # Set by begin_walk, stamped onto every point that walk saves.
        self._restore_fingerprint = None
        # The pipeline's writer hooks (kofin.sync.hooks), one composition per
        # sync.
        self.hooks = pipeline_hooks()

    def __enter__(self):
        """Take the claim and mark the sync active."""
        if self.host is not None and not self.host.claim():
            # Deviation from the fork: a refusal, not a failure — the sync
            # already under way is fine and is what the user wanted.
            notification(localized(30410), warning=True)

            raise Exception("Sync is already running.")

        self._claimed = self.host is not None
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
        if self._claimed and self.host is not None:
            self.host.release()
            self._claimed = False

    def libraries(self, libraries=None, update=False):
        """Map the syncing process and start the sync. Ensure only one sync is running."""
        self.update_library = update
        self.sync = self._load()

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

        self._save(self.sync)

    def start(self):
        """Main sync process."""
        LOG.info("starting sync with %s", self.sync["Libraries"])
        self._save(self.sync)
        start_time = datetime.datetime.now()

        # Watermark-at-start (phase 5, plan §2): the very first sync stamps
        # the watermark before paging begins, so the first catch-up replays
        # the sync window. Full syncs never advance the watermark at their
        # end — that jumped it past pending queue records for other
        # libraries; the incremental path is the sole owner.
        self.host.stamp_watermark_if_empty()

        libraries = list(self.sync["Libraries"])
        failures: List[Exception] = []

        self.process_libraries(libraries, failures)
        self._save(self.sync)

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
            self.host.refresh_libraries(databases, force_reload=True)

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

            with self.host.music_database_lock:
                music_playlists.refresh_with_databases(self.server)
            self.host.defer_playlist_poll()
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

            self._save(self.sync)

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

        self.host.refresh_libraries(databases)

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
        """The position to resume this walk at, when it still means
        something (restorepoints.resume_at)."""
        return restorepoints.resume_at(self.sync["RestorePoints"], key, fingerprint)

    def set_restore_point(self, key, restore_point):
        restorepoints.save(
            self.sync["RestorePoints"], key, restore_point, self._restore_fingerprint
        )

    def clear_restore_point(self, key):
        restorepoints.clear(self.sync["RestorePoints"], key)

    def clear_library_restore_points(self, library_id):
        """Drop every restore point belonging to a library
        (restorepoints.clear_library)."""
        restorepoints.clear_library(self.sync["RestorePoints"], library_id)

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
                self._save(self.sync)
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
            self._save(self.sync)

            raise

        except Exception as error:
            self._notify_sync_failure(library_id)

            LOG.error("full sync exited unexpectedly")
            LOG.exception(error)

            self._save(self.sync)

            raise

    def _notify_sync_failure(self, library_id):
        """One failure toast per library per service lifetime.

        A failing library retries on the resume backoff (60s doubling to 30
        minutes, reset each boot); toasting every attempt turns one dead
        library into a nag loop (healing-loops-plan F3). The log still
        carries every attempt.
        """
        toasted = self.host.failure_toasted if self.host is not None else None

        if toasted is None or library_id not in toasted:
            if toasted is not None:
                toasted.add(library_id)

            notification(localized(30406), error=True)

    @contextmanager
    def video_database_locks(self):
        with self.host.database_lock:
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
                with self.host.database_lock:
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
                    applied, value = self.apply_or_skip(apply, obj, item, item_type)

                    if applied:
                        results.append((item, value))
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
                    self.server, jellyfindb, videodb, library, hooks=self.hooks
                ),
                lambda obj, movie: obj.movie(movie),
                lambda movie: movie["Name"],
                dialog,
                self._heading(library),
                page,
            )

        self.clear_restore_point(restore_key)

    def apply_or_skip(self, apply, obj, item, item_type):
        """Write one item: ``(True, what apply returned)`` on success,
        ``(False, None)`` when it could not be applied.

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
            return True, apply(obj, item)
        except LibraryOrphanException as error:
            LOG.warning(
                "%s %s could not be applied: %s", item_type, item.get("Id"), error
            )
            return False, None
        except HttpError as error:
            if error.status != 404 and not self._gone(item):
                raise
            LOG.warning(
                "%s %s is gone from the server; skipped",
                item_type,
                item.get("Id"),
            )
            return False, None

    def _gone(self, item):
        """Whether the item a child fetch just failed for no longer exists.

        The status a child fetch answers with is the endpoint's, not the
        item's: a show's ``/Seasons`` 404s on a deleted show, but
        ``/Items?ParentId=<deleted set>`` is a **400** on Jellyfin 12 (live,
        S-P1.3c), and keying the skip on 404 alone let that one abort the
        boxsets pass exactly as before. So on any other HTTP status the item
        itself is asked for once -- one request, only on the failure path --
        and a 404 there is the answer that means "gone". Anything else, the
        original error stands: a malformed query must still stop the pass.
        """
        try:
            self.server.item(item.get("Id"))
        except HttpError as probe:
            return probe.status == 404
        except Exception:
            return False
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
                        self.server,
                        jellyfindb,
                        videodb,
                        library,
                        True,
                        hooks=self.hooks,
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
                    self.server, jellyfindb, videodb, library, hooks=self.hooks
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
        with self.host.music_database_lock:
            # kofin.db outermost for the same commit-order reason as
            # video_database_locks.
            with Database("kofin") as jellyfindb:
                with Database("music") as musicdb:
                    obj = Music(
                        self.server, jellyfindb, musicdb, library, hooks=self.hooks
                    )

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
        """Update-mode pass: plan the diff and hand the work to the
        incremental pipeline (prune.plan)."""
        prune.plan(self.server, self.host, library, library_id, dialog)

    @progress(30407)
    def boxsets(self, library, dialog=None):
        """Process all boxsets (boxsets.walk)."""
        boxsets.walk(self, library, dialog)

    def sweep_stale_boxsets(self, walked):
        return boxsets.sweep_stale(self, walked)

    def refresh_boxsets(self, library):
        boxsets.refresh(self, library)

    @progress(30408)
    def remove_library(self, library_id, dialog):
        """Remove a library's rows from Kodi's database and drop it from the
        whitelist (removal.remove_library)."""
        self.sync = self._load()
        removal.remove_library(self.host, self.server, self.sync, library_id, dialog)
        self._save(self.sync)

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exiting sync"""
        self.release()
        state.set_sync_active(False)

        LOG.info("--<[ fullsync ]")
