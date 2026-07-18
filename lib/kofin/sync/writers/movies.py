# -*- coding: utf-8 -*-
"""Movie/boxset writer (fork ``objects/movies.py`` port). Adaptations per
plan §3: imports/shims, addon id and path base, ``direct_path`` branches
stripped (plugin mode only — ``get_path_filename`` keeps the plugin:// arm),
``self.server`` is the kofin Api."""

from urllib.parse import urlencode

from kofin.core.log import Logger
from kofin.sync import downloader as server
from kofin.sync import kofindb as jellyfin_db
from kofin.sync import queries_map as QUEM
from kofin.sync import fields as api
from kofin.sync import schema
from kofin.sync.fields import check_unchanged, find_library
from kofin.sync.shims import stop, jellyfin_item, values, Local

from kofin.sync.obj import Objects
from kofin.sync.kodidb import Movies as KodiDb
from kofin.sync.kodidb import queries as QU

##################################################################################################

LOG = Logger(__name__)

##################################################################################################


class Movies(KodiDb):

    def __init__(self, server, jellyfindb, videodb, library=None):

        self.server = server
        self.jellyfin = jellyfindb
        self.video = videodb
        # Native mode is gone; the flag stays because the checksum format
        # bakes it in ("<etag>|plugin") and check_unchanged reads it.
        self.direct_path = False

        self.jellyfin_db = jellyfin_db.JellyfinDatabase(jellyfindb.cursor)
        self.objects = Objects()
        self.item_ids = []
        self.library = library

        KodiDb.__init__(self, videodb.cursor)

    @stop
    @jellyfin_item
    def movie(self, item, e_item):
        """If item does not exist, entry will be added.
        If item exists, entry will be updated.
        """
        server_address = self.server.server
        API = api.API(item, server_address)
        obj = self.objects.map(item, "Movie")
        update = True

        try:
            obj["MovieId"] = e_item[0]
            obj["FileId"] = e_item[1]
            obj["PathId"] = e_item[2]
            obj["LibraryId"] = e_item[6]
            obj["LibraryName"] = self.jellyfin_db.get_view_name(obj["LibraryId"])
        except TypeError:
            update = False
            LOG.debug("MovieId %s not found", obj["Id"])

            library = self.library or find_library(self.server, item)
            if not library:
                # This item doesn't belong to a whitelisted library
                return

            obj["MovieId"] = self.create_entry()
            obj["LibraryId"] = library["Id"]
            obj["LibraryName"] = library["Name"]
        else:
            if self.get(*values(obj, QU.get_movie_obj)) is None:

                update = False
                LOG.info(
                    "MovieId %s missing from kodi. repairing the entry.", obj["MovieId"]
                )

        if check_unchanged(self, obj, item, e_item, update):
            return False

        obj["Path"] = API.get_file_path(obj["Path"])
        obj["Genres"] = obj["Genres"] or []
        obj["Studios"] = [
            API.validate_studio(studio) for studio in (obj["Studios"] or [])
        ]
        obj["People"] = obj["People"] or []
        obj["Genre"] = " / ".join(obj["Genres"])
        obj["Writers"] = " / ".join(obj["Writers"] or [])
        obj["Directors"] = " / ".join(obj["Directors"] or [])
        obj["Plot"] = API.get_overview(obj["Plot"])
        obj["Mpaa"] = API.get_mpaa(obj["Mpaa"])
        obj["Resume"] = API.adjust_resume((obj["Resume"] or 0) / 10000000.0)
        obj["Runtime"] = round(float((obj["Runtime"] or 0) / 10000000.0), 6)
        obj["People"] = API.get_people_artwork(obj["People"])
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
        if obj["Premiere"] is not None:
            obj["Premiere"] = str(obj["Premiere"]).split("T")[0]

        self.get_path_filename(obj)
        self.trailer(obj)

        if obj["Countries"]:
            self.add_countries(*values(obj, QU.update_country_obj))

        tags = list(obj["Tags"] or [])
        tags.append(obj["LibraryName"])

        if obj["Favorite"]:
            tags.append("Favorite movies")

        obj["Tags"] = tags

        if update:
            self.movie_update(obj)
        else:
            self.movie_add(obj)

        self.update_path(*values(obj, QU.update_path_movie_obj))
        self.update_file(*values(obj, QU.update_file_obj))
        self.add_tags(*values(obj, QU.add_tags_movie_obj))
        self.add_genres(*values(obj, QU.add_genres_movie_obj))
        self.add_studios(*values(obj, QU.add_studios_movie_obj))
        self.add_playstate(*values(obj, QU.add_bookmark_obj))
        self.add_people(*values(obj, QU.add_people_movie_obj))
        self.add_streams(*values(obj, QU.add_streams_obj))
        self.artwork.add(obj["Artwork"], obj["MovieId"], "movie")
        self.extras(obj, item)
        self.item_ids.append(obj["Id"])

        return not update

    def movie_add(self, obj):
        """Add object to kodi."""
        obj["RatingId"] = self.create_entry_rating()
        self.add_ratings(*values(obj, QU.add_rating_movie_obj))

        obj["Unique"] = self.create_entry_unique_id()
        self.add_unique_id(*values(obj, QU.add_unique_id_movie_obj))

        obj["PathId"] = self.add_path(*values(obj, QU.add_path_obj))
        obj["FileId"] = self.add_file(*values(obj, QU.add_file_obj))
        obj["VideoVersionItemType"] = self.itemtype

        self.add(*values(obj, QU.add_movie_obj))
        self.add_videoversion(*values(obj, QU.add_video_version_obj))
        self.jellyfin_db.add_reference(*values(obj, QUEM.add_reference_movie_obj))
        LOG.debug(
            "ADD movie [%s/%s/%s] %s: %s",
            obj["PathId"],
            obj["FileId"],
            obj["MovieId"],
            obj["Id"],
            obj["Title"],
        )

    def movie_update(self, obj):
        """Update object to kodi."""
        obj["RatingId"] = self.get_rating_id(*values(obj, QU.get_rating_movie_obj))
        self.update_ratings(*values(obj, QU.update_rating_movie_obj))

        obj["Unique"] = self.get_unique_id(*values(obj, QU.get_unique_id_movie_obj))
        self.update_unique_id(*values(obj, QU.update_unique_id_movie_obj))

        self.update(*values(obj, QU.update_movie_obj))
        self.jellyfin_db.update_reference(*values(obj, QUEM.update_reference_obj))
        LOG.debug(
            "UPDATE movie [%s/%s/%s] %s: %s",
            obj["PathId"],
            obj["FileId"],
            obj["MovieId"],
            obj["Id"],
            obj["Title"],
        )

    def trailer(self, obj):

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

            LOG.exception("Failed to get trailer for movie %s: %s", obj["Id"], error)
            obj["Trailer"] = None

    def get_path_filename(self, obj):
        """Get the path and filename and build it into protocol://path"""
        obj["Filename"] = (
            obj["Path"].rsplit("\\", 1)[1]
            if "\\" in obj["Path"]
            else obj["Path"].rsplit("/", 1)[1]
        )

        obj["Path"] = "plugin://plugin.video.kofin/%s/" % obj["LibraryId"]
        params = {
            "filename": obj["Filename"],
            "id": obj["Id"],
            "dbid": obj["MovieId"],
            "mode": "play",
        }
        obj["Filename"] = "%s?%s" % (obj["Path"], urlencode(params))

    def extras(self, obj, item):
        """Sync special features as native Kodi extras: one ``files`` +
        ``videoversion`` row per feature (plan §2 — movies are native,
        ``itemType`` = the schema-keyed EXTRA constant). Upserts against the
        stored play URLs so an unchanged set writes nothing; best-effort —
        a failed fetch or write never gates the movie sync."""
        item_type = self.extra_itemtype
        if item_type is None:
            return

        try:
            existing = {
                row[1]: row[0]  # strFilename -> idFile
                for row in self.get_extra_assets(obj["MovieId"], item_type)
            }
            count = item.get("SpecialFeatureCount") or 0
            if not count and not existing:
                return

            features = self.server.special_features(obj["Id"]) if count else []
            desired = {}
            for feature in features:
                if feature.get("Id"):
                    desired[self.extra_filename(obj, feature)] = feature

            for filename, file_id in existing.items():
                if filename not in desired:
                    self.delete_extra_asset(file_id)
                    LOG.debug("DELETE extra [%s] %s", file_id, obj["Id"])

            for filename, feature in desired.items():
                if filename in existing:
                    continue
                name = schema.extra_type_name(feature.get("ExtraType"))
                type_id = self.get_extra_type_id(name, item_type)
                file_id = self.add_extra_asset(
                    obj["PathId"],
                    filename,
                    obj["DateAdded"],
                    obj["MovieId"],
                    item_type,
                    type_id,
                )
                LOG.debug(
                    "ADD extra [%s/%s] %s: %s",
                    file_id,
                    name,
                    obj["Id"],
                    feature.get("Name"),
                )
        except Exception as error:
            LOG.exception("extras failed for %s: %s", obj["Id"], error)

    def extra_filename(self, obj, feature):
        """The plugin play URL stored as the extra's files row (same
        path-identity convention as the movie's own file)."""
        path = feature.get("Path") or ""
        basename = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        params = {
            "filename": basename or "%s.extra" % feature["Id"],
            "id": feature["Id"],
            "mode": "play",
        }
        return "%s?%s" % (obj["Path"], urlencode(params))

    @stop
    @jellyfin_item
    def boxset(self, item, e_item):
        """If item does not exist, entry will be added.
        If item exists, entry will be updated.

        Process movies inside boxset.
        Process removals from boxset.
        """
        server_address = self.server.server
        API = api.API(item, server_address)
        obj = self.objects.map(item, "Boxset")

        if check_unchanged(
            self, obj, item, e_item, e_item is not None, apply_userdata=False
        ):
            return

        obj["Overview"] = API.get_overview(obj["Overview"])

        try:
            obj["SetId"] = e_item[0]
            self.update_boxset(*values(obj, QU.update_set_obj))
        except TypeError:
            LOG.debug("SetId %s not found", obj["Id"])
            obj["SetId"] = self.add_boxset(*values(obj, QU.add_set_obj))

        self.boxset_current(obj)
        obj["Artwork"] = API.get_all_artwork(self.objects.map(item, "Artwork"))

        for movie in obj["Current"]:

            temp_obj = dict(obj)
            temp_obj["Movie"] = movie
            temp_obj["MovieId"] = obj["Current"][temp_obj["Movie"]]
            self.remove_from_boxset(*values(temp_obj, QU.delete_movie_set_obj))
            self.jellyfin_db.update_parent_id(
                *values(temp_obj, QUEM.delete_parent_boxset_obj)
            )
            LOG.debug(
                "DELETE from boxset [%s] %s: %s",
                temp_obj["SetId"],
                temp_obj["Title"],
                temp_obj["MovieId"],
            )

        self.artwork.add(obj["Artwork"], obj["SetId"], "set")
        self.jellyfin_db.add_reference(*values(obj, QUEM.add_reference_boxset_obj))
        LOG.debug("UPDATE boxset [%s] %s", obj["SetId"], obj["Title"])

    def boxset_current(self, obj):
        """Add or removes movies based on the current movies found in the boxset."""
        try:
            current = self.jellyfin_db.get_item_id_by_parent_id(
                *values(obj, QUEM.get_item_id_by_parent_boxset_obj)
            )
            movies = dict(current)
        except ValueError:
            movies = {}

        obj["Current"] = movies

        for all_movies in server.get_movies_by_boxset(self.server, obj["Id"]):
            for movie in all_movies["Items"]:

                temp_obj = dict(obj)
                temp_obj["Title"] = movie["Name"]
                temp_obj["Id"] = movie["Id"]

                try:
                    temp_obj["MovieId"] = self.jellyfin_db.get_item_by_id(
                        *values(temp_obj, QUEM.get_item_obj)
                    )[0]
                except TypeError:
                    LOG.info("Failed to process %s to boxset.", temp_obj["Title"])

                    continue

                if temp_obj["Id"] not in obj["Current"]:

                    self.set_boxset(*values(temp_obj, QU.update_movie_set_obj))
                    self.jellyfin_db.update_parent_id(
                        *values(temp_obj, QUEM.update_parent_movie_obj)
                    )
                    LOG.debug(
                        "ADD to boxset [%s/%s] %s: %s to boxset",
                        temp_obj["SetId"],
                        temp_obj["MovieId"],
                        temp_obj["Title"],
                        temp_obj["Id"],
                    )
                else:
                    obj["Current"].pop(temp_obj["Id"])

    def boxsets_reset(self):
        """Special function to remove all existing boxsets."""
        boxsets = self.jellyfin_db.get_items_by_media("set")
        for boxset in boxsets:
            self.remove(boxset[0])

    @stop
    @jellyfin_item
    def userdata(self, item, e_item):
        """This updates: Favorite, LastPlayedDate, Playcount, PlaybackPositionTicks
        Poster with progress bar
        """
        server_address = self.server.server
        API = api.API(item, server_address)
        obj = self.objects.map(item, "MovieUserData")

        try:
            obj["MovieId"] = e_item[0]
            obj["FileId"] = e_item[1]
        except TypeError:
            return

        obj["Resume"] = API.adjust_resume((obj["Resume"] or 0) / 10000000.0)
        obj["Runtime"] = self.resolve_runtime(obj["Runtime"], obj["FileId"])
        obj["PlayCount"] = API.get_playcount(obj["Played"], obj["PlayCount"])

        if obj["DatePlayed"]:
            obj["DatePlayed"] = Local(obj["DatePlayed"]).split(".")[0].replace("T", " ")

        if obj["Favorite"]:
            self.get_tag(*values(obj, QU.get_tag_movie_obj))
        else:
            self.remove_tag(*values(obj, QU.delete_tag_movie_obj))

        LOG.debug("New resume point %s: %s", obj["Id"], obj["Resume"])
        self.add_playstate(*values(obj, QU.add_bookmark_obj))
        # The reference checksum tracks metadata state (Etag); userdata
        # changes must not overwrite it.
        LOG.debug(
            "USERDATA movie [%s/%s] %s: %s",
            obj["FileId"],
            obj["MovieId"],
            obj["Id"],
            obj["Title"],
        )

    @stop
    @jellyfin_item
    def remove(self, item_id, e_item):
        """Remove movieid, fileid, jellyfin reference.
        Remove artwork, boxset
        """
        obj = {"Id": item_id}

        try:
            obj["KodiId"] = e_item[0]
            obj["FileId"] = e_item[1]
            obj["Media"] = e_item[4]
        except TypeError:
            return

        self.artwork.delete(obj["KodiId"], obj["Media"])

        if obj["Media"] == "movie":
            self.remove_extras(obj["KodiId"])
            self.delete(*values(obj, QU.delete_movie_obj))
        elif obj["Media"] == "set":

            for movie in self.jellyfin_db.get_item_by_parent_id(
                *values(obj, QUEM.get_item_by_parent_movie_obj)
            ):

                temp_obj = dict(obj)
                temp_obj["MovieId"] = movie[1]
                temp_obj["Movie"] = movie[0]
                self.remove_from_boxset(*values(temp_obj, QU.delete_movie_set_obj))
                self.jellyfin_db.update_parent_id(
                    *values(temp_obj, QUEM.delete_parent_boxset_obj)
                )

            self.delete_boxset(*values(obj, QU.delete_set_obj))

        self.jellyfin_db.remove_item(*values(obj, QUEM.delete_item_obj))
        LOG.debug(
            "DELETE %s [%s/%s] %s",
            obj["Media"],
            obj["FileId"],
            obj["KodiId"],
            obj["Id"],
        )

    def remove_extras(self, movie_id):
        """Drop every extras asset of a movie (the movie delete trigger only
        cascades the movie's own file, not the extra files rows)."""
        item_type = self.extra_itemtype
        if item_type is None:
            return

        for row in self.get_extra_assets(movie_id, item_type):
            self.delete_extra_asset(row[0])
