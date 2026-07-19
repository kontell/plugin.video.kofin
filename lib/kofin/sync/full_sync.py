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

import xbmc

from kofin.core import settings, state
from kofin.core.http import HttpError
from kofin.core.log import Logger
from kofin.sync import downloader as server
from kofin.sync.writers import Movies, TVShows, MusicVideos, Music
from kofin.sync.db import Database, get_sync, save_sync
from kofin.sync import kofindb as jellyfin_db
from kofin.sync.shims import (
    LibraryException,
    LibraryExitException,
    get_screensaver,
    localized,
    notification,
    progress,
    set_screensaver,
)

LOG = Logger(__name__)

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


class FullSync(object):
    """This should be called like a context.
    i.e. with FullSync(library, server) as sync:
        sync.libraries()
    """

    # Borg - multiple instances, shared state
    _shared_state: dict = {}
    sync = None
    running = False
    screensaver = None
    update_library = False

    def __init__(self, library, server):
        """You can call all big syncing methods here.
        Initial, update, repair, remove.
        """
        self.__dict__ = self._shared_state

        if self.running:
            notification(localized(30410), error=True)

            raise Exception("Sync is already running.")

        self.library = library
        self.server = server

    def __enter__(self):
        """Do everything we need before the sync"""
        LOG.info("-->[ fullsync ]")

        if not settings.get_bool("dbSyncScreensaver"):

            xbmc.executebuiltin("InhibitIdleShutdown(true)")
            self.screensaver = get_screensaver()
            set_screensaver(value="")

        self.running = True
        state.set_sync_active(True)

        return self

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
        failures = []

        self.process_libraries(libraries, failures)

        if failures:
            raise failures[0]

        elapsed = datetime.datetime.now() - start_time
        save_sync(self.sync)

        # Refresh the databases this sync actually wrote. Refreshing only video
        # left a freshly synced music library invisible in the music widgets.
        synced_video, synced_music = split_libraries(libraries, self._media_type)
        databases = set()

        if synced_video:
            databases.add("video")

        if synced_music:
            databases.add("music")

        self.library.refresh_libraries(databases)
        notification(
            "%s %s" % (localized(30409), str(elapsed).split(".")[0]),
        )
        LOG.info("Full sync completed in: %s", str(elapsed).split(".")[0])

    def process_libraries(self, libraries, failures):
        """Process libraries in order, recording completion after each.

        Failures are collected for the caller to re-raise, so one bad library
        does not abandon the rest.
        """
        try:
            for library in libraries:

                synced = self.process_library(library)

                if (
                    synced
                    and not library.startswith("Boxsets:")
                    and library not in self.sync["Whitelist"]
                ):
                    self.sync["Whitelist"].append(library)

                if library in self.sync["Libraries"]:
                    self.sync["Libraries"].remove(library)

                save_sync(self.sync)
        except Exception as error:
            failures.append(error)

    def _media_type(self, library_id):
        view = self.get_library(library_id)

        return view.media_type if view else None

    def get_restore_point(self, key):
        return self.sync["RestorePoints"].get(key, {}).get("params")

    def set_restore_point(self, key, restore_point):
        self.sync["RestorePoints"][key] = restore_point

    def clear_restore_point(self, key):
        self.sync["RestorePoints"].pop(key, None)

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
                return True

            if library_id.startswith("Mixed:"):
                for mixed in ("movies", "tvshows"):
                    # Each pass keeps its own restore point slot.
                    media[mixed](library)
            else:
                media[library["CollectionType"]](library)
            return True
        except LibraryException as error:
            # TODO: Fixme; We're catching all LibraryException here,
            # but silently ignoring any that isn't the exit condition.
            # Investigate what would be appropriate behavior here.
            if isinstance(error, LibraryExitException):
                save_sync(self.sync)
                raise
            LOG.warning("Ignoring exception %s", error)
            return True

        except Exception as error:
            notification(localized(30406), error=True)

            LOG.error("full sync exited unexpectedly")
            LOG.exception(error)

            save_sync(self.sync)

            raise

    @contextmanager
    def video_database_locks(self):
        with self.library.database_lock:
            with Database() as videodb:
                with Database("kofin") as jellyfindb:
                    yield videodb, jellyfindb

    @progress()
    def movies(self, library, dialog):
        """Process movies from a single library.

        Connections are held across the pass (phase 5, sync-plan Phase 3);
        the writer lock is still taken and the transaction committed per
        page, so realtime writers interleave exactly as before — only the
        per-page open/close churn is gone.
        """
        restore_key = "%s/movies" % library["Id"]

        with Database() as videodb, Database("kofin") as jellyfindb:
            for items in server.get_items(
                self.server,
                library["Id"],
                "Movie",
                False,
                self.get_restore_point(restore_key),
            ):

                with self.library.database_lock:
                    obj = Movies(self.server, jellyfindb, videodb, library)

                    self.set_restore_point(restore_key, items["RestorePoint"])
                    start_index = items["RestorePoint"]["params"]["StartIndex"]

                    for index, movie in enumerate(items["Items"]):

                        dialog.update(
                            int(
                                (
                                    float(start_index + index)
                                    / float(items["TotalRecordCount"])
                                )
                                * 100
                            ),
                            heading="%s: %s" % ("Kofin", library["Name"]),
                            message=movie["Name"],
                        )
                        obj.movie(movie)

                    videodb.conn.commit()
                    jellyfindb.conn.commit()

        self.clear_restore_point(restore_key)

    @progress()
    def tvshows(self, library, dialog):
        """Process tvshows, seasons and episodes from a single library.

        Three per-library passes (phase 5, sync-plan P5) instead of one
        episode request per show: Series pages, then Season pages, then
        Episode pages — parents land before children by construction, and
        a 500-show library costs pages, not 500+ requests. Restore points
        are per pass and cleared together at the end: an interruption
        resumes inside the pass it happened in, completed passes re-do only
        their final page (writes are idempotent and Etag-short-circuited).
        A pre-phase-5 pending ``{lib}/tvshows`` key simply restarts the
        library's passes; it is cleared alongside.
        """
        heading = "%s: %s" % ("Kofin", library["Name"])

        with Database() as videodb, Database("kofin") as jellyfindb:

            def tvshows_pass(item_type, key_suffix, apply, describe):
                restore_key = "%s/tvshows-%s" % (library["Id"], key_suffix)

                for items in server.get_items(
                    self.server,
                    library["Id"],
                    item_type,
                    False,
                    self.get_restore_point(restore_key),
                ):

                    with self.library.database_lock:
                        obj = TVShows(self.server, jellyfindb, videodb, library, True)

                        self.set_restore_point(restore_key, items["RestorePoint"])
                        start_index = items["RestorePoint"]["params"]["StartIndex"]

                        for index, item in enumerate(items["Items"]):

                            dialog.update(
                                int(
                                    (
                                        float(start_index + index)
                                        / float(items["TotalRecordCount"])
                                    )
                                    * 100
                                ),
                                heading=heading,
                                message=describe(item),
                            )
                            apply(obj, item)

                        videodb.conn.commit()
                        jellyfindb.conn.commit()

            def child_label(item):
                return "%s / %s" % (item.get("SeriesName") or "", item.get("Name"))

            tvshows_pass(
                "Series",
                "series",
                lambda obj, show: obj.tvshow(show),
                lambda show: show["Name"],
            )
            tvshows_pass(
                "Season",
                "seasons",
                lambda obj, season: obj.season(season),
                child_label,
            )
            tvshows_pass(
                "Episode",
                "episodes",
                lambda obj, episode: (
                    obj.episode(episode) if episode.get("Path") else None
                ),
                child_label,
            )

        for key_suffix in ("series", "seasons", "episodes"):
            self.clear_restore_point("%s/tvshows-%s" % (library["Id"], key_suffix))
        # Legacy single-pass key from a pre-phase-5 interrupted sync.
        self.clear_restore_point("%s/tvshows" % library["Id"])

    @progress()
    def musicvideos(self, library, dialog):
        """Process musicvideos from a single library."""
        restore_key = "%s/musicvideos" % library["Id"]

        with Database() as videodb, Database("kofin") as jellyfindb:
            for items in server.get_items(
                self.server,
                library["Id"],
                "MusicVideo",
                False,
                self.get_restore_point(restore_key),
            ):

                with self.library.database_lock:
                    obj = MusicVideos(self.server, jellyfindb, videodb, library)

                    self.set_restore_point(restore_key, items["RestorePoint"])
                    start_index = items["RestorePoint"]["params"]["StartIndex"]

                    for index, mvideo in enumerate(items["Items"]):

                        dialog.update(
                            int(
                                (
                                    float(start_index + index)
                                    / float(items["TotalRecordCount"])
                                )
                                * 100
                            ),
                            heading="%s: %s" % ("Kofin", library["Name"]),
                            message=mvideo["Name"],
                        )
                        obj.musicvideo(mvideo)

                    videodb.conn.commit()
                    jellyfindb.conn.commit()

        self.clear_restore_point(restore_key)

    @progress()
    def music(self, library, dialog):
        """Process artists, album, songs from a single library."""
        with self.library.music_database_lock:
            with Database("music") as musicdb:
                with Database("kofin") as jellyfindb:
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

                    albums = server.get_items(
                        self.server,
                        library_id,
                        item_type="MusicAlbum",
                        params={"SortBy": "AlbumArtist"},
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

                    songs = server.get_items(
                        self.server,
                        library_id,
                        item_type="Audio",
                        params={"SortBy": "AlbumArtist"},
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
        if library_id.startswith("Mixed:"):
            classes = ("movies", "tvshows")
        else:
            classes = (library.get("CollectionType"),)

        missing = []
        changed = []
        stale = []

        for media_class in classes:
            server_types = PRUNE_SERVER_TYPES.get(media_class)

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

            for item_id, (etag, _item_type) in server_map.items():
                if item_id not in local_map:
                    missing.append(item_id)
                    continue

                # No Etag from the server (unexpected with Fields=Etag) →
                # re-fetch: the safe direction is a redundant download.
                if not etag or local_map[item_id] != "%s|plugin" % etag:
                    changed.append(item_id)

            for item_id in local_map:
                if item_id not in server_map:
                    stale.append(item_id)

        LOG.info(
            "--[ prune/%s ] missing:%s changed:%s stale:%s",
            library["Id"],
            len(missing),
            len(changed),
            len(stale),
        )

        self.library.removed(stale)
        self.library.added(missing)
        self.library.updated(changed)

    def _local_reference_map(self, library_id, media_class):
        """{jellyfin_id: stored checksum} for everything kofin.db attributes
        to the library.

        Movies/musicvideos/music rows carry media_folder directly. TV
        children (seasons/episodes) do not — they are collected through the
        kodi-id parent chain plus the jellyfin_parent_id fallback, mirroring
        the writers' get_child walk. Checksums load once per involved
        jellyfin_type via the existing get_checksum query.
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

                    for season in db.get_item_id_by_parent_id(
                        reference.kodi_id, "season"
                    ):
                        ids.append(season[0])

                        for episode in db.get_item_id_by_parent_id(
                            season[1], "episode"
                        ):
                            ids.append(episode[0])

                    # Episodes referencing the series directly (the writers'
                    # get_child fallback arm).
                    for row in db.get_media_by_parent_id(series_id):
                        ids.append(row[0])

        return {item_id: checksums.get(item_id) for item_id in dict.fromkeys(ids)}

    @progress(30407)
    def boxsets(self, library, dialog=None):
        """Process all boxsets."""
        restore_key = "%s/boxsets" % library["Id"]

        for items in server.get_items(
            self.server,
            library["Id"],
            "BoxSet",
            False,
            self.get_restore_point(restore_key),
        ):

            with self.video_database_locks() as (videodb, jellyfindb):
                obj = Movies(self.server, jellyfindb, videodb, library)

                self.set_restore_point(restore_key, items["RestorePoint"])
                start_index = items["RestorePoint"]["params"]["StartIndex"]

                for index, boxset in enumerate(items["Items"]):

                    dialog.update(
                        int(
                            (
                                float(start_index + index)
                                / float(items["TotalRecordCount"])
                            )
                            * 100
                        ),
                        heading="%s: %s" % ("Kofin", localized(30407)),
                        message=boxset["Name"],
                    )
                    obj.boxset(boxset)

        self.clear_restore_point(restore_key)

    def refresh_boxsets(self, library):
        """Delete all existing boxsets and re-add."""
        with self.video_database_locks() as (videodb, jellyfindb):
            obj = Movies(self.server, jellyfindb, videodb, library)
            obj.boxsets_reset()

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

            if items:
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
                                    "AlbumArtist",
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

        self.sync = get_sync()

        if library_id in self.sync["Whitelist"]:
            self.sync["Whitelist"].remove(library_id)

        elif "Mixed:%s" % library_id in self.sync["Whitelist"]:
            self.sync["Whitelist"].remove("Mixed:%s" % library_id)

        save_sync(self.sync)

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exiting sync"""
        self.running = False
        state.set_sync_active(False)

        if not settings.get_bool("dbSyncScreensaver") and self.screensaver is not None:

            xbmc.executebuiltin("InhibitIdleShutdown(false)")
            set_screensaver(value=self.screensaver)

        LOG.info("--<[ fullsync ]")
