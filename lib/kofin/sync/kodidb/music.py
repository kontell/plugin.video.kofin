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

# The `source` row kofin writes per synced Jellyfin music library is keyed on
# this, not on its name: the name is the user-visible thing a node's rule
# matches and the server can change it, while the key has to stay the same
# row. It is also the ownership gate every deletion path here is guarded on —
# the user's own scanned music sources live in this table. Kodi reads
# strMultipath in exactly two places (CheckSources and GetSourceFromPath),
# both equality lookups, so a synthetic value never becomes a path anything
# tries to open.
KOFIN_SOURCE_PREFIX = "plugin://plugin.video.kofin/"

# How many song ids go into one album_source backfill statement.
SOURCE_LINK_CHUNK = 500


def source_key(library_id):
    return "%s%s/" % (KOFIN_SOURCE_PREFIX, library_id)


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
        """Write the artist's row for this album, replacing any it had.

        ``discography`` has no unique index, so the INSERT OR REPLACE this
        used to be never replaced: every rewrite appended, and because the
        album leg writes the album's year while the song leg writes 0, the
        rows were not even identical. A 12-track album left 13 rows after a
        single pass and grew by that much again on every Etag change --
        measured 251 rows for AC/DC's 22 albums on a real library.

        Kodi keeps one row per album by clearing first
        (``CMusicDatabase::UpdateArtist`` calls ``DeleteArtistDiscography``
        then ``AddArtistDiscography`` per album). kofin writes one album at a
        time rather than an artist's whole discography, so the clear is
        scoped to the pair the row is about.
        """
        self.cursor.execute(QU.delete_discography, args[:2])
        self.cursor.execute(QU.update_discography, args)

    def add_discography_if_absent(self, *args):
        """Add a discography row only where the artist has none for the album.

        The song leg calls this. It exists for the single -- a song whose
        album Jellyfin never modelled, so ``song_add`` creates the album and
        the album writer never runs -- but it fires once per track, and it
        knows no album year (it writes 0). Letting it replace would mean the
        last track of every album overwriting the album writer's real year
        with a 0.
        """
        self.cursor.execute(QU.get_discography, args[:2])

        if self.cursor.fetchone() is None:
            self.cursor.execute(QU.update_discography, args)

    def get_album_title(self, kodi_id):
        """The album's own title, or None when it has no row.

        discography is keyed by title, and the two legs used to key on
        different strings: the album leg on the album item's name, the song
        leg on the song's ``Album`` tag. Jellyfin reports those separately
        and they do disagree -- a track tagged ``The Terminator`` on an album
        named ``The Terminator: Original Soundtrack``. Rows written under the
        odd title match no album in ``GetArtistDiscography``'s fold, so they
        survive it and render on their own as ``0 - <album>``.
        """
        self.cursor.execute(QU.get_album_title, (kodi_id,))
        row = self.cursor.fetchone()

        return row[0] if row else None

    def delete_album_discography(self, kodi_id):
        """Drop the discography rows the album being removed accounts for.

        Kodi cascades an album delete into song, album_artist, album_source
        and art (``tgrDeleteAlbum``) but never into discography -- only
        ``tgrDeleteArtist`` clears that, and only when the artist row itself
        goes. So a removed album left its rows behind for good, and they
        stop being invisible the moment the album is gone: the fold in
        ``GetArtistDiscography`` matches discography against the album table,
        so with no album left to match, the album leg's row and the song
        leg's year-0 row render as two entries, one of them titled
        ``0 - <album>``.

        Known limit: the scope is the artists Kodi links to the album, which
        is every artist the song leg wrote a row for (it writes album_artist
        beside it) and the album leg's album artists. An artist that was an
        ArtistItem without ever being an album artist -- a guest credit on a
        compilation -- keeps its row. That row is the album leg's, carries
        the real year, and reads as the appearance it describes.
        """
        self.cursor.execute(QU.delete_album_discography, (kodi_id, kodi_id))

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

    def get_song_playlist_row(self, song_id):
        """Everything a playlist line needs about a song, or None.

        ``(strPath, strFileName, strTitle, strArtistDisp, iTrack, iDuration)``
        — the path Kodi rebuilds playback from, plus the fields that make an
        ``#EXTINF`` line self-describing (see ``sync/playlists.py``).
        """
        self.cursor.execute(QU.get_song_playlist_row, (song_id,))
        return self.cursor.fetchone()

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

    def unlink_blank_artist(self, song_id):
        """Drop a song's [Missing Tag] credit once it has a real one: the
        blank row is the writer's last-resort visibility guarantee, and a
        song that regained its artists must not keep showing under the
        placeholder too."""
        self.cursor.execute(QU.delete_blank_song_artist, (song_id, BLANKARTIST_ID))

    def get_songs_by_artist(self, artist_id):
        """Kodi ids of every song credited to the artist (role 1), wherever
        its album lives — compilation appearances included."""
        self.cursor.execute(QU.get_songs_by_artist, (artist_id,))

        return [row[0] for row in self.cursor.fetchall()]

    def album_has_album_artist(self, album_id, artist_id):
        self.cursor.execute(QU.get_album_artist_link, (album_id, artist_id))

        return self.cursor.fetchone() is not None

    def recredit_songs(self, song_ids):
        """Give songs that just lost their only role-1 artist link the
        [Missing Tag] credit, so they stay visible.

        Kodi reaches songs through song_artist (its song listings inner-join
        songartistview), so a song with no row there is not artist-less, it
        is invisible. The blank artist rather than something plausible like
        the album artist because the substitute must also *leave*: the
        writer's rewrite drops a blank credit the moment a real one lands
        (unlink_blank_artist), while any other borrowed credit would linger
        forever. Returns the kodi ids actually re-credited.
        """
        healed = []

        for song_id in song_ids:
            self.cursor.execute(QU.get_song_exists, (song_id,))
            if self.cursor.fetchone() is None:
                continue

            self.cursor.execute(QU.get_song_role1_artist, (song_id,))
            if self.cursor.fetchone() is not None:
                continue

            self.cursor.execute(
                QU.update_song_artist,
                (BLANKARTIST_ID, song_id, 1, 0, BLANKARTIST_NAME),
            )
            healed.append(song_id)

        return healed

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

    # -- the per-library source rows -------------------------------------------

    def create_entry_source(self):
        self.cursor.execute(QU.create_source)

        return self.cursor.fetchone()[0] + 1

    def get_source(self, library_id):
        self.cursor.execute(QU.get_source, (source_key(library_id),))

        return self.cursor.fetchone()

    def ensure_source(self, library_id, name):
        """The library's MyMusic source row, created or renamed to match.

        Found by ``strMultipath`` rather than by name, because the name is the
        only thing a node's rule matches on and the server may change it at
        any time: keyed on the name, a rename writes a *second* source and
        leaves every album linked to the old one, so the renamed node matches
        nothing at all. Keyed on the library id the row and its links survive
        the rename untouched.
        """
        found = self.get_source(library_id)

        if found is None:
            source_id = self.create_entry_source()
            self.cursor.execute(
                QU.add_source, (source_id, name, source_key(library_id))
            )

            return source_id

        source_id, current = found

        if current != name:
            self.cursor.execute(QU.update_source_name, (name, source_id))

        return source_id

    def link_album_source(self, album_id, source_id):
        """Link the album to its library's source, and unlink it from any
        other kofin-owned one.

        The second half is what makes a library move stick: an album that
        moved between libraries on the server comes back on the *same*
        idAlbum (the MBID match in get_album), so without it the album sits
        in both libraries' nodes for good. Scoped to kofin's own sources, so
        an album the user also has scanned locally keeps its real link. The
        insert goes first — a failure between the two leaves the album in two
        places, which is visible and self-heals, rather than in none.
        """
        self.cursor.execute(QU.add_album_source, (source_id, album_id))
        self.cursor.execute(QU.delete_album_other_sources, (album_id, source_id))

    def link_song_albums_source(self, song_ids, source_id):
        """Link the albums *behind* a set of songs — the singles path.

        A single's album is created by the writer on the fly and has no
        kofin.db reference of its own, so walking the album mappings misses
        it and every single would drop out of a source-scoped node. Chunked,
        because this runs over a whole library's songs.
        """
        song_ids = [song_id for song_id in song_ids if song_id is not None]

        for start in range(0, len(song_ids), SOURCE_LINK_CHUNK):
            chunk = song_ids[start : start + SOURCE_LINK_CHUNK]
            self.cursor.execute(
                QU.add_album_source_by_songs % ",".join("?" * len(chunk)),
                [source_id] + list(chunk),
            )

    def kofin_sources(self):
        """(id, name, multipath) for every source kofin owns — the prefix is
        the ownership gate, because the user's own scanned sources share this
        table and are never ours to touch."""
        self.cursor.execute(QU.get_kofin_sources)

        return self.cursor.fetchall()

    def delete_source_for(self, library_id):
        """Drop a library's source row; True when there was one.

        The row is the whole cleanup: tgrDeleteSource takes its album_source
        links with it, and tgrDeleteAlbum has already taken the links of every
        album deleted alongside.
        """
        found = self.get_source(library_id)

        if found is None:
            return False

        self.cursor.execute(QU.delete_source, (found[0],))

        return True

    def prune_sources(self, keep_library_ids):
        """Remove kofin sources for libraries that are no longer synced.

        The database-side twin of views.prune_nodes: a library can leave the
        whitelist by a route that never ran remove_library (a server-side
        deletion, a settings diff, an install that predates this), and the
        whitelist is the whole truth about what should exist.
        """
        keep = {source_key(library_id) for library_id in keep_library_ids}
        removed = 0

        for source_id, _name, multipath in self.kofin_sources():
            if multipath in keep:
                continue

            self.cursor.execute(QU.delete_source, (source_id,))
            removed += 1

        return removed

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
