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
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlsplit

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

# The directory the pass asks the music scanner to hold open while it writes.
_HOLD = re.compile(r"^/native/([0-9a-f]{32})/hold/(music|video)/$")

_PATH = re.compile(
    r"^/native/([0-9a-f]{32})/"
    r"(?:([0-9a-f]{32})/"
    r"(?:(movies|tvshows|musicvideos|music)/"
    r"(?:(singles/)?([0-9a-f]{32})/"
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
    """The file extension a song is named by, from its container."""
    container = str(item.get("Container") or "")
    sources = item.get("MediaSources") or []
    if not container and sources and isinstance(sources[0], dict):
        container = str(sources[0].get("Container") or "")
    container = container.split(",")[0].strip().lower()
    return container if container in CONTAINERS else FALLBACK_CONTAINER


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
    if location.folder:
        parts.append("folder " + location.folder[-8:])
    if location.hold:
        parts.append("hold")
    return " ".join(parts)


def key_from_url(url):
    location = parse(url)
    return location.key if location else None
