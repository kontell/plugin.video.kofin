# -*- coding: utf-8 -*-

##################################################################################################

from sqlite3 import DatabaseError

from kofin.core.log import Logger

from kofin.sync import schema
from kofin.sync.kodidb import queries as QU
from kofin.sync.kodidb.kodi import Kodi

##################################################################################################

LOG = Logger(__name__)

##################################################################################################


class TVShows(Kodi):

    season_has_plot: bool

    def __init__(self, cursor):

        self.cursor = cursor
        Kodi.__init__(self)
        # seasons.plot exists from MyVideos146; Omega's season_view still
        # aliases the show overview. Keyed in schema.py like EXTRA_ITEM_TYPE.
        try:
            self.cursor.execute(QU.get_version)
            self.season_has_plot = schema.SEASON_HAS_PLOT.get(
                self.cursor.fetchone()[0], False
            )
        except (IndexError, DatabaseError, TypeError) as error:
            LOG.warning("Unable to fetch video schema version: %s", error)
            self.season_has_plot = False

    def create_entry_unique_id(self):
        self.cursor.execute(QU.create_unique_id)

        return self.cursor.fetchone()[0] + 1

    def create_entry_rating(self):
        self.cursor.execute(QU.create_rating)

        return self.cursor.fetchone()[0] + 1

    def create_entry(self):
        self.cursor.execute(QU.create_tvshow)

        return self.cursor.fetchone()[0] + 1

    def create_entry_season(self):
        self.cursor.execute(QU.create_season)

        return self.cursor.fetchone()[0] + 1

    def create_entry_episode(self):
        self.cursor.execute(QU.create_episode)

        return self.cursor.fetchone()[0] + 1

    def get(self, *args):

        try:
            self.cursor.execute(QU.get_tvshow, args)

            return self.cursor.fetchone()[0]
        except TypeError:
            return

    def get_episode(self, *args):

        try:
            self.cursor.execute(QU.get_episode, args)

            return self.cursor.fetchone()[0]
        except TypeError:
            return

    def get_rating_id(self, *args):

        try:
            self.cursor.execute(QU.get_rating, args)

            return self.cursor.fetchone()[0]
        except TypeError:
            return

    def add_ratings(self, *args):
        self.cursor.execute(QU.add_rating, args)

    def update_ratings(self, *args):
        self.cursor.execute(QU.update_rating, args)

    def get_total_episodes(self, *args):

        try:
            self.cursor.execute(QU.get_total_episodes, args)

            return self.cursor.fetchone()[0]
        except TypeError:
            return

    def add_unique_id(self, *args):
        self.cursor.execute(QU.add_unique_id, args)

    def update_unique_id(self, *args):
        self.cursor.execute(QU.update_unique_id, args)

    def add(self, *args):
        self.cursor.execute(QU.add_tvshow, args)

    def update(self, *args):
        self.cursor.execute(QU.update_tvshow, args)

    def link(self, *args):
        self.cursor.execute(QU.update_tvshow_link, args)

    def get_season(self, name, *args):

        self.cursor.execute(QU.get_season, args)
        try:
            season_id = self.cursor.fetchone()[0]
        except TypeError:
            season_id = self.add_season(*args)

        if name:
            self.cursor.execute(QU.update_season, (name, season_id))

        return season_id

    def set_season_plot(self, season_id, plot):
        """Write ``seasons.plot`` when the schema has the column.

        Omega has none; its season_view exposes the show overview as plot,
        and writing the column would raise. A missing overview stores NULL
        so Kodi can still fall back to the show text.
        """
        if not self.season_has_plot:
            return
        self.cursor.execute(QU.update_season_plot, (plot, season_id))

    def add_season(self, *args):

        season_id = self.create_entry_season()
        self.cursor.execute(QU.add_season, (season_id,) + args)

        return season_id

    def get_by_unique_id(self, *args):
        self.cursor.execute(QU.get_show_by_unique_id, args)

        return self.cursor.fetchall()

    def add_episode(self, *args):
        self.cursor.execute(QU.add_episode, args)

    def update_episode(self, *args):
        self.cursor.execute(QU.update_episode, args)

    def delete_tvshow(self, *args):
        self.cursor.execute(QU.delete_tvshow, args)

    def delete_season(self, *args):
        self.cursor.execute(QU.delete_season, args)

    def delete_episode(self, kodi_id, file_id):

        self.cursor.execute(QU.delete_episode, (kodi_id,))
        self.cursor.execute(QU.delete_file, (file_id,))
