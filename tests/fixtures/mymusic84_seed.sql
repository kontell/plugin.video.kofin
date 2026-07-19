-- Rows Kodi itself writes when creating MyMusic (MusicDatabase::CreateTables).
-- The Piers box's copies were mutated by library scans, so these come from
-- Kodi Piers source (identical to Omega):
--   role: xbmc/music/MusicDatabase.cpp CreateTables ("Default role")
--   artist: the [Missing Tag] blank artist (BLANKARTIST_*, xbmc/music/Artist.h)
INSERT INTO role(idRole, strRole) VALUES (1, 'Artist');
INSERT INTO artist (idArtist, strArtist, strSortName, strMusicBrainzArtistID)
VALUES (1, '[Missing Tag]', '[Missing Tag]', 'Artist Tag Missing');
