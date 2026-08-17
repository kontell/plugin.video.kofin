create_artist = """
SELECT      coalesce(max(idArtist), 1)
FROM        artist
"""
create_album = """
SELECT      coalesce(max(idAlbum), 0)
FROM        album
"""
create_song = """
SELECT      coalesce(max(idSong), 0)
FROM        song
"""
create_genre = """
SELECT      coalesce(max(idGenre), 0)
FROM        genre
"""


get_artist = """
SELECT      idArtist, strArtist
FROM        artist
WHERE       strMusicBrainzArtistID = ?
"""
get_artist_obj = ["{ArtistId}", "{Name}", "{UniqueId}"]
get_artist_by_name = """
SELECT      idArtist
FROM        artist
WHERE       strArtist = ?
            COLLATE NOCASE
"""
get_artist_by_id = """
SELECT      idArtist
FROM        artist
WHERE       idArtist = ?
"""
get_artist_name_by_id = """
SELECT      strArtist
FROM        artist
WHERE       idArtist = ?
"""
add_blank_artist = """
INSERT INTO     artist(idArtist, strArtist, strSortName, strMusicBrainzArtistID)
VALUES          (?, ?, ?, ?)
"""
get_artist_by_id = """
SELECT      *
FROM        artist
WHERE       idArtist = ?
"""
get_artist_by_id_obj = ["{ArtistId}"]
get_album_by_id = """
SELECT      *
FROM        album
WHERE       idAlbum = ?
"""
get_album_by_id_obj = ["{AlbumId}"]
get_song_by_id = """
SELECT      *
FROM        song
WHERE       idSong = ?
"""
get_song_by_id_obj = ["{SongId}"]
get_album = """
SELECT      idAlbum
FROM        album
WHERE       strMusicBrainzAlbumID = ?
"""
get_album_obj = ["{AlbumId}", "{Title}", "{UniqueId}", "{Artists}", "album"]
get_album_obj82 = [
    "{AlbumId}",
    "{Title}",
    "{UniqueId}",
    "{Artists}",
    "album",
    "{DateAdded}",
]
get_album_by_name = """
SELECT      idAlbum, strArtists
FROM        album
WHERE       strAlbum = ?
"""
get_album_by_name72 = """
SELECT      idAlbum, strArtistDisp
FROM        album
WHERE       strAlbum = ?
"""
get_album_artist = """
SELECT      strArtists
FROM        album
WHERE       idAlbum = ?
"""
get_album_artist72 = """
SELECT      strArtistDisp
FROM        album
WHERE       idAlbum = ?
"""
get_album_artist_obj = ["{AlbumId}", "{strAlbumArtists}"]
get_genre = """
SELECT      idGenre
FROM        genre
WHERE       strGenre = ?
            COLLATE NOCASE
"""
get_total_episodes = """
SELECT      totalCount
FROM        tvshowcounts
WHERE       idShow = ?
"""


add_artist = """
INSERT INTO     artist(idArtist, strArtist, strMusicBrainzArtistID)
VALUES          (?, ?, ?)
"""
add_album = """
INSERT INTO     album(idAlbum, strAlbum, strMusicBrainzAlbumID, strReleaseType)
VALUES          (?, ?, ?, ?)
"""
add_album72 = """
INSERT INTO     album(idAlbum, strAlbum, strMusicBrainzAlbumID, strReleaseType, bScrapedMBID)
VALUES          (?, ?, ?, ?, 1)
"""
add_album82 = """
INSERT INTO     album(idAlbum, strAlbum, strMusicBrainzAlbumID, strReleaseType, bScrapedMBID, DateAdded)
VALUES          (?, ?, ?, ?, 1, ?)
"""
add_single = """
INSERT INTO     album(idAlbum, strGenres, iYear, strReleaseType)
VALUES          (?, ?, ?, ?)
"""
add_single74 = """
INSERT INTO     album(idAlbum, strGenres, strReleaseDate, strReleaseType)
VALUES          (?, ?, ?, ?)
"""
add_single_obj = ["{AlbumId}", "{Genre}", "{Year}", "single"]
add_song = """
INSERT INTO     song(idSong, idAlbum, idPath, strArtists, strGenres, strTitle, iTrack,
                iDuration, iYear, strFileName, strMusicBrainzTrackID, iTimesPlayed, lastplayed,
                rating, comment, dateAdded)
VALUES          (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
add_song72 = """
INSERT INTO     song(idSong, idAlbum, idPath, strArtistDisp, strGenres, strTitle, iTrack,
                iDuration, iYear, strFileName, strMusicBrainzTrackID, iTimesPlayed, lastplayed,
                rating, comment, dateAdded)
VALUES          (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
add_song74 = """
INSERT INTO     song(idSong, idAlbum, idPath, strArtistDisp, strGenres, strTitle, iTrack,
                iDuration, strReleaseDate, strFileName, strMusicBrainzTrackID, iTimesPlayed, lastplayed,
                rating, comment, dateAdded)
VALUES          (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
add_song_obj = [
    "{SongId}",
    "{AlbumId}",
    "{PathId}",
    "{Artists}",
    "{Genre}",
    "{Title}",
    "{Index}",
    "{Runtime}",
    "{Year}",
    "{Filename}",
    "{UniqueId}",
    "{PlayCount}",
    "{DatePlayed}",
    "{Rating}",
    "{Comment}",
    "{DateAdded}",
]
add_genre = """
INSERT INTO     genre(idGenre, strGenre)
VALUES          (?, ?)
"""
add_genres_obj = ["{AlbumId}", "{Genres}", "album"]


update_path = """
UPDATE      path
SET         strPath = ?
WHERE       idPath = ?
"""
update_path_obj = ["{Path}", "{PathId}"]
update_role = """
INSERT OR REPLACE INTO      role(idRole, strRole)
VALUES                      (?, ?)
"""
update_role_obj = [1, "Composer"]
update_artist_name = """
UPDATE      artist
SET         strArtist = ?
WHERE       idArtist = ?
"""
update_artist_name_obj = ["{Name}", "{ArtistId}"]
update_artist74 = """
UPDATE      artist
SET         strGenres = ?, strBiography = ?, strImage = ?, strFanart = ?, lastScraped = ?
WHERE       idArtist = ?
"""
update_artist82 = """
UPDATE      artist
SET         strGenres = ?, strBiography = ?, strImage = ?, lastScraped = ?
WHERE       idArtist = ?
"""
update_link = """
INSERT OR REPLACE INTO      album_artist(idArtist, idAlbum, strArtist)
VALUES                      (?, ?, ?)
"""
update_link_obj = ["{ArtistId}", "{AlbumId}", "{Name}"]
# Plain INSERT, not INSERT OR REPLACE: discography carries no unique index
# (Kodi's schema gives it only idxDiscography_1 on idArtist), so OR REPLACE
# has no conflict target and never replaced anything. Kodi's own writer is a
# plain INSERT too, kept to one row per album by clearing first -- see
# MusicKodiDb.add_discography for how that shape is reproduced here.
update_discography = """
INSERT INTO                 discography(idArtist, strAlbum, strYear)
VALUES                      (?, ?, ?)
"""
update_discography_obj = ["{ArtistId}", "{Title}", "{Year}"]
delete_discography = """
DELETE FROM                 discography
WHERE                       idArtist = ? AND strAlbum = ?
"""
get_discography = """
SELECT                      1
FROM                        discography
WHERE                       idArtist = ? AND strAlbum = ?
"""
get_album_title = """
SELECT                      strAlbum
FROM                        album
WHERE                       idAlbum = ?
"""
# Scoped by album_artist rather than by title alone: album titles repeat
# across artists ("Greatest Hits", "Anthology"), and discography legitimately
# holds albums that are not in the library at all -- a scraped artist's rows
# are mostly those -- so a title-only delete would take other artists' rows
# and Kodi's own with them.
delete_album_discography = """
DELETE FROM                 discography
WHERE                       strAlbum = (SELECT strAlbum FROM album WHERE idAlbum = ?)
AND                         idArtist IN (SELECT idArtist FROM album_artist WHERE idAlbum = ?)
"""
update_album = """
UPDATE      album
SET         strAlbum = ?, strArtists = ?, iYear = ?, strGenres = ?, strReview = ?, strImage = ?,
            iUserrating = ?, lastScraped = ?, strReleaseType = ?
WHERE       idAlbum = ?
"""
update_album72 = """
UPDATE      album
SET         strAlbum = ?, strArtistDisp = ?, iYear = ?, strGenres = ?, strReview = ?, strImage = ?,
            iUserrating = ?, lastScraped = ?, bScrapedMBID = 1, strReleaseType = ?
WHERE       idAlbum = ?
"""
update_album74 = """
UPDATE      album
SET         strAlbum = ?, strArtistDisp = ?, strReleaseDate = ?, strGenres = ?, strReview = ?, strImage = ?,
            iUserrating = ?, lastScraped = ?, bScrapedMBID = 1, strReleaseType = ?
WHERE       idAlbum = ?
"""
update_album_obj = [
    "{Title}",
    "{Artists}",
    "{Year}",
    "{Genre}",
    "{Bio}",
    "{Thumb}",
    "{Rating}",
    "{LastScraped}",
    "album",
    "{AlbumId}",
]
update_album_duration = """
UPDATE      album
SET         iAlbumDuration = ?
WHERE       idAlbum = ?
"""
update_album_duration_obj = ["{Runtime}", "{AlbumId}"]
update_album_artist = """
UPDATE      album
SET         strArtists = ?
WHERE       idAlbum = ?
"""
update_album_artist72 = """
UPDATE      album
SET         strArtistDisp = ?
WHERE       idAlbum = ?
"""
update_song = """
UPDATE      song
SET         idAlbum = ?, strArtists = ?, strGenres = ?, strTitle = ?, iTrack = ?,
            iDuration = ?, iYear = ?, strFilename = ?, iTimesPlayed = ?, lastplayed = ?,
            rating = ?, comment = ?, dateAdded = ?
WHERE       idSong = ?
"""
update_song72 = """
UPDATE      song
SET         idAlbum = ?, strArtistDisp = ?, strGenres = ?, strTitle = ?, iTrack = ?,
            iDuration = ?, iYear = ?, strFilename = ?, iTimesPlayed = ?, lastplayed = ?,
            rating = ?, comment = ?, dateAdded = ?
WHERE       idSong = ?
"""
update_song74 = """
UPDATE      song
SET         idAlbum = ?, strArtistDisp = ?, strGenres = ?, strTitle = ?, iTrack = ?,
            iDuration = ?, strReleaseDate = ?, strFilename = ?, iTimesPlayed = ?, lastplayed = ?,
            rating = ?, comment = ?, dateAdded = ?
WHERE       idSong = ?
"""
update_song_obj = [
    "{AlbumId}",
    "{Artists}",
    "{Genre}",
    "{Title}",
    "{Index}",
    "{Runtime}",
    "{Year}",
    "{Filename}",
    "{PlayCount}",
    "{DatePlayed}",
    "{Rating}",
    "{Comment}",
    "{DateAdded}",
    "{SongId}",
]
update_song_artist = """
INSERT OR REPLACE INTO      song_artist(idArtist, idSong, idRole, iOrder, strArtist)
VALUES                      (?, ?, ?, ?, ?)
"""
update_song_artist_obj = ["{ArtistId}", "{SongId}", 1, "{Index}", "{Name}"]
update_song_album = """
INSERT OR REPLACE INTO      albuminfosong(idAlbumInfoSong, idAlbumInfo, iTrack,
                            strTitle, iDuration)
VALUES                      (?, ?, ?, ?, ?)
"""
update_song_album_obj = ["{SongId}", "{AlbumId}", "{Index}", "{Title}", "{Runtime}"]
update_song_rating = """
UPDATE      song
SET         iTimesPlayed = ?, lastplayed = ?, rating = ?
WHERE       idSong = ?
"""
update_song_rating_obj = ["{PlayCount}", "{DatePlayed}", "{Rating}", "{KodiId}"]
update_genre_album = """
INSERT OR REPLACE INTO      album_genre(idGenre, idAlbum)
VALUES                      (?, ?)
"""
update_genre_song = """
INSERT OR REPLACE INTO      song_genre(idGenre, idSong)
VALUES                      (?, ?)
"""
update_genre_song_obj = ["{SongId}", "{Genres}", "song"]


delete_blank_song_artist = """
DELETE FROM     song_artist
WHERE           idSong = ?
AND             idArtist = ?
AND             idRole = 1
"""
get_songs_by_artist = """
SELECT          idSong
FROM            song_artist
WHERE           idArtist = ?
AND             idRole = 1
"""
get_album_artist_link = """
SELECT          1
FROM            album_artist
WHERE           idAlbum = ?
AND             idArtist = ?
"""
get_song_role1_artist = """
SELECT          1
FROM            song_artist
WHERE           idSong = ?
AND             idRole = 1
LIMIT           1
"""
get_song_exists = """
SELECT          1
FROM            song
WHERE           idSong = ?
"""
delete_genres_album = """
DELETE FROM     album_genre
WHERE           idAlbum = ?
"""
delete_genres_song = """
DELETE FROM     song_genre
WHERE           idSong = ?
"""
delete_artist = """
DELETE FROM     artist
WHERE           idArtist = ?
"""
delete_album = """
DELETE FROM     album
WHERE           idAlbum = ?
"""
delete_song = """
DELETE FROM     song
WHERE           idSong = ?
"""
get_song_path_id = """
SELECT          idPath
FROM            song
WHERE           idSong = ?
"""
get_song_playlist_row = """
SELECT          path.strPath, song.strFileName, song.strTitle,
                song.strArtistDisp, song.iTrack, song.iDuration
FROM            song
JOIN            path ON path.idPath = song.idPath
WHERE           song.idSong = ?
"""
delete_path_if_unused = """
DELETE FROM     path
WHERE           idPath = ?
AND             NOT EXISTS (SELECT 1 FROM song WHERE song.idPath = ?)
AND             NOT EXISTS (SELECT 1 FROM source_path WHERE source_path.idPath = ?)
"""
prune_orphan_paths = """
DELETE FROM     path
WHERE           NOT EXISTS (SELECT 1 FROM song WHERE song.idPath = path.idPath)
AND             NOT EXISTS (SELECT 1 FROM source_path
                            WHERE source_path.idPath = path.idPath)
AND             (strPath LIKE 'plugin://plugin.video.kofin/%'
                 OR strPath LIKE 'http://%/Audio/%'
                 OR strPath LIKE 'https://%/Audio/%')
"""
# -- the per-library `source` rows ---------------------------------------------
#
# MyMusic has no tag table, so the video side's "tag is <library name>" cannot
# scope a music node, and a path rule cannot either — a downloaded song's path
# is repointed at the filesystem. Kodi's own source surface can: the smart
# playlist `source` rule compiles to an EXISTS over album_source joined to
# source.strName for all three music contents (artists, albums and songs —
# xbmc/playlists/SmartPlayList.cpp), and album_source is a link table that
# repointing leaves alone.
#
# `source_path` is deliberately never written. Nothing in the rule reads it,
# and its idPath column is a per-source ordinal (CMusicDatabase::AddSource
# counts 1, 2, 3...) rather than a path.idPath — while delete_path_if_unused
# and prune_orphan_paths above both join it *as* a path.idPath. Populating the
# table would make those two statements start sparing real path rows whose id
# happens to fall inside the ordinal range. (That mismatch is theirs and
# predates this; it is harmless only while the table stays empty.)

create_source = """
SELECT      coalesce(max(idSource), 0)
FROM        source
"""
get_source = """
SELECT      idSource, strName
FROM        source
WHERE       strMultipath = ?
"""
get_kofin_sources = """
SELECT      idSource, strName, strMultipath
FROM        source
WHERE       strMultipath LIKE 'plugin://plugin.video.kofin/%'
"""
add_source = """
INSERT INTO     source(idSource, strName, strMultipath)
VALUES          (?, ?, ?)
"""
update_source_name = """
UPDATE      source
SET         strName = ?
WHERE       idSource = ?
"""
add_album_source = """
INSERT OR IGNORE INTO   album_source(idSource, idAlbum)
VALUES                  (?, ?)
"""
delete_album_other_sources = """
DELETE FROM     album_source
WHERE           idAlbum = ?
AND             idSource != ?
AND             idSource IN (SELECT idSource FROM source
                             WHERE strMultipath LIKE 'plugin://plugin.video.kofin/%')
"""
add_album_source_by_songs = """
INSERT OR IGNORE INTO   album_source(idSource, idAlbum)
SELECT                  ?, idAlbum
FROM                    song
WHERE                   idSong IN (%s)
"""
# -- the reconcile's set-based legs ---------------------------------------------
#
# Same three writes as the per-album pair above and add_album_source_by_songs,
# but driven from kofin.db through an ATTACHed schema so that a whole library's
# mapping rows never cross into Python. That boundary is the entire cost of the
# reconcile: on a Bravia (Kodi 22, ARM 32-bit) fetching one library's 20,799
# song ids took 11.6s and its 1,538 album ids 1.1s, while these statements do
# the identical work in-engine in 0.04s and 0.01s. Indexing does not touch it —
# a covering index made the plan optimal and the time did not move, because the
# rows were never the problem, shipping them was.
#
# The alias is fixed rather than passed in: SQLite cannot parameterise a schema
# name, and a constant keeps these strings free of interpolation.
MAPPING_SCHEMA = "kofinmap"

add_album_source_by_folder = """
INSERT OR IGNORE INTO   album_source(idSource, idAlbum)
SELECT                  ?, kodi_id
FROM                    kofinmap.jellyfin
WHERE                   media_type = 'album'
AND                     media_folder = ?
AND                     kodi_id IS NOT NULL
"""
delete_album_other_sources_by_folder = """
DELETE FROM     album_source
WHERE           idSource != ?
AND             idAlbum IN (SELECT kodi_id FROM kofinmap.jellyfin
                            WHERE media_type = 'album'
                            AND media_folder = ?
                            AND kodi_id IS NOT NULL)
AND             idSource IN (SELECT idSource FROM source
                             WHERE strMultipath LIKE 'plugin://plugin.video.kofin/%')
"""
add_album_source_by_folder_songs = """
INSERT OR IGNORE INTO   album_source(idSource, idAlbum)
SELECT DISTINCT         ?, song.idAlbum
FROM                    song
WHERE                   song.idSong IN (SELECT kodi_id FROM kofinmap.jellyfin
                                        WHERE media_type = 'song'
                                        AND media_folder = ?
                                        AND kodi_id IS NOT NULL)
"""
delete_source = """
DELETE FROM     source
WHERE           idSource = ?
"""
get_version = """
SELECT      idVersion
FROM        version
"""
update_versiontag = """
INSERT OR REPLACE INTO      versiontagscan(idVersion, iNeedsScan)
VALUES                      (?, 0)
"""
get_versiontagcount = """
SELECT COUNT    (*)
FROM            versiontagscan
"""
