"""Directory listings: addon root, library nodes, and drill-down browsing."""

from typing import Any, Dict, List, Optional, Tuple

import xbmcgui
import xbmcplugin

from kofin.core import settings
from kofin.core.api import Api
from kofin.core.http import Http, JellyfinError
from kofin.core.log import Logger
from kofin.core.settings import Credentials
from kofin.plugin import listitems
from kofin.plugin.router import Request

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

BROWSE_FIELDS = (
    "Overview,Genres,Studios,Taglines,PremiereDate,ProductionYear,"
    "OfficialRating,CommunityRating,RunTimeTicks,DateCreated,MediaStreams,"
    "ProviderIds,SortName"
)

# Node menus per collection type: (folder key, label string id).
NODES: Dict[str, List[Tuple[str, int]]] = {
    "movies": [
        ("all", 30030),
        ("recent", 30031),
        ("inprogress", 30033),
        ("unwatched", 30034),
        ("favorites", 30035),
        ("sets", 30038),
        ("genres", 30036),
        ("random", 30037),
    ],
    "tvshows": [
        ("all", 30030),
        ("recentepisodes", 30031),
        ("nextup", 30032),
        ("inprogressepisodes", 30033),
        ("favorites", 30035),
        ("genres", 30036),
        ("random", 30037),
    ],
    "music": [
        ("artists", 30039),
        ("albums", 30040),
        ("recentalbums", 30031),
        ("favoritealbums", 30035),
        ("genres", 30036),
    ],
    "musicvideos": [
        ("all", 30030),
        ("recent", 30031),
        ("unwatched", 30034),
        ("favorites", 30035),
        ("random", 30037),
    ],
}

CONTENT_TYPES = {
    "movies": "movies",
    "tvshows": "tvshows",
    "musicvideos": "musicvideos",
    "music": "artists",
}


def node_query(media: str, node: str, view_id: str) -> Optional[JsonDict]:
    """API query params for a browse node; None for nodes with special routes."""
    base: JsonDict = {
        "ParentId": view_id,
        "Recursive": True,
        "Fields": BROWSE_FIELDS,
        "ImageTypeLimit": 1,
        "SortBy": "SortName",
        "SortOrder": "Ascending",
    }
    types = {
        "movies": "Movie",
        "tvshows": "Series",
        "music": "MusicAlbum",
        "musicvideos": "MusicVideo",
    }.get(media, "")

    if node == "all":
        base["IncludeItemTypes"] = types
    elif node == "recent":
        base.update(
            IncludeItemTypes=types,
            SortBy="DateCreated",
            SortOrder="Descending",
            Limit=25,
        )
    elif node == "recentepisodes":
        base.update(
            IncludeItemTypes="Episode",
            SortBy="DateCreated",
            SortOrder="Descending",
            Limit=25,
        )
    elif node == "inprogress":
        base.update(
            IncludeItemTypes=types,
            Filters="IsResumable",
            SortBy="DatePlayed",
            SortOrder="Descending",
            Limit=25,
        )
    elif node == "inprogressepisodes":
        base.update(
            IncludeItemTypes="Episode",
            Filters="IsResumable",
            SortBy="DatePlayed",
            SortOrder="Descending",
            Limit=25,
        )
    elif node == "unwatched":
        base.update(IncludeItemTypes=types, Filters="IsUnplayed")
    elif node == "favorites":
        base.update(
            IncludeItemTypes=types if media != "tvshows" else "Series",
            Filters="IsFavorite",
        )
    elif node == "favoritealbums":
        base.update(IncludeItemTypes="MusicAlbum", Filters="IsFavorite")
    elif node == "sets":
        base.update(IncludeItemTypes="BoxSet")
    elif node == "random":
        base.update(IncludeItemTypes=types, SortBy="Random", Limit=25)
    elif node == "albums":
        base.update(IncludeItemTypes="MusicAlbum", SortBy="AlbumArtist,SortName")
    elif node == "recentalbums":
        base.update(
            IncludeItemTypes="MusicAlbum",
            SortBy="DateCreated",
            SortOrder="Descending",
            Limit=25,
        )
    elif node.startswith("genre-"):
        base["GenreIds"] = node.split("-", 1)[1]
        base["IncludeItemTypes"] = types
    else:
        return None
    return base


def _api() -> Optional[Api]:
    creds = Credentials.load()
    if not creds.is_logged_in:
        return None
    return Api.from_credentials(Http(settings.get_bool("sslVerify")), creds)


def root(request: Request) -> None:
    if request.handle < 0:
        return
    api = _api()
    entries: List[Tuple[str, xbmcgui.ListItem, bool]] = []

    if api is not None:
        try:
            views = api.views().get("Items", [])
        except JellyfinError as error:
            LOG.warning("views unavailable: %s", error)
            views = []
        for view in views:
            if view.get("CollectionType") == "livetv":
                continue  # live TV is pvr.kofin's job
            collection = view.get("CollectionType") or ""
            li = listitems.build(view, api.server)
            params = {"mode": "browse", "view": view.get("Id", ""), "type": collection}
            if collection not in NODES:
                params["folder"] = "children"
            entries.append((listitems.plugin_url(params), li, True))

    import xbmc

    settings_li = xbmcgui.ListItem(xbmc.getLocalizedString(5))  # "Settings"
    entries.append((listitems.plugin_url({"mode": "settings"}), settings_li, False))

    xbmcplugin.addDirectoryItems(request.handle, entries, len(entries))
    xbmcplugin.setContent(request.handle, "files")
    xbmcplugin.endOfDirectory(request.handle)


def browse(request: Request) -> None:
    if request.handle < 0:
        return
    api = _api()
    if api is None:
        xbmcplugin.endOfDirectory(request.handle, succeeded=False)
        return

    view_id = request.params.get("view", "")
    media = request.params.get("type", "")
    folder = request.params.get("folder", "")

    try:
        if not folder and media in NODES:
            _node_menu(request, media, view_id)
            return
        items, content = _list_items(api, media, folder or "children", view_id, request)
    except JellyfinError as error:
        LOG.warning("browse failed (%s/%s): %s", media, folder, error)
        xbmcplugin.endOfDirectory(request.handle, succeeded=False)
        return

    _add_items(request, api, items, view_id, media)
    xbmcplugin.setContent(request.handle, content)
    for method in (
        xbmcplugin.SORT_METHOD_UNSORTED,
        xbmcplugin.SORT_METHOD_LABEL,
        xbmcplugin.SORT_METHOD_VIDEO_YEAR,
        xbmcplugin.SORT_METHOD_DATEADDED,
    ):
        xbmcplugin.addSortMethod(request.handle, method)
    xbmcplugin.endOfDirectory(request.handle)


def _node_menu(request: Request, media: str, view_id: str) -> None:
    entries = []
    for key, label_id in NODES[media]:
        li = xbmcgui.ListItem(settings.localized(label_id))
        path = listitems.plugin_url(
            {"mode": "browse", "view": view_id, "type": media, "folder": key}
        )
        entries.append((path, li, True))
    xbmcplugin.addDirectoryItems(request.handle, entries, len(entries))
    xbmcplugin.setContent(request.handle, "files")
    xbmcplugin.endOfDirectory(request.handle)


def _list_items(
    api: Api, media: str, folder: str, view_id: str, request: Request
) -> Tuple[List[JsonDict], str]:
    """Fetch the item list for a folder; returns (items, kodi content type)."""
    # Special routes first.
    if folder == "nextup":
        result = api.next_up(view_id)
        return result.get("Items", []), "episodes"
    if folder == "genres":
        result = api.genres(view_id, _genre_types(media))
        return result.get("Items", []), "files"
    if folder == "artists":
        result = api.artists(view_id)
        return result.get("Items", []), "artists"

    query = node_query(media, folder, view_id)
    if query is not None:
        content = _node_content(media, folder)
        return api.items(query).get("Items", []), content

    # Drill-down into a concrete item id.
    item_type = media  # for drill-down paths, `type` carries the item type
    if item_type == "series":
        return api.seasons(folder).get("Items", []), "seasons"
    if item_type == "season":
        series = request.params.get("series", "")
        return (
            api.episodes(series, folder, BROWSE_FIELDS).get("Items", []),
            "episodes",
        )
    if item_type == "musicartist":
        result = api.items(
            {
                "ArtistIds": folder,
                "IncludeItemTypes": "MusicAlbum",
                "Recursive": True,
                "Fields": BROWSE_FIELDS,
                "SortBy": "ProductionYear,SortName",
            }
        )
        return result.get("Items", []), "albums"
    if item_type == "musicalbum":
        result = api.items(
            {
                "ParentId": folder,
                "Fields": BROWSE_FIELDS,
                "SortBy": "ParentIndexNumber,IndexNumber,SortName",
            }
        )
        return result.get("Items", []), "songs"

    # Generic container (boxset, playlist, folder, photo album, view children).
    parent = folder if folder != "children" else view_id
    result = api.items(
        {"ParentId": parent, "Fields": BROWSE_FIELDS, "SortBy": "SortName"}
    )
    items = result.get("Items", [])
    content = "movies" if item_type == "boxset" else _guess_content(items)
    return items, content


def _add_items(
    request: Request, api: Api, items: List[JsonDict], view_id: str, media: str
) -> None:
    entries = []
    for item in items:
        li = listitems.build(item, api.server)
        item_type = item.get("Type", "")

        if item_type in ("Genre", "MusicGenre"):
            path = listitems.plugin_url(
                {
                    "mode": "browse",
                    "view": view_id,
                    "type": media,
                    "folder": "genre-%s" % item.get("Id", ""),
                }
            )
            entries.append((path, li, True))
            continue

        if item_type == "Photo":
            tags = item.get("ImageTags") or {}
            path = api.image_url(item.get("Id", ""), "Primary", tags.get("Primary", ""))
            entries.append((path, li, False))
            continue

        path = listitems.path_for(item)
        if item_type == "Season":
            path = listitems.plugin_url(
                {
                    "mode": "browse",
                    "folder": item.get("Id", ""),
                    "type": "season",
                    "series": item.get("SeriesId", ""),
                }
            )
        li.addContextMenuItems(listitems.watched_context(item))
        entries.append((path, li, listitems.is_folder(item)))

    xbmcplugin.addDirectoryItems(request.handle, entries, len(entries))


def _node_content(media: str, node: str) -> str:
    if node in ("recentepisodes", "inprogressepisodes", "nextup"):
        return "episodes"
    if node in ("albums", "recentalbums", "favoritealbums"):
        return "albums"
    if node == "sets":
        return "movies"
    return CONTENT_TYPES.get(media, "videos")


def _genre_types(media: str) -> str:
    return {
        "movies": "Movie",
        "tvshows": "Series",
        "music": "MusicAlbum",
        "musicvideos": "MusicVideo",
    }.get(media, "")


def _guess_content(items: List[JsonDict]) -> str:
    for item in items:
        content = {
            "Movie": "movies",
            "Series": "tvshows",
            "Episode": "episodes",
            "Audio": "songs",
            "MusicAlbum": "albums",
            "Photo": "images",
            "PhotoAlbum": "images",
        }.get(item.get("Type", ""))
        if content:
            return content
    return "videos"
