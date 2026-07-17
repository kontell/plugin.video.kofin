# -*- coding: utf-8 -*-

##################################################################################################

from kofin.core.log import Logger

from kofin.sync.kodidb import queries as QU
from kofin.sync.kodidb.kodi import Kodi

##################################################################################################

LOG = Logger(__name__)

##################################################################################################


class MusicVideos(Kodi):

    def __init__(self, cursor):

        self.cursor = cursor
        Kodi.__init__(self)

    def create_entry(self):
        self.cursor.execute(QU.create_musicvideo)

        return self.cursor.fetchone()[0] + 1

    def get(self, *args):

        try:
            self.cursor.execute(QU.get_musicvideo, args)

            return self.cursor.fetchone()[0]
        except TypeError:
            return

    def add(self, *args):
        self.cursor.execute(QU.add_musicvideo, args)

    def update(self, *args):
        self.cursor.execute(QU.update_musicvideo, args)

    def delete(self, kodi_id, file_id):

        self.cursor.execute(QU.delete_musicvideo, (kodi_id,))
        self.cursor.execute(QU.delete_file, (file_id,))
