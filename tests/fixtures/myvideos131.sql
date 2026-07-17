CREATE TABLE version (idVersion integer, iCompressCount integer);
CREATE TABLE bookmark ( idBookmark integer primary key, idFile integer, timeInSeconds double, totalTimeInSeconds double, thumbNailImage text, player text, playerState text, type integer);
CREATE TABLE settings ( idFile integer, Deinterlace bool,ViewMode integer,ZoomAmount float, PixelRatio float, VerticalShift float, AudioStream integer, SubtitleStream integer,SubtitleDelay float, SubtitlesOn bool, Brightness float, Contrast float, Gamma float,VolumeAmplification float, AudioDelay float, ResumeTime integer,Sharpness float, NoiseReduction float, NonLinStretch bool, PostProcess bool,ScalingMethod integer, DeinterlaceMode integer, StereoMode integer, StereoInvert bool, VideoStream integer,TonemapMethod integer, TonemapParam float, Orientation integer, CenterMixLevel integer);
CREATE TABLE stacktimes (idFile integer, times text);
CREATE TABLE genre ( genre_id integer primary key, name TEXT);
CREATE TABLE genre_link (genre_id integer, media_id integer, media_type TEXT);
CREATE TABLE country ( country_id integer primary key, name TEXT);
CREATE TABLE country_link (country_id integer, media_id integer, media_type TEXT);
CREATE TABLE movie ( idMovie integer primary key, idFile integer,c00 text,c01 text,c02 text,c03 text,c04 text,c05 text,c06 text,c07 text,c08 text,c09 text,c10 text,c11 text,c12 text,c13 text,c14 text,c15 text,c16 text,c17 text,c18 text,c19 text,c20 text,c21 text,c22 text,c23 text, idSet integer, userrating integer, premiered text);
CREATE TABLE actor ( actor_id INTEGER PRIMARY KEY, name TEXT, art_urls TEXT );
CREATE TABLE actor_link(actor_id INTEGER, media_id INTEGER, media_type TEXT, role TEXT, cast_order INTEGER);
CREATE TABLE director_link(actor_id INTEGER, media_id INTEGER, media_type TEXT);
CREATE TABLE writer_link(actor_id INTEGER, media_id INTEGER, media_type TEXT);
CREATE TABLE path ( idPath integer primary key, strPath text, strContent text, strScraper text, strHash text, scanRecursive integer, useFolderNames bool, strSettings text, noUpdate bool, exclude bool, allAudio bool, dateAdded text, idParentPath integer);
CREATE TABLE files ( idFile integer primary key, idPath integer, strFilename text, playCount integer, lastPlayed text, dateAdded text);
CREATE TABLE tvshow ( idShow integer primary key,c00 text,c01 text,c02 text,c03 text,c04 text,c05 text,c06 text,c07 text,c08 text,c09 text,c10 text,c11 text,c12 text,c13 text,c14 text,c15 text,c16 text,c17 text,c18 text,c19 text,c20 text,c21 text,c22 text,c23 text, userrating integer, duration INTEGER);
CREATE TABLE episode ( idEpisode integer primary key, idFile integer,c00 text,c01 text,c02 text,c03 text,c04 text,c05 text,c06 text,c07 text,c08 text,c09 text,c10 text,c11 text,c12 varchar(24),c13 varchar(24),c14 text,c15 text,c16 text,c17 varchar(24),c18 text,c19 text,c20 text,c21 text,c22 text,c23 text, idShow integer, userrating integer, idSeason integer);
CREATE TABLE tvshowlinkpath (idShow integer, idPath integer);
CREATE TABLE movielinktvshow ( idMovie integer, IdShow integer);
CREATE TABLE studio ( studio_id integer primary key, name TEXT);
CREATE TABLE studio_link (studio_id integer, media_id integer, media_type TEXT);
CREATE TABLE musicvideo ( idMVideo integer primary key, idFile integer,c00 text,c01 text,c02 text,c03 text,c04 text,c05 text,c06 text,c07 text,c08 text,c09 text,c10 text,c11 text,c12 text,c13 text,c14 text,c15 text,c16 text,c17 text,c18 text,c19 text,c20 text,c21 text,c22 text,c23 text, userrating integer, premiered text);
CREATE TABLE streamdetails (idFile integer, iStreamType integer, strVideoCodec text, fVideoAspect float, iVideoWidth integer, iVideoHeight integer, strAudioCodec text, iAudioChannels integer, strAudioLanguage text, strSubtitleLanguage text, iVideoDuration integer, strStereoMode text, strVideoLanguage text, strHdrType text);
CREATE TABLE sets ( idSet integer primary key, strSet text, strOverview text);
CREATE TABLE seasons ( idSeason integer primary key, idShow integer, season integer, name text, userrating integer);
CREATE TABLE art(art_id INTEGER PRIMARY KEY, media_id INTEGER, media_type TEXT, type TEXT, url TEXT);
CREATE TABLE tag (tag_id integer primary key, name TEXT);
CREATE TABLE tag_link (tag_id integer, media_id integer, media_type TEXT);
CREATE TABLE rating (rating_id INTEGER PRIMARY KEY, media_id INTEGER, media_type TEXT, rating_type TEXT, rating FLOAT, votes INTEGER);
CREATE TABLE uniqueid (uniqueid_id INTEGER PRIMARY KEY, media_id INTEGER, media_type TEXT, value TEXT, type TEXT);
CREATE TABLE videoversiontype (id INTEGER PRIMARY KEY, name TEXT, owner INTEGER, itemType INTEGER);
CREATE TABLE IF NOT EXISTS "videoversion" (idFile INTEGER PRIMARY KEY, idMedia INTEGER, media_type TEXT,  itemType INTEGER, idType INTEGER);
CREATE INDEX ix_bookmark ON bookmark (idFile, type);
CREATE UNIQUE INDEX ix_settings ON settings ( idFile )
;
CREATE UNIQUE INDEX ix_stacktimes ON stacktimes ( idFile )
;
CREATE INDEX ix_path ON path ( strPath );
CREATE INDEX ix_path2 ON path ( idParentPath );
CREATE INDEX ix_files ON files ( idPath, strFilename );
CREATE UNIQUE INDEX ix_movie_file_1 ON movie (idFile, idMovie);
CREATE UNIQUE INDEX ix_movie_file_2 ON movie (idMovie, idFile);
CREATE UNIQUE INDEX ix_tvshowlinkpath_1 ON tvshowlinkpath ( idShow, idPath )
;
CREATE UNIQUE INDEX ix_tvshowlinkpath_2 ON tvshowlinkpath ( idPath, idShow )
;
CREATE UNIQUE INDEX ix_movielinktvshow_1 ON movielinktvshow ( idShow, idMovie)
;
CREATE UNIQUE INDEX ix_movielinktvshow_2 ON movielinktvshow ( idMovie, idShow)
;
CREATE UNIQUE INDEX ix_episode_file_1 on episode (idEpisode, idFile);
CREATE UNIQUE INDEX id_episode_file_2 on episode (idFile, idEpisode);
CREATE INDEX ix_episode_season_episode on episode (c12, c13);
CREATE INDEX ix_episode_bookmark on episode (c17);
CREATE INDEX ix_episode_show1 on episode(idEpisode,idShow);
CREATE INDEX ix_episode_show2 on episode(idShow,idEpisode);
CREATE UNIQUE INDEX ix_musicvideo_file_1 on musicvideo (idMVideo, idFile);
CREATE UNIQUE INDEX ix_musicvideo_file_2 on musicvideo (idFile, idMVideo);
CREATE INDEX ixMovieBasePath ON movie ( c23 );
CREATE INDEX ixMusicVideoBasePath ON musicvideo ( c14 );
CREATE INDEX ixEpisodeBasePath ON episode ( c19 );
CREATE INDEX ix_streamdetails ON streamdetails (idFile);
CREATE INDEX ix_seasons ON seasons (idShow, season);
CREATE INDEX ix_art ON art(media_id, media_type, type);
CREATE INDEX ix_rating ON rating(media_id, media_type);
CREATE INDEX ix_uniqueid1 ON uniqueid(media_id, media_type, type);
CREATE INDEX ix_uniqueid2 ON uniqueid(media_type, value);
CREATE UNIQUE INDEX ix_actor_1 ON actor (name);
CREATE UNIQUE INDEX ix_actor_link_1 ON actor_link (actor_id, media_type, media_id, role);
CREATE INDEX ix_actor_link_2 ON actor_link (media_id, media_type, actor_id);
CREATE INDEX ix_actor_link_3 ON actor_link (media_type);
CREATE INDEX ix_videoversion ON videoversion (idMedia, media_type);
CREATE INDEX ix_movie_title ON movie (c00);
CREATE UNIQUE INDEX ix_tag_1 ON tag (name);
CREATE UNIQUE INDEX ix_tag_link_1 ON tag_link (tag_id, media_type, media_id);
CREATE UNIQUE INDEX ix_tag_link_2 ON tag_link (media_id, media_type, tag_id);
CREATE INDEX ix_tag_link_3 ON tag_link (media_type);
CREATE UNIQUE INDEX ix_director_link_1 ON director_link (actor_id, media_type, media_id);
CREATE UNIQUE INDEX ix_director_link_2 ON director_link (media_id, media_type, actor_id);
CREATE INDEX ix_director_link_3 ON director_link (media_type);
CREATE UNIQUE INDEX ix_writer_link_1 ON writer_link (actor_id, media_type, media_id);
CREATE UNIQUE INDEX ix_writer_link_2 ON writer_link (media_id, media_type, actor_id);
CREATE INDEX ix_writer_link_3 ON writer_link (media_type);
CREATE UNIQUE INDEX ix_studio_1 ON studio (name);
CREATE UNIQUE INDEX ix_studio_link_1 ON studio_link (studio_id, media_type, media_id);
CREATE UNIQUE INDEX ix_studio_link_2 ON studio_link (media_id, media_type, studio_id);
CREATE INDEX ix_studio_link_3 ON studio_link (media_type);
CREATE UNIQUE INDEX ix_genre_1 ON genre (name);
CREATE UNIQUE INDEX ix_genre_link_1 ON genre_link (genre_id, media_type, media_id);
CREATE UNIQUE INDEX ix_genre_link_2 ON genre_link (media_id, media_type, genre_id);
CREATE INDEX ix_genre_link_3 ON genre_link (media_type);
CREATE UNIQUE INDEX ix_country_1 ON country (name);
CREATE UNIQUE INDEX ix_country_link_1 ON country_link (country_id, media_type, media_id);
CREATE UNIQUE INDEX ix_country_link_2 ON country_link (media_id, media_type, country_id);
CREATE INDEX ix_country_link_3 ON country_link (media_type);
CREATE TRIGGER delete_movie AFTER DELETE ON movie FOR EACH ROW BEGIN DELETE FROM genre_link WHERE media_id=old.idMovie AND media_type='movie'; DELETE FROM actor_link WHERE media_id=old.idMovie AND media_type='movie'; DELETE FROM director_link WHERE media_id=old.idMovie AND media_type='movie'; DELETE FROM studio_link WHERE media_id=old.idMovie AND media_type='movie'; DELETE FROM country_link WHERE media_id=old.idMovie AND media_type='movie'; DELETE FROM writer_link WHERE media_id=old.idMovie AND media_type='movie'; DELETE FROM movielinktvshow WHERE idMovie=old.idMovie; DELETE FROM art WHERE media_id=old.idMovie AND media_type='movie'; DELETE FROM tag_link WHERE media_id=old.idMovie AND media_type='movie'; DELETE FROM rating WHERE media_id=old.idMovie AND media_type='movie'; DELETE FROM uniqueid WHERE media_id=old.idMovie AND media_type='movie'; DELETE FROM videoversion WHERE idFile=old.idFile AND idMedia=old.idMovie AND media_type='movie'; END;
CREATE TRIGGER delete_tvshow AFTER DELETE ON tvshow FOR EACH ROW BEGIN DELETE FROM actor_link WHERE media_id=old.idShow AND media_type='tvshow'; DELETE FROM director_link WHERE media_id=old.idShow AND media_type='tvshow'; DELETE FROM studio_link WHERE media_id=old.idShow AND media_type='tvshow'; DELETE FROM tvshowlinkpath WHERE idShow=old.idShow; DELETE FROM genre_link WHERE media_id=old.idShow AND media_type='tvshow'; DELETE FROM movielinktvshow WHERE idShow=old.idShow; DELETE FROM seasons WHERE idShow=old.idShow; DELETE FROM art WHERE media_id=old.idShow AND media_type='tvshow'; DELETE FROM tag_link WHERE media_id=old.idShow AND media_type='tvshow'; DELETE FROM rating WHERE media_id=old.idShow AND media_type='tvshow'; DELETE FROM uniqueid WHERE media_id=old.idShow AND media_type='tvshow'; END;
CREATE TRIGGER delete_musicvideo AFTER DELETE ON musicvideo FOR EACH ROW BEGIN DELETE FROM actor_link WHERE media_id=old.idMVideo AND media_type='musicvideo'; DELETE FROM director_link WHERE media_id=old.idMVideo AND media_type='musicvideo'; DELETE FROM genre_link WHERE media_id=old.idMVideo AND media_type='musicvideo'; DELETE FROM studio_link WHERE media_id=old.idMVideo AND media_type='musicvideo'; DELETE FROM art WHERE media_id=old.idMVideo AND media_type='musicvideo'; DELETE FROM tag_link WHERE media_id=old.idMVideo AND media_type='musicvideo'; DELETE FROM uniqueid WHERE media_id=old.idMVideo AND media_type='musicvideo'; END;
CREATE TRIGGER delete_episode AFTER DELETE ON episode FOR EACH ROW BEGIN DELETE FROM actor_link WHERE media_id=old.idEpisode AND media_type='episode'; DELETE FROM director_link WHERE media_id=old.idEpisode AND media_type='episode'; DELETE FROM writer_link WHERE media_id=old.idEpisode AND media_type='episode'; DELETE FROM art WHERE media_id=old.idEpisode AND media_type='episode'; DELETE FROM rating WHERE media_id=old.idEpisode AND media_type='episode'; DELETE FROM uniqueid WHERE media_id=old.idEpisode AND media_type='episode'; END;
CREATE TRIGGER delete_season AFTER DELETE ON seasons FOR EACH ROW BEGIN DELETE FROM art WHERE media_id=old.idSeason AND media_type='season'; END;
CREATE TRIGGER delete_set AFTER DELETE ON sets FOR EACH ROW BEGIN DELETE FROM art WHERE media_id=old.idSet AND media_type='set'; END;
CREATE TRIGGER delete_person AFTER DELETE ON actor FOR EACH ROW BEGIN DELETE FROM art WHERE media_id=old.actor_id AND media_type IN ('actor','artist','writer','director'); END;
CREATE TRIGGER delete_tag AFTER DELETE ON tag_link FOR EACH ROW BEGIN DELETE FROM tag WHERE tag_id=old.tag_id AND tag_id NOT IN (SELECT DISTINCT tag_id FROM tag_link); END;
CREATE TRIGGER delete_file AFTER DELETE ON files FOR EACH ROW BEGIN DELETE FROM bookmark WHERE idFile=old.idFile; DELETE FROM settings WHERE idFile=old.idFile; DELETE FROM stacktimes WHERE idFile=old.idFile; DELETE FROM streamdetails WHERE idFile=old.idFile; DELETE FROM videoversion WHERE idFile=old.idFile; DELETE FROM art WHERE media_id=old.idFile AND media_type='videoversion'; END;
CREATE VIEW episode_view AS SELECT   episode.*,  files.strFileName AS strFileName,  path.strPath AS strPath,  files.playCount AS playCount,  files.lastPlayed AS lastPlayed,  files.dateAdded AS dateAdded,  tvshow.c00 AS strTitle,  tvshow.c08 AS genre,  tvshow.c14 AS studio,  tvshow.c05 AS premiered,  tvshow.c13 AS mpaa,  bookmark.timeInSeconds AS resumeTimeInSeconds,   bookmark.totalTimeInSeconds AS totalTimeInSeconds,   bookmark.playerState AS playerState,   rating.rating AS rating,   rating.votes AS votes,   rating.rating_type AS rating_type,   uniqueid.value AS uniqueid_value,   uniqueid.type AS uniqueid_type FROM episode  JOIN files ON    files.idFile=episode.idFile  JOIN tvshow ON    tvshow.idShow=episode.idShow  JOIN seasons ON    seasons.idSeason=episode.idSeason  JOIN path ON    files.idPath=path.idPath  LEFT JOIN bookmark ON    bookmark.idFile=episode.idFile AND bookmark.type=1  LEFT JOIN rating ON    rating.rating_id=episode.c03  LEFT JOIN uniqueid ON    uniqueid.uniqueid_id=episode.c20
/* episode_view(idEpisode,idFile,c00,c01,c02,c03,c04,c05,c06,c07,c08,c09,c10,c11,c12,c13,c14,c15,c16,c17,c18,c19,c20,c21,c22,c23,idShow,userrating,idSeason,strFileName,strPath,playCount,lastPlayed,dateAdded,strTitle,genre,studio,premiered,mpaa,resumeTimeInSeconds,totalTimeInSeconds,playerState,rating,votes,rating_type,uniqueid_value,uniqueid_type) */;
CREATE VIEW tvshowcounts AS SELECT       tvshow.idShow AS idShow,      MAX(files.lastPlayed) AS lastPlayed,      NULLIF(COUNT(episode.c12), 0) AS totalCount,      COUNT(files.playCount) AS watchedcount,      NULLIF(COUNT(DISTINCT(episode.c12)), 0) AS totalSeasons,       MAX(files.dateAdded) as dateAdded,       COUNT(bookmark.type) AS inProgressCount     FROM tvshow      LEFT JOIN episode ON        episode.idShow=tvshow.idShow      LEFT JOIN files ON        files.idFile=episode.idFile       LEFT JOIN bookmark ON        bookmark.idFile=files.idFile AND bookmark.type=1 GROUP BY tvshow.idShow
/* tvshowcounts(idShow,lastPlayed,totalCount,watchedcount,totalSeasons,dateAdded,inProgressCount) */;
CREATE VIEW tvshowlinkpath_minview AS SELECT   idShow,   min(idPath) AS idPath FROM tvshowlinkpath GROUP BY idShow
/* tvshowlinkpath_minview(idShow,idPath) */;
CREATE VIEW tvshow_view AS SELECT   tvshow.*,  path.idParentPath AS idParentPath,  path.strPath AS strPath,  tvshowcounts.dateAdded AS dateAdded,  lastPlayed, totalCount, watchedcount, totalSeasons,   rating.rating AS rating,   rating.votes AS votes,   rating.rating_type AS rating_type,   uniqueid.value AS uniqueid_value,   uniqueid.type AS uniqueid_type,   tvshowcounts.inProgressCount AS inProgressCount FROM tvshow  LEFT JOIN tvshowlinkpath_minview ON     tvshowlinkpath_minview.idShow=tvshow.idShow  LEFT JOIN path ON    path.idPath=tvshowlinkpath_minview.idPath  INNER JOIN tvshowcounts ON    tvshow.idShow = tvshowcounts.idShow   LEFT JOIN rating ON    rating.rating_id=tvshow.c04   LEFT JOIN uniqueid ON    uniqueid.uniqueid_id=tvshow.c12
/* tvshow_view(idShow,c00,c01,c02,c03,c04,c05,c06,c07,c08,c09,c10,c11,c12,c13,c14,c15,c16,c17,c18,c19,c20,c21,c22,c23,userrating,duration,idParentPath,strPath,dateAdded,lastPlayed,totalCount,watchedcount,totalSeasons,rating,votes,rating_type,uniqueid_value,uniqueid_type,inProgressCount) */;
CREATE VIEW season_view AS SELECT   seasons.idSeason AS idSeason,  seasons.idShow AS idShow,  seasons.season AS season,  seasons.name AS name,  seasons.userrating AS userrating,  tvshow_view.strPath AS strPath,  tvshow_view.c00 AS showTitle,  tvshow_view.c01 AS plot,  tvshow_view.c05 AS premiered,  tvshow_view.c08 AS genre,  tvshow_view.c14 AS studio,  tvshow_view.c13 AS mpaa,  count(DISTINCT episode.idEpisode) AS episodes,  count(files.playCount) AS playCount,  min(episode.c05) AS aired,   count(bookmark.type) AS inProgressCount FROM seasons  JOIN tvshow_view ON    tvshow_view.idShow = seasons.idShow  JOIN episode ON    episode.idShow = seasons.idShow AND episode.c12 = seasons.season  JOIN files ON    files.idFile = episode.idFile   LEFT JOIN bookmark ON    bookmark.idFile = files.idFile AND bookmark.type = 1 GROUP BY seasons.idSeason,         seasons.idShow,         seasons.season,         seasons.name,         seasons.userrating,         tvshow_view.strPath,         tvshow_view.c00,         tvshow_view.c01,         tvshow_view.c05,         tvshow_view.c08,         tvshow_view.c14,         tvshow_view.c13
/* season_view(idSeason,idShow,season,name,userrating,strPath,showTitle,plot,premiered,genre,studio,mpaa,episodes,playCount,aired,inProgressCount) */;
CREATE VIEW musicvideo_view AS SELECT  musicvideo.*,  files.strFileName as strFileName,  path.strPath as strPath,  files.playCount as playCount,  files.lastPlayed as lastPlayed,  files.dateAdded as dateAdded,   bookmark.timeInSeconds AS resumeTimeInSeconds,   bookmark.totalTimeInSeconds AS totalTimeInSeconds,   bookmark.playerState AS playerState,   uniqueid.value AS uniqueid_value,   uniqueid.type AS uniqueid_type FROM musicvideo  JOIN files ON    files.idFile=musicvideo.idFile  JOIN path ON    path.idPath=files.idPath  LEFT JOIN bookmark ON    bookmark.idFile=musicvideo.idFile AND bookmark.type=1  LEFT JOIN uniqueid ON    uniqueid.uniqueid_id=musicvideo.c15
/* musicvideo_view(idMVideo,idFile,c00,c01,c02,c03,c04,c05,c06,c07,c08,c09,c10,c11,c12,c13,c14,c15,c16,c17,c18,c19,c20,c21,c22,c23,userrating,premiered,strFileName,strPath,playCount,lastPlayed,dateAdded,resumeTimeInSeconds,totalTimeInSeconds,playerState,uniqueid_value,uniqueid_type) */;
CREATE VIEW movie_view AS SELECT  movie.*,  sets.strSet AS strSet,  sets.strOverview AS strSetOverview,  files.strFileName AS strFileName,  path.strPath AS strPath,  files.playCount AS playCount,  files.lastPlayed AS lastPlayed,   files.dateAdded AS dateAdded,   bookmark.timeInSeconds AS resumeTimeInSeconds,   bookmark.totalTimeInSeconds AS totalTimeInSeconds,   bookmark.playerState AS playerState,   rating.rating AS rating,   rating.votes AS votes,   rating.rating_type AS rating_type,   uniqueid.value AS uniqueid_value,   uniqueid.type AS uniqueid_type,   EXISTS(     SELECT 1     FROM  videoversion vv     WHERE vv.idMedia = movie.idMovie     AND   vv.media_type = 'movie'     AND   vv.itemType = 0     AND   vv.idFile <> movie.idFile   ) AS hasVideoVersions,   EXISTS(     SELECT 1     FROM  videoversion vv     WHERE vv.idMedia = movie.idMovie     AND   vv.media_type = 'movie'     AND   vv.itemType = 1   ) AS hasVideoExtras,   CASE     WHEN vv.idFile = movie.idFile AND vv.itemType = 0 THEN 1     ELSE 0   END AS isDefaultVersion,   vv.idFile AS videoVersionIdFile,   vvt.id AS videoVersionTypeId,  vvt.name AS videoVersionTypeName,  vvt.itemType AS videoVersionTypeItemType FROM movie  LEFT JOIN sets ON    sets.idSet = movie.idSet  LEFT JOIN rating ON    rating.rating_id = movie.c05  LEFT JOIN uniqueid ON    uniqueid.uniqueid_id = movie.c09  LEFT JOIN videoversion vv ON    vv.idMedia = movie.idMovie AND vv.media_type = 'movie' AND vv.itemType = 0  JOIN videoversiontype vvt ON    vvt.id = vv.idType AND vvt.itemType = vv.itemType  JOIN files ON    files.idFile = vv.idFile  JOIN path ON    path.idPath = files.idPath  LEFT JOIN bookmark ON    bookmark.idFile = vv.idFile AND bookmark.type = 1
/* movie_view(idMovie,idFile,c00,c01,c02,c03,c04,c05,c06,c07,c08,c09,c10,c11,c12,c13,c14,c15,c16,c17,c18,c19,c20,c21,c22,c23,idSet,userrating,premiered,strSet,strSetOverview,strFileName,strPath,playCount,lastPlayed,dateAdded,resumeTimeInSeconds,totalTimeInSeconds,playerState,rating,votes,rating_type,uniqueid_value,uniqueid_type,hasVideoVersions,hasVideoExtras,isDefaultVersion,videoVersionIdFile,videoVersionTypeId,videoVersionTypeName,videoVersionTypeItemType) */;
