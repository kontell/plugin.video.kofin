"""Build Kodi ListItems from Jellyfin item DTOs via InfoTagVideo/InfoTagMusic.

Pure helpers (paths, art, resume, type mapping) are separated from the
tag-setter glue so they can be unit tested; the setters are validated by the
Kodistubs type check and exercised live.
"""

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import xbmc
import xbmcgui

from kofin.core import settings
from kofin.core.log import Logger

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

BASE_URL = "plugin://plugin.video.kofin/"

FOLDER_TYPES = frozenset(
    {
        "CollectionFolder",
        "UserView",
        "Folder",
        "Series",
        "Season",
        "BoxSet",
        "MusicArtist",
        "MusicAlbum",
        "Playlist",
        "PhotoAlbum",
        "Genre",
        "MusicGenre",
    }
)

PLAYABLE_TYPES = frozenset(
    {"Movie", "Episode", "MusicVideo", "Video", "Trailer", "Audio", "Recording"}
)

MUSIC_TYPES = frozenset({"Audio", "MusicAlbum", "MusicArtist", "MusicGenre"})

MEDIATYPE = {
    "Movie": "movie",
    "Series": "tvshow",
    "Season": "season",
    "Episode": "episode",
    "MusicVideo": "musicvideo",
    "BoxSet": "set",
    "Video": "video",
    "Trailer": "video",
    "Recording": "video",
}


def is_folder(item: JsonDict) -> bool:
    if item.get("Type") in PLAYABLE_TYPES:
        return False
    return bool(item.get("IsFolder")) or item.get("Type") in FOLDER_TYPES


def plugin_url(params: Dict[str, str]) -> str:
    return BASE_URL + "?" + urlencode(params)


def path_for(item: JsonDict) -> str:
    """The navigation/playback URL for an item."""
    item_type = item.get("Type", "")
    item_id = item.get("Id", "")
    if item_type in PLAYABLE_TYPES:
        return plugin_url({"mode": "play", "id": item_id})
    return plugin_url({"mode": "browse", "folder": item_id, "type": item_type.lower()})


def resume_of(item: JsonDict) -> Tuple[float, float]:
    """(resume seconds, total seconds) from UserData/RunTimeTicks.

    The position carries the Advanced-tab resume offset, so the time Kodi's
    resume prompt names is the time playback will actually start at.
    """
    userdata = item.get("UserData") or {}
    position = float(userdata.get("PlaybackPositionTicks") or 0) / 10_000_000
    total = float(item.get("RunTimeTicks") or 0) / 10_000_000
    return settings.adjusted_resume(position), total


def playcount_of(item: JsonDict) -> int:
    """Kodi playcount for a DTO -- 0 unless the server calls the item played.

    ``Played`` is the watch state and ``PlayCount`` only sizes it. Reading the
    count as the state instead gets one case wrong, and gets it wrong on a
    scattered few percent of a real library: Jellyfin *keeps* the count when an
    item is marked unwatched, so everything ever watched and then unmarked came
    back as watched. The sync path has always tied the two this way
    (``sync/fields.get_playcount``); dynamic listings now agree with it.
    """
    userdata = item.get("UserData") or {}
    if not userdata.get("Played"):
        return 0
    return int(userdata.get("PlayCount") or 0) or 1


def art_for(item: JsonDict, server: str) -> Dict[str, str]:
    """Art dict with parent fallbacks (series poster on episodes, etc.)."""

    def image(
        item_id: str, image_type: str, tag: str, index: Optional[int] = None
    ) -> str:
        path = "%s/Items/%s/Images/%s" % (server, item_id, image_type)
        if index is not None:
            path += "/%d" % index
        return path + "?tag=%s" % tag

    art: Dict[str, str] = {}
    item_id = item.get("Id", "")
    tags = item.get("ImageTags") or {}

    if tags.get("Primary"):
        primary = image(item_id, "Primary", tags["Primary"])
        art["thumb"] = primary
        art["poster"] = primary
    if tags.get("Logo"):
        art["clearlogo"] = image(item_id, "Logo", tags["Logo"])
    if tags.get("Thumb"):
        art["landscape"] = image(item_id, "Thumb", tags["Thumb"])
    if tags.get("Banner"):
        art["banner"] = image(item_id, "Banner", tags["Banner"])

    backdrops = item.get("BackdropImageTags") or []
    if backdrops:
        art["fanart"] = image(item_id, "Backdrop", backdrops[0], index=0)
    else:
        parent_backdrops = item.get("ParentBackdropImageTags") or []
        parent_id = item.get("ParentBackdropItemId")
        if parent_backdrops and parent_id:
            art["fanart"] = image(parent_id, "Backdrop", parent_backdrops[0], index=0)

    series_id = item.get("SeriesId")
    series_tag = item.get("SeriesPrimaryImageTag")
    if series_id and series_tag:
        series_primary = image(series_id, "Primary", series_tag)
        art["tvshow.poster"] = series_primary
        if item.get("Type") == "Episode":
            art["poster"] = series_primary
        if "thumb" not in art:
            art["thumb"] = series_primary

    album_id = item.get("AlbumId")
    album_tag = item.get("AlbumPrimaryImageTag")
    if album_id and album_tag and "thumb" not in art:
        art["thumb"] = image(album_id, "Primary", album_tag)

    return art


def build(
    item: JsonDict, server: str, resume_seconds: Optional[float] = None
) -> xbmcgui.ListItem:
    """A fully populated ListItem for a Jellyfin DTO.

    ``resume_seconds`` replaces the resume point derived from the item's
    UserData, and 0 means no resume point at all. The play route passes the
    position this playback was resolved to start at: a resume point stamped on
    a *resolved* item makes Kodi resume whatever the user chose at the prompt,
    so it has to say exactly what this playback means (see ``plugin.play``).
    """
    li = xbmcgui.ListItem(item.get("Name", ""), offscreen=True)
    li.setArt(art_for(item, server))
    li.setProperty("kofin.id", item.get("Id", ""))

    if item.get("Type") in MUSIC_TYPES:
        _fill_music(li, item)
    elif item.get("Type") not in ("Photo", "PhotoAlbum", "Genre"):
        _fill_video(li, item, resume_seconds)

    if not is_folder(item) and item.get("Type") != "Photo":
        li.setProperty("IsPlayable", "true")
    return li


# Library containers (addon root views, etc.) are folders, not playable
# media — do not stamp setMediaType("video") (ListItem.DBTYPE).
_CONTAINER_TYPES = frozenset({"CollectionFolder", "UserView"})


def _fill_video(
    li: xbmcgui.ListItem, item: JsonDict, resume_seconds: Optional[float] = None
) -> None:
    tag = li.getVideoInfoTag()
    item_type = item.get("Type", "")
    tag.setTitle(item.get("Name", ""))

    if item_type in _CONTAINER_TYPES:
        if item.get("Overview"):
            tag.setPlot(item["Overview"])
        return

    tag.setMediaType(MEDIATYPE.get(item_type, "video"))

    if item.get("OriginalTitle"):
        tag.setOriginalTitle(item["OriginalTitle"])
    if item.get("SortName"):
        tag.setSortTitle(item["SortName"])
    if item.get("Overview"):
        tag.setPlot(item["Overview"])
    if item.get("Taglines"):
        tag.setTagLine(item["Taglines"][0])
    if item.get("ProductionYear"):
        tag.setYear(int(item["ProductionYear"]))
    if item.get("PremiereDate"):
        premiered = str(item["PremiereDate"])[:10]
        tag.setPremiered(premiered)
        if item_type == "Episode":
            tag.setFirstAired(premiered)
    if item.get("DateCreated"):
        tag.setDateAdded(str(item["DateCreated"])[:19].replace("T", " "))
    if item.get("OfficialRating"):
        tag.setMpaa(item["OfficialRating"])
    if item.get("RunTimeTicks"):
        tag.setDuration(int(item["RunTimeTicks"] // 10_000_000))
    if item.get("Genres"):
        tag.setGenres(list(item["Genres"]))
    if item.get("Studios"):
        tag.setStudios([studio.get("Name", "") for studio in item["Studios"]])
    if item.get("CommunityRating") is not None:
        tag.setRating(float(item["CommunityRating"]), isdefault=True)

    provider_ids = item.get("ProviderIds") or {}
    if provider_ids:
        unique_ids = {
            key.lower(): value for key, value in provider_ids.items() if value
        }
        if unique_ids:
            tag.setUniqueIDs(unique_ids)

    if item_type == "Episode":
        if item.get("IndexNumber") is not None:
            tag.setEpisode(int(item["IndexNumber"]))
        if item.get("ParentIndexNumber") is not None:
            tag.setSeason(int(item["ParentIndexNumber"]))
        if item.get("SeriesName"):
            tag.setTvShowTitle(item["SeriesName"])
    elif item_type == "Season":
        if item.get("IndexNumber") is not None:
            tag.setSeason(int(item["IndexNumber"]))
        if item.get("SeriesName"):
            tag.setTvShowTitle(item["SeriesName"])

    tag.setPlaycount(playcount_of(item))
    position, total = resume_of(item)
    if resume_seconds is not None:
        position = resume_seconds
    # There is no way to unset a resume point once stamped -- Kodi reads one as
    # set from its total, which a later zero-time call leaves in place -- so a
    # position of 0 has to mean "never call this".
    if position > 0 and total > 0:
        tag.setResumePoint(position, total)

    people = item.get("People") or []
    if people:
        actors = [
            xbmc.Actor(person.get("Name", ""), person.get("Role", ""), index)
            for index, person in enumerate(people)
            if person.get("Type") in ("Actor", "GuestStar")
        ]
        if actors:
            tag.setCast(actors)
        directors = [p.get("Name", "") for p in people if p.get("Type") == "Director"]
        if directors:
            tag.setDirectors(directors)
        writers = [p.get("Name", "") for p in people if p.get("Type") == "Writer"]
        if writers:
            tag.setWriters(writers)

    for stream in item.get("MediaStreams") or []:
        stream_type = stream.get("Type")
        if stream_type == "Video":
            tag.addVideoStream(
                xbmc.VideoStreamDetail(
                    width=int(stream.get("Width") or 0),
                    height=int(stream.get("Height") or 0),
                    codec=stream.get("Codec") or "",
                    duration=int(item.get("RunTimeTicks") or 0) // 10_000_000,
                )
            )
        elif stream_type == "Audio":
            tag.addAudioStream(
                xbmc.AudioStreamDetail(
                    channels=int(stream.get("Channels") or 2),
                    codec=stream.get("Codec") or "",
                    language=stream.get("Language") or "",
                )
            )
        elif stream_type == "Subtitle":
            tag.addSubtitleStream(
                xbmc.SubtitleStreamDetail(language=stream.get("Language") or "")
            )


def _fill_music(li: xbmcgui.ListItem, item: JsonDict) -> None:
    tag = li.getMusicInfoTag()
    item_type = item.get("Type", "")
    tag.setMediaType(
        {"Audio": "song", "MusicAlbum": "album", "MusicArtist": "artist"}.get(
            item_type, "song"
        )
    )
    tag.setTitle(item.get("Name", ""))
    if item.get("Artists"):
        tag.setArtist(" / ".join(item["Artists"]))
    if item.get("AlbumArtist"):
        tag.setAlbumArtist(item["AlbumArtist"])
    if item.get("Album"):
        tag.setAlbum(item["Album"])
    if item.get("ProductionYear"):
        tag.setYear(int(item["ProductionYear"]))
    if item.get("RunTimeTicks"):
        tag.setDuration(int(item["RunTimeTicks"] // 10_000_000))
    if item.get("IndexNumber") is not None:
        tag.setTrack(int(item["IndexNumber"]))
    if item.get("ParentIndexNumber") is not None:
        tag.setDisc(int(item["ParentIndexNumber"]))
    if item.get("Genres"):
        tag.setGenres(list(item["Genres"]))
