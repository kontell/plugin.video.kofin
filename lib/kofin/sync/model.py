"""Pure metadata shared by native SQL and public-API serializers.

No Kodi modules, settings reads, networking or storage. The legacy API wrapper
supplies settings-dependent art/resume policy separately. Stream conversion
retains the existing writer representation and ordering exactly.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple


@dataclass(frozen=True)
class MediaItem:
    item_id: str
    kind: str
    payload: Dict[str, Any]

    @classmethod
    def from_dto(cls, item: Mapping[str, Any], kind: str = "") -> "MediaItem":
        identity = item.get("Id")
        media = item.get("Type") or kind
        if (
            not isinstance(identity, str)
            or not identity
            or not isinstance(media, str)
            or not media
        ):
            raise ValueError("media needs a non-empty server ID and type")
        payload = dict(item)
        payload["Type"] = media
        return cls(identity, media, payload)


class Metadata:
    def __init__(self, item: Mapping[str, Any]) -> None:
        self.item = item

    def get_playcount(self, played: bool, playcount: Optional[int]) -> Optional[int]:
        """Convert Jellyfin played/playcount into
        the Kodi equivalent. The playcount is tied to the watch status.
        """
        return (playcount or 1) if played else None

    def get_naming(self) -> Any:

        if self.item["Type"] == "Episode" and "SeriesName" in self.item:
            return "%s: %s" % (self.item["SeriesName"], self.item["Name"])

        elif self.item["Type"] == "MusicAlbum" and "AlbumArtist" in self.item:
            return "%s: %s" % (self.item["AlbumArtist"], self.item["Name"])

        elif self.item["Type"] == "Audio" and self.item.get("Artists"):
            return "%s: %s" % (self.item["Artists"][0], self.item["Name"])

        return self.item["Name"]

    def media_streams(self, video: Any, audio: Any, subtitles: Any) -> Dict[str, Any]:
        return {"video": video or [], "audio": audio or [], "subtitle": subtitles or []}

    def video_streams(
        self, tracks: List[Dict[str, Any]], container: Optional[str] = None
    ) -> List[Dict[str, Any]]:

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

    def audio_streams(self, tracks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:

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

    def get_runtime(self) -> Any:

        try:
            runtime = self.item["RunTimeTicks"] / 10000000.0

        except KeyError:
            runtime = self.item.get("CumulativeRunTimeTicks", 0) / 10000000.0

        return runtime

    def validate_studio(self, studio_name: str) -> str:
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

    def get_overview(self, overview: Optional[str] = None) -> Any:

        overview = overview or self.item.get("Overview")

        if not overview:
            return

        overview = overview.replace('"', "'")
        overview = overview.replace("\n", "[CR]")
        overview = overview.replace("\r", " ")
        overview = overview.replace("<br>", "[CR]")

        return overview

    def get_mpaa(self, rating: Optional[str] = None) -> Any:

        mpaa = rating or self.item.get("OfficialRating", "")

        if mpaa in ("NR", "UR"):
            # Kodi seems to not like NR, but will accept Not Rated
            mpaa = "Not Rated"

        if "FSK-" in mpaa:
            mpaa = mpaa.replace("-", " ")

        return mpaa

    def get_file_path(self, path: Optional[str] = None) -> Any:

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


def streams_and_runtime(item: Mapping[str, Any]) -> Tuple[Dict[str, Any], float]:
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
    helper = Metadata(shaped)
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


def ratings(obj: Mapping[str, Any]) -> Dict[str, Tuple[Any, Any]]:
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


PROVIDER_PRIORITY = ("imdb", "tvdb", "tmdb")


def unique_ids(obj: Mapping[str, Any]) -> Dict[str, str]:
    """Ordered ``{provider: value}`` for Kodi's uniqueid table.

    That table is a *set* with a pointer, exactly as ``rating`` is: the media
    row names the default member -- ``movie.c09``, ``tvshow.c12``,
    ``episode.c20``, the columns ``movie_view`` and kin LEFT JOIN on -- so
    every provider the server sends gets a row and the pointer picks one of
    them (:meth:`kodidb.kodi.Kodi.sync_unique_ids`).

    The fork wrote one row per item, typed from a provider hardcoded per media
    kind in ``obj_map.json`` (Imdb for movies, Tvdb for TV), whether or not
    the item had that provider. An item carrying only a TMDB id therefore got
    a row with an empty value while its real id was discarded -- 15,796 empty
    rows on a 16,404-item corpus, and nothing able to resolve any of those
    items by external id (the mechanism behind jellyfin/jellyfin-kodi#920).

    Keys are lowercased because that is what Kodi's own scrapers write and
    what ``ListItem.UniqueID(imdb)`` looks up, while Jellyfin capitalises
    them. Empty values are skipped rather than stored, which is the fix. And
    nothing is filtered by name: an allowlist here is the defect, so a
    provider kofin has never heard of still gets its row.

    PROVIDER_PRIORITY first, then whatever else the server sent in its own
    order, which keeps uniqueid_id allocation deterministic for the
    idempotency dumps.
    """
    found = {}

    for key, value in (obj.get("ProviderIds") or {}).items():
        if value:
            found[str(key).lower()] = str(value)

    ordered = {name: found.pop(name) for name in PROVIDER_PRIORITY if name in found}
    ordered.update(found)

    return ordered


BOXSET_UNCHANGED = "unchanged"
BOXSET_WRITTEN = "written"
BOXSET_HEALED = "healed"
BOXSET_GUARDED = "guarded"
