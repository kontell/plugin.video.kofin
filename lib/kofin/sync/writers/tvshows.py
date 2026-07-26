# -*- coding: utf-8 -*-
"""Series/season/episode writer (fork ``objects/tvshows.py`` port).
Adaptations per plan §3: imports/shims, addon id and path base,
``direct_path`` branches stripped (plugin mode only), ``self.server`` is the
kofin Api."""

import sqlite3
from urllib.parse import urlencode

from kofin.core.log import Logger
from kofin.sync import downloader as server
from kofin.sync import kofindb as jellyfin_db
from kofin.sync import queries_map as QUEM
from kofin.sync import fields as api
from kofin.sync.fields import check_unchanged, find_library, sync_checksum
from kofin.sync.shims import (
    LibraryOrphanException,
    stop,
    jellyfin_item,
    values,
    Local,
)

from kofin.sync.obj import Objects
from kofin.sync.kodidb import TVShows as KodiDb
from kofin.sync.kodidb import queries as QU

##################################################################################################

LOG = Logger(__name__)

##################################################################################################


class TVShows(KodiDb):

    def __init__(
        self,
        server,
        jellyfindb,
        videodb,
        library=None,
        update_library=False,
    ):

        self.server = server
        self.jellyfin = jellyfindb
        self.video = videodb
        # Native mode is gone; the flag stays because the checksum format
        # bakes it in ("<etag>|plugin") and check_unchanged reads it.
        self.direct_path = False
        self.update_library = update_library

        self.jellyfin_db = jellyfin_db.JellyfinDatabase(jellyfindb.cursor)
        self.objects = Objects()
        self.item_ids = []
        self.library = library
        # Memo for find_library, per writer instance (see fields.find_library).
        self.library_cache = {}

        KodiDb.__init__(self, videodb.cursor)

    @stop
    @jellyfin_item
    def tvshow(self, item, e_item):
        """If item does not exist, entry will be added.
        If item exists, entry will be updated.

        If the show is empty, try to remove it.
        Process seasons.
        Apply series pooling.
        """
        server_address = self.server.server
        API = api.API(item, server_address)
        obj = self.objects.map(item, "Series")
        update = True

        try:
            obj["ShowId"] = e_item[0]
            obj["PathId"] = e_item[2]
            obj["LibraryId"] = e_item[6]
            obj["LibraryName"] = self.jellyfin_db.get_view_name(obj["LibraryId"])
        except TypeError:
            update = False
            LOG.debug("ShowId %s not found", obj["Id"])

            library = self.library or find_library(
                self.server, item, self.library_cache
            )
            if not library:
                # This item doesn't belong to a whitelisted library
                return

            obj["ShowId"] = self.create_entry()
            obj["LibraryId"] = library["Id"]
            obj["LibraryName"] = library["Name"]
        else:
            if self.get(*values(obj, QU.get_tvshow_obj)) is None:

                update = False
                LOG.info(
                    "ShowId %s missing from kodi. repairing the entry.", obj["ShowId"]
                )

        if check_unchanged(self, obj, item, e_item, update):
            # Return None (not False) so full sync callers still process the
            # show's episodes.
            return

        obj["Path"] = API.get_file_path(obj["Path"])
        obj["Genres"] = obj["Genres"] or []
        obj["People"] = obj["People"] or []
        obj["Mpaa"] = API.get_mpaa(obj["Mpaa"])
        obj["Studios"] = [
            API.validate_studio(studio) for studio in (obj["Studios"] or [])
        ]
        obj["Genre"] = " / ".join(obj["Genres"])
        obj["People"] = API.get_people_artwork(obj["People"])
        obj["Plot"] = API.get_overview(obj["Plot"])
        obj["Studio"] = " / ".join(obj["Studios"])
        obj["Artwork"] = API.get_all_artwork(self.objects.map(item, "Artwork"))
        self.trailer(obj)

        if obj["Status"] != "Ended":
            obj["Status"] = None

        self.get_path_filename(obj)

        if obj["Premiere"]:
            obj["Premiere"] = (
                str(Local(obj["Premiere"])).split(".")[0].replace("T", " ")
            )

        tags = []
        tags.extend(obj["Tags"] or [])
        tags.append(obj["LibraryName"])

        if obj["Favorite"]:
            tags.append("Favorite tvshows")

        obj["Tags"] = tags

        if update:
            self.tvshow_update(obj)
        else:
            self.tvshow_add(obj)

        self.link(*values(obj, QU.update_tvshow_link_obj))
        self.update_path(*values(obj, QU.update_path_tvshow_obj))
        self.add_tags(*values(obj, QU.add_tags_tvshow_obj))
        self.add_people(*values(obj, QU.add_people_tvshow_obj))
        self.add_genres(*values(obj, QU.add_genres_tvshow_obj))
        self.add_studios(*values(obj, QU.add_studios_tvshow_obj))
        self.artwork.add(obj["Artwork"], obj["ShowId"], "tvshow")
        self.item_ids.append(obj["Id"])

        season_episodes = {}

        for season in server.get_seasons(self.server, obj["Id"])["Items"]:

            if season["SeriesId"] != obj["Id"]:
                obj["SeriesId"] = season["SeriesId"]
                self.item_ids.append(season["SeriesId"])

                try:
                    self.jellyfin_db.get_item_by_id(
                        *values(obj, QUEM.get_item_series_obj)
                    )[0]

                    if self.update_library:
                        season_episodes[season["Id"]] = season["SeriesId"]
                except TypeError:

                    self.jellyfin_db.add_reference(
                        *values(obj, QUEM.add_reference_pool_obj)
                    )
                    LOG.info(
                        "POOL %s [%s/%s]", obj["Title"], obj["Id"], obj["SeriesId"]
                    )
                    season_episodes[season["Id"]] = season["SeriesId"]

            try:
                self.jellyfin_db.get_item_by_id(season["Id"])[0]
                self.item_ids.append(season["Id"])
            except TypeError:
                self.season(season, obj["ShowId"])
        else:
            season_id = self.get_season(*values(obj, QU.get_season_special_obj))
            self.artwork.add(obj["Artwork"], season_id, "season")

        for season in season_episodes:
            for episodes in server.get_episode_by_season(
                self.server, season_episodes[season], season
            ):

                for episode in episodes["Items"]:
                    self.episode(episode)

    def tvshow_add(self, obj):
        """Add object to kodi."""
        obj["RatingId"] = self.create_entry_rating()
        self.add_ratings(*values(obj, QU.add_rating_tvshow_obj))

        obj["Unique"] = self.create_entry_unique_id()
        self.add_unique_id(*values(obj, QU.add_unique_id_tvshow_obj))

        obj["TopPathId"] = self.add_path(obj["TopLevel"])

        # Deviation from the fork, and the one place tvshow paths had to move:
        # the content/scraper pair sits on this library's own path, exactly
        # where Movies and MusicVideos put theirs -- never on the addon root.
        #
        # The fork stamped the root ("Hack to allow cast information in add-on
        # mode") and on Kodi 21 that is what breaks the info dialog in kofin's
        # own listings: a listing item's path is plugin://plugin.video.kofin/?...,
        # whose *directory* is the addon root, so CGUIWindowVideoBase::OnItemInfo
        # resolves tvshows content "found directly" there and returns early --
        # "dont lookup on root tvshow folder" -- swallowing the action with no
        # dialog and nothing logged, for movies and episodes alike. One level
        # down, the root resolves no scraper at all and Kodi falls back to
        # showing the listitem's own tag, which is what carries the cast the
        # hack was reaching for. Library items are unaffected: they still drill
        # up from <library>/<show>/ to the content row, one hop sooner.
        self.update_path(*values(obj, QU.update_path_toptvshow_obj))
        temp_obj = dict()
        temp_obj["TopLevel"] = "plugin://plugin.video.kofin/"
        temp_obj["TopPathId"] = self.add_path(temp_obj["TopLevel"])
        self.update_path(*values(temp_obj, QU.update_path_toptvshow_addon_obj))
        self.update_path_parent_id(obj["TopPathId"], temp_obj["TopPathId"])

        obj["PathId"] = self.add_path(*values(obj, QU.get_path_obj))

        self.add(*values(obj, QU.add_tvshow_obj))
        self.jellyfin_db.add_reference(*values(obj, QUEM.add_reference_tvshow_obj))
        LOG.debug(
            "ADD tvshow [%s/%s/%s] %s: %s",
            obj["TopPathId"],
            obj["PathId"],
            obj["ShowId"],
            obj["Title"],
            obj["Id"],
        )

        self.update_path_parent_id(obj["PathId"], obj["TopPathId"])

    def tvshow_update(self, obj):
        """Update object to kodi."""
        obj["RatingId"] = self.get_rating_id(*values(obj, QU.get_unique_id_tvshow_obj))
        self.update_ratings(*values(obj, QU.update_rating_tvshow_obj))

        obj["Unique"] = self.get_unique_id(*values(obj, QU.get_unique_id_tvshow_obj))
        self.update_unique_id(*values(obj, QU.update_unique_id_tvshow_obj))

        obj["TopPathId"] = self.get_path(obj["TopLevel"])

        self.update(*values(obj, QU.update_tvshow_obj))
        self.jellyfin_db.update_reference(*values(obj, QUEM.update_reference_obj))
        LOG.debug(
            "UPDATE tvshow [%s/%s] %s: %s",
            obj["PathId"],
            obj["ShowId"],
            obj["Title"],
            obj["Id"],
        )

        self.update_path_parent_id(obj["PathId"], obj["TopPathId"])

    def trailer(self, obj):
        """Resolve the trailer URL, preferring a locally stored Jellyfin trailer
        over a remote one.  Mirrors Movies.trailer().
        """
        try:
            if obj["LocalTrailer"]:
                trailer = server.get_local_trailers(self.server, obj["Id"])
                obj["Trailer"] = (
                    "plugin://plugin.video.kofin/trailer?id=%s&mode=play"
                    % trailer[0]["Id"]
                )
            elif obj["Trailer"]:
                obj["Trailer"] = (
                    "plugin://plugin.video.youtube/play/?video_id=%s"
                    % obj["Trailer"].rsplit("=", 1)[1]
                )
        except Exception as error:
            LOG.exception("Failed to get trailer for tvshow %s: %s", obj["Id"], error)
            obj["Trailer"] = None

    def get_path_filename(self, obj):
        """Get the path and build it into protocol://path"""
        obj["TopLevel"] = "plugin://plugin.video.kofin/%s/" % obj["LibraryId"]
        obj["Path"] = "%s%s/" % (obj["TopLevel"], obj["Id"])

    @stop
    def season(self, item, show_id=None):
        """If item does not exist, entry will be added.
        If item exists, entry will be updated.

        If the show is empty, try to remove it.
        """
        server_address = self.server.server
        API = api.API(item, server_address)
        obj = self.objects.map(item, "Season")

        obj["ShowId"] = show_id

        # Deviation from the fork: get_show_id in place of a bare lookup, so an
        # orphan season self-heals exactly as an orphan episode does -- fetch
        # the series, write it, retry. Ordering upstream (the prune's
        # parent-first sort, the feed's parent prefetch) is what keeps this
        # from being the normal path; the nested write inside tvshow() passes
        # show_id, so it never re-enters get_show_id.
        #
        # Unresolvable is raised, not returned: `return False` is what the
        # unchanged short-circuit means in this file, so a caller could not
        # tell "nothing to do" from "this never landed". That is how the fork
        # dropped the write in silence -- no Kodi row, no kofin.db reference,
        # and the watermark past it. The raise lands in the UpdateWorker's
        # LibraryException handler, which flags the item unapplied and earns
        # it a recovery prune.
        if obj["ShowId"] is None and not self.get_show_id(obj):
            raise LibraryOrphanException(
                "season %s: unresolved series %s" % (obj["Id"], obj["SeriesId"])
            )

        obj["SeasonId"] = self.get_season(*values(obj, QU.get_season_obj))
        obj["Artwork"] = API.get_all_artwork(self.objects.map(item, "Artwork"))

        # Unconditional, including Location == "Virtual". A virtual season is
        # not phantom content: Jellyfin marks a season virtual when it has no
        # folder of its own, which is what a flat series layout looks like --
        # episodes sitting beside each other in the series directory rather
        # than under "Season 01". Those episodes are real files and play. On
        # the Piers box all 8 virtual seasons in the Shows library held only
        # FileSystem episodes with paths on disk.
        #
        # get_season above is get-or-create and runs whatever the Location,
        # so Kodi already has the seasons row (and the artwork below lands on
        # it). Skipping only the reference left kofin.db permanently short by
        # exactly the number of flat-layout seasons: the prune reported those
        # 8 missing on every pass, re-fetched them, and the writer declined
        # again -- and nothing could ever remove their Kodi rows, because the
        # mapping to remove them by was never written.
        #
        # Seasons stored no checksum either, which was invisible in the fork
        # (nothing read one) and became load-bearing here: the prune compares
        # the stored value against "<Etag>|plugin", so a NULL made every
        # season read as changed on every pass. Same stamp the other types
        # get via check_unchanged, which season() does not call -- it has no
        # skip path and always writes.
        obj["Checksum"] = sync_checksum(item, self.direct_path)
        self.jellyfin_db.add_reference(*values(obj, QUEM.add_reference_season_obj))

        # One reference per Kodi season row. get_season above is keyed on
        # (idShow, season), so two Jellyfin ids for the same season resolve to
        # the same idSeason -- and Jellyfin does hand out two: the id
        # /Shows/{id}/Seasons reports for a season can differ from the one the
        # /Items listing reports for it, and this writer is reached from both
        # (the per-series walk in tvshow() and the library's Season pass).
        #
        # Two references to one row is not a cosmetic duplicate. The prune
        # diffs against the /Items listing, so the other id reads as stale on
        # every pass; removing it deletes the *shared* row and its episodes,
        # and the surviving reference is left pointing at nothing -- which the
        # prune cannot see, because its id and Etag both still match. On the
        # Omega box that took Breaking Bad S1/S3/S4 and Yellowstone S1 out of
        # the library on every prune, and put them back on every series walk.
        #
        # Dropping the alias here makes the reference set converge on whichever
        # id wrote last. Within a full sync that is the Season pass, i.e. the
        # /Items family the prune diffs -- so the set the prune compares
        # against is the set it queried, and the churn has nothing to feed on.
        self.jellyfin_db.remove_item_alias_by_kodi_id(
            *values(obj, QUEM.delete_alias_season_obj)
        )
        self.item_ids.append(obj["Id"])

        self.artwork.add(obj["Artwork"], obj["SeasonId"], "season")
        LOG.debug(
            "UPDATE season [%s/%s] %s: %s",
            obj["ShowId"],
            obj["SeasonId"],
            obj["Title"] or obj["Index"],
            obj["Id"],
        )

    @stop
    @jellyfin_item
    def episode(self, item, e_item):
        """If item does not exist, entry will be added.
        If item exists, entry will be updated.

        Create additional entry for widgets.
        This is only required for plugin/episode.
        """
        server_address = self.server.server
        API = api.API(item, server_address)
        obj = self.objects.map(item, "Episode")
        update = True

        if obj["Location"] == "Virtual":
            LOG.info("Skipping virtual episode %s: %s", obj["Title"], obj["Id"])

            return

        elif obj["SeriesId"] is None:
            LOG.info("Skipping episode %s with missing SeriesId", obj["Id"])

            return

        try:
            obj["EpisodeId"] = e_item[0]
            obj["FileId"] = e_item[1]
            obj["PathId"] = e_item[2]
        except TypeError:
            update = False
            LOG.debug("EpisodeId %s not found", obj["Id"])

            library = self.library or find_library(
                self.server, item, self.library_cache
            )
            if not library:
                # This item doesn't belong to a whitelisted library
                return

            obj["EpisodeId"] = self.create_entry_episode()
        else:
            if self.get_episode(*values(obj, QU.get_episode_obj)) is None:

                update = False
                LOG.info(
                    "EpisodeId %s missing from kodi. repairing the entry.",
                    obj["EpisodeId"],
                )

        if check_unchanged(self, obj, item, e_item, update):
            return False

        obj["Path"] = API.get_file_path(obj["Path"])
        obj["Index"] = obj["Index"] or -1
        obj["Writers"] = " / ".join(obj["Writers"] or [])
        obj["Directors"] = " / ".join(obj["Directors"] or [])
        obj["Plot"] = API.get_overview(obj["Plot"])
        obj["Resume"] = API.adjust_resume((obj["Resume"] or 0) / 10000000.0)
        obj["Runtime"] = round(float((obj["Runtime"] or 0) / 10000000.0), 6)
        obj["People"] = API.get_people_artwork(obj["People"] or [])
        obj["DateAdded"] = Local(obj["DateAdded"]).split(".")[0].replace("T", " ")
        obj["DatePlayed"] = (
            None
            if not obj["DatePlayed"]
            else Local(obj["DatePlayed"]).split(".")[0].replace("T", " ")
        )
        obj["PlayCount"] = API.get_playcount(obj["Played"], obj["PlayCount"])
        obj["Artwork"] = API.get_all_artwork(self.objects.map(item, "Artwork"))
        obj["Video"] = API.video_streams(obj["Video"] or [], obj["Container"])
        obj["Audio"] = API.audio_streams(obj["Audio"] or [])
        obj["Streams"] = API.media_streams(obj["Video"], obj["Audio"], obj["Subtitles"])

        self.get_episode_path_filename(obj)

        if obj["Premiere"]:
            obj["Premiere"] = Local(obj["Premiere"]).split(".")[0].replace("T", " ")

        if obj["Season"] is None:
            if obj["AbsoluteNumber"]:

                obj["Season"] = 1
                obj["Index"] = obj["AbsoluteNumber"]
            else:
                obj["Season"] = 0

        if obj["AirsAfterSeason"]:

            obj["AirsBeforeSeason"] = obj["AirsAfterSeason"]
            obj["AirsBeforeEpisode"] = (
                4096  # Kodi default number for afterseason ordering
            )

        if obj["MultiEpisode"]:
            obj["Title"] = "| %02d | %s" % (obj["MultiEpisode"], obj["Title"])

        # Same reporting as the season leg above: the fork's `return False`
        # here was indistinguishable from its unchanged short-circuit, so an
        # episode whose series could not be fetched was dropped as quietly as
        # one that needed no work.
        if not self.get_show_id(obj):
            raise LibraryOrphanException(
                "episode %s: unresolved series %s" % (obj["Id"], obj["SeriesId"])
            )

        obj["SeasonId"] = self.get_season(*values(obj, QU.get_season_episode_obj))

        if update:
            self.episode_update(obj)
        else:
            self.episode_add(obj)

        self.update_path(*values(obj, QU.update_path_episode_obj))
        self.update_file(*values(obj, QU.update_file_obj))
        self.add_people(*values(obj, QU.add_people_episode_obj))
        self.add_streams(*values(obj, QU.add_streams_obj))
        self.add_playstate(*values(obj, QU.add_bookmark_obj))
        self.artwork.update(
            obj["Artwork"]["Primary"], obj["EpisodeId"], "episode", "thumb"
        )
        self.item_ids.append(obj["Id"])

        if obj["Resume"]:

            temp_obj = dict(obj)
            temp_obj["Path"] = "plugin://plugin.video.kofin/"
            temp_obj["PathId"] = self.get_path(*values(temp_obj, QU.get_path_obj))
            temp_obj["FileId"] = self.add_file(*values(temp_obj, QU.add_file_obj))
            self.update_file(*values(temp_obj, QU.update_file_obj))
            self.add_playstate(*values(temp_obj, QU.add_bookmark_obj))

        return not update

    def episode_add(self, obj):
        """Add object to kodi."""
        obj["RatingId"] = self.create_entry_rating()
        self.add_ratings(*values(obj, QU.add_rating_episode_obj))

        obj["Unique"] = self.create_entry_unique_id()
        self.add_unique_id(*values(obj, QU.add_unique_id_episode_obj))

        obj["PathId"] = self.add_path(*values(obj, QU.add_path_obj))
        obj["FileId"] = self.add_file(*values(obj, QU.add_file_obj))

        try:
            self.add_episode(*values(obj, QU.add_episode_obj))
        except sqlite3.IntegrityError:
            LOG.error("IntegrityError for %s", obj)
            obj["EpisodeId"] = self.create_entry_episode()

            return self.episode_add(obj)

        self.jellyfin_db.add_reference(*values(obj, QUEM.add_reference_episode_obj))

        parentPathId = self.jellyfin_db.get_episode_kodi_parent_path_id(
            *values(obj, QUEM.get_episode_kodi_parent_path_id_obj)
        )
        if obj["PathId"] != parentPathId:
            LOG.debug(
                "Setting episode pathParentId, episode %s, title %s, pathId %s, pathParentId %s",
                obj["Id"],
                obj["Title"],
                obj["PathId"],
                parentPathId,
            )
            self.update_path_parent_id(obj["PathId"], parentPathId)

        LOG.debug(
            "ADD episode [%s/%s] %s: %s",
            obj["PathId"],
            obj["FileId"],
            obj["Id"],
            obj["Title"],
        )

    def episode_update(self, obj):
        """Update object to kodi."""
        obj["RatingId"] = self.get_rating_id(*values(obj, QU.get_rating_episode_obj))
        self.update_ratings(*values(obj, QU.update_rating_episode_obj))

        obj["Unique"] = self.get_unique_id(*values(obj, QU.get_unique_id_episode_obj))
        self.update_unique_id(*values(obj, QU.update_unique_id_episode_obj))

        self.update_episode(*values(obj, QU.update_episode_obj))

        self.jellyfin_db.update_reference(*values(obj, QUEM.update_reference_obj))
        self.jellyfin_db.update_parent_id(*values(obj, QUEM.update_parent_episode_obj))
        LOG.debug(
            "UPDATE episode [%s/%s] %s: %s",
            obj["PathId"],
            obj["FileId"],
            obj["Id"],
            obj["Title"],
        )

    def get_episode_path_filename(self, obj):
        """Get the path and build it into protocol://path"""
        if "\\" in obj["Path"]:
            obj["Filename"] = obj["Path"].rsplit("\\", 1)[1]
        else:
            obj["Filename"] = obj["Path"].rsplit("/", 1)[1]

        # We need LibraryId
        library = self.library or find_library(self.server, obj, self.library_cache)
        obj["LibraryId"] = library["Id"]
        obj["Path"] = "plugin://plugin.video.kofin/%s/%s/" % (
            obj["LibraryId"],
            obj["SeriesId"],
        )
        params = {
            "filename": obj["Filename"],
            "id": obj["Id"],
            "dbid": obj["EpisodeId"],
            "mode": "play",
        }
        obj["Filename"] = "%s?%s" % (obj["Path"], urlencode(params))
        obj["FullFilePath"] = obj["Filename"]

    def get_show_id(self, obj):
        obj["ShowId"] = self.jellyfin_db.get_item_by_id(
            *values(obj, QUEM.get_item_series_obj)
        )

        if obj["ShowId"] is None:

            try:
                self.tvshow(self.server.item(obj["SeriesId"]))
                obj["ShowId"] = self.jellyfin_db.get_item_by_id(
                    *values(obj, QUEM.get_item_series_obj)
                )[0]
            except (TypeError, KeyError) as error:
                LOG.error("Unable to add series %s", obj["SeriesId"])
                LOG.exception(error)

                return False
        else:
            obj["ShowId"] = obj["ShowId"][0]

        self.item_ids.append(obj["SeriesId"])

        return True

    @stop
    @jellyfin_item
    def userdata(self, item, e_item):
        """This updates: Favorite, LastPlayedDate, Playcount, PlaybackPositionTicks
        Poster with progress bar

        Make sure there's no other bookmarks created by widget.
        Create additional entry for widgets. This is only required for plugin/episode.
        """
        server_address = self.server.server
        API = api.API(item, server_address)
        obj = self.objects.map(item, "EpisodeUserData")

        try:
            obj["KodiId"] = e_item[0]
            obj["FileId"] = e_item[1]
            obj["Media"] = e_item[4]
        except TypeError:
            return

        if obj["Media"] == "tvshow":

            if obj["Favorite"]:
                self.get_tag(*values(obj, QU.get_tag_episode_obj))
            else:
                self.remove_tag(*values(obj, QU.delete_tag_episode_obj))

        elif obj["Media"] == "episode":

            obj["Resume"] = API.adjust_resume((obj["Resume"] or 0) / 10000000.0)
            obj["Runtime"] = self.resolve_runtime(obj["Runtime"], obj["FileId"])
            obj["PlayCount"] = API.get_playcount(obj["Played"], obj["PlayCount"])

            if obj["DatePlayed"]:
                obj["DatePlayed"] = (
                    Local(obj["DatePlayed"]).split(".")[0].replace("T", " ")
                )

            if obj["DateAdded"]:
                obj["DateAdded"] = (
                    Local(obj["DateAdded"]).split(".")[0].replace("T", " ")
                )

            self.add_playstate(*values(obj, QU.add_bookmark_obj))

            if not obj["Resume"]:

                temp_obj = dict(obj)
                temp_obj["Filename"] = self.get_filename(
                    *values(temp_obj, QU.get_file_obj)
                )
                temp_obj["Path"] = "plugin://plugin.video.kofin/"
                self.remove_file(*values(temp_obj, QU.delete_file_obj))

            elif obj["Resume"]:

                temp_obj = dict(obj)
                temp_obj["Filename"] = self.get_filename(
                    *values(temp_obj, QU.get_file_obj)
                )
                temp_obj["PathId"] = self.get_path("plugin://plugin.video.kofin/")
                temp_obj["FileId"] = self.add_file(*values(temp_obj, QU.add_file_obj))
                self.update_file(*values(temp_obj, QU.update_file_obj))
                self.add_playstate(*values(temp_obj, QU.add_bookmark_obj))

        # The reference checksum tracks metadata state (Etag); userdata
        # changes must not overwrite it.
        LOG.debug(
            "USERDATA %s [%s/%s] %s: %s",
            obj["Media"],
            obj["FileId"],
            obj["KodiId"],
            obj["Id"],
            obj["Title"],
        )

    @stop
    @jellyfin_item
    def remove(self, item_id, e_item):
        """Remove showid, fileid, pathid, jellyfin reference.
        There's no episodes left, delete show and any possible remaining seasons
        """
        obj = {"Id": item_id}

        try:
            obj["KodiId"] = e_item[0]
            obj["FileId"] = e_item[1]
            obj["ParentId"] = e_item[3]
            obj["Media"] = e_item[4]
        except TypeError:
            return

        if obj["Media"] == "episode":

            temp_obj = dict(obj)
            self.remove_episode(obj["KodiId"], obj["FileId"], obj["Id"])
            season = self.jellyfin_db.get_full_item_by_kodi_id(
                *values(obj, QUEM.delete_item_by_parent_season_obj)
            )

            # Deviation from the fork: an unresolvable season skips the parent
            # cascade, it does not abandon the removal. The fork returned here,
            # and that return jumped the tail of this method -- including the
            # remove_item that drops this episode's own kofin.db reference.
            # The Kodi row is already gone by then (remove_episode above), so
            # what survived was a reference to a deleted row: exactly the
            # damage the prune cannot see, because it diffs ids and Etags and
            # so reads the surviving reference as present.
            #
            # Reached more often than "cannot happen" suggests. The lookup
            # keys on the episode's *recorded* season kodi_id, so it misses
            # whenever the season has since been re-created at a fresh
            # idSeason -- which is what a removed-then-re-added season leaves
            # behind, with the episode rows still pointing at the old id.
            #
            # Only the cascade needs the season: it decides whether the season
            # (and then the series) is now empty enough to remove too. That
            # question is unanswerable without the reference; this episode's
            # own removal is not.
            if season is not None:
                temp_obj["Id"] = season[0]
                temp_obj["ParentId"] = season[1]

                if not self.jellyfin_db.get_item_by_parent_id(
                    *values(obj, QUEM.get_item_by_parent_episode_obj)
                ):

                    self.remove_season(obj["ParentId"], obj["Id"])
                    self.jellyfin_db.remove_item(
                        *values(temp_obj, QUEM.delete_item_obj)
                    )

                temp_obj["Id"] = self.jellyfin_db.get_item_by_kodi_id(
                    *values(temp_obj, QUEM.get_item_by_parent_tvshow_obj)
                )

                if not self.get_total_episodes(
                    *values(temp_obj, QU.get_total_episodes_obj)
                ):

                    for season in self.jellyfin_db.get_item_by_parent_id(
                        *values(temp_obj, QUEM.get_item_by_parent_season_obj)
                    ):
                        self.remove_season(season[1], obj["Id"])
                    else:
                        self.jellyfin_db.remove_items_by_parent_id(
                            *values(temp_obj, QUEM.delete_item_by_parent_season_obj)
                        )

                    self.remove_tvshow(temp_obj["ParentId"], obj["Id"])
                    self.jellyfin_db.remove_item(
                        *values(temp_obj, QUEM.delete_item_obj)
                    )

        elif obj["Media"] == "tvshow":
            obj["ParentId"] = obj["KodiId"]

            for season in self.jellyfin_db.get_item_by_parent_id(
                *values(obj, QUEM.get_item_by_parent_season_obj)
            ):

                temp_obj = dict(obj)
                temp_obj["ParentId"] = season[1]

                for episode in self.jellyfin_db.get_item_by_parent_id(
                    *values(temp_obj, QUEM.get_item_by_parent_episode_obj)
                ):
                    self.remove_episode(episode[1], episode[2], obj["Id"])
                else:
                    self.jellyfin_db.remove_items_by_parent_id(
                        *values(temp_obj, QUEM.delete_item_by_parent_episode_obj)
                    )
            else:
                self.jellyfin_db.remove_items_by_parent_id(
                    *values(obj, QUEM.delete_item_by_parent_season_obj)
                )

            self.remove_tvshow(obj["KodiId"], obj["Id"])

        elif obj["Media"] == "season":

            # Episodes hang off the season's *own* Kodi id. obj["ParentId"] is
            # this season's parent -- the series' idShow -- and idShow/idSeason
            # are separate Kodi sequences whose ranges overlap almost entirely
            # (80 series against 335 seasons on the Piers box), so looking
            # episodes up by it matched whichever unrelated season happened to
            # carry that number as its idSeason.
            #
            # That is not theoretical: removing a stale Breaking Bad season
            # (idShow 10) deleted The Americans S4 (idSeason 10), and a stale
            # Yellowstone season (idShow 71) deleted Cheers S2 (idSeason 71) --
            # 35 episodes gone from Kodi and from kofin.db, with the victims'
            # own season rows left behind because only their children matched.
            #
            # The tvshow branch above already does this correctly: it sets
            # ParentId to season[1], the season's kodi_id, before the identical
            # lookup.
            season_obj = dict(obj, ParentId=obj["KodiId"])

            for episode in self.jellyfin_db.get_item_by_parent_id(
                *values(season_obj, QUEM.get_item_by_parent_episode_obj)
            ):
                self.remove_episode(episode[1], episode[2], obj["Id"])
            else:
                self.jellyfin_db.remove_items_by_parent_id(
                    *values(season_obj, QUEM.delete_item_by_parent_episode_obj)
                )

            self.remove_season(obj["KodiId"], obj["Id"])

            if not self.jellyfin_db.get_item_by_parent_id(
                *values(obj, QUEM.delete_item_by_parent_season_obj)
            ):

                self.remove_tvshow(obj["ParentId"], obj["Id"])
                self.jellyfin_db.remove_item_by_kodi_id(
                    *values(obj, QUEM.delete_item_by_parent_tvshow_obj)
                )

        # Remove any series pooling episodes
        for episode in self.jellyfin_db.get_media_by_parent_id(obj["Id"]):
            self.remove_episode(episode[2], episode[3], obj["Id"])
        else:
            self.jellyfin_db.remove_media_by_parent_id(obj["Id"])

        self.jellyfin_db.remove_item(*values(obj, QUEM.delete_item_obj))

    def remove_tvshow(self, kodi_id, item_id):

        self.artwork.delete(kodi_id, "tvshow")
        self.delete_tvshow(kodi_id)
        LOG.debug("DELETE tvshow [%s] %s", kodi_id, item_id)

    def remove_season(self, kodi_id, item_id):

        self.artwork.delete(kodi_id, "season")
        self.delete_season(kodi_id)
        LOG.debug("DELETE season [%s] %s", kodi_id, item_id)

    def remove_episode(self, kodi_id, file_id, item_id):

        self.artwork.delete(kodi_id, "episode")
        self.delete_episode(kodi_id, file_id)
        LOG.debug("DELETE episode [%s/%s] %s", file_id, kodi_id, item_id)

    @jellyfin_item
    def get_child(self, item_id, e_item):
        """Get all child elements from tv show jellyfin id."""
        obj = {"Id": item_id}
        child = []

        try:
            obj["KodiId"] = e_item[0]
            obj["FileId"] = e_item[1]
            obj["ParentId"] = e_item[3]
            obj["Media"] = e_item[4]
        except TypeError:
            return child

        obj["ParentId"] = obj["KodiId"]

        for season in self.jellyfin_db.get_item_by_parent_id(
            *values(obj, QUEM.get_item_by_parent_season_obj)
        ):

            temp_obj = dict(obj)
            temp_obj["ParentId"] = season[1]
            child.append(season[0])

            for episode in self.jellyfin_db.get_item_by_parent_id(
                *values(temp_obj, QUEM.get_item_by_parent_episode_obj)
            ):
                child.append(episode[0])

        for episode in self.jellyfin_db.get_media_by_parent_id(obj["Id"]):
            child.append(episode[0])

        return child
