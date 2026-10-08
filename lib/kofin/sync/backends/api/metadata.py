"""One normalized policy for scanner ListItems and public detail patches.

``details`` is the desired native state of an item as the JSON-RPC setter of
its kind takes it, and ``listitem`` is the same facts expressed through the
InfoTag the scanner reads, so a row the scanner just created already matches
most of what the patch would send. Kodi persists its own representation --
trimmed strings, arrays through the configured separator, the year from a
full premiere date -- and ``details`` speaks that representation.
"""

import hashlib
from typing import Any, Dict, Iterable, List, Optional, Tuple

import xbmc
import xbmcgui

from kofin.plugin import listitems
from kofin.sync.model import ratings as shared_ratings, streams_and_runtime
from . import paths
from .store import encode

ASCII_SPACE = " \t\n\r\v\f"

MEDIATYPE = {
    "Movie": "movie",
    "Series": "tvshow",
    "Season": "season",
    "Episode": "episode",
    "MusicVideo": "musicvideo",
    "BoxSet": "set",
}
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
    """The DTO minus what nothing reads; applied before hashing or storing."""
    result = {k: v for k, v in item.items() if k not in _DROP}
    sources = item.get("MediaSources")
    if isinstance(sources, list):
        if not result.get("MediaStreams"):
            for source in sources:
                if isinstance(source, dict) and source.get("MediaStreams"):
                    result["MediaStreams"] = source["MediaStreams"]
                    break
        result["MediaSources"] = [
            (
                {k: v for k, v in s.items() if k not in _DROP + ("MediaStreams",)}
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


def item_separator():
    """Read Kodi's configured video-array separator through its public tag API."""
    tag = xbmcgui.ListItem(offscreen=True).getVideoInfoTag()
    tag.setGenres(["kofin_left", "kofin_right"])
    joined = tag.getGenre()
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


def userdata(item):
    data = item.get("UserData") or {}
    result = {
        "playcount": listitems.playcount_of(item),
        "resume": {
            "position": float(data.get("PlaybackPositionTicks") or 0) / 10000000,
            "total": float(item.get("RunTimeTicks") or 0) / 10000000,
        },
    }
    # Kodi's watched setter generates a timestamp when none is supplied.
    # Preserve that supported behavior for old server records without a date;
    # explicit server dates and unwatched clears still round-trip exactly.
    if data.get("LastPlayedDate") or not result["playcount"]:
        result["lastplayed"] = str(data.get("LastPlayedDate") or "")[:19].replace(
            "T", " "
        )
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
    values.append("kofin.library." + library)
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
    data: Dict[str, Any] = {
        "title": item.get("Name", ""),
        "plot": item.get("Overview") or "",
        "art": listitems.art_for(item, server),
        "dateadded": _date(item.get("DateCreated"), 19),
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
    if not data.get("dateadded"):
        data.pop("dateadded", None)
    return data


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


def show_hash(episodes: Iterable[Dict[str, Any]]) -> str:
    """What the scanner may skip a show for: its importable episode set.

    Kodi compares the folder's ``hash`` property with the one it stored after
    the last import and skips the listing when they match, so the hash must
    move when an episode arrives, leaves or is renumbered -- and must not move
    for a plot edit, which the detail patch carries.
    """
    rows = []
    for episode in episodes:
        numbers = episode_numbers(episode)
        if numbers is not None:
            rows.append((episode["Id"], numbers["season"], numbers["episode"]))
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
