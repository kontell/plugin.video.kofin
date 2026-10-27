"""The Downloaded folder of the music tree: the finished song downloads,
listed by the add-on (``mode=downloadedmusic``).

Kodi's music nodes are filter nodes over song, album and artist fields, and
the one that reaches an album or an artist through its songs is ``path``. A
download leaves its row's plugin URL alone (downloads/nativeport.py) and
MyMusic has no tag, so no rule of Kodi's tells a downloaded song apart; the
retired builds filtered on the local file because they repointed the row.
The folder's three nodes are folder nodes on this route instead
(sync/nodes/music.py), which reads the download store and the catalogue and
asks nothing of the server: both stores are local, so the folder answers
with the server down, which is when it is wanted.

A row carries what a library row would: the catalogue's payload builds it
(listitems.build), the mapping gives it Kodi's database id, and a song's
path is the plugin's play URL, which the resolver answers from disk for a
download (plugin/play.py). A finished song the catalogue does not place (its
library is not synced) is not listed, as ``Downloaded music.m3u8`` does not
list it: there is no payload to build its row from.
"""

from typing import TYPE_CHECKING, Any, Dict, List, NamedTuple, Optional, Tuple

import xbmcgui
import xbmcplugin

from kofin.core import settings
from kofin.core.api import Api
from kofin.core.log import Logger
from kofin.core.urls import plugin_url
from kofin.plugin import browse, listitems
from kofin.plugin.router import Request

if TYPE_CHECKING:
    from kofin.sync.backends.api.store import Store

LOG = Logger(__name__)

MODE = "downloadedmusic"
# The folder's three nodes, in the order the music tree lists them.
CONTENTS = ("artists", "albums", "songs")
# The folder's label: the Downloaded-music string the retired builds' smart
# playlist carried, and the playlist file still does.
LABEL = 30736

SORTS = {
    "artists": (
        xbmcplugin.SORT_METHOD_UNSORTED,
        xbmcplugin.SORT_METHOD_LABEL_IGNORE_THE,
    ),
    "albums": (
        xbmcplugin.SORT_METHOD_UNSORTED,
        xbmcplugin.SORT_METHOD_ALBUM_IGNORE_THE,
        xbmcplugin.SORT_METHOD_ARTIST_IGNORE_THE,
        xbmcplugin.SORT_METHOD_VIDEO_YEAR,
    ),
    "songs": (
        xbmcplugin.SORT_METHOD_UNSORTED,
        xbmcplugin.SORT_METHOD_TRACKNUM,
        xbmcplugin.SORT_METHOD_TITLE_IGNORE_THE,
        xbmcplugin.SORT_METHOD_ARTIST_IGNORE_THE,
        xbmcplugin.SORT_METHOD_ALBUM_IGNORE_THE,
        xbmcplugin.SORT_METHOD_DURATION,
    ),
}

JsonDict = Dict[str, Any]
Row = Tuple[str, xbmcgui.ListItem, bool]


class Song(NamedTuple):
    item_id: str
    album: str
    payload: JsonDict
    kodi_id: Optional[int]


def node_url(content: str, **params: str) -> str:
    """The route's URL for one level: a node's path, or a row's folder."""
    query = {"mode": MODE, "content": content}
    query.update(params)
    return plugin_url(query)


def music(request: Request) -> None:
    """The route: the artists, albums or songs of the finished downloads,
    narrowed to one artist's albums or one album's songs."""
    browse.listing(request, _build, "downloaded music")


def _build(request: Request, api: Api) -> None:
    from kofin.sync.backends.api.identity import current_store

    content = request.params.get("content") or "songs"
    if content not in CONTENTS:
        content = "songs"
    artist_id = request.params.get("artist", "")
    album_id = request.params.get("album", "")
    store = current_store()
    songs = downloaded_songs(store)
    if artist_id:
        songs = [s for s in songs if artist_id in {a for a, _ in _credits(s.payload)}]
    if album_id:
        songs = [s for s in songs if s.album == album_id]
    if content == "artists":
        rows = _artist_rows(store, songs, api.server)
    elif content == "albums":
        rows = _album_rows(store, songs, api.server, artist_id)
    else:
        rows = _song_rows(songs, api.server)
    browse.set_category(request.handle, _category(songs, artist_id, album_id))
    xbmcplugin.addDirectoryItems(request.handle, rows, len(rows))
    xbmcplugin.setContent(request.handle, content)
    for method in SORTS[content]:
        xbmcplugin.addSortMethod(request.handle, method)
    xbmcplugin.endOfDirectory(request.handle)


def downloaded_songs(store: "Store") -> List[Song]:
    """The finished song downloads the catalogue places, each with its
    payload and the Kodi id its row was acknowledged against (None while
    the pass has not filed it). One query for the payloads; the mappings
    are read one each, since the downloads are tens where the library's
    mappings are tens of thousands."""
    from kofin.downloads import store as downloads

    rows = [
        row
        for row in downloads.rows(downloads.DONE)
        if row.media_type == "song" and row.jellyfin_id
    ]
    if not rows:
        return []
    entries = store.entries(kind="Audio")
    placed = [row for row in rows if row.jellyfin_id in entries]
    payloads = store.payloads([row.jellyfin_id for row in placed])
    songs = []
    for row in placed:
        payload = payloads.get(row.jellyfin_id)
        if not payload:
            continue
        mapping = store.mapping(row.jellyfin_id)
        songs.append(
            Song(
                row.jellyfin_id,
                str(payload.get("AlbumId") or row.series_id or ""),
                payload,
                mapping.kodi_id if mapping is not None else None,
            )
        )
    return songs


def _credits(payload: JsonDict) -> List[Tuple[str, str]]:
    """``(id, name)`` of the artists a song is listed under: its album's,
    as Kodi's own Artists node lists album artists, or its own when the
    album credits nobody."""
    for key in ("AlbumArtists", "ArtistItems"):
        found = [
            (str(a["Id"]), str(a.get("Name") or ""))
            for a in payload.get(key) or []
            if isinstance(a, dict) and a.get("Id")
        ]
        if found:
            return found
    return []


def _stamp(li: xbmcgui.ListItem, kodi_id: Optional[int], media: str) -> None:
    """Kodi's database id on a row the pass has filed, so the row reads as
    the library's: the info dialog, the context menu and a widget's
    identity go by it."""
    if kodi_id:
        li.getMusicInfoTag().setDbId(int(kodi_id), media)


def _artist_rows(store: "Store", songs: List[Song], server: str) -> List[Row]:
    names: Dict[str, str] = {}
    for song in songs:
        for artist_id, name in _credits(song.payload):
            names.setdefault(artist_id, name)
    payloads = store.payloads(names)
    rows = []
    for artist_id, name in names.items():
        item = payloads.get(artist_id) or {
            "Id": artist_id,
            "Type": "MusicArtist",
            "Name": name,
        }
        li = listitems.build(item, server)
        mapping = store.mapping(artist_id)
        _stamp(li, mapping.kodi_id if mapping is not None else None, "artist")
        rows.append((node_url("albums", artist=artist_id), li, True))
    rows.sort(key=lambda row: _sort_name(row[1], names))
    return rows


def _sort_name(li: xbmcgui.ListItem, names: Dict[str, str]) -> str:
    return (names.get(li.getProperty("kofin.id")) or li.getLabel() or "").casefold()


def _album_rows(
    store: "Store", songs: List[Song], server: str, artist_id: str = ""
) -> List[Row]:
    first: Dict[str, Song] = {}
    for song in songs:
        # A song without an album (a single) has no album row to stand
        # under; it is still listed among the songs and under its artist.
        if song.album:
            first.setdefault(song.album, song)
    payloads = store.payloads(first)
    ordered = []
    for album_id, song in first.items():
        item = payloads.get(album_id) or _album_from_song(album_id, song.payload)
        li = listitems.build(item, server)
        mapping = store.mapping(album_id)
        _stamp(li, mapping.kodi_id if mapping is not None else None, "album")
        narrow = {"artist": artist_id} if artist_id else {}
        ordered.append(
            (
                (
                    _album_artist(item).casefold(),
                    int(item.get("ProductionYear") or 0),
                    str(item.get("Name") or "").casefold(),
                ),
                (node_url("songs", album=album_id, **narrow), li, True),
            )
        )
    ordered.sort(key=lambda pair: pair[0])
    return [row for _key, row in ordered]


def _album_from_song(album_id: str, payload: JsonDict) -> JsonDict:
    """An album row from one of its songs, for an album the catalogue has no
    record of (its listing left the album out, or the song moved album)."""
    item: JsonDict = {
        "Id": album_id,
        "Type": "MusicAlbum",
        "Name": payload.get("Album") or "",
        "AlbumArtists": payload.get("AlbumArtists") or [],
        "ArtistItems": payload.get("ArtistItems") or [],
        "Artists": payload.get("Artists") or [],
        "ProductionYear": payload.get("ProductionYear"),
    }
    if payload.get("AlbumPrimaryImageTag"):
        item["ImageTags"] = {"Primary": payload["AlbumPrimaryImageTag"]}
    return item


def _album_artist(item: JsonDict) -> str:
    credits = [
        str(a.get("Name") or "")
        for a in item.get("AlbumArtists") or []
        if isinstance(a, dict)
    ]
    return " / ".join(credits) or str(item.get("AlbumArtist") or "")


def _song_rows(songs: List[Song], server: str) -> List[Row]:
    def order(song: Song) -> Tuple[str, str, int, int, str]:
        payload = song.payload
        return (
            _album_artist(payload).casefold(),
            str(payload.get("Album") or "").casefold(),
            int(payload.get("ParentIndexNumber") or 0),
            int(payload.get("IndexNumber") or 0),
            str(payload.get("Name") or "").casefold(),
        )

    rows = []
    for song in sorted(songs, key=order):
        li = listitems.build(song.payload, server)
        _stamp(li, song.kodi_id, "song")
        rows.append((listitems.path_for(song.payload), li, False))
    return rows


def _category(songs: List[Song], artist_id: str, album_id: str) -> str:
    """The listing's title: the album's or the artist's name on a narrowed
    level, the folder's own above."""
    if album_id and songs:
        return str(songs[0].payload.get("Album") or "")
    if artist_id and songs:
        for found, name in _credits(songs[0].payload):
            if found == artist_id:
                return name
    return str(settings.localized(LABEL))
