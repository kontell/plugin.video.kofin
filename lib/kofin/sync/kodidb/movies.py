# -*- coding: utf-8 -*-
"""Movie/boxset rows in MyVideos (fork ``objects/kodi/movies.py``,
verbatim port)."""

from sqlite3 import DatabaseError

from kofin.core.log import Logger

from kofin.sync import schema
from kofin.sync.kodidb.kodi import Kodi
from kofin.sync.kodidb import queries as QU

LOG = Logger(__name__)


class Movies(Kodi):

    itemtype: int

    def __init__(self, cursor):

        self.cursor = cursor
        Kodi.__init__(self)
        try:
            self.cursor.execute(QU.get_videoversion_itemtype, [40400])
            self.itemtype = self.cursor.fetchone()[0]
        except (IndexError, DatabaseError, TypeError) as e:
            LOG.warning("Unable to fetch videoversion itemtype: %s", e)
            self.itemtype = 0
        # The EXTRA constant is schema-version keyed (renumbered on Piers);
        # None disables the extras pass without touching the movie sync.
        try:
            self.cursor.execute(QU.get_version)
            self.extra_itemtype = schema.EXTRA_ITEM_TYPE.get(self.cursor.fetchone()[0])
        except (IndexError, DatabaseError, TypeError) as e:
            LOG.warning("Unable to fetch video schema version: %s", e)
            self.extra_itemtype = None

    def create_entry_unique_id(self):
        self.cursor.execute(QU.create_unique_id)

        return self.cursor.fetchone()[0] + 1

    def create_entry_rating(self):
        self.cursor.execute(QU.create_rating)

        return self.cursor.fetchone()[0] + 1

    def create_entry(self):
        self.cursor.execute(QU.create_movie)

        return self.cursor.fetchone()[0] + 1

    def get(self, *args):

        try:
            self.cursor.execute(QU.get_movie, args)
            return self.cursor.fetchone()[0]
        except TypeError:
            return

    def add(self, *args):
        self.cursor.execute(QU.add_movie, args)

    def add_videoversion(self, *args):
        self.cursor.execute(QU.check_video_version)
        if self.cursor.fetchone()[0] == 1:
            self.cursor.execute(QU.add_video_version, args)

    def set_video_version_type(self, file_id, type_id):
        """Rewrite the primary (or any) version row's idType."""
        self.cursor.execute(QU.check_video_version)
        if self.cursor.fetchone()[0] == 1:
            self.cursor.execute(QU.update_video_version_type, (type_id, file_id))

    def get_extra_assets(self, movie_id, item_type):
        """Existing asset rows for a movie: [(idFile, strFilename, idType)].

        ``item_type`` is VERSION or EXTRA (schema-keyed); used for both.
        """
        self.cursor.execute(QU.get_extra_assets, (movie_id, item_type))
        return self.cursor.fetchall()

    def get_extra_type_id(self, name, item_type):
        """Find-or-create the named videoversiontype row (owner USER, like
        Kodi's own convert-to-extra flow). Matches builtins first via name
        + itemType (e.g. Director's Cut at 40407)."""
        self.cursor.execute(QU.get_videoversiontype_by_name, (name, item_type))
        row = self.cursor.fetchone()
        if row:
            return row[0]
        self.cursor.execute(
            QU.add_videoversiontype,
            (name, schema.VIDEO_ASSET_OWNER_USER, item_type),
        )
        return self.cursor.lastrowid

    def resolve_version_type(self, name):
        """Map ``MediaSource.Name`` to a VERSION ``videoversiontype`` id.

        Empty / ``Standard Edition`` → the seeded Standard Edition (40400).
        Otherwise find-or-create under VERSION itemType (builtins first).
        """
        name = (name or "").strip()
        if not name or name.lower() == "standard edition":
            return 40400
        return self.get_extra_type_id(name, self.itemtype)

    def add_extra_asset(
        self, path_id, filename, date_added, movie_id, item_type, type_id
    ):
        """One files row + one videoversion row (extra or alternate version)."""
        file_id = self.add_file(path_id, filename)
        self.update_file(path_id, filename, date_added, file_id)
        self.cursor.execute(
            QU.add_extra_version, (file_id, movie_id, item_type, type_id)
        )
        return file_id

    def delete_extra_asset(self, file_id):
        """Drop the files row; the delete_file trigger cascades the
        videoversion, bookmark, settings, streamdetails and art rows."""
        self.cursor.execute(QU.delete_extra_file, (file_id,))

    def update(self, *args):
        self.cursor.execute(QU.update_movie, args)

    def delete(self, kodi_id, file_id):

        self.cursor.execute(QU.delete_movie, (kodi_id,))
        self.cursor.execute(QU.delete_file, (file_id,))
        self.cursor.execute(QU.check_video_version)
        if self.cursor.fetchone()[0] == 1:
            self.cursor.execute(QU.delete_video_version, (file_id,))

    def get_rating_id(self, *args):

        try:
            self.cursor.execute(QU.get_rating, args)

            return self.cursor.fetchone()[0]
        except TypeError:
            return None

    def add_ratings(self, *args):
        """Add ratings, rating type and votes."""
        self.cursor.execute(QU.add_rating, args)

    def update_ratings(self, *args):
        """Update rating by rating_id."""
        self.cursor.execute(QU.update_rating, args)

    def get_unique_id(self, *args):

        try:
            self.cursor.execute(QU.get_unique_id, args)

            return self.cursor.fetchone()[0]
        except TypeError:
            return

    def add_unique_id(self, *args):
        """Add the provider id, imdb, tvdb."""
        self.cursor.execute(QU.add_unique_id, args)

    def update_unique_id(self, *args):
        """Update the provider id, imdb, tvdb."""
        self.cursor.execute(QU.update_unique_id, args)

    def add_countries(self, countries, *args):

        for country in countries:
            self.cursor.execute(QU.update_country, (self.get_country(country),) + args)

    def add_country(self, *args):
        self.cursor.execute(QU.add_country, args)
        return self.cursor.lastrowid

    def get_country(self, *args):

        try:
            self.cursor.execute(QU.get_country, args)

            return self.cursor.fetchone()[0]
        except TypeError:
            return self.add_country(*args)

    def add_boxset(self, *args):
        self.cursor.execute(QU.add_set, args)
        return self.cursor.lastrowid

    def update_boxset(self, *args):
        self.cursor.execute(QU.update_set, args)

    def set_boxset(self, *args):
        self.cursor.execute(QU.update_movie_set, args)

    def remove_from_boxset(self, *args):
        self.cursor.execute(QU.delete_movie_set, args)

    def delete_boxset(self, *args):
        self.cursor.execute(QU.delete_set, args)

    def migrations(self):
        """
        Used to trigger required database migrations for new versions
        """
        self.cursor.execute(QU.get_version)
        version_id = self.cursor.fetchone()[0]
        changes = False

        # Will run every time Kodi starts, but will be fast enough on
        # subsequent runs to not be a meaningful delay
        if version_id >= 131:
            changes = self.omega_migration()

        # Deliberately not folded into `changes`: this one moves a path row and
        # nothing on screen depends on it, so it must not drag a library update
        # and a skin reload along behind it.
        self.root_content_migration()

        return changes

    def root_content_migration(self):
        """
        Move the tvshows content/scraper off the addon root onto each synced
        library's own path (kofin deviation, see writers/tvshows.py).

        New syncs write the moved shape, but an install that adds no further
        shows would keep the old one -- and with it a video info dialog that
        silently refuses to open anywhere in kofin's own listings. Two narrow
        UPDATEs, guarded so an already-migrated install does nothing and says
        nothing.
        """
        self.cursor.execute(QU.get_root_tvshow_content)
        if not self.cursor.fetchone():
            return False

        LOG.info("Moving the tvshows path content off the addon root")
        self.cursor.execute(QU.set_library_tvshow_content)
        self.cursor.execute(QU.clear_root_tvshow_content)
        return True

    def omega_migration(self):
        """
        Adds a video version for all existing movies

        For Omega: video_version_id = 0
        For Piers: video_version_id = 1

        Migration from Nexus to Omega adds video version with id 0
        Migration from Nexus to Peirs adds video version with id 1
        Migration from Omega to Piers this does nothing and is handled by kodi itself
        """
        LOG.info("Starting migration for Omega database changes")
        # Tracks if this migration made any changes
        changes = False
        self.cursor.execute(QU.get_missing_versions)

        # Sets all existing movies without a version to standard version
        for entry in self.cursor.fetchall():
            self.add_videoversion(entry[0], entry[1], "movie", self.itemtype, 40400)
            changes = True

        LOG.info("Omega database migration is complete")
        return changes
