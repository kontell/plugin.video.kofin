"""Directory listings: addon root, library nodes, and drill-down browsing."""

import os
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

# The addon fanart asset, used as the backdrop behind structural listings.
# Named rather than imported from service/backdrop.py (which owns the file and
# rewrites it): the plugin process has no business importing service code for
# a filename. Keep the two in step — this is the same asset addon.xml declares,
# so whatever the backdrop setting has put there is what listings show.
BACKDROP_IMAGE = "fanart.png"

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

# Stock Kodi icons for structural entries (never addon art — the skin
# substitutes its own native artwork for Default*.png names). Server artwork
# belongs only on real media items.
MEDIA_ICONS = {
    "movies": "DefaultMovies.png",
    "tvshows": "DefaultTVShows.png",
    "musicvideos": "DefaultMusicVideos.png",
    "music": "DefaultAddonMusic.png",
    "books": "DefaultAddonInformation.png",
    "homevideos": "DefaultAddonVideo.png",
    "playlists": "DefaultPlaylist.png",
    "boxsets": "DefaultSets.png",
    # Jellyfin DVR UserView often has no CollectionType; we map it to this key.
    "recordings": "DefaultPVRRecordings.png",
}

NODE_ICONS: Dict[str, Any] = {
    "recent": {
        "movies": "DefaultRecentlyAddedMovies.png",
        "musicvideos": "DefaultRecentlyAddedMusicVideos.png",
    },
    "recentepisodes": "DefaultRecentlyAddedEpisodes.png",
    "recentalbums": "DefaultRecentlyAddedAlbums.png",
    "inprogress": "DefaultInProgressShows.png",
    "inprogressepisodes": "DefaultInProgressShows.png",
    "nextup": "DefaultInProgressShows.png",
    "favorites": "DefaultFavourites.png",
    "favoritealbums": "DefaultFavourites.png",
    "sets": "DefaultSets.png",
    "genres": "DefaultGenre.png",
    "artists": "DefaultMusicArtists.png",
    "albums": "DefaultMusicAlbums.png",
}


# Rows that stand for a piece of media rather than for a place to go. The
# addon backdrop never fills one (:func:`apply_backdrop` is called only for
# the others): it is the *addon's* artwork, and drawn behind a media item it
# reads as that item's own. Recordings are where that showed — a DVR library's
# items carry a Primary thumbnail and no backdrop at all, so every row in it
# was backed by kofin's fanart. An empty background is the honest answer for an
# item the server has no artwork for; filling it is the skin's call, not ours.
#
# Everything else a listing holds — genres, folders, playlists, library rows,
# the Extras link — stands for a query or a container and keeps the backdrop.
MEDIA_TYPES = frozenset(
    {
        "Movie",
        "Episode",
        "Series",
        "Season",
        "MusicVideo",
        "Video",
        "Trailer",
        "Recording",
        "BoxSet",
        "Audio",
        "MusicAlbum",
        "MusicArtist",
        "Photo",
    }
)


def is_media_row(item: JsonDict) -> bool:
    return item.get("Type", "") in MEDIA_TYPES


def node_icon(media: str, node: str = "") -> str:
    icon = NODE_ICONS.get(node)
    if isinstance(icon, dict):
        icon = icon.get(media)
    return icon or MEDIA_ICONS.get(media, "DefaultVideo.png")


def structural_art(icon: str) -> Dict[str, str]:
    """Art for a structural row: a node-menu entry, a genre, an Extras link.

    Both keys, always. Contuary binds the list glyph to ListItem.Icon, which
    prefers thumb over Art(icon), so an icon-only row draws nothing at all --
    and the listing that holds these rows has to leave its content type empty
    for the same reason the root does: with "files" the skin switches to
    ListWatchedIconVar (folder and status overlays) and ignores setArt(icon)
    outright. The two rules only work together, which is why they are
    described in one place.

    Server artwork never comes through here. These rows stand for a query, not
    for a thing that has a poster.

    The addon backdrop comes from :func:`with_backdrop`, not from here: a
    structural row is only one of the kinds of row that has no fanart of its
    own.
    """
    return with_backdrop({"icon": icon, "thumb": icon})


def with_backdrop(art: Dict[str, str]) -> Dict[str, str]:
    """Fill in the addon backdrop for an item that has no fanart of its own.

    A fallback, not a decoration — it only ever fills an empty slot, so an
    item with real server artwork keeps it. Media rows are excluded at the
    call site rather than here (see :data:`MEDIA_TYPES`).

    Two kinds of row need this, which is why it is not folded into
    :func:`structural_art`. Structural rows stand for a query and have no art
    at all. Library rows *look* like they should be covered — they carry a
    Primary image, which is what the skin's side panel shows — but a Jellyfin
    UserView has no ``BackdropImageTags``, so ``art_for`` never sets fanart
    for one and the background stayed empty on exactly the rows the addon
    root is mostly made of.

    It goes on the *item* because that is the only mechanism that works:
    ``setPluginFanart`` and ``setProperty(handle, "fanart_image")`` were both
    measured live on Kodi 21 and left ``Container.Art(fanart)`` empty with
    nothing drawn. Being per-item, the backdrop tracks focus, and Kodi's
    synthesised ".." row carries no art and takes none, so the background
    still blanks while the parent row is focused. Fixing that needs a
    skin-side fallback; there is no addon-side hook for it.
    """
    if not art.get("fanart"):
        backdrop = _addon_media(BACKDROP_IMAGE)
        if backdrop:
            art["fanart"] = backdrop
    return art


def apply_backdrop(li: xbmcgui.ListItem) -> None:
    """:func:`with_backdrop` for a row whose art is already on the ListItem.

    For the structural rows a listing holds — a genre, a folder, a playlist —
    which carry no server artwork of their own and would otherwise leave the
    background empty. Media rows are the caller's to exclude
    (:func:`is_media_row`).
    """
    if not li.getArt("fanart"):
        li.setArt(with_backdrop({}))


def _collection_type(view: JsonDict) -> str:
    """Jellyfin CollectionType for a root view, with recordings inferred.

    The DVR Recordings UserView commonly ships with an empty CollectionType,
    so node_icon would fall through to DefaultVideo.png. Name-match is the
    stable signal Jellyfin clients use when the enum is missing.
    """
    collection = view.get("CollectionType") or ""
    if collection:
        return collection
    name = (view.get("Name") or "").lower()
    if "recording" in name:
        return "recordings"
    return ""


def _addon_media(filename: str) -> str:
    """Absolute path to a file under resources/media/.

    Empty when the addon path is unknown (same defensive posture as
    toast.addon_icon): Kodi draws a blank glyph for a path to nowhere.
    """
    try:
        path = settings.addon_path()
    except Exception:  # pragma: no cover - defensive
        return ""
    if not path:
        return ""
    return os.path.join(path, "resources", "media", filename)


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


def _who_is_watching_label(api: Api) -> str:
    """Root label reflecting who is on the session: the base 'Who's watching?'
    plus any additional users. One extra /Sessions round trip per root render —
    negligible next to the views() call already made, and skipped silently on
    error so the entry always renders."""
    base = settings.localized(30041)
    try:
        sessions = api.device_sessions(Credentials.load().device_id)
    except JellyfinError as error:
        LOG.debug("who's-watching label: sessions unavailable: %s", error)
        return base
    if not sessions:
        return base
    names = [
        user.get("UserName", "")
        for user in (sessions[0].get("AdditionalUsers") or [])
        if user.get("UserName")
    ]
    if not names:
        return base
    return settings.localized(30046) % ", ".join(names)


def root(request: Request) -> None:
    if request.handle < 0:
        return
    api = _api()
    entries: List[Tuple[str, xbmcgui.ListItem, bool]] = []

    if api is not None:
        # First, the way the web client leads with it: the one entry that is
        # about what the viewer was in the middle of rather than about where it
        # is filed. Offered without asking the server whether it has anything —
        # every root render would pay for that question, and the answer changes
        # with every playback. Stock art on icon and thumb: Contuary list
        # glyphs bind ListItem.Icon, which prefers thumb over Art(icon).
        resume_li = xbmcgui.ListItem(settings.localized(30049))
        resume_art = node_icon("", "inprogress")
        resume_li.setArt(structural_art(resume_art))
        entries.append(
            (listitems.plugin_url({"mode": "continuewatching"}), resume_li, True)
        )

        try:
            views = api.views().get("Items", [])
        except JellyfinError as error:
            LOG.warning("views unavailable: %s", error)
            views = []
        for view in views:
            if view.get("CollectionType") == "livetv":
                continue  # live TV is pvr.kofin's job
            collection = _collection_type(view)
            li = listitems.build(view, api.server)
            # Contuary binds the list glyph to ListItem.Icon, which prefers
            # thumb over Art(icon). Put stock Default*.png on icon *and*
            # thumb so the list shows themed icons; keep server Primary on
            # poster for the focus pane (InfoWallThumbVar prefers poster).
            art = listitems.art_for(view, api.server)
            icon = node_icon(collection)
            primary = art.get("thumb") or art.get("poster")
            art["icon"] = icon
            art["thumb"] = icon
            if primary:
                art["poster"] = primary
            li.setArt(with_backdrop(art))
            params = {"mode": "browse", "view": view.get("Id", ""), "type": collection}
            if collection not in NODES:
                params["folder"] = "children"
            entries.append((listitems.plugin_url(params), li, True))

    import xbmc

    if api is not None:
        adduser_li = xbmcgui.ListItem(_who_is_watching_label(api))
        watching_art = _addon_media("person-search.png") or "DefaultUser.png"
        adduser_li.setArt(structural_art(watching_art))
        entries.append((listitems.plugin_url({"mode": "adduser"}), adduser_li, False))

    # SyncPlay root entry (phase 4): gated on the master toggle, read fresh
    # each listing, and hidden when an external player is configured.
    from kofin.plugin import syncplay

    if api is not None and syncplay.available():
        syncplay_li = xbmcgui.ListItem(settings.localized(30560))
        syncplay_art = _addon_media("syncplay-groups.png") or "DefaultUser.png"
        syncplay_li.setArt(structural_art(syncplay_art))
        entries.append((listitems.plugin_url({"mode": "syncplay"}), syncplay_li, False))
    settings_li = xbmcgui.ListItem(xbmc.getLocalizedString(5))  # "Settings"
    settings_art = "DefaultAddonService.png"
    settings_li.setArt(structural_art(settings_art))
    entries.append((listitems.plugin_url({"mode": "settings"}), settings_li, False))

    xbmcplugin.addDirectoryItems(request.handle, entries, len(entries))
    # Empty content (not "files"): Contuary/Estuary WideList only binds
    # $INFO[ListItem.Icon] when Container.Content() is empty — with "files"
    # it uses ListWatchedIconVar (folder/dot status overlays) and ignores
    # setArt(icon=…). Library focus art stays on poster (see above).
    xbmcplugin.setContent(request.handle, "")
    xbmcplugin.endOfDirectory(request.handle)


def next_episodes(request: Request) -> None:
    """Next-up episodes for a library — the target of the generated
    'nextepisodes' video node (dynamic content Kodi can't express as a
    node filter)."""
    if request.handle < 0:
        return
    api = _api()
    if api is None:
        xbmcplugin.endOfDirectory(request.handle, succeeded=False)
        return

    view_id = request.params.get("id", "")
    try:
        items = api.next_up(view_id, BROWSE_FIELDS).get("Items", [])
    except JellyfinError as error:
        LOG.warning("next episodes failed (%s): %s", view_id, error)
        xbmcplugin.endOfDirectory(request.handle, succeeded=False)
        return

    _add_items(request, api, items, view_id, "tvshows")
    xbmcplugin.setContent(request.handle, "episodes")
    xbmcplugin.endOfDirectory(request.handle)


def continue_watching(request: Request) -> None:
    """In-progress items across every library (mode=continuewatching).

    A live listing, like the extras node: nothing is written and nothing is
    cached, because a resume point is exactly the thing that has changed by the
    time the viewer comes back to it. The per-library "In progress" nodes ask
    /Items for one library's resumable items; this asks the server for the list
    it already keeps, so the entries and their order are the ones the web
    client shows.

    No sort methods are offered. The server hands these back most recently
    played first, which is the whole point of the listing -- letting Kodi sort
    them by title would throw that away.
    """
    if request.handle < 0:
        return
    api = _api()
    if api is None:
        xbmcplugin.endOfDirectory(request.handle, succeeded=False)
        return

    try:
        items = api.resume(BROWSE_FIELDS).get("Items", [])
    except JellyfinError as error:
        LOG.warning("continue watching failed: %s", error)
        xbmcplugin.endOfDirectory(request.handle, succeeded=False)
        return

    _add_items(request, api, items, "", "")
    # Movies and episodes in one listing: "videos" is the content type that
    # describes both, and the items carry their own media type for the skin.
    xbmcplugin.setContent(request.handle, "videos")
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
            _node_menu(request, api, media, view_id)
            return
        if folder == "extras" and media == "tvshows":
            _extras_node(request, api, view_id)
            return
        items, content = _list_items(api, media, folder or "children", view_id, request)
    except JellyfinError as error:
        LOG.warning("browse failed (%s/%s): %s", media, folder, error)
        xbmcplugin.endOfDirectory(request.handle, succeeded=False)
        return

    _add_items(request, api, items, view_id, media)
    if media in ("series", "season"):
        _append_extras_entry(request, api, folder)
    xbmcplugin.setContent(request.handle, content)
    for method in (
        xbmcplugin.SORT_METHOD_UNSORTED,
        xbmcplugin.SORT_METHOD_LABEL,
        xbmcplugin.SORT_METHOD_VIDEO_YEAR,
        xbmcplugin.SORT_METHOD_DATEADDED,
    ):
        xbmcplugin.addSortMethod(request.handle, method)
    xbmcplugin.endOfDirectory(request.handle)


def extras(request: Request) -> None:
    """Special features of a series/season (mode=extras) — a live listing
    over the SpecialFeatures endpoint, no DB writes (plan §2 TV extras)."""
    if request.handle < 0:
        return
    api = _api()
    if api is None:
        xbmcplugin.endOfDirectory(request.handle, succeeded=False)
        return

    item_id = request.params.get("id", "")
    try:
        items = api.special_features(item_id)
    except JellyfinError as error:
        LOG.warning("extras listing failed (%s): %s", item_id, error)
        xbmcplugin.endOfDirectory(request.handle, succeeded=False)
        return

    _add_items(request, api, items, "", "")
    xbmcplugin.setContent(request.handle, "videos")
    xbmcplugin.endOfDirectory(request.handle)


def _node_menu(request: Request, api: Api, media: str, view_id: str) -> None:
    nodes = list(NODES[media])
    if media == "tvshows" and _view_has_specials(api, view_id):
        nodes.append(("extras", 30500))
    entries = []
    for key, label_id in nodes:
        li = xbmcgui.ListItem(settings.localized(label_id))
        li.setArt(structural_art(node_icon(media, key)))
        path = listitems.plugin_url(
            {"mode": "browse", "view": view_id, "type": media, "folder": key}
        )
        entries.append((path, li, True))
    xbmcplugin.addDirectoryItems(request.handle, entries, len(entries))
    xbmcplugin.setContent(request.handle, "")
    xbmcplugin.endOfDirectory(request.handle)


def _extras_node(request: Request, api: Api, view_id: str) -> None:
    """The Extras node: series in the view that advertise special features,
    each opening its extras listing."""
    result = api.items(
        {
            "ParentId": view_id,
            "IncludeItemTypes": "Series",
            "Recursive": True,
            "HasSpecialFeature": True,
            "Fields": BROWSE_FIELDS,
            "SortBy": "SortName",
            "SortOrder": "Ascending",
        }
    )
    entries = []
    for item in result.get("Items", []):
        # No apply_backdrop: these are series rows, and a media row keeps
        # whatever backdrop the server gave it or none (MEDIA_TYPES).
        li = listitems.build(item, api.server)
        path = listitems.plugin_url({"mode": "extras", "id": item.get("Id", "")})
        entries.append((path, li, True))
    xbmcplugin.addDirectoryItems(request.handle, entries, len(entries))
    xbmcplugin.setContent(request.handle, "tvshows")
    xbmcplugin.endOfDirectory(request.handle)


def _view_has_specials(api: Api, view_id: str) -> bool:
    """Whether any series in the view has special features (gates the node)."""
    try:
        result = api.items(
            {
                "ParentId": view_id,
                "IncludeItemTypes": "Series",
                "Recursive": True,
                "HasSpecialFeature": True,
                "Limit": 1,
            }
        )
    except JellyfinError:
        return False
    return bool(result.get("Items"))


def _append_extras_entry(request: Request, api: Api, item_id: str) -> None:
    """An Extras entry at the end of a series/season drill-down when its DTO
    reports special features."""
    try:
        count = api.item(item_id).get("SpecialFeatureCount") or 0
    except JellyfinError:
        return
    if not count:
        return
    li = xbmcgui.ListItem(settings.localized(30500))
    li.setArt(structural_art("DefaultVideo.png"))
    path = listitems.plugin_url({"mode": "extras", "id": item_id})
    xbmcplugin.addDirectoryItems(request.handle, [(path, li, True)], 1)


def _list_items(
    api: Api, media: str, folder: str, view_id: str, request: Request
) -> Tuple[List[JsonDict], str]:
    """Fetch the item list for a folder; returns (items, kodi content type)."""
    # Special routes first.
    if folder == "nextup":
        result = api.next_up(view_id)
        return result.get("Items", []), "episodes"
    if folder == "genres":
        # Empty content, like the node menu above it: genres are structural
        # rows carrying a stock glyph, and "files" is what hides it
        # (structural_art).
        result = api.genres(view_id, _genre_types(media))
        return result.get("Items", []), ""
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
        if not is_media_row(item):
            apply_backdrop(li)
        item_type = item.get("Type", "")

        if item_type in ("Genre", "MusicGenre"):
            if not li.getArt("thumb"):
                li.setArt(structural_art("DefaultGenre.png"))
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
        # No addContextMenuItems here: Kodi pins a listing's own entries to the
        # very top of the context menu, above its Play, and the two kofin used
        # to add there duplicated the wording of Kodi's own "Mark as watched"
        # and "Add to favourites" lower down. Both now live in the Jellyfin
        # actions menu (plugin/context.py), which owns server-side actions.
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
