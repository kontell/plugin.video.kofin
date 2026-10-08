"""Movie InfoTags and public detail patches share one normalized policy."""

import hashlib

import xbmc
import xbmcgui

from kofin.plugin import listitems
from kofin.sync.model import ratings as shared_ratings, streams_and_runtime
from .store import encode, identity, playback_url

ASCII_SPACE = " \t\n\r\v\f"


def item_separator():
    """Read Kodi's configured video-array separator through its public tag API."""
    tag = xbmcgui.ListItem(offscreen=True).getVideoInfoTag()
    tag.setGenres(["kofin_left", "kofin_right"])
    joined = tag.getGenre()
    return joined[len("kofin_left") : -len("kofin_right")] if joined else " / "


def refresh_token(item):
    # These fields have no complete public detail setter on Piers.
    value = {
        key: item.get(key)
        for key in (
            "People",
            "MediaStreams",
            "MediaSources",
            "Video3DFormat",
            "AspectRatio",
        )
    }
    return hashlib.sha256(encode(value).encode()).hexdigest()[:24]


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


def unique_ids(item, key):
    result = {
        k.lower(): str(v) for k, v in (item.get("ProviderIds") or {}).items() if v
    }
    result.update(kofin=identity(key, item["Id"]), kofinrefresh=refresh_token(item))
    return result


def tags(item, library):
    values = [value.strip(ASCII_SPACE) for value in item.get("Tags") or []]
    values.append("kofin.library." + library)
    if (item.get("UserData") or {}).get("IsFavorite"):
        values.append("Favorite movies")
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


def details(item, server, key, library, separator=" / "):
    """Explicit empty values clear metadata removed by the server."""
    data = {
        "title": item.get("Name", ""),
        "originaltitle": item.get("OriginalTitle") or "",
        "sorttitle": item.get("SortName") or "",
        "plot": item.get("Overview") or "",
        "plotoutline": item.get("ShortOverview") or "",
        "tagline": next(iter(item.get("Taglines") or []), ""),
        "year": int(item.get("ProductionYear") or 0),
        "premiered": str(item.get("PremiereDate") or "")[:10],
        "mpaa": item.get("OfficialRating") or "",
        "runtime": int(item.get("RunTimeTicks") or 0) // 10000000,
        "genre": item.get("Genres") or [],
        "studio": [s["Name"] for s in item.get("Studios") or []],
        "country": item.get("ProductionLocations") or [],
        "director": [
            p["Name"] for p in item.get("People") or [] if p.get("Type") == "Director"
        ],
        "writer": [
            p["Name"] for p in item.get("People") or [] if p.get("Type") == "Writer"
        ],
        "tag": tags(item, library),
        "uniqueid": unique_ids(item, key),
        "ratings": ratings(item),
        "art": listitems.art_for(item, server),
        "dateadded": str(item.get("DateCreated") or "")[:19].replace("T", " "),
        "trailer": next(
            (t["Url"] for t in item.get("RemoteTrailers") or [] if t.get("Url")), ""
        ),
    }
    # InfoTag setters trim ASCII whitespace. String-array fields round-trip
    # through Kodi's configured separator; tags use separate native links.
    for field in (
        "title",
        "originaltitle",
        "sorttitle",
        "plot",
        "plotoutline",
        "tagline",
        "mpaa",
        "trailer",
    ):
        data[field] = data[field].strip(ASCII_SPACE)
    for field in ("genre", "studio", "country", "director", "writer"):
        data[field] = list(
            dict.fromkeys(
                part.strip(ASCII_SPACE)
                for value in data[field]
                for part in (value.split(separator) if separator else [value])
                if part.strip(ASCII_SPACE)
            )
        )
    # Kodi stores one movie release date. Its public year is derived from a
    # full premiere date, even when Jellyfin's ProductionYear differs.
    if data["premiered"]:
        data["year"] = int(data["premiered"][:4])
    data.update(userdata(item))
    if not data["dateadded"]:
        data.pop("dateadded")
    return data


def build(item, server, key, library):
    data = details(item, server, key, library)
    # Browsing deliberately sends fewer stream fields. The scanner uses the
    # shared writer conversion, including aspect, stereo, HDR and codec policy.
    # A zero resume point is never stamped: the scanner would persist it as a
    # bookmark and the movie would sit in "in progress" until the patch below.
    li = listitems.build(
        dict(item, MediaStreams=[]), server, resume_offset=0, stamp_zero_resume=False
    )
    tag = li.getVideoInfoTag()
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
    tag.setUniqueIDs(data["uniqueid"], "kofin")
    tag.setTags(data["tag"])
    tag.setCountries(data["country"])
    tag.setPlotOutline(data["plotoutline"])
    tag.setTrailer(data["trailer"])
    if data.get("lastplayed"):
        tag.setLastPlayed(data["lastplayed"])
    for name, rating in data["ratings"].items():
        tag.setRating(rating["rating"], rating["votes"], name, rating["default"])
    li.setPath(playback_url(key, item["Id"]))
    return li
