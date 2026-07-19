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

        libraries = list(self.sync["Libraries"])
        failures = []

        self.process_libraries(libraries, failures)

        if failures:
            raise failures[0]

        elapsed = datetime.datetime.now() - start_time
        self.library.save_last_sync()
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
        """Process movies from a single library."""
        processed_ids = []
        restore_key = "%s/movies" % library["Id"]

        for items in server.get_items(
            self.server,
            library["Id"],
            "Movie",
            False,
            self.get_restore_point(restore_key),
        ):

            with self.video_database_locks() as (videodb, jellyfindb):
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
                    processed_ids.append(movie["Id"])

        with self.video_database_locks() as (videodb, jellyfindb):
            obj = Movies(self.server, jellyfindb, videodb, library)
            obj.item_ids = processed_ids

            if self.update_library:
                self.movies_compare(library, obj, jellyfindb)

        self.clear_restore_point(restore_key)

    def movies_compare(self, library, obj, jellyfinydb):
        """Compare entries from library to what's in the jellyfindb. Remove surplus"""
        db = jellyfin_db.JellyfinDatabase(jellyfinydb.cursor)

        items = db.get_item_by_media_folder(library["Id"])
        current = obj.item_ids

        for x in items:
            if x[0] not in current and x[1] == "Movie":
                obj.remove(x[0])

    @progress()
    def tvshows(self, library, dialog):
        """Process tvshows and episodes from a single library."""
        processed_ids = []
        restore_key = "%s/tvshows" % library["Id"]

        for items in server.get_items(
            self.server,
            library["Id"],
            "Series",
            False,
            self.get_restore_point(restore_key),
        ):

            with self.video_database_locks() as (videodb, jellyfindb):
                obj = TVShows(self.server, jellyfindb, videodb, library, True)

                self.set_restore_point(restore_key, items["RestorePoint"])
                start_index = items["RestorePoint"]["params"]["StartIndex"]

                for index, show in enumerate(items["Items"]):

                    percent = int(
                        (float(start_index + index) / float(items["TotalRecordCount"]))
                        * 100
                    )
                    message = show["Name"]
                    dialog.update(
                        percent,
                        heading="%s: %s" % ("Kofin", library["Name"]),
                        message=message,
                    )

                    if obj.tvshow(show) is not False:

                        for episodes in server.get_episode_by_show(
                            self.server, show["Id"]
                        ):
                            for episode in episodes["Items"]:
                                if episode.get("Path"):
                                    dialog.update(
                                        percent,
                                        message="%s/%s"
                                        % (message, episode["Name"][:10]),
                                    )
                                    obj.episode(episode)
                    processed_ids.append(show["Id"])

        with self.video_database_locks() as (videodb, jellyfindb):
            obj = TVShows(self.server, jellyfindb, videodb, library, True)
            obj.item_ids = processed_ids
            if self.update_library:
                self.tvshows_compare(library, obj, jellyfindb)

        self.clear_restore_point(restore_key)

    def tvshows_compare(self, library, obj, jellyfindb):
        """Compare entries from library to what's in the jellyfindb. Remove surplus"""
        db = jellyfin_db.JellyfinDatabase(jellyfindb.cursor)

        items = db.get_item_by_media_folder(library["Id"])
        for x in list(items):
            items.extend(obj.get_child(x[0]))

        current = obj.item_ids

        for x in items:
            if x[0] not in current and x[1] == "Series":
                obj.remove(x[0])

    @progress()
    def musicvideos(self, library, dialog):
        """Process musicvideos from a single library."""
        processed_ids = []
        restore_key = "%s/musicvideos" % library["Id"]

        for items in server.get_items(
            self.server,
            library["Id"],
            "MusicVideo",
            False,
            self.get_restore_point(restore_key),
        ):

            with self.video_database_locks() as (videodb, jellyfindb):
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
                    processed_ids.append(mvideo["Id"])

        with self.video_database_locks() as (videodb, jellyfindb):
            obj = MusicVideos(self.server, jellyfindb, videodb, library)
            obj.item_ids = processed_ids
            if self.update_library:
                self.musicvideos_compare(library, obj, jellyfindb)

        self.clear_restore_point(restore_key)

    def musicvideos_compare(self, library, obj, jellyfindb):
        """Compare entries from library to what's in the jellyfindb. Remove surplus"""
        db = jellyfin_db.JellyfinDatabase(jellyfindb.cursor)

        items = db.get_item_by_media_folder(library["Id"])
        current = obj.item_ids

        for x in items:
            if x[0] not in current and x[1] == "MusicVideo":
                obj.remove(x[0])

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

                    if self.update_library:
                        self.music_compare(library, obj, jellyfindb)

    def music_compare(self, library, obj, jellyfindb):
        """Compare entries from library to what's in the jellyfindb. Remove surplus"""
        db = jellyfin_db.JellyfinDatabase(jellyfindb.cursor)

        items = db.get_item_by_media_folder(library["Id"])
        for x in list(items):
            items.extend(obj.get_child(x[0]))

        current = obj.item_ids

        for x in items:
            if x[0] not in current and x[1] == "MusicArtist":
                obj.remove(x[0])

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
