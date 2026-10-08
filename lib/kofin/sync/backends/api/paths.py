"""Ownership URLs: one scanner root per selected library and content type.

The URL is part of the storage contract. Kodi files a plugin item under the
directory before its query string, so a query-style item URL keeps the
directory its row belongs to, and ``RemoveContentForPath`` on a library root
removes every row filed beneath it in one call. Shows are folders under the
library's ``tvshows/`` root and their episodes are filed under the show
folder, which is where Kodi's scanner looks a plugin episode's show up.
"""

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode, urlsplit

BASE = "plugin://plugin.video.kofin/native/"

# Scanner content type per catalogue kind. Seasons have no scanner directory
# of their own: they are rows of the show the scanner creates from its tag.
CONTENT = {
    "Movie": "movies",
    "Series": "tvshows",
    "Season": "tvshows",
    "Episode": "tvshows",
    "MusicVideo": "musicvideos",
}
CONTENTS = ("movies", "tvshows", "musicvideos")

_PATH = re.compile(
    r"^/native/([0-9a-f]{32})/"
    r"(?:([0-9a-f]{32})/"
    r"(?:(movies|tvshows|musicvideos)/"
    r"(?:([0-9a-f]{32})/)?)?)?$"
)


@dataclass(frozen=True)
class Location:
    key: str
    library: Optional[str] = None
    content: Optional[str] = None
    series: Optional[str] = None


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


def item_dir(key, kind, library, item_id, parent_id=""):
    """The directory Kodi files an item of ``kind`` under."""
    if kind == "Series":
        return show_dir(key, library, item_id)
    if kind in ("Season", "Episode"):
        if not parent_id:
            raise ValueError("episodes and seasons need their series")
        return show_dir(key, library, parent_id)
    return library_dir(key, library, CONTENT[kind])


def playback_url(key, kind, library, item_id, parent_id=""):
    directory = item_dir(key, kind, library, item_id, parent_id)
    return directory + "?" + urlencode({"mode": "play", "id": item_id})


def identity(key, item_id):
    return key + ":" + item_id


def parse(url) -> Optional[Location]:
    parsed = urlsplit(url or "")
    if parsed.scheme != "plugin" or parsed.netloc != "plugin.video.kofin":
        return None
    match = _PATH.match(parsed.path)
    if not match:
        return None
    key, library, content, series = match.groups()
    if series and content != "tvshows":
        return None
    return Location(key, library, content, series)


def key_from_url(url):
    location = parse(url)
    return location.key if location else None
