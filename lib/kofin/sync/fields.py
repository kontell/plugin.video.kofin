# -*- coding: utf-8 -*-
"""Item-shaping helpers the writers depend on (fork ``helper/api.py`` plus
the checksum/skip logic from ``objects/utils.py``).

Adaptations (plan §3): native-mode path replacement dropped (plugin paths
only); artwork-quality settings read kofin's Sync tab ids (``compressArt``,
``maxArtResolution`` where 0 means original size);
``find_library`` resolves through the kofin Api and sync.json whitelist.
"""

from kofin.core import settings
from kofin.core.log import Logger

LOG = Logger(__name__)


class API(object):
    def __init__(self, item, server=None):
        """Get item information in special cases.
        server is the server address, provide if your functions requires it.
        """
        self.item = item
        self.server = server

    def get_playcount(self, played, playcount):
        """Convert Jellyfin played/playcount into
        the Kodi equivalent. The playcount is tied to the watch status.
        """
        return (playcount or 1) if played else None

    def get_naming(self):

        if self.item["Type"] == "Episode" and "SeriesName" in self.item:
            return "%s: %s" % (self.item["SeriesName"], self.item["Name"])

        elif self.item["Type"] == "MusicAlbum" and "AlbumArtist" in self.item:
            return "%s: %s" % (self.item["AlbumArtist"], self.item["Name"])

        elif self.item["Type"] == "Audio" and self.item.get("Artists"):
            return "%s: %s" % (self.item["Artists"][0], self.item["Name"])

        return self.item["Name"]

    def media_streams(self, video, audio, subtitles):
        return {"video": video or [], "audio": audio or [], "subtitle": subtitles or []}

    def video_streams(self, tracks, container=None):

        if container:
            container = container.split(",")[0]

        for track in tracks:

            if "DvProfile" in track:
                track["hdrtype"] = "dolbyvision"
            elif track.get("VideoRangeType", "") in ["HDR10", "HDR10Plus"]:
                track["hdrtype"] = "hdr10"
            elif "HLG" in track.get("VideoRangeType", ""):
                track["hdrtype"] = "hlg"

            track.update(
                {
                    "hdrtype": track.get("hdrtype", "").lower(),
                    "codec": track.get("Codec", "").lower(),
                    "profile": track.get("Profile", "").lower(),
                    "height": track.get("Height"),
                    "width": track.get("Width"),
                    "3d": self.item.get("Video3DFormat"),
                    "aspect": 1.85,
                }
            )

            if "msmpeg4" in track["codec"]:
                track["codec"] = "divx"

            elif "mpeg4" in track["codec"] and (
                "simple profile" in track["profile"] or not track["profile"]
            ):
                track["codec"] = "xvid"

            elif "h264" in track["codec"] and container in ("mp4", "mov", "m4v"):
                track["codec"] = "avc1"

            try:
                width, height = self.item.get(
                    "AspectRatio", track.get("AspectRatio", "0")
                ).split(":")
                track["aspect"] = round(float(width) / float(height), 6)
            except (ValueError, ZeroDivisionError):

                if track["width"] and track["height"]:
                    track["aspect"] = round(float(track["width"] / track["height"]), 6)

            track["duration"] = self.get_runtime()

        return tracks

    def audio_streams(self, tracks):

        for track in tracks:

            track.update(
                {
                    "codec": track.get("Codec", "").lower(),
                    "profile": track.get("Profile", "").lower(),
                    "channels": track.get("Channels"),
                    "language": track.get("Language"),
                }
            )

            if "dts-hd ma" in track["profile"]:
                track["codec"] = "dtshd_ma"

            elif "dts-hd hra" in track["profile"]:
                track["codec"] = "dtshd_hra"

        return tracks

    def get_runtime(self):

        try:
            runtime = self.item["RunTimeTicks"] / 10000000.0

        except KeyError:
            runtime = self.item.get("CumulativeRunTimeTicks", 0) / 10000000.0

        return runtime

    @classmethod
    def adjust_resume(cls, resume_seconds):

        resume = 0
        if resume_seconds:
            # The jumpback rule itself lives in core.settings so the bookmark
            # written here, the resume point a listing advertises and the
            # position playback starts at cannot drift apart.
            resume = settings.adjusted_resume(round(float(resume_seconds), 6))

        return resume

    def validate_studio(self, studio_name):
        # Convert studio for Kodi to properly detect them
        studios = {
            "abc (us)": "ABC",
            "fox (us)": "FOX",
            "mtv (us)": "MTV",
            "showcase (ca)": "Showcase",
            "wgn america": "WGN",
            "bravo (us)": "Bravo",
            "tnt (us)": "TNT",
            "comedy central": "Comedy Central (US)",
        }
        return studios.get(studio_name.lower(), studio_name)

    def get_overview(self, overview=None):

        overview = overview or self.item.get("Overview")

        if not overview:
            return

        overview = overview.replace('"', "'")
        overview = overview.replace("\n", "[CR]")
        overview = overview.replace("\r", " ")
        overview = overview.replace("<br>", "[CR]")

        return overview

    def get_mpaa(self, rating=None):

        mpaa = rating or self.item.get("OfficialRating", "")

        if mpaa in ("NR", "UR"):
            # Kodi seems to not like NR, but will accept Not Rated
            mpaa = "Not Rated"

        if "FSK-" in mpaa:
            mpaa = mpaa.replace("-", " ")

        return mpaa

    def get_file_path(self, path=None):

        if path is None:
            path = self.item.get("Path")

        if not path:
            return ""

        if path.startswith("\\\\"):
            path = (
                path.replace("\\\\", "smb://", 1)
                .replace("\\\\", "\\")
                .replace("\\", "/")
            )

        if "Container" in self.item:

            if self.item["Container"] == "dvd":
                path = "%s/VIDEO_TS/VIDEO_TS.IFO" % path
            elif self.item["Container"] == "bluray":
                path = "%s/BDMV/index.bdmv" % path

        path = path.replace("\\\\", "\\")

        if "\\" in path:
            path = path.replace("/", "\\")

        if "://" in path:
            protocol = path.split("://")[0]
            path = path.replace(protocol, protocol.lower())

        return path

    def get_user_artwork(self, user_id):
        """Get jellyfin user profile picture."""
        return "%s/UserImage?userId=%s&Format=original" % (self.server, user_id)

    def get_people_artwork(self, people):
        """Get people (actor, director, etc) artwork."""
        for person in people:

            if "PrimaryImageTag" in person:

                query = "&MaxWidth=400&MaxHeight=400&Index=0"
                person["imageurl"] = self.get_artwork(
                    person["Id"], "Primary", person["PrimaryImageTag"], query
                )
            else:
                person["imageurl"] = None

        return people

    def get_all_artwork(self, obj, parent_info=False):
        """Get all artwork possible. If parent_info is True,
        it will fill missing artwork with parent artwork.

        obj is from objects.Objects().map(item, 'Artwork')
        """
        query = ""
        all_artwork = {
            "Primary": "",
            "BoxRear": "",
            "Art": "",
            "Banner": "",
            "Logo": "",
            "Thumb": "",
            "Disc": "",
            "Backdrop": [],
        }

        if settings.get_bool("compressArt"):
            query = "&Quality=90"

        maxheight = settings.get_int("maxArtResolution")
        if maxheight:
            query += "&MaxHeight=%d" % maxheight

        all_artwork["Backdrop"] = self.get_backdrops(
            obj["Id"], obj["BackdropTags"] or [], query
        )

        for artwork in obj["Tags"] or []:
            all_artwork[artwork] = self.get_artwork(
                obj["Id"], artwork, obj["Tags"][artwork], query
            )

        if parent_info:

            if not all_artwork["Backdrop"] and obj["ParentBackdropId"]:
                all_artwork["Backdrop"] = self.get_backdrops(
                    obj["ParentBackdropId"], obj["ParentBackdropTags"], query
                )

            for art in ("Logo", "Art", "Thumb"):
                if not all_artwork[art] and obj["Parent%sId" % art]:
                    all_artwork[art] = self.get_artwork(
                        obj["Parent%sId" % art], art, obj["Parent%sTag" % art], query
                    )

            if obj.get("SeriesTag"):
                all_artwork["Series.Primary"] = self.get_artwork(
                    obj["SeriesId"], "Primary", obj["SeriesTag"], query
                )

                if not all_artwork["Primary"]:
                    all_artwork["Primary"] = all_artwork["Series.Primary"]

            elif not all_artwork["Primary"] and obj.get("AlbumId"):
                all_artwork["Primary"] = self.get_artwork(
                    obj["AlbumId"], "Primary", obj["AlbumTag"], query
                )

        return all_artwork

    def get_backdrops(self, item_id, tags, query=None):
        """Get backdrops based of "BackdropImageTags" in the jellyfin object."""
        backdrops = []

        if item_id is None:
            return backdrops

        for index, tag in enumerate(tags):

            artwork = "%s/Items/%s/Images/Backdrop/%s?Format=original&Tag=%s%s" % (
                self.server,
                item_id,
                index,
                tag,
                (query or ""),
            )
            backdrops.append(artwork)

        return backdrops

    def get_artwork(self, item_id, image, tag=None, query=None):
        """Get any type of artwork: Primary, Art, Banner, Logo, Thumb, Disc"""
        if item_id is None:
            return ""

        url = "%s/Items/%s/Images/%s/0?Format=original" % (self.server, item_id, image)

        if tag is not None:
            url += "&Tag=%s" % tag

        if query is not None:
            url += query or ""

        return url


def streams_and_runtime(item):
    """``(streams, runtime_seconds)`` for ``add_streams`` from a Jellyfin DTO.

    Used for movie extras (and later video versions). Prefers
    ``MediaSources[0]`` streams — the same shape the Movie map uses — and
    falls back to top-level ``MediaStreams``. When the payload has a runtime
    but no video track, a stub video row is synthesised so Kodi still gets
    ``iVideoDuration`` (without it the extras UI falls back to the film's
    length). Track dicts are copied so the caller's DTO is not mutated.
    """
    sources = item.get("MediaSources") or []
    if sources:
        source = sources[0]
        raw = source.get("MediaStreams") or []
        container = source.get("Container") or item.get("Container")
        ticks = source.get("RunTimeTicks")
        if ticks is None:
            ticks = item.get("RunTimeTicks") or item.get("CumulativeRunTimeTicks") or 0
    else:
        raw = item.get("MediaStreams") or []
        container = item.get("Container")
        ticks = item.get("RunTimeTicks") or item.get("CumulativeRunTimeTicks") or 0

    runtime = round(float(ticks or 0) / 10000000.0, 6)
    # Only stamp keys with real values: video_streams does
    # item.get("AspectRatio", fallback).split(":") and a present-but-None
    # AspectRatio would raise.
    shaped = {"RunTimeTicks": ticks or 0}
    if item.get("Video3DFormat") is not None:
        shaped["Video3DFormat"] = item["Video3DFormat"]
    if item.get("AspectRatio"):
        shaped["AspectRatio"] = item["AspectRatio"]
    helper = API(shaped)
    video = [dict(s) for s in raw if s.get("Type") == "Video"]
    audio = [dict(s) for s in raw if s.get("Type") == "Audio"]
    subs = [s.get("Language") for s in raw if s.get("Type") == "Subtitle"]
    video = helper.video_streams(video, container)
    audio = helper.audio_streams(audio)
    if runtime and not video:
        video = [
            {
                "codec": "",
                "profile": "",
                "aspect": None,
                "width": None,
                "height": None,
                "3d": None,
                "hdrtype": "",
                "duration": runtime,
            }
        ]
    return helper.media_streams(video, audio, subs), runtime


def ratings(obj):
    """Ordered ``{rating_type: (rating, votes)}`` for Kodi's rating table.

    The community rating is always present, even as ``None``: Kodi keys the
    default-rating pointer on a row, so an unrated item still needs one (and
    the fork's single ``default``-typed row is exactly this entry, kept under
    its old name so no existing install is rewritten for cosmetics).

    The critic rating (Jellyfin's ``CriticRating``, the OMDb plugin's Rotten
    Tomatoes tomatometer) is a percentage, so it is scaled onto Kodi's 0-10
    scale -- 78 becomes 7.8. Star ratings and rating sorts assume that scale,
    and a raw 78 next to a community 7.1 would break both the moment the user
    made critic the default. Rounded because the dumps must be byte-identical
    across an idempotent re-write.

    First entry first: :meth:`kodidb.Movies.sync_ratings` uses insertion order
    for both id allocation and the fallback pointer.
    """
    rows = {"default": (obj.get("Rating"), obj.get("Votes"))}
    critic = obj.get("CriticRating")

    if critic is not None:
        rows["critic"] = (round(float(critic) / 10.0, 2), None)

    return rows


def reference_checksum(etag, direct_path=False):
    """The one spelling of a stored reference checksum (healing-loops-plan
    F4). Every comparator — the writers' etag_match, the change feed's
    skip-before-download, the prune's diff — must call this rather than
    inline the format: the three used to agree only because direct_path is
    hardcoded False in every writer, and a resurrected direct mode would
    have made the feed and the prune classify every item as changed forever
    while the writer agreed with itself.
    """
    if not etag:
        return None

    return "%s|%s" % (etag, "direct" if direct_path else "plugin")


def sync_checksum(item, direct_path):
    """Reference checksum stored with a fully synced item.

    The server Etag hashes DateLastSaved, so it changes on every metadata,
    image or file change but not on userdata changes. The path mode is baked
    in so toggling direct paths can never be masked by a matching Etag.
    """
    etag = item.get("Etag") if isinstance(item, dict) else None

    return reference_checksum(etag, direct_path)


def etag_match(item, e_item, direct_path):
    """True when the synced reference is already up to date with the item."""
    expected = sync_checksum(item, direct_path)

    return expected is not None and getattr(e_item, "checksum", None) == expected


def check_unchanged(writer, obj, item, e_item, update, apply_userdata=True):
    """Stamp the reference checksum on obj and report whether the full write
    cascade can be skipped.

    On a match only userdata can differ, so it is applied from the payload in
    hand (unless the type has no userdata handler) and the item is recorded
    as processed.

    An incremental sync tags the item with whether it actually carried a
    userdata change; when it did not, the userdata write is skipped. Items
    without the tag (full sync) apply userdata as before.
    """
    # Unconditional: the mapping default was json.dumps(item["UserData"]) — a
    # value no comparator can ever match, moving on every playback, so an
    # Etag-less item rewrote itself on every walk and dragged the widget
    # reference digest with it (healing-loops-plan F4). NULL has defined
    # semantics instead: re-verify every walk, visibly (kofindb warns).
    obj["Checksum"] = sync_checksum(item, writer.direct_path)

    if not (update and etag_match(item, e_item, writer.direct_path)):
        return False

    LOG.debug(
        "Skipping unchanged %s %s: %s", item.get("Type"), obj["Id"], obj.get("Title")
    )

    if apply_userdata and item.get("_userdata_changed", True):
        writer.userdata(item)

    writer.item_ids.append(obj["Id"])

    return True


# Art media types the artwork-only path can stamp (Kodi video art tables).
ARTWORK_MEDIA = ("movie", "tvshow", "season", "episode", "musicvideo")


def artwork_only(writer, item, e_item):
    """Apply an image-only update: artwork rows + reference checksum, no
    write cascade (phase 5, plan §2).

    Writer-adjacent, not a writer change — it reuses the exact seam every
    writer already exercises (``self.artwork.add(...)`` on the mapped
    Artwork object) plus the update_reference checksum stamp. Returns False
    on anything unexpected (unknown reference, missing ids); the caller
    falls the item back to the full update path.
    """
    if e_item is None:
        LOG.debug("artwork-only: %s unknown locally", item.get("Id"))
        return False

    try:
        kodi_id = e_item[0]
        media = e_item[4]

        if not kodi_id or media not in ARTWORK_MEDIA:
            return False

        item_api = API(item, writer.server.server)
        artwork = item_api.get_all_artwork(writer.objects.map(item, "Artwork"))
        writer.artwork.add(artwork, kodi_id, media)

        checksum = sync_checksum(item, writer.direct_path)
        if checksum:
            writer.jellyfin_db.update_reference(checksum, item["Id"])

        LOG.debug("Artwork-only update %s %s", media, item["Id"])
        return True
    except Exception as error:
        LOG.exception(error)
        return False


def find_library(server, item, cache=None):
    """The whitelisted ancestor view of an item that arrived without library
    context (realtime events), or {} when it belongs to no synced library.

    ``server`` is the kofin Api.

    ``cache`` is an optional dict owned by the caller (one per writer
    instance). The ``/Items/{id}/Ancestors`` walk this needs is a round trip
    the server is slow to answer — ~450ms against a real library — and it was
    being charged once per item, which is what capped a queued backlog at
    roughly one item a second. The answer depends only on the whitelist and
    the item's parent, so resolving it once per parent is exact: siblings
    share a parent, hence a library. Songs key on their album and albums on
    their artist, so a 20k-track backlog pays per album rather than per track.
    """
    from kofin.sync.db import get_sync

    sync = get_sync()
    whitelist = [x.replace("Mixed:", "") for x in sync["Whitelist"]]
    # The whitelist is part of the key: it can change mid-drain from the
    # settings dialog, and a memo that outlived it would keep writing items
    # into a library the user just de-selected.
    key = (frozenset(whitelist), item.get("ParentId"))
    cacheable = cache is not None and key[1]

    if cacheable and key in cache:
        return cache[key]

    ancestors = server.ancestors(item["Id"])
    for ancestor in ancestors:
        if ancestor["Id"] in whitelist:
            if cacheable:
                cache[key] = ancestor

            return ancestor

    if item.get("Type") == "MusicArtist":
        # Artists are server-global entities, not children of a library:
        # a folder-less artist (metadata-only, every artist whose tag came
        # from files inside album folders) answers /Ancestors with [], and
        # even a folder-backed one only names the folder it happens to live
        # in. So a realtime "artist added" could never resolve a library
        # here and the artist was silently dropped — which is how an artist
        # deleted and re-created by the server (it still had a track on a
        # compilation) stayed missing until a repair. The artist's albums
        # and songs do have ancestors; resolve through one of them. Not
        # memoized: the cache key is the parent folder, and an artist's
        # content decides this answer, not its folder siblings.
        library = _artist_library(server, item["Id"], whitelist)
        if library:
            LOG.info(
                "Artist %s resolved to library %s through its content",
                item["Id"],
                library["Id"],
            )
            return library

    LOG.error("No ancestor found, not syncing item with ID: %s", item["Id"])
    return {}


def _artist_library(server, artist_id, whitelist):
    """The whitelisted ancestor view of one of the artist's albums or songs,
    or {} for an artist with no content in any whitelisted library."""
    listing = server.items(
        {
            "ArtistIds": artist_id,
            "Recursive": True,
            "IncludeItemTypes": "MusicAlbum,Audio",
            "Limit": 1,
            "EnableTotalRecordCount": False,
        }
    )

    for child in listing.get("Items") or []:
        for ancestor in server.ancestors(child["Id"]):
            if ancestor["Id"] in whitelist:
                return ancestor

    return {}
