# -*- coding: utf-8 -*-
"""Movie/boxset writer (fork ``objects/movies.py`` port). Adaptations per
plan §3: imports/shims, addon id and path base, ``direct_path`` branches
stripped (plugin mode only — ``get_path_filename`` keeps the plugin:// arm),
``self.server`` is the kofin Api."""

from urllib.parse import urlencode

from kofin.core import settings
from kofin.core.log import Logger
from kofin.sync import downloader as server
from kofin.sync import kofindb as jellyfin_db
from kofin.sync import queries_map as QUEM
from kofin.sync import fields as api
from kofin.sync import schema
from kofin.sync.fields import check_unchanged, find_library, streams_and_runtime
from kofin.sync.shims import stop, jellyfin_item, values, Local

from kofin.sync.obj import Objects
from kofin.sync.kodidb import Movies as KodiDb
from kofin.sync.kodidb import queries as QU
from kofin.downloads import TAG as DOWNLOADS_TAG
from kofin.downloads import repoint as downloads_repoint
from kofin.downloads import store as downloads_store

##################################################################################################

LOG = Logger(__name__)

# Outcome codes boxset() reports back to the walk's summary line
# (full_sync.boxsets). Strings, not an enum: they are logged as-is.
BOXSET_UNCHANGED = "unchanged"
BOXSET_WRITTEN = "written"
BOXSET_HEALED = "healed"
BOXSET_GUARDED = "guarded"

##################################################################################################


def server_children(item):
    """The DTO's own claim about a set's child count.

    ``ChildCount`` when the fetch asked for it (the boxsets walk does), else
    ``RecursiveItemCount`` — already in ``info()``, but it recurses into a
    series' seasons and episodes, so it is only a non-empty signal — else
    None for unknown. Direct children of any type count (a mixed set of
    series reads > 0 with zero movie members), which is why the callers only
    ever compare against zero.
    """
    count = item.get("ChildCount")

    return item.get("RecursiveItemCount") if count is None else count


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
        # Memo for find_library, per writer instance (see fields.find_library).
        self.library_cache = {}
        # Ids this writer declined to write, so the caller can tell a
        # refusal from a write. A writer refuses by returning early, and
        # the return value cannot carry the news: it already means
        # something else (tvshow returns None on unchanged *so that* full
        # sync still walks its episodes). See UpdateWorker's notify gate.
        self.refused = set()
        # Which rating row Kodi treats as the default. Read once per writer
        # rather than per movie (settings.get_bool builds a fresh
        # xbmcaddon.Addon each call); a flip mid-sync is picked up by the next
        # writer, and for everything already written by the settings handler's
        # repoint pass (service/settings_apply.py).
        self.rating_type = (
            "critic" if settings.get_bool("preferCriticRating") else "default"
        )

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

            library = self.library or find_library(
                self.server, item, self.library_cache
            )
            if not library:
                # This item doesn't belong to a whitelisted library
                self.refused.add(obj["Id"])
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

        # Downloaded items carry the downloads tag through every rewrite the
        # same way favorites do: add_tags below replaces the set wholesale,
        # so an out-of-band stamp dies on the next pass without this
        # (docs/offline-downloads-plan.md W1.8; the Downloads node filters
        # on the tag).
        if downloads_store.is_done_on(self.jellyfin_db.cursor, obj["Id"]):
            tags.append(DOWNLOADS_TAG)

        obj["Tags"] = tags

        primary, _alternates = self.split_media_sources(item)
        obj["VideoVersionItemType"] = self.itemtype
        obj["VideoVersionTypeId"] = self.resolve_version_type(
            primary.get("Name") if primary else None
        )

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
        self.versions(obj, item)
        self.extras(obj, item)
        # A changed item's rewrite put the file row back in writer shape;
        # a downloaded one is re-pointed at its local file before the page
        # commits, with the fresh URL recaptured for restore (plan W1.8 —
        # the L2 suite pins both halves).
        downloads_repoint.reassert_on(self.cursor, self.jellyfin_db.cursor, obj["Id"])
        self.item_ids.append(obj["Id"])

        return not update

    def movie_add(self, obj):
        """Add object to kodi."""
        obj["RatingId"] = self.sync_ratings(
            obj["MovieId"], api.ratings(obj), self.rating_type
        )

        obj["Unique"] = self.create_entry_unique_id()
        self.add_unique_id(*values(obj, QU.add_unique_id_movie_obj))

        obj["PathId"] = self.add_path(*values(obj, QU.add_path_obj))
        obj["FileId"] = self.add_file(*values(obj, QU.add_file_obj))
        obj["VideoVersionItemType"] = self.itemtype
        obj.setdefault("VideoVersionTypeId", 40400)

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
        obj["RatingId"] = self.sync_ratings(
            obj["MovieId"], api.ratings(obj), self.rating_type
        )

        obj["Unique"] = self.get_unique_id(*values(obj, QU.get_unique_id_movie_obj))
        self.update_unique_id(*values(obj, QU.update_unique_id_movie_obj))

        self.update(*values(obj, QU.update_movie_obj))
        self.set_video_version_type(obj["FileId"], obj.get("VideoVersionTypeId", 40400))
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

    @staticmethod
    def split_media_sources(item):
        """(primary MediaSource or None, alternate MediaSources).

        Primary is the source whose Id matches the item Id when present,
        otherwise the first source (Jellyfin's usual default). A non-list
        MediaSources payload is treated as empty so a bad DTO cannot sink
        the movie write before the versions pass's try/except.
        """
        raw = item.get("MediaSources")
        if not isinstance(raw, list) or not raw:
            return None, []
        sources = raw
        item_id = item.get("Id")
        primary = None
        for source in sources:
            if isinstance(source, dict) and source.get("Id") == item_id:
                primary = source
                break
        if primary is None:
            primary = sources[0] if isinstance(sources[0], dict) else None
            if primary is None:
                return None, []
        primary_id = primary.get("Id")
        alternates = [
            source
            for source in sources
            if isinstance(source, dict)
            and source.get("Id")
            and source.get("Id") != primary_id
        ]
        return primary, alternates

    def versions(self, obj, item):
        """Sync alternate MediaSources as native Kodi video versions
        (``itemType`` = VERSION from the seeded 40400 row). Primary type is
        set in movie_add/update; this pass only manages non-primary files.
        Best-effort — never gates the movie sync."""
        try:
            _primary, alternates = self.split_media_sources(item)
            existing = {
                row[1]: row[0]  # strFilename -> idFile
                for row in self.get_extra_assets(obj["MovieId"], self.itemtype)
                if row[0] != obj["FileId"]  # never touch the primary file
            }
            if not alternates and not existing:
                return

            desired = {}
            for source in alternates:
                desired[self.version_filename(obj, source)] = source

            for filename, file_id in existing.items():
                if filename not in desired:
                    self.delete_extra_asset(file_id)
                    LOG.debug("DELETE version [%s] %s", file_id, obj["Id"])

            for filename, source in desired.items():
                if filename in existing:
                    file_id = existing[filename]
                else:
                    type_id = self.resolve_version_type(source.get("Name"))
                    file_id = self.add_extra_asset(
                        obj["PathId"],
                        filename,
                        obj["DateAdded"],
                        obj["MovieId"],
                        self.itemtype,
                        type_id,
                    )
                    LOG.debug(
                        "ADD version [%s/%s] %s: %s",
                        file_id,
                        type_id,
                        obj["Id"],
                        source.get("Name"),
                    )
                streams, runtime = streams_and_runtime(source)
                self.add_streams(file_id, streams, runtime)
        except Exception as error:
            LOG.exception("versions failed for %s: %s", obj["Id"], error)

    def version_filename(self, obj, source):
        """Plugin play URL for an alternate MediaSource (movie id +
        mediasourceid so play resolves the right source)."""
        path = source.get("Path") or ""
        basename = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        params = {
            "filename": basename or "%s.version" % source["Id"],
            "id": obj["Id"],
            "mediasourceid": source["Id"],
            "mode": "play",
        }
        return "%s?%s" % (obj["Path"], urlencode(params))

    def extras(self, obj, item):
        """Sync special features as native Kodi extras: one ``files`` +
        ``videoversion`` row per feature (plan §2 — movies are native,
        ``itemType`` = the schema-keyed EXTRA constant). Upserts against the
        stored play URLs; streamdetails (including duration) are always
        rewritten for the desired set so the extras UI does not fall back to
        the film's runtime. Best-effort — a failed fetch or write never
        gates the movie sync."""
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
                    file_id = existing[filename]
                else:
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
                # Always (re)write streams so duration is correct even for
                # extras added before this fix (on the next movie update).
                streams, runtime = streams_and_runtime(feature)
                self.add_streams(file_id, streams, runtime)
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

        Deliberate deviations from the fork (docs/boxsets-robustness-plan.md):
        membership can drift while the set's Etag stands still (a member
        removed and re-added arrives as a fresh movie row with no idSet), so
        an Etag match alone no longer skips the pass — it must also pass
        boxset_healthy. A suspicious empty membership answer never
        mass-unlinks, and a pass that ends with zero members leaves the
        reference checksum unstamped so the set is re-verified every walk.

        Returns one of the BOXSET_* outcome codes for the walk's summary.
        """
        server_address = self.server.server
        API = api.API(item, server_address)
        obj = self.objects.map(item, "Boxset")

        force_relink = False

        if check_unchanged(
            self, obj, item, e_item, e_item is not None, apply_userdata=False
        ):
            if self.boxset_healthy(obj, item, e_item):
                return BOXSET_UNCHANGED

            force_relink = True
            LOG.info("healing boxset [%s] %s", obj["Id"], obj["Title"])

        obj["Overview"] = API.get_overview(obj["Overview"])
        obj["SetId"] = e_item[0] if e_item else None

        if obj["SetId"] is not None and self.get_boxset(obj["SetId"]) is None:
            # The fork keyed update-vs-add on the reference row alone; a sets
            # row deleted underneath it (Kodi's clean-library drops memberless
            # sets) took the update leg and UPDATEd nothing, leaving links
            # pointing at a dead id. Clear those and take the add leg.
            LOG.info("SetId %s missing from kodi. repairing the entry.", obj["SetId"])

            for movie in self.jellyfin_db.get_item_id_by_parent_id(
                obj["SetId"], "movie"
            ):
                temp_obj = dict(obj)
                temp_obj["Movie"] = movie[0]
                temp_obj["MovieId"] = movie[1]
                self.remove_from_boxset(*values(temp_obj, QU.delete_movie_set_obj))
                self.jellyfin_db.update_parent_id(
                    *values(temp_obj, QUEM.delete_parent_boxset_obj)
                )

            obj["SetId"] = None
            force_relink = True

        if obj["SetId"] is None:
            LOG.debug("SetId %s not found", obj["Id"])
            obj["SetId"] = self.add_boxset(*values(obj, QU.add_set_obj))
        else:
            self.update_boxset(*values(obj, QU.update_set_obj))

        fetched = self.boxset_current(obj, force_relink)
        obj["Artwork"] = API.get_all_artwork(self.objects.map(item, "Artwork"))
        children = server_children(item)

        if obj["Current"] and not fetched and children != 0:
            # 200-with-zero-items while the server says the set has children
            # (or will not say): permission and filter edges look exactly
            # like this, and unlinking on it is how a whole library's sets go
            # empty in one pass. Keep the links and the old checksum — a
            # changed Etag retries on the next walk — and warn once per set.
            LOG.warning(
                "boxset [%s] %s: server returned 0 members but reports %s "
                "children; keeping %s local link(s). A permissions or filter "
                "change can cause this; Refresh boxsets forces a relink.",
                obj["Id"],
                obj["Title"],
                "unknown" if children is None else children,
                len(obj["Current"]),
            )
            self.artwork.add(obj["Artwork"], obj["SetId"], "set")

            return BOXSET_GUARDED

        if obj["Current"] and not fetched:
            LOG.info(
                "boxset [%s] %s emptied server-side; unlinking %s member(s)",
                obj["Id"],
                obj["Title"],
                len(obj["Current"]),
            )
        elif obj["Current"]:
            LOG.info(
                "unlinking %s member(s) no longer in boxset [%s] %s",
                len(obj["Current"]),
                obj["Id"],
                obj["Title"],
            )

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

        linked = self.get_boxset_movie_count(obj["SetId"])
        self.jellyfin_db.add_boxset_state(obj["Id"], linked)

        if not linked:
            # A memberless pass never stamps its checksum: the reference row
            # is still written (mapping and removal dispatch need it) but
            # with a NULL checksum, so every walk re-verifies membership and
            # the set springs back the moment the server shows members again
            # (a permission flap restoring) without any Etag movement.
            obj["Checksum"] = None

        self.jellyfin_db.add_reference(*values(obj, QUEM.add_reference_boxset_obj))
        LOG.debug("UPDATE boxset [%s] %s", obj["SetId"], obj["Title"])

        return BOXSET_HEALED if force_relink else BOXSET_WRITTEN

    def boxset_healthy(self, obj, item, e_item):
        """Whether an Etag-matched set may skip the membership pass.

        Local-only checks: the sets row exists, the MyVideos link count
        equals the count stamped at the last successful pass, and kofin.db's
        parent rows agree. A missing state row (first pass after upgrade) is
        unhealthy on purpose — the one-time relink migration. Zero members
        while the DTO itself reports children is never healthy, Etag or no
        Etag.
        """
        set_id = e_item[0]

        if self.get_boxset(set_id) is None:
            return False

        linked = self.get_boxset_movie_count(set_id)
        stored = self.jellyfin_db.get_boxset_state(obj["Id"])

        if stored is None or stored != linked:
            return False

        if len(self.jellyfin_db.get_item_id_by_parent_id(set_id, "movie")) != linked:
            return False

        return bool(linked) or not server_children(item)

    def boxset_current(self, obj, force=False):
        """Add or removes movies based on the current movies found in the boxset.

        Returns how many members the server yielded. ``force`` rewrites every
        link even when kofin.db already claims it (heal mode: MyVideos can be
        damaged while the mapping still looks right, and the normal diff
        would pop the member as current and fix nothing).
        """
        try:
            current = self.jellyfin_db.get_item_id_by_parent_id(
                *values(obj, QUEM.get_item_id_by_parent_boxset_obj)
            )
            movies = dict(current)
        except ValueError:
            movies = {}

        obj["Current"] = movies
        fetched = 0
        unsynced = 0

        for all_movies in server.get_movies_by_boxset(self.server, obj["Id"]):
            for movie in all_movies["Items"]:

                fetched += 1
                temp_obj = dict(obj)
                temp_obj["Title"] = movie["Name"]
                temp_obj["Id"] = movie["Id"]

                try:
                    temp_obj["MovieId"] = self.jellyfin_db.get_item_by_id(
                        *values(temp_obj, QUEM.get_item_obj)
                    )[0]
                except TypeError:
                    # Routine for members outside the synced libraries;
                    # counted below instead of shouting per member.
                    LOG.debug("boxset member %s not synced, skipped", temp_obj["Id"])
                    unsynced += 1

                    continue

                if force or temp_obj["Id"] not in obj["Current"]:

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

                obj["Current"].pop(temp_obj["Id"], None)

        if unsynced:
            LOG.debug(
                "boxset [%s] %s: %s member(s) not in any synced library",
                obj["Id"],
                obj["Title"],
                unsynced,
            )

        return fetched

    def restamp_boxset_states(self, guarded_ids=None):
        """Re-stamp every set's boxset_state from post-walk reality.

        The per-set stamp in boxset() measures mid-walk, and movie.idSet is
        single-valued: a later set's pass steals a member the sets share, so
        the earlier set's stored count is one high the moment the walk ends
        -- the startup probe then schedules a heal every service start that
        can never converge (V7, docs/healing-loops-plan.md). Measured once
        after the walk stops moving rows, with the same queries the probe
        reads, stored equals reality for every set including both sides of a
        steal.

        Guarded sets are excluded on purpose: their stale or missing state is
        the designed retry-every-walk, and restamping would grade a
        suspicious server answer as healthy.
        """
        guarded = guarded_ids or set()
        counts = self.get_boxset_movie_counts()
        restamped = 0

        for jellyfin_id, kodi_id in self.jellyfin_db.get_item_ids_by_media("set"):

            if jellyfin_id in guarded:
                continue

            self.jellyfin_db.add_boxset_state(jellyfin_id, counts.get(kodi_id, 0))
            restamped += 1

        LOG.debug("restamped %s boxset state(s)", restamped)

        return restamped

    def boxsets_reset(self):
        """Special function to remove all existing boxsets."""
        boxsets = self.jellyfin_db.get_items_by_media("set")
        for boxset in boxsets:
            self.remove(boxset[0])

        # remove() drops each set's own state row; this catches strays whose
        # reference was already gone (a state row can never outlive a reset).
        self.jellyfin_db.remove_boxset_states()

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
            self.remove_versions(obj["KodiId"], obj["FileId"])
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
            self.jellyfin_db.remove_boxset_state(obj["Id"])

        self.jellyfin_db.remove_item(*values(obj, QUEM.delete_item_obj))
        LOG.debug(
            "DELETE %s [%s/%s] %s",
            obj["Media"],
            obj["FileId"],
            obj["KodiId"],
            obj["Id"],
        )

    def remove_versions(self, movie_id, primary_file_id):
        """Drop alternate VERSION asset files (not the primary movie file)."""
        for row in self.get_extra_assets(movie_id, self.itemtype):
            if row[0] != primary_file_id:
                self.delete_extra_asset(row[0])

    def remove_extras(self, movie_id):
        """Drop every extras asset of a movie (the movie delete trigger only
        cascades the movie's own file, not the extra files rows)."""
        item_type = self.extra_itemtype
        if item_type is None:
            return

        for row in self.get_extra_assets(movie_id, item_type):
            self.delete_extra_asset(row[0])
