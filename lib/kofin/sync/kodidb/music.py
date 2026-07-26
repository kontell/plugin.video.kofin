# -*- coding: utf-8 -*-

##################################################################################################

from kofin.core.log import Logger

from kofin.sync.kodidb import queries_music as QU
from kofin.sync.kodidb.kodi import Kodi

##################################################################################################

LOG = Logger(__name__)

# Kodi's blank artist, written by MusicDatabase::CreateTables and reserved by
# id (BLANKARTIST_ID/BLANKARTIST_NAME, xbmc/music/Artist.h). Untagged tracks
# hang off it, and Kodi renders whatever occupies the id as "[Missing]".
BLANKARTIST_ID = 1
BLANKARTIST_NAME = "[Missing Tag]"
BLANKARTIST_MBID = "Artist Tag Missing"

##################################################################################################


class Music(Kodi):

    def __init__(self, cursor):

        self.cursor = cursor
        self.version_id = self.get_version()
        self.update_versiontagscan()
        Kodi.__init__(self)

    def create_entry(self):
        """Krypton has a dummy first entry
        idArtist: 1  strArtist: [Missing Tag]  strMusicBrainzArtistID: Artist Tag Missing
        """
        self.cursor.execute(QU.create_artist)

        return self.cursor.fetchone()[0] + 1

    def create_entry_album(self):
        self.cursor.execute(QU.create_album)

        return self.cursor.fetchone()[0] + 1

    def create_entry_song(self):
        self.cursor.execute(QU.create_song)

        return self.cursor.fetchone()[0] + 1

    def create_entry_genre(self):
        self.cursor.execute(QU.create_genre)

        return self.cursor.fetchone()[0] + 1

    def update_path(self, *args):
        self.cursor.execute(QU.update_path, args)

    def add_role(self, *args):
        self.cursor.execute(QU.update_role, args)

    def get(self, artist_id, name, musicbrainz):
        """Get artist or create the entry."""
        try:
            self.cursor.execute(QU.get_artist, (musicbrainz,))
            result = self.cursor.fetchone()
            artist_id_res = result[0]
            artist_name = result[1]
        except TypeError:
            artist_id_res = self.add_artist(artist_id, name, musicbrainz)
        else:
            if artist_name != name:
                # Args in the order the statement takes them (the spec beside
                # it says so: update_artist_name_obj = [Name, ArtistId]). They
                # were reversed, and against an INTEGER idArtist the name never
                # matched anything, so this changed 0 rows — a rename has
                # silently never applied. The id is the row we just found, not
                # the caller's, which is None on this path.
                self.update_artist_name(name, artist_id_res)

        return artist_id_res

    def add_artist(self, artist_id, name, *args):
        """Safety check, when musicbrainz does not exist"""
        try:
            self.cursor.execute(QU.get_artist_by_name, (name,))
            artist_id_res = self.cursor.fetchone()[0]
        except TypeError:
            artist_id_res = artist_id or self.create_entry()
            # Insert the id we just worked out, not the one we were handed.
            # ``artist_id`` is None for a new artist, and idArtist is an
            # INTEGER PRIMARY KEY, so a NULL let sqlite pick the rowid: on an
            # artist table with no rows that is 1, which Kodi reserves for the
            # [Missing Tag] blank artist (BLANKARTIST_*, xbmc/music/Artist.h).
            # The real artist then rendered as [Missing] everywhere, and it was
            # recorded under create_entry()'s answer (2) rather than the 1 it
            # actually landed on, so kofin.db pointed at somebody else's row.
            self.cursor.execute(
                QU.add_artist,
                (
                    artist_id_res,
                    name,
                )
                + args,
            )

        return artist_id_res

    def update_artist_name(self, *args):
        self.cursor.execute(QU.update_artist_name, args)

    def update(self, *args):
        if self.version_id < 74:
            self.cursor.execute(QU.update_artist74, args)
        else:
            # No field for backdrops in Kodi 19, so we need to omit that here
            args = args[:3] + args[4:]
            self.cursor.execute(QU.update_artist82, args)

    def link(self, *args):
        self.cursor.execute(QU.update_link, args)

    def add_discography(self, *args):
        self.cursor.execute(QU.update_discography, args)

    def validate_artist(self, *args):

        try:
            self.cursor.execute(QU.get_artist_by_id, args)

            return self.cursor.fetchone()[0]
        except TypeError:
            return

    def validate_album(self, *args):

        try:
            self.cursor.execute(QU.get_album_by_id, args)

            return self.cursor.fetchone()[0]
        except TypeError:
            return

    def validate_song(self, *args):

        try:
            self.cursor.execute(QU.get_song_by_id, args)

            return self.cursor.fetchone()[0]
        except TypeError:
            return

    def get_album(self, album_id, name, musicbrainz, artists=None, *args):

        try:
            if musicbrainz is not None:
                self.cursor.execute(QU.get_album, (musicbrainz,))
                album = None
            else:
                if self.version_id < 72:
                    self.cursor.execute(QU.get_album_by_name, (name,))
                else:
                    self.cursor.execute(QU.get_album_by_name72, (name,))
                album = self.cursor.fetchone()

                if album[1] and album[1].split(" / ")[0] not in artists.split(" / "):
                    LOG.info("Album found, but artist doesn't match?")
                    LOG.info("Album [ %s/%s ] %s", name, album[1], artists)

                    raise TypeError

            album_id = (album or self.cursor.fetchone())[0]
        except TypeError:
            album_id = self.add_album(
                *(
                    album_id,
                    name,
                    musicbrainz,
                )
                + args
            )

        return album_id

    def add_album(self, album_id, *args):

        album_id = album_id or self.create_entry_album()
        if self.version_id < 72:
            self.cursor.execute(QU.add_album, (album_id,) + args)
        elif self.version_id < 82:
            self.cursor.execute(QU.add_album72, (album_id,) + args)
        else:
            self.cursor.execute(QU.add_album82, (album_id,) + args)
        return album_id

    def update_album(self, *args):
        if self.version_id < 72:
            self.cursor.execute(QU.update_album, args)
        elif self.version_id < 74:
            self.cursor.execute(QU.update_album72, args)
        else:
            self.cursor.execute(QU.update_album74, args)

    def update_album_duration(self, *args):
        # iAlbumDuration column was added to the album table in music db schema 80
        if self.version_id >= 80:
            self.cursor.execute(QU.update_album_duration, args)

    def get_album_artist(self, album_id, artists):

        try:
            if self.version_id < 72:
                self.cursor.execute(QU.get_album_artist, (album_id,))
            else:
                self.cursor.execute(QU.get_album_artist72, (album_id,))
            curr_artists = self.cursor.fetchone()[0]
        except TypeError:
            return

        if curr_artists != artists:
            self.update_album_artist(artists, album_id)

    def update_album_artist(self, *args):
        if self.version_id < 72:
            self.cursor.execute(QU.update_album_artist, args)
        else:
            self.cursor.execute(QU.update_album_artist72, args)

    def add_single(self, *args):
        if self.version_id < 74:
            self.cursor.execute(QU.add_single, args)
        else:
            self.cursor.execute(QU.add_single74, args)

    def add_song(self, *args):
        if self.version_id < 72:
            self.cursor.execute(QU.add_song, args)
        elif self.version_id < 74:
            self.cursor.execute(QU.add_song72, args)
        else:
            self.cursor.execute(QU.add_song74, args)

    def update_song(self, *args):
        if self.version_id < 72:
            self.cursor.execute(QU.update_song, args)
        elif self.version_id < 74:
            self.cursor.execute(QU.update_song72, args)
        else:
            self.cursor.execute(QU.update_song74, args)

    def link_song_artist(self, *args):
        self.cursor.execute(QU.update_song_artist, args)

    def link_song_album(self, *args):
        if self.version_id < 72:
            self.cursor.execute(QU.update_song_album, args)

    def rate_song(self, *args):
        self.cursor.execute(QU.update_song_rating, args)

    def add_genres(self, kodi_id, genres, media):
        """Add genres, but delete current genres first.
        Album_genres was removed in kodi 18
        """
        if media == "album" and self.version_id < 72:
            self.cursor.execute(QU.delete_genres_album, (kodi_id,))

            for genre in genres:

                genre_id = self.get_genre(genre)
                self.cursor.execute(QU.update_genre_album, (genre_id, kodi_id))

        if media == "song":
            self.cursor.execute(QU.delete_genres_song, (kodi_id,))

            for genre in genres:

                genre_id = self.get_genre(genre)
                self.cursor.execute(QU.update_genre_song, (genre_id, kodi_id))

    def get_genre(self, *args):

        try:
            self.cursor.execute(QU.get_genre, args)

            return self.cursor.fetchone()[0]
        except TypeError:
            return self.add_genre(*args)

    def add_genre(self, *args):

        genre_id = self.create_entry_genre()
        self.cursor.execute(QU.add_genre, (genre_id,) + args)

        return genre_id

    def delete(self, *args):
        if args and args[0] == BLANKARTIST_ID:
            # Kodi's own [Missing Tag] row. It is never ours to remove, and a
            # reference pointing at it means something was mis-recorded
            # earlier rather than that the row should go.
            LOG.warning("refusing to delete Kodi's blank artist row")
            return

        self.cursor.execute(QU.delete_artist, args)

    def ensure_blank_artist(self):
        """Put Kodi's [Missing Tag] artist back when the slot is empty.

        Kodi writes this row when it creates MyMusic and reserves the id for
        it; it is what untagged tracks hang off. Returns True when it inserted
        something.

        Three states, and only one of them is repairable here:

        * absent — restore it, which is the whole point;
        * correct — nothing to do;
        * occupied by a real artist — the damage the NULL-id insert used to
          cause. Warned about, deliberately not fixed. Evicting the occupant
          means moving it to a free id and rewriting every album_artist,
          song_artist and discography row that points at it, plus kofin.db's
          mapping in a separate database — surgery across two files that has
          no business running unattended at startup.
        """
        self.cursor.execute(QU.get_artist_by_id, (BLANKARTIST_ID,))
        row = self.cursor.fetchone()

        if row is None:
            LOG.warning("Kodi's blank artist row is missing; restoring it")
            self.cursor.execute(
                QU.add_blank_artist,
                (BLANKARTIST_ID, BLANKARTIST_NAME, BLANKARTIST_NAME, BLANKARTIST_MBID),
            )
            return True

        self.cursor.execute(QU.get_artist_name_by_id, (BLANKARTIST_ID,))
        name = (self.cursor.fetchone() or [None])[0]

        if name != BLANKARTIST_NAME:
            LOG.warning(
                "artist %r occupies Kodi's blank-artist id %s; it will render as "
                "[Missing] until the row is relocated",
                name,
                BLANKARTIST_ID,
            )

        return False

    def delete_album(self, *args):
        self.cursor.execute(QU.delete_album, args)

    def delete_song(self, *args):
        """Delete a song and, with it, the path row it leaves behind.

        Kodi's music schema has no cascade here and the fork never cleaned up,
        so every removal abandoned the song's ``path`` row: a repair of a
        21k-song library left 21k orphans and doubled the table, since the
        re-add creates fresh rows. Deliberately conditional — the row goes only
        when no other song and no Kodi music source still points at it, so a
        shared path (or a real scanned source that happens to collide) is left
        alone. The song is deleted first so its own reference does not count.
        """
        self.cursor.execute(QU.get_song_path_id, args)
        row = self.cursor.fetchone()

        self.cursor.execute(QU.delete_song, args)

        if row is not None:
            path_id = row[0]
            self.cursor.execute(QU.delete_path_if_unused, (path_id,) * 3)

    def prune_orphan_paths(self):
        """Drop path rows abandoned before ``delete_song`` learned to clean up.

        That fix stops the bleeding; this heals databases already affected,
        where the count is one dead row per song per repair ever run.

        Deliberately narrow. An orphaned path row is perfectly legitimate for
        Kodi — its scanner keeps folder rows that hold no songs — so only rows
        that nothing references *and* whose path is a shape kofin writes itself
        are ours to remove.
        """
        self.cursor.execute(QU.prune_orphan_paths)

        return self.cursor.rowcount

    def get_version(self):
        self.cursor.execute(QU.get_version)

        return self.cursor.fetchone()[0]

    # current bug in Kodi 18 that will ask for a scan of music tags unless this is set without a lastscanned
    def update_versiontagscan(self):
        if self.version_id < 72:
            return
        else:
            self.cursor.execute(QU.get_versiontagcount)
            if self.cursor.fetchone()[0] == 0:
                self.cursor.execute(QU.update_versiontag, (self.version_id,))
