"""Ownership URLs: one scanner root per selected library and content type.

The URL is part of the storage contract. Kodi files a plugin item under the
directory before its query string, so a query-style item URL keeps the
directory its row belongs to, and ``RemoveContentForPath`` on a library root
removes every row filed beneath it in one call. Shows are folders under the
library's ``tvshows/`` root and their episodes are filed under the show
folder, which is where Kodi's scanner looks a plugin episode's show up.

Music has one directory per album under the library's ``music/`` root, with
every song filed beneath its album, and ``music/singles/<artist>/`` for the
songs Jellyfin gives no album. A music scan replaces a directory's songs with
whatever its listing returns, so the album is the unit a song leaves by: its
removal is the album directory listed without it.

A song's URL is a file, ``<album dir>/<item id>.<container>``, not a query:
the music database splits a URL into path and file name with the options
dropped (``URIUtils::Split``), where the video database keeps a plugin URL
whole, so a query-style song came back from Kodi as its bare directory --
unplayable, and matched by nothing on a rescan.
"""

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from kofin.core.urls import PARAM_MEDIA_SOURCE

BASE = "plugin://plugin.video.kofin/native/"

# Scanner content type per catalogue kind. Seasons have no scanner directory
# of their own: they are rows of the show the scanner creates from its tag;
# albums and artists likewise are rows the music scanner derives from songs.
CONTENT = {
    "Movie": "movies",
    "Series": "tvshows",
    "Season": "tvshows",
    "Episode": "tvshows",
    "MusicVideo": "musicvideos",
    "Audio": "music",
    "MusicAlbum": "music",
    "MusicArtist": "music",
}
CONTENTS = ("movies", "tvshows", "musicvideos", "music")
# Which of Kodi's two scanners walks a content type.
SCANNER = {
    "movies": "video",
    "tvshows": "video",
    "musicvideos": "video",
    "music": "music",
}
SINGLES = "singles/"
NO_ARTIST = "0" * 32
# Containers a song file may be named by; anything else is named "audio".
# The resolved stream carries the real format, so the name is a label, but
# it must never read as a playlist, a picture or lyrics to Kodi's classifiers.
CONTAINERS = (
    "mp3",
    "flac",
    "m4a",
    "ogg",
    "oga",
    "opus",
    "wav",
    "wma",
    "aac",
    "aiff",
    "aif",
    "ape",
    "mpc",
    "wv",
    "dsf",
    "dff",
    "alac",
    "mka",
    "mp4",
    "webm",
)
FALLBACK_CONTAINER = "audio"
# The folder beside a movie's file that Kodi reads its extras from
# (VIDEO_EXTRAS_FOLDER_REGEXP: "extras", "bonus disc", ...), lower case so
# the one spelling is ours.
EXTRAS = "extras/"
# Containers an extra's file may be named by; anything else is named "mkv".
# Kodi lists the folder with its video-extension mask (IDirectory::IsAllowed),
# so the name has to end in an extension it knows.
VIDEO_CONTAINERS = (
    "mkv",
    "mp4",
    "m4v",
    "avi",
    "mov",
    "wmv",
    "webm",
    "ts",
    "m2ts",
    "mpg",
    "mpeg",
    "flv",
    "ogv",
    "3gp",
    "vob",
)
FALLBACK_VIDEO_CONTAINER = "mkv"

# The directory the pass asks the music scanner to hold open while it writes.
_HOLD = re.compile(r"^/native/([0-9a-f]{32})/hold/(music|video)/$")

# A movie folder's extras directory and the files in it. Kodi names an extra
# by its path below the extras folder minus the extension
# (CGUIDialogVideoManagerExtras::GenerateVideoExtra), so the file is the
# extra's title and carries no query; its id is looked up from the title.
_EXTRAS = re.compile(
    r"^/native/([0-9a-f]{32})/([0-9a-f]{32})/movies/([0-9a-z]{1,64})/extras/"
    r"(?:([^/]+)\.([a-z0-9]{1,8}))?$"
)
# A folder below an extras folder: the disc structures Kodi probes for
# (VIDEO_TS/, BDMV/), answered with an empty listing.
_EXTRAS_SUB = re.compile(
    r"^/native/([0-9a-f]{32})/([0-9a-f]{32})/movies/([0-9a-z]{1,64})/extras/[^/]+/$"
)
# What an extra's title may not carry into a URL path or past Kodi's parsing.
_UNSAFE = re.compile(r"[/\\?#%|\x00-\x1f]+")

_PATH = re.compile(
    r"^/native/([0-9a-f]{32})/"
    r"(?:([0-9a-f]{32})/"
    r"(?:(movies|tvshows|musicvideos|music)/"
    r"(?:(singles/)?([0-9a-z]{1,64})/"
    r"(?:([0-9a-z]{1,64})\.([a-z0-9]{1,8}))?)?)?)?$"
)


@dataclass(frozen=True)
class Location:
    key: str
    library: Optional[str] = None
    content: Optional[str] = None
    # A show folder under a tvshows root.
    series: Optional[str] = None
    # A movie folder under a movies root (one movie, bound like a show).
    movie: Optional[str] = None
    # A music directory's key: an album id, or ``singles/<artist id>``.
    folder: Optional[str] = None
    # The song a file-style music URL names.
    song: Optional[str] = None
    # The scanner a hold directory belongs to.
    hold: Optional[str] = None
    # A movie folder's extras directory; the extra a file in it names (its
    # stem); or a disc-structure folder Kodi probes for below the directory.
    extras: bool = False
    extra: Optional[str] = None
    probe: bool = False


def root(key):
    return BASE + key + "/"


def library_root(key, library):
    return root(key) + library + "/"


def library_dir(key, library, content):
    if content not in CONTENTS:
        raise ValueError("unknown scanner content " + repr(content))
    return library_root(key, library) + content + "/"


def show_dir(key, library, series_id):
    return library_dir(key, library, "tvshows") + series_id + "/"


def movie_dir(key, library, item_id):
    """One folder per movie, so a change re-scans one folder and the extras
    Kodi looks for beside a movie have somewhere to live."""
    return library_dir(key, library, "movies") + item_id + "/"


def extras_dir(key, library, item_id):
    return movie_dir(key, library, item_id) + EXTRAS


def extra_name(item) -> str:
    """An extra's title as a file stem: what a URL path and Kodi's parsing
    can carry. The sanitizer only; ``extra_stems`` makes the stems unique."""
    name = _UNSAFE.sub("-", str(item.get("Name") or "")).strip(" .")
    return name or "Extra"


def extra_stems(features) -> List[Tuple[str, Dict[str, Any]]]:
    """``(stem, feature)`` for each of a movie's extras, the stem unique
    within the movie: Kodi names the extra by its path below the extras
    folder, so the stem is the extra's whole identity and two features
    titled alike (or alike once sanitized) would be one file. A taken title
    gets ``Title (2)``, ``Title (3)``, assigned in id order so a stem never
    moves between listings. The listing, the assets token, ``exists`` and
    ``resolve_extra`` all read this list and nothing else."""
    used: set = set()
    stems: List[Tuple[str, Dict[str, Any]]] = []
    for feature in sorted(features, key=lambda f: str(f.get("Id") or "")):
        base = extra_name(feature)
        stem, n = base, 1
        while stem.lower() in used:
            n += 1
            stem = "%s (%d)" % (base, n)
        used.add(stem.lower())
        stems.append((stem, feature))
    return stems


def video_container_of(item) -> str:
    container = str(item.get("Container") or "")
    sources = item.get("MediaSources") or []
    if not container and sources and isinstance(sources[0], dict):
        container = str(sources[0].get("Container") or "")
    for name in container.split(","):
        name = name.strip().lower()
        if name in VIDEO_CONTAINERS:
            return name
    return FALLBACK_VIDEO_CONTAINER


def extra_url(key, library, item_id, stem, extra):
    """The file an extra is listed as: its stem from ``extra_stems`` and
    the container of the feature."""
    return extras_dir(key, library, item_id) + stem + "." + video_container_of(extra)


def version_url(key, library, item_id, source_id):
    """The movie's URL with one of its media sources named: a version file,
    which the resolver plays from that source."""
    return (
        playback_url(key, "Movie", library, item_id)
        + "&"
        + urlencode({PARAM_MEDIA_SOURCE: source_id})
    )


def without_source(url) -> str:
    """A version file's URL reduced to its movie's."""
    parsed = urlsplit(url or "")
    if parsed.scheme != "plugin" or not parsed.query:
        return url or ""
    query = [
        (name, value)
        for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        if name != PARAM_MEDIA_SOURCE
    ]
    return urlunsplit(parsed._replace(query=urlencode(query)))


def is_version_url(url) -> bool:
    return PARAM_MEDIA_SOURCE in parse_qs(urlsplit(url or "").query)


def source_of(url) -> str:
    """The media source a version file's URL names, or empty."""
    return (parse_qs(urlsplit(url or "").query).get(PARAM_MEDIA_SOURCE) or [""])[0]


def hold_dir(key, scanner="music"):
    return root(key) + "hold/" + scanner + "/"


def music_dir(key, library, folder):
    if not folder:
        raise ValueError("a song directory needs its album or singles folder")
    return library_dir(key, library, "music") + folder + "/"


def song_folder(item) -> str:
    """The directory key a song files under: its album, or the singles
    folder of its first credited artist when Jellyfin gives it no album."""
    album = item.get("AlbumId")
    if album:
        return str(album)
    for credits in (item.get("ArtistItems"), item.get("AlbumArtists")):
        for artist in credits or []:
            if isinstance(artist, dict) and artist.get("Id"):
                return SINGLES + str(artist["Id"])
    return SINGLES + NO_ARTIST


def container_of(item) -> str:
    """The file extension a song is named by, from its container.

    The server names a container family as a list ("mov,mp4,m4a,3gp,3g2,mj2"
    for an AAC file); the first name Kodi knows is the extension. Kodi
    imports the row whatever the extension says -- a loaded music tag is
    audio to the scanner (MusicFileItemClassify.cpp) -- so the fallback
    only ever shows in a file name.
    """
    container = str(item.get("Container") or "")
    sources = item.get("MediaSources") or []
    if not container and sources and isinstance(sources[0], dict):
        container = str(sources[0].get("Container") or "")
    for name in container.split(","):
        name = name.strip().lower()
        if name in CONTAINERS:
            return name
    return FALLBACK_CONTAINER


def song_url(key, library, folder, item_id, container):
    return music_dir(key, library, folder) + item_id + "." + container


def item_dir(key, kind, library, item_id, parent_id=""):
    """The directory Kodi files an item of ``kind`` under."""
    if kind == "Series":
        return show_dir(key, library, item_id)
    if kind == "Movie":
        return movie_dir(key, library, item_id)
    if kind in ("Season", "Episode"):
        if not parent_id:
            raise ValueError("episodes and seasons need their series")
        return show_dir(key, library, parent_id)
    if kind == "Audio":
        return music_dir(key, library, parent_id)
    if kind == "MusicAlbum":
        return music_dir(key, library, item_id)
    if kind == "MusicArtist":
        raise ValueError("an artist has no directory of its own")
    return library_dir(key, library, CONTENT[kind])


def playback_url(key, kind, library, item_id, parent_id="", container=""):
    directory = item_dir(key, kind, library, item_id, parent_id)
    if kind == "Audio":
        return directory + item_id + "." + (container or FALLBACK_CONTAINER)
    return directory + "?" + urlencode({"mode": "play", "id": item_id})


def identity(key, item_id):
    return key + ":" + item_id


def parse(url) -> Optional[Location]:
    parsed = urlsplit(url or "")
    if parsed.scheme != "plugin" or parsed.netloc != "plugin.video.kofin":
        return None
    held = _HOLD.match(parsed.path)
    if held:
        return Location(held.group(1), hold=held.group(2))
    probe = _EXTRAS_SUB.match(parsed.path)
    if probe:
        key, library, movie = probe.groups()
        return Location(key, library, "movies", movie=movie, extras=True, probe=True)
    extras = _EXTRAS.match(parsed.path)
    if extras:
        key, library, movie, name, _ = extras.groups()
        return Location(key, library, "movies", movie=movie, extras=True, extra=name)
    match = _PATH.match(parsed.path)
    if not match:
        return None
    key, library, content, singles, folder, song, _ = match.groups()
    if folder is None:
        return Location(key, library, content)
    if content == "tvshows" and not singles and song is None:
        return Location(key, library, content, series=folder)
    if content == "movies" and not singles and song is None:
        return Location(key, library, content, movie=folder)
    if content == "music":
        return Location(
            key, library, content, folder=(singles or "") + folder, song=song
        )
    return None


def parse_item(url) -> Tuple[Optional[Location], str]:
    """The directory an item URL is filed under and the item id it carries:
    the file name for a song, the query for everything else."""
    location = parse(url)
    if location is None:
        return None, ""
    if location.song:
        return location, location.song
    query = parse_qs(urlsplit(url).query)
    return location, (query.get("id") or [""])[0]


def describe(url) -> str:
    """A log-safe name for a scanner directory: its content and folder."""
    location = parse(url)
    if location is None:
        return "directory"
    parts = [location.content or "root"]
    if location.series:
        parts.append("show " + location.series[:8])
    if location.movie:
        parts.append("movie " + location.movie[:8])
    if location.extras:
        parts.append("extras")
    if location.probe:
        parts.append("probe")
    if location.folder:
        parts.append("folder " + location.folder[-8:])
    if location.hold:
        parts.append("hold")
    return " ".join(parts)


def key_from_url(url):
    location = parse(url)
    return location.key if location else None
