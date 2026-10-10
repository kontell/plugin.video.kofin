"""One normalized policy for scanner ListItems and public detail patches.

``details`` is the desired native state of an item as the JSON-RPC setter of
its kind takes it, and ``listitem`` is the same facts expressed through the
InfoTag the scanner reads, so a row the scanner just created already matches
most of what the patch would send. Kodi persists its own representation --
trimmed strings, arrays through the configured separator, the year from a
full premiere date -- and ``details`` speaks that representation.
"""

import hashlib
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import xbmc
import xbmcgui

from kofin.plugin import listitems
from kofin.sync import dynamic
from kofin.sync.model import ratings as shared_ratings, streams_and_runtime
from kofin.sync.shims import convert_to_local
from . import paths
from .store import encode, payload_hash

ASCII_SPACE = " \t\n\r\v\f"

MEDIATYPE = {
    "Movie": "movie",
    "Series": "tvshow",
    "Season": "season",
    "Episode": "episode",
    "MusicVideo": "musicvideo",
    "BoxSet": "set",
    "Audio": "song",
    "MusicAlbum": "album",
    "MusicArtist": "artist",
}
MUSIC = ("Audio", "MusicAlbum", "MusicArtist")
# Jellyfin's "no date" for music: year 1, 0001-01-01.
NO_YEAR = 1
FAVORITE_TAG = {
    "Movie": "Favorite movies",
    "Series": "Favorite tvshows",
    "MusicVideo": "Favorite musicvideos",
}
# Kodi's sort position for a special that airs after a whole season.
AFTER_SEASON_EPISODE = 4096

# Payload keys no serializer reads. A Jellyfin DTO carries them on every
# item and every cast member; stored once per item they were a third of the
# 0.90.0 catalogue.
_DROP = ("ImageBlurHashes", "Chapters", "Trickplay", "MediaAttachments")


def compact(item):
    """The DTO minus what nothing reads; applied before hashing or storing.

    A song keeps its sources (the file size is the scanner's hash input)
    and drops its streams: no music setter reads them, and 22,000 of them
    would be the catalogue's largest field.
    """
    result = {k: v for k, v in item.items() if k not in _DROP}
    if not result.get("SpecialFeatureCount"):
        # Every movie is sent the count; only one with extras keeps it, or
        # every payload hash would have moved the day the field was asked.
        result.pop("SpecialFeatureCount", None)
    sources = item.get("MediaSources")
    if item.get("Type") == "Audio":
        result.pop("MediaStreams", None)
    if isinstance(sources, list):
        if not result.get("MediaStreams") and item.get("Type") != "Audio":
            for source in sources:
                if isinstance(source, dict) and source.get("MediaStreams"):
                    result["MediaStreams"] = source["MediaStreams"]
                    break
        # A movie with several sources lists a file per source, each with
        # the source's own streams; a single source's are the item's.
        strip = (
            _DROP
            if len(sources) > 1 and item.get("Type") == "Movie"
            else _DROP + ("MediaStreams",)
        )
        result["MediaSources"] = [
            (
                {k: v for k, v in s.items() if k not in strip}
                if isinstance(s, dict)
                else s
            )
            for s in sources
        ]
    people = item.get("People")
    if isinstance(people, list):
        result["People"] = [
            (
                {k: v for k, v in p.items() if k != "ImageBlurHashes"}
                if isinstance(p, dict)
                else p
            )
            for p in people
        ]
    return result


# What a special feature's payload keeps: its row, its file name and the
# streams Kodi reads off the listing.
_EXTRA_KEYS = (
    "Id",
    "Name",
    "Overview",
    "Container",
    "RunTimeTicks",
    "ImageTags",
    "BackdropImageTags",
    "ExtraType",
    "DateCreated",
    "MediaStreams",
)


def compact_extra(feature):
    """A movie's special feature as its payload carries it."""
    result = {k: feature[k] for k in _EXTRA_KEYS if k in feature}
    sources = feature.get("MediaSources")
    if not result.get("MediaStreams") and isinstance(sources, list):
        for source in sources:
            if isinstance(source, dict) and source.get("MediaStreams"):
                result["MediaStreams"] = source["MediaStreams"]
                break
    return result


def version_sources(item) -> List[Dict[str, Any]]:
    """The media sources a movie is listed by, a file each, when it has
    more than one; Jellyfin's first is the one the movie's plain URL plays."""
    sources = [
        s
        for s in (item.get("MediaSources") or [])
        if isinstance(s, dict) and s.get("Id")
    ]
    return sources if len(sources) > 1 else []


def special_features(item) -> List[Dict[str, Any]]:
    return [
        f
        for f in (item.get("SpecialFeatures") or [])
        if isinstance(f, dict) and f.get("Id") and f.get("Name")
    ]


def assets_token(item) -> str:
    """What a movie's folder lists beyond the movie's own file: its version
    files and its extras, as the listing names them. Empty for a movie with
    neither. The pass scans the folder by name when the token moves, since
    the root walk lists the movie's file alone."""
    sources = version_sources(item)
    features = special_features(item)
    if not sources and not features:
        return ""
    return _token(
        {
            "versions": [[s["Id"], s.get("Name") or ""] for s in sources],
            "extras": [
                [f["Id"], paths.extra_name(f), paths.video_container_of(f)]
                for f in features
            ],
        }
    )


def item_separator():
    """Read Kodi's configured video-array separator through its public tag API.

    The ListItem must outlive the tag: ``getVideoInfoTag`` hands out a
    pointer into the item (``InfoTagVideo(tag, offscreen)``, owned=false),
    and the item's destructor deletes it. Taken from a temporary, the tag
    dangles and the first setter writes freed memory; a 32-bit ARM Kodi
    segfaulted in ``setGenres`` on it (LibreELEC 22.0b2, 9 October 2026).
    """
    probe = xbmcgui.ListItem(offscreen=True)
    tag = probe.getVideoInfoTag()
    tag.setGenres(["kofin_left", "kofin_right"])
    joined = tag.getGenre()
    del tag
    del probe
    return joined[len("kofin_left") : -len("kofin_right")] if joined else " / "


def _token(value) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()[:24]


def refresh_token(item, seasons=()):
    """Fields with no complete public setter: a change here means a refresh.

    A show's season plots have ingestion only (``addSeason``), and an
    episode's sort numbering has no setter, so both join the token the way
    cast and streams did for movies.
    """
    kind = item.get("Type")
    keys: Tuple[str, ...] = ("People",)
    if kind in ("Movie", "Episode", "MusicVideo"):
        keys += ("MediaStreams", "MediaSources", "Video3DFormat", "AspectRatio")
    if kind == "Episode":
        keys += (
            "AirsBeforeSeasonNumber",
            "AirsAfterSeasonNumber",
            "AirsBeforeEpisodeNumber",
            "IndexNumberEnd",
        )
    value: Dict[str, Any] = {key: item.get(key) for key in keys}
    if kind == "Series":
        value["Seasons"] = sorted(
            (int(s.get("IndexNumber") or 0), s.get("Overview") or "")
            for s in seasons
            if s.get("IndexNumber") is not None
        )
    return _token(value)


def userdata(item) -> Dict[str, Any]:
    """The userdata Kodi keeps for a playable row. A song has no resume point
    in Kodi's music library, so a song's is its playcount and last played."""
    data = item.get("UserData") or {}
    result: Dict[str, Any] = {"playcount": listitems.playcount_of(item)}
    if item.get("Type") != "Audio":
        result["resume"] = {
            "position": float(data.get("PlaybackPositionTicks") or 0) / 10000000,
            "total": float(item.get("RunTimeTicks") or 0) / 10000000,
        }
    # Kodi's watched setter generates a timestamp when none is supplied.
    # Preserve that supported behavior for old server records without a date;
    # explicit server dates and unwatched clears still round-trip exactly.
    if data.get("LastPlayedDate") or not result["playcount"]:
        result["lastplayed"] = _timestamp(data.get("LastPlayedDate"))
    elif item.get("Type") == "Audio":
        # Played, but the server never recorded when. Kodi's UpdateSong
        # stamps the current time on a played song with no date
        # (MusicDatabase.cpp), which put sixteen songs marked played years
        # ago at the top of a tablet's recently played albums on the day of
        # its import. The date the server first saw the song is the latest
        # moment certainly no later than the play, and it never reads as
        # recent. The video setters leave a missing date alone.
        result["lastplayed"] = _timestamp(item.get("DateCreated"))
    return result


def unique_ids(item, key, seasons=()):
    result = {
        k.lower(): str(v) for k, v in (item.get("ProviderIds") or {}).items() if v
    }
    result.update(
        kofin=paths.identity(key, item["Id"]),
        kofinrefresh=refresh_token(item, seasons),
    )
    return result


def tags(item, library, kind="Movie"):
    if kind not in FAVORITE_TAG:
        return []
    values = [value.strip(ASCII_SPACE) for value in item.get("Tags") or []]
    values.append(dynamic.library_tag(library))
    if (item.get("UserData") or {}).get("IsFavorite"):
        values.append(FAVORITE_TAG[kind])
    return sorted(set(value for value in values if value))


def ratings(item):
    result = {}
    source = shared_ratings(
        dict(
            Rating=item.get("CommunityRating"),
            Votes=item.get("VoteCount"),
            CriticRating=item.get("CriticRating"),
        )
    )
    for name, (rating, votes) in source.items():
        if rating is not None:
            result[name] = {
                "rating": float(rating),
                "votes": int(votes or 0),
                "default": name == "default",
            }
    return result


def episode_numbers(item) -> Optional[Dict[str, int]]:
    """Kodi's numbering for an episode, or None when Kodi cannot file it.

    The scanner keeps an episode whose tag says season >= 0 and episode > 0,
    or -- for a plugin -- season > 0 and episode >= 0. An unnumbered special
    (season 0, no number) matches neither and would be dropped with a log
    line, so it stays dynamic-only and is counted, not imported.
    """
    season = item.get("ParentIndexNumber")
    episode = item.get("IndexNumber")
    if season is None:
        if item.get("AbsoluteEpisodeNumber"):
            season, episode = 1, item["AbsoluteEpisodeNumber"]
        else:
            season = 0
    season = int(season)
    episode = int(episode) if episode is not None else 0
    if not ((season >= 0 and episode > 0) or (season > 0 and episode >= 0)):
        return None
    result = {"season": season, "episode": episode}
    after = item.get("AirsAfterSeasonNumber")
    before = item.get("AirsBeforeSeasonNumber")
    if after is not None:
        result["sortseason"] = int(after)
        result["sortepisode"] = AFTER_SEASON_EPISODE
    elif before is not None:
        result["sortseason"] = int(before)
        result["sortepisode"] = int(item.get("AirsBeforeEpisodeNumber") or 0)
    return result


def season_title(season) -> str:
    """A season's custom name; '' when Jellyfin's default label is in use.

    Kodi labels an unnamed season itself, localized, so the server's English
    "Season 3" must not be written over it.
    """
    name = (season.get("Name") or "").strip(ASCII_SPACE)
    number = season.get("IndexNumber")
    if number is None:
        return name
    defaults = {"Season %d" % int(number), "Season %02d" % int(number)}
    if int(number) == 0:
        defaults |= {"Specials", "Special"}
    return "" if name in defaults else str(name)


def _clean_strings(data, fields):
    for field in fields:
        data[field] = (data.get(field) or "").strip(ASCII_SPACE)


def _clean_arrays(data, fields, separator):
    # InfoTag setters trim ASCII whitespace. String-array fields round-trip
    # through Kodi's configured separator; tags use separate native links.
    for field in fields:
        data[field] = list(
            dict.fromkeys(
                part.strip(ASCII_SPACE)
                for value in data.get(field) or []
                for part in (value.split(separator) if separator else [value])
                if part.strip(ASCII_SPACE)
            )
        )


def _people(item, role):
    return [p["Name"] for p in item.get("People") or [] if p.get("Type") == role]


def _date(value, length=10):
    return str(value or "")[:length].replace("T", " ")


def _int(value) -> int:
    """A number the server sent, or the missing-field zero.

    ``song_tags`` is both the directory listing and the hash the pass
    compares, so it has to be total: a track number of "x" raised out of
    ``int`` and skipped the scan of every directory in its library on every
    pass. A value that does not parse is treated as absent, which is what
    the listing, the hash and the directory picker already agree on for a
    field that is not there.
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _timestamp(value):
    """A server UTC instant as the local-time text Kodi stores and shows.

    Kodi writes its own lastplayed and dateadded in local time, so a UTC
    string would read an hour or more off in the UI and differ from every
    value Kodi sets itself. Dates (premiered, first aired) stay as given:
    shifting a calendar date by the zone would move it a day.
    """
    if not value:
        return ""
    return convert_to_local(value)[:19].replace("T", " ")


def details(
    item,
    server,
    key,
    library,
    separator=" / ",
    seasons=(),
    set_name=None,
):
    """The desired native state, keyed as the kind's JSON-RPC setter takes it.

    Explicit empty values clear metadata the server removed.
    """
    kind = item.get("Type", "Movie")
    if kind in MUSIC:
        return music_details(item, server)
    data: Dict[str, Any] = {
        "title": item.get("Name", ""),
        "plot": item.get("Overview") or "",
        # An episode's tvshow.* and season.* art is Kodi's own, read off the
        # show and season rows: the episode setter cannot make such a key
        # true, and a show whose art URL changed after its episodes were
        # acknowledged (the artwork cap) left two episodes on the LibreELEC
        # box failing "readback differs: art" on every pass.
        "art": {
            key: value
            for key, value in listitems.art_for(item, server).items()
            if "." not in key
        },
        "dateadded": _timestamp(item.get("DateCreated")),
    }
    if kind == "BoxSet":
        _clean_strings(data, ("title", "plot"))
        data.pop("dateadded")
        return data
    data["uniqueid"] = unique_ids(item, key, seasons)
    if kind != "Season":
        data["runtime"] = int(item.get("RunTimeTicks") or 0) // 10000000
    if kind in ("Movie", "Series", "MusicVideo"):
        data["tag"] = tags(item, library, kind)
    if kind in ("Movie", "Series", "Episode"):
        data["originaltitle"] = item.get("OriginalTitle") or ""
        data["ratings"] = ratings(item)
    if kind in ("Movie", "Series"):
        data.update(
            sorttitle=item.get("SortName") or "",
            premiered=_date(item.get("PremiereDate")),
            mpaa=item.get("OfficialRating") or "",
            genre=item.get("Genres") or [],
            studio=[s["Name"] for s in item.get("Studios") or []],
            trailer=next(
                (t["Url"] for t in item.get("RemoteTrailers") or [] if t.get("Url")),
                "",
            ),
        )
    if kind == "Movie":
        data.update(
            plotoutline=item.get("ShortOverview") or "",
            tagline=next(iter(item.get("Taglines") or []), ""),
            year=int(item.get("ProductionYear") or 0),
            country=item.get("ProductionLocations") or [],
            director=_people(item, "Director"),
            writer=_people(item, "Writer"),
        )
        if set_name is not None:
            data["set"] = set_name.strip(ASCII_SPACE)
    elif kind == "Series":
        data["status"] = item.get("Status") or ""
        # A show's date added is derived: tvshowcounts takes the newest of
        # its episode files, and SetTVShowDetails cannot move it.
        data.pop("dateadded")
    elif kind == "Episode":
        numbers = episode_numbers(item) or {}
        data.update(
            firstaired=_date(item.get("PremiereDate")),
            season=numbers.get("season", 0),
            episode=numbers.get("episode", 0),
            director=_people(item, "Director"),
            writer=_people(item, "Writer"),
        )
    elif kind == "MusicVideo":
        data.update(
            year=int(item.get("ProductionYear") or 0),
            premiered=_date(item.get("PremiereDate")),
            genre=item.get("Genres") or [],
            studio=[s["Name"] for s in item.get("Studios") or []],
            director=_people(item, "Director"),
            artist=list(item.get("Artists") or []),
            album=item.get("Album") or "",
            track=int(item.get("IndexNumber") or 0),
        )
        if item.get("CommunityRating") is not None:
            data["rating"] = float(item["CommunityRating"])
    elif kind == "Season":
        data = {
            "title": season_title(item),
            "art": data["art"],
            "uniqueid": data["uniqueid"],
        }
        return data
    _clean_strings(
        data,
        [
            f
            for f in (
                "title",
                "originaltitle",
                "sorttitle",
                "plot",
                "plotoutline",
                "tagline",
                "mpaa",
                "trailer",
                "status",
                "album",
            )
            if f in data
        ],
    )
    _clean_arrays(
        data,
        [
            f
            for f in ("genre", "studio", "country", "director", "writer", "artist")
            if f in data
        ],
        separator,
    )
    # Kodi stores one release date. Its public year is derived from a
    # full premiere date, even when Jellyfin's ProductionYear differs.
    if data.get("premiered") and "year" in data:
        data["year"] = int(data["premiered"][:4])
    if kind in ("Movie", "Episode", "MusicVideo"):
        data.update(userdata(item))
    # No opinion on a date the server does not have. Kodi fills a missing
    # premiere or aired date itself (its zero date on the P1D, a sibling's
    # date on the LibreELEC box) and its setters ignore an empty one, so an
    # empty desired value would differ from the row on every readback and
    # keep the item pending for ever (187 rows on the box, re-patched by
    # every pass).
    for key in ("dateadded", "premiered", "firstaired"):
        if key in data and not data.get(key):
            data.pop(key)
    return data


# -- music ---------------------------------------------------------------------


def music_art(item, server) -> Dict[str, str]:
    """The two art types Kodi's music library shows for albums and artists."""
    art = listitems.art_for(item, server)
    return {key: art[key] for key in ("thumb", "fanart") if art.get(key)}


def music_details(item, server) -> Dict[str, Any]:
    """The desired native state of a music kind.

    A song's tags are the scanner's to write: the only public setter a song
    needs after import is for the userdata the scanner discards (phase 0:
    ``CSong`` zeroes the play count of every new row). An album or artist
    is derived from its songs, and what it cannot derive -- its art and its
    description -- is the whole of its desired state.
    """
    kind = item.get("Type")
    if kind == "Audio":
        return userdata(item)
    return {
        "art": music_art(item, server),
        "description": (item.get("Overview") or "").strip(ASCII_SPACE),
    }


def names(credits) -> List[str]:
    result = []
    for credit in credits or []:
        name = credit.get("Name", "") if isinstance(credit, dict) else str(credit)
        name = name.strip(ASCII_SPACE)
        if name and name not in result:
            result.append(name)
    return result


def song_year(item) -> int:
    year = _int(item.get("ProductionYear"))
    return year if year > NO_YEAR else 0


def song_tags(item, album=None) -> Dict[str, Any]:
    """Every tag the song's ListItem sets, as plain values: the scanner's
    input, and the hash a metadata change moves the directory by.

    The album's MusicBrainz ids come from the album's own record so every
    song in the directory carries the same ones; a Jellyfin id is never
    dressed as an MBID. Userdata is deliberately absent: a play on the
    server must not re-import the album. Every number goes through ``_int``:
    the function is total, so one malformed song cannot stall its library.
    """
    album = album or {}
    sources = item.get("MediaSources") or []
    size = 0
    if sources and isinstance(sources[0], dict):
        size = _int(sources[0].get("Size"))
    providers = item.get("ProviderIds") or {}
    album_providers = album.get("ProviderIds") or {}
    year = song_year(item)
    release = _date(item.get("PremiereDate")) if year else ""
    return {
        "title": (item.get("Name") or "").strip(ASCII_SPACE),
        "album": (item.get("Album") or album.get("Name") or "").strip(ASCII_SPACE),
        "albumartist": names(item.get("AlbumArtists") or album.get("AlbumArtists")),
        "artist": names(item.get("ArtistItems")) or names(item.get("Artists")),
        "genre": [g.strip(ASCII_SPACE) for g in item.get("Genres") or [] if g],
        "track": _int(item.get("IndexNumber")),
        "disc": _int(item.get("ParentIndexNumber")),
        "duration": _int(item.get("RunTimeTicks")) // 10000000,
        "year": year,
        "releasedate": release,
        "musicbrainztrackid": str(providers.get("MusicBrainzTrack") or ""),
        "musicbrainzalbumid": str(album_providers.get("MusicBrainzAlbum") or ""),
        "musicbrainzreleasegroupid": str(
            album_providers.get("MusicBrainzReleaseGroup") or ""
        ),
        "size": size,
    }


def tag_hash(item, album=None) -> str:
    return _token(song_tags(item, album))


def hash_time(digest: str) -> str:
    """A timestamp derived from a hash, for the directory's own hash.

    The music scanner hashes a directory by each item's path, size and
    full date, and skips a directory whose hash it already holds; a song
    whose tags changed therefore has to present a different date, or the
    rescan never happens. Twenty-five years of seconds is the range.
    """
    import datetime

    seconds = int(digest[:12], 16) % (25 * 365 * 86400)
    moment = datetime.datetime(2000, 1, 1) + datetime.timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def mark_loaded(li, size):
    """Tell the scanner the tag is complete so it reads no file.

    ``InfoTagMusic`` has no ``setLoaded``; the legacy ``setInfo`` sets the
    flag, and its ``size`` key is the one that does so without the
    per-row deprecation warning (checked on 22.0b2: ``size`` takes the
    non-deprecated branch). The real file size is the scanner's hash
    input, never an invented one. An upstream ``setLoaded`` replaces this
    here and nowhere else.
    """
    li.setInfo("music", {"size": str(size)})
    tag = li.getMusicInfoTag()
    if hasattr(tag, "setLoaded"):
        tag.setLoaded(True)


def song_listitem(item, album=None, salt: int = 0):
    """A scanner row for a song: its tags, its size, and a date that moves
    with its tags so a change re-imports the directory."""
    tags = song_tags(item, album)
    li = xbmcgui.ListItem(tags["title"] or item.get("Name", ""), offscreen=True)
    tag = li.getMusicInfoTag()
    tag.setMediaType("song")
    tag.setTitle(tags["title"])
    if tags["album"]:
        tag.setAlbum(tags["album"])
    if tags["albumartist"]:
        tag.setAlbumArtist(" / ".join(tags["albumartist"]))
    if tags["artist"]:
        tag.setArtist(" / ".join(tags["artist"]))
    if tags["genre"]:
        tag.setGenres(tags["genre"])
    if tags["track"]:
        tag.setTrack(tags["track"])
    if tags["disc"]:
        tag.setDisc(tags["disc"])
    if tags["duration"]:
        tag.setDuration(tags["duration"])
    if tags["year"]:
        tag.setYear(tags["year"])
    if tags["releasedate"]:
        tag.setReleaseDate(tags["releasedate"])
    if tags["musicbrainztrackid"]:
        tag.setMusicBrainzTrackID(tags["musicbrainztrackid"])
    if tags["musicbrainzalbumid"]:
        tag.setMusicBrainzAlbumID(tags["musicbrainzalbumid"])
    if tags["musicbrainzreleasegroupid"]:
        tag.setMusicBrainzReleaseGroupID(tags["musicbrainzreleasegroupid"])
    # The date is what the scanner hashes a listing by; a salt moves it
    # without touching a tag (store.bump_salt).
    li.setDateTime(hash_time(_token([tags, salt]) if salt else _token(tags)))
    mark_loaded(li, tags["size"])
    return li


def fallback_song_listitem(item):
    """The least a song row needs to survive a serializer fault: a title,
    its size and a loaded tag. Better one plain row than a deleted song."""
    sources = item.get("MediaSources") or []
    size = sources[0].get("Size") if sources and isinstance(sources[0], dict) else 0
    li = xbmcgui.ListItem(str(item.get("Name") or item.get("Id") or ""), offscreen=True)
    tag = li.getMusicInfoTag()
    tag.setMediaType("song")
    tag.setTitle(str(item.get("Name") or item.get("Id") or ""))
    if item.get("Album"):
        tag.setAlbum(str(item["Album"]))
    mark_loaded(li, int(size or 0))
    return li


def folder_label(album, folder: str, salt: int = 0) -> str:
    """The root listing's label for an album directory: Kodi walks a
    directory's folders in label order and numbers albums as it adds
    them, so oldest-first on the server's creation date puts the
    server's newest album at the top of Kodi's recently added. A salted
    directory carries its salt so a root walk sees it changed too."""
    created = str((album or {}).get("DateCreated") or "0000-00-00T00:00:00")[:19]
    return created + " " + folder + (" %d" % salt if salt else "")


def collections_of(boxsets: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """Movie id -> the one set Kodi can hold, chosen stably by name then id."""
    result: Dict[str, Tuple[str, str, str]] = {}
    for boxset in boxsets:
        name = (boxset.get("Name") or "").strip(ASCII_SPACE)
        if not name:
            continue
        rank = (name.lower(), boxset.get("Id", ""), name)
        for member in boxset.get("KofinMembers") or []:
            if member not in result or rank < result[member]:
                result[member] = rank
    return {member: rank[2] for member, rank in result.items()}


# -- scanner ListItems -------------------------------------------------------


def _cast(tag, item, server):
    actors = [
        xbmc.Actor(
            person.get("Name", ""),
            person.get("Role", ""),
            index,
            listitems.person_thumb(server, person),
        )
        for index, person in enumerate(item.get("People") or [])
        if person.get("Type") in ("Actor", "GuestStar")
    ]
    if actors:
        tag.setCast(actors)


def _streams(tag, item):
    streams, runtime = streams_and_runtime(item)
    for stream in streams["video"]:
        stereo = {
            "HalfSideBySide": "left_right",
            "FullSideBySide": "left_right",
            "HalfTopAndBottom": "top_bottom",
            "FullTopAndBottom": "top_bottom",
        }.get(stream.get("3d"), "")
        tag.addVideoStream(
            xbmc.VideoStreamDetail(
                width=int(stream.get("width") or 0),
                height=int(stream.get("height") or 0),
                aspect=float(stream.get("aspect") or 0),
                duration=int(runtime),
                codec=stream.get("codec") or "",
                stereomode=stereo,
                hdrtype=stream.get("hdrtype") or "",
            )
        )
    for stream in streams["audio"]:
        tag.addAudioStream(
            xbmc.AudioStreamDetail(
                channels=int(stream.get("channels") or 0),
                codec=stream.get("codec") or "",
                language=stream.get("language") or "",
            )
        )
    for language in streams["subtitle"]:
        tag.addSubtitleStream(xbmc.SubtitleStreamDetail(language=language or ""))


def inputs_token(server, key, library, separator, seasons=(), set_name=None) -> str:
    """Everything ``details`` reads besides the payload, as one token.

    A mapping that carries the payload hash and this token was acknowledged
    for exactly the desired state the same inputs would produce again, so a
    pass can skip building and comparing it (``patch.Applier.plan``). The
    token is that contract: an input missing from it freezes the state the
    next change of that input should have moved. Besides the caller's
    arguments ``details`` reads the artwork cap and encoding
    (``listitems.art_query``) and renders its timestamps in the local zone
    (``shims.convert_to_local``); both are here, and anything new that
    ``details`` reads joins them.
    """
    return _token(
        [
            server or "",
            key,
            library or "",
            separator,
            set_name or "",
            sorted(payload_hash(season) for season in seasons),
            listitems.art_query(),
            # The zone, not the current offset: the names list both halves
            # of a zone with daylight saving, so a token does not flip twice
            # a year for a change that convert_to_local already renders
            # correctly per instant.
            [time.timezone, list(time.tzname)],
        ]
    )


def episode_row(item) -> Optional[Tuple[str, int, int]]:
    """All the show hash takes from an episode: its id and numbering, or
    None for one Kodi cannot file. A listing reduces each payload to this
    and lets the payload go."""
    numbers = episode_numbers(item)
    if numbers is None:
        return None
    return (str(item["Id"]), numbers["season"], numbers["episode"])


def show_hash(episodes: Iterable[Any]) -> str:
    """What the scanner may skip a show for: its importable episode set.

    Kodi compares the folder's ``hash`` property with the one it stored after
    the last import and skips the listing when they match, so the hash must
    move when an episode arrives, leaves or is renumbered -- and must not move
    for a plot edit, which the detail patch carries. Takes payloads or the
    rows ``episode_row`` reduces them to.
    """
    rows = []
    for episode in episodes:
        row = episode if isinstance(episode, tuple) else episode_row(episode)
        if row is not None:
            rows.append(row)
    return _token(sorted(rows))


def hash_date(value: str) -> str:
    """A calendar day derived from a hash, for the root listing's own hash.

    The scanner hashes a plugin folder by path, size and *date* (day
    precision), so a show whose contents changed has to show a different
    day or a root-level update skips every show at once.
    """
    days = int(value[:8], 16) % 20000
    import datetime

    day = datetime.date(1971, 1, 1) + datetime.timedelta(days=days)
    return day.strftime("%Y-%m-%dT00:00:00Z")


def listitem(
    item,
    server,
    key,
    library,
    separator=" / ",
    seasons=(),
    episodes=None,
    set_name=None,
):
    """A scanner row: everything ``details`` wants, through the InfoTag.

    No zero resume point is ever stamped: the scanner persists any *set*
    point as a bookmark, and the in-progress rule for movies and episodes
    alike is that row's existence.
    """
    kind = item.get("Type", "Movie")
    if kind == "Audio":
        return song_listitem(item)
    data = details(item, server, key, library, separator, seasons, set_name)
    li = xbmcgui.ListItem(data.get("title", item.get("Name", "")), offscreen=True)
    li.setArt(data["art"])
    tag = li.getVideoInfoTag()
    tag.setMediaType(MEDIATYPE[kind])
    tag.setTitle(data["title"])
    tag.setPlot(data.get("plot", ""))
    if data.get("originaltitle"):
        tag.setOriginalTitle(data["originaltitle"])
    if data.get("sorttitle"):
        tag.setSortTitle(data["sorttitle"])
    if data.get("plotoutline"):
        tag.setPlotOutline(data["plotoutline"])
    if data.get("tagline"):
        tag.setTagLine(data["tagline"])
    if data.get("year"):
        tag.setYear(data["year"])
    if data.get("premiered"):
        tag.setPremiered(data["premiered"])
    if data.get("firstaired"):
        tag.setFirstAired(data["firstaired"])
    if data.get("dateadded"):
        tag.setDateAdded(data["dateadded"])
    if data.get("mpaa"):
        tag.setMpaa(data["mpaa"])
    if data.get("runtime"):
        tag.setDuration(data["runtime"])
    if data.get("genre"):
        tag.setGenres(data["genre"])
    if data.get("studio"):
        tag.setStudios(data["studio"])
    if data.get("country"):
        tag.setCountries(data["country"])
    if data.get("director"):
        tag.setDirectors(data["director"])
    if data.get("writer"):
        tag.setWriters(data["writer"])
    if data.get("tag"):
        tag.setTags(data["tag"])
    if data.get("trailer"):
        tag.setTrailer(data["trailer"])
    if data.get("status"):
        tag.setTvShowStatus(data["status"])
    if data.get("set"):
        # The scanner files the set at import; no patch per member later.
        tag.setSet(data["set"])
    tag.setUniqueIDs(data["uniqueid"], "kofin")
    if data.get("ratings"):
        tag.setRatings(
            {name: (r["rating"], r["votes"]) for name, r in data["ratings"].items()},
            "default" if "default" in data["ratings"] else "",
        )
    elif data.get("rating") is not None:
        tag.setRating(float(data["rating"]), isdefault=True)
    if kind == "Episode":
        numbers = episode_numbers(item) or {}
        tag.setSeason(numbers.get("season", 0))
        tag.setEpisode(numbers.get("episode", 0))
        if "sortseason" in numbers:
            tag.setSortSeason(numbers["sortseason"])
            tag.setSortEpisode(numbers["sortepisode"])
        if item.get("SeriesName"):
            tag.setTvShowTitle(item["SeriesName"])
    if kind == "MusicVideo":
        if data.get("artist"):
            tag.setArtists(data["artist"])
        if data.get("album"):
            tag.setAlbum(data["album"])
        if data.get("track"):
            tag.setTrackNumber(data["track"])
    if kind == "Series":
        for season in sorted(
            (s for s in seasons if s.get("IndexNumber") is not None),
            key=lambda s: int(s["IndexNumber"]),
        ):
            tag.addSeason(
                int(season["IndexNumber"]),
                season_title(season),
                (season.get("Overview") or "").strip(ASCII_SPACE),
            )
        if episodes is not None:
            digest = show_hash(episodes)
            li.setProperty("hash", digest)
            li.setDateTime(hash_date(digest))
    if kind in ("Movie", "Episode", "MusicVideo"):
        if "playcount" in data:
            tag.setPlaycount(data["playcount"])
        if data.get("lastplayed"):
            tag.setLastPlayed(data["lastplayed"])
        resume = data.get("resume") or {}
        if resume.get("position", 0) > 0 and resume.get("total", 0) > 0:
            tag.setResumePoint(resume["position"], resume["total"])
        _cast(tag, item, server)
        _streams(tag, item)
    elif kind == "Series":
        _cast(tag, item, server)
    return li


def extra_listitem(item, server):
    """A scanner row for a movie extra. Kodi files the extra by its URL under
    the movie it found beside it (CVideoInfoScanner::AddVideoExtras) and keeps
    the row's dates, streams and art; the name it shows is the file's stem,
    not this tag's title."""
    title = str(item.get("Name") or "")
    li = xbmcgui.ListItem(title, offscreen=True)
    li.setArt(listitems.art_for(item, server))
    tag = li.getVideoInfoTag()
    tag.setMediaType("video")
    tag.setTitle(title)
    plot = (item.get("Overview") or "").strip(ASCII_SPACE)
    if plot:
        tag.setPlot(plot)
    runtime = int((item.get("RunTimeTicks") or 0) // 10_000_000)
    if runtime:
        tag.setDuration(runtime)
    _streams(tag, item)
    return li


def owned(data) -> Dict[str, List[str]]:
    """The map keys and tags this write puts its name to; a later write may
    clear exactly these and nothing a user or another add-on added."""
    result: Dict[str, List[str]] = {}
    for field in ("art", "ratings", "uniqueid"):
        if isinstance(data.get(field), dict):
            result[field] = sorted(data[field])
    if isinstance(data.get("tag"), list):
        result["tag"] = sorted(data["tag"])
    if data.get("set"):
        result["set"] = [data["set"]]
    if "runtime" not in data and data.get("title"):
        # A season's custom name. Withdrawn, it is cleared once -- the pass
        # that still finds it owned -- and then left to Kodi's own label.
        result["title"] = [data["title"]]
    return result
