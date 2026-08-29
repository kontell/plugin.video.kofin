"""Directory listings: addon root, library nodes, and drill-down browsing."""

import os
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import xbmcgui
import xbmcplugin

from kofin.core import settings, state
from kofin.core.api import Api
from kofin.core.http import JellyfinError
from kofin.core.log import Logger
from kofin.core.settings import Credentials
from kofin.core.urls import plugin_url
from kofin.plugin import listitems
from kofin.plugin.router import Request

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

# The addon fanart asset, used as the backdrop behind structural listings.
# Named rather than imported from service/backdrop.py (which owns the file and
# rewrites it): the plugin process has no business importing service code for
# a filename. Keep the two in step — this is the same asset addon.xml declares,
# so whatever the backdrop setting has put there is what listings show.
BACKDROP_IMAGE = "fanart.webp"

BROWSE_FIELDS = (
    "Overview,Genres,Studios,Taglines,PremiereDate,ProductionYear,"
    "OfficialRating,CommunityRating,RunTimeTicks,DateCreated,"
    "ProviderIds,SortName"
)

# MediaStreams is the field that prices a listing: measured on a 1,766-movie
# library, the whole-library query is 12.7 MB and 1.4 s of server time with it
# against 3.5 MB and 0.47 s without (docs/perf-hardening-plan.md W2.1). So
# bounded listings keep it — 25 rows of codec/HDR flags and stream details are
# worth 25 rows of payload — and whole-library nodes drop it. Music drops it
# unconditionally: _fill_music never reads stream details. The rows that lose
# it render without codec/resolution flags, which is the stated trade.
BROWSE_FIELDS_STREAMS = BROWSE_FIELDS + ",MediaStreams"

# How many rows a "recent" node shows. Also what makes those nodes bounded,
# which is what earns them the fields above.
RECENT_LIMIT = 25


def bounded_fields() -> str:
    """The field list for a listing short enough to carry the expensive ones.

    MediaStreams always (BROWSE_FIELDS_STREAMS). People only when the viewer
    asked for cast in the add-on's listings (browseCast, off by default).

    The gate is boundedness, and it is not a preference: People costs the
    *server* about 25 ms per row on 10.11, linear in the result set. Measured
    on one library: 25 recently-added films 0.12 s -> 0.71 s, 61 favourites
    0.10 s -> 1.6 s, a 417-film genre 0.28 s -> 10.4 s, 901 unwatched 0.39 s
    -> 21.9 s, and the whole 1,778-film library 0.66 s -> 44.0 s. A listing
    that takes 44 seconds to open is a broken listing, so the whole-library
    and filter nodes (all, unwatched, favorites, sets, genres, years, tags,
    the alphabet) never ask for it, whatever the setting says — and neither
    does music, which reads no people at all.

    Every caller is a listing something already bounds: a node with a Limit,
    Next up, Continue watching, a season's episodes, search results and a
    person's filmography (both SEARCH_LIMIT). Extras are the one bounded
    listing without it — /Items/{id}/SpecialFeatures takes no Fields.

    Closing the gap for the big nodes needs paging, not a bigger field list
    (docs/dynamic-libraries-plan.md §2 and W7).
    """
    fields = BROWSE_FIELDS_STREAMS
    if settings.get_bool("browseCast"):
        fields += ",People"
    return fields


# Node menus per collection type: (folder key, label string id).
#
# A label id under 30000 or over 30999 is Kodi's own — :func:`node_label`
# resolves it through xbmc rather than the addon, so a node whose wording Kodi
# already ships costs nothing in the 27 generated locales. Years, Tags, Album
# artists, Last played and Top 100 songs are all lifted from Kodi's own library
# nodes (system/library/{video,music}), which is also where their icons come
# from.
NODES: Dict[str, List[Tuple[str, int]]] = {
    "movies": [
        ("all", 30030),
        ("recent", 30031),
        ("inprogress", 30033),
        ("unwatched", 30034),
        ("favorites", 30035),
        ("sets", 30038),
        ("genres", 30036),
        ("years", 652),
        ("tags", 20459),
        ("alpha", 30818),
        ("random", 30037),
    ],
    "tvshows": [
        ("all", 30030),
        ("recentepisodes", 30031),
        ("nextup", 30032),
        ("inprogressepisodes", 30033),
        ("unwatched", 30034),
        ("favorites", 30035),
        ("genres", 30036),
        ("years", 652),
        ("tags", 20459),
        ("alpha", 30818),
        ("random", 30037),
    ],
    "music": [
        ("artists", 30039),
        ("albumartists", 38043),
        ("albums", 30040),
        ("recentalbums", 30031),
        ("lastplayed", 568),
        ("topsongs", 10504),
        ("favoritealbums", 30035),
        ("genres", 30036),
        ("alpha", 30818),
    ],
    "musicvideos": [
        ("all", 30030),
        ("recent", 30031),
        ("unwatched", 30034),
        ("favorites", 30035),
        ("genres", 30036),
        ("alpha", 30818),
        ("random", 30037),
    ],
}

# The alphabet menu's rows. "#" is everything sorting before A, which Jellyfin
# answers with NameLessThan rather than a NameStartsWith it has no character
# for (verified: 26 films on this server, against 99 for D).
ALPHABET = ("#",) + tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

# Play history is song-level. Filters=IsPlayed against MusicAlbum returns zero,
# and sorting albums by DatePlayed with no filter puts unplayed ones first, so
# both history nodes list songs (verified against a 20,802-song library: 7,176
# played).
PLAY_HISTORY_LIMIT = 100

# "Recently added albums" is not a SortBy either: an album's DateCreated is
# when the scanner last created its row, and a rescan re-creates rows in folder
# order, so sorting albums by it listed a scan rather than arrivals — 25
# A-artists stamped within one second, on the test library. The node goes
# through /Items/Latest (Api.latest), which sorts the songs and groups them by
# album, exactly as the web client does.

# Past this many tags the menu shows initials first. Under it, a flat list is
# friendlier than a letter you have to guess.
TAG_MENU_MAX = 200

# What search offers, as {type key: (Kodi core string id, IncludeItemTypes,
# Kodi content type)}. Core string ids throughout — a feature whose every
# label already exists in Kodi costs nothing in the 27 generated locales.
SEARCH_KINDS: Dict[str, Tuple[int, str, str]] = {
    "movies": (20342, "Movie", "movies"),
    "tvshows": (20343, "Series", "tvshows"),
    "episodes": (20360, "Episode", "episodes"),
    "albums": (132, "MusicAlbum", "albums"),
    "songs": (134, "Audio", "songs"),
    "people": (344, "", ""),
}

SEARCH_ICONS = {
    "movies": "DefaultMovies.png",
    "tvshows": "DefaultTVShows.png",
    "episodes": "DefaultTVShows.png",
    "albums": "DefaultMusicAlbums.png",
    "songs": "DefaultMusicSongs.png",
    "people": "DefaultActor.png",
}

# Search results are bounded on purpose: the caller is waiting on this fetch,
# and a hundred rows is already past what anyone reads. Plain BROWSE_FIELDS
# rather than the streams set — a hundred rows of codec detail is the payload
# BROWSE_FIELDS_STREAMS is measured against, and a search row is picked by
# name, not by codec.
SEARCH_LIMIT = 100

CONTENT_TYPES = {
    "movies": "movies",
    "tvshows": "tvshows",
    "musicvideos": "musicvideos",
    "music": "artists",
}

# The Jellyfin item type one collection type is made of: the genre listing,
# the year/tag filter menus and the tag pages all ask by it, and each used to
# carry its own copy of this map (P2.1).
ITEM_TYPES = {
    "movies": "Movie",
    "tvshows": "Series",
    "music": "MusicAlbum",
    "musicvideos": "MusicVideo",
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
    # Not DefaultRecentlyAddedAlbums.png, which reads like the video names
    # above and renders as nothing: no skin has it. Kodi's own node for this
    # (system/library/music/recentlyaddedalbums.xml) says
    # DefaultMusicRecentlyAdded.png, and the music icons follow that shape
    # throughout — DefaultMusicRecentlyPlayed, DefaultMusicRoles, and so on.
    "recentalbums": "DefaultMusicRecentlyAdded.png",
    "inprogress": "DefaultInProgressShows.png",
    "inprogressepisodes": "DefaultInProgressShows.png",
    "nextup": "DefaultInProgressShows.png",
    "favorites": "DefaultFavourites.png",
    "favoritealbums": "DefaultFavourites.png",
    "sets": "DefaultSets.png",
    "genres": "DefaultGenre.png",
    "artists": "DefaultMusicArtists.png",
    "albumartists": "DefaultMusicArtists.png",
    "albums": "DefaultMusicAlbums.png",
    # Kodi's own nodes for the same things: video/movies/years.xml says
    # DefaultYear, music/years.xml says DefaultMusicYears, and both tags.xml
    # files say DefaultTags. Verified present in the skin's media folder.
    "years": {
        "movies": "DefaultYear.png",
        "tvshows": "DefaultYear.png",
        "musicvideos": "DefaultYear.png",
        "music": "DefaultMusicYears.png",
    },
    "tags": "DefaultTags.png",
    "lastplayed": "DefaultMusicRecentlyPlayed.png",
    "topsongs": "DefaultMusicTop100Songs.png",
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
    if node == "alpha":
        # The one structural row with no Kodi counterpart to substitute: no
        # skin ships an alphabet glyph. Material Symbols "sort_by_alpha"
        # (Apache-2.0), shipped white-on-transparent like the addon's others.
        return _addon_media("alphabet.png") or "DefaultMusicArtists.png"
    icon = NODE_ICONS.get(node)
    if isinstance(icon, dict):
        icon = icon.get(media)
    return icon or MEDIA_ICONS.get(media, "DefaultVideo.png")


def node_label(label_id: int) -> str:
    """A node's label, from whichever string table owns the id.

    The addon's own ids are 30000-30999; everything else in NODES is a Kodi
    core id, lifted from the shipped node that means the same thing. Asking
    the addon for a core id returns an empty string, which renders as a
    nameless row.
    """
    if 30000 <= label_id <= 30999:
        return settings.localized(label_id)

    import xbmc

    return xbmc.getLocalizedString(label_id)


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
    elif node.startswith("genre-"):
        base["GenreIds"] = node.split("-", 1)[1]
        base["IncludeItemTypes"] = types
    elif node.startswith("alpha-"):
        letter = node.split("-", 1)[1]
        base["IncludeItemTypes"] = "MusicArtist" if media == "music" else types
        # "#" is everything before A, which has no NameStartsWith character.
        if letter == "#":
            base["NameLessThan"] = "A"
        else:
            base["NameStartsWith"] = letter
    elif node.startswith("year-"):
        base.update(IncludeItemTypes=types, Years=node.split("-", 1)[1])
    elif node.startswith("tag-"):
        base.update(IncludeItemTypes=types, Tags=node.split("-", 1)[1])
    elif node == "lastplayed":
        base.update(
            IncludeItemTypes="Audio",
            Filters="IsPlayed",
            SortBy="DatePlayed",
            SortOrder="Descending",
            Limit=PLAY_HISTORY_LIMIT,
        )
    elif node == "topsongs":
        base.update(
            IncludeItemTypes="Audio",
            Filters="IsPlayed",
            SortBy="PlayCount",
            SortOrder="Descending",
            Limit=PLAY_HISTORY_LIMIT,
        )
    else:
        return None
    # Boundedness decides the field list: a Limit is what makes the payload
    # per-row cost worth paying (see bounded_fields).
    if media != "music" and "Limit" in base:
        base["Fields"] = bounded_fields()
    return base


def _api() -> Optional[Api]:
    creds = Credentials.load()
    if not creds.is_logged_in:
        return None
    return Api.for_plugin(creds)


Builder = Callable[[Request, Api], None]


def listing(request: Request, build: Builder, what: str) -> None:
    """The opening every listing route shares (P2.1): nothing for a
    handle-less invocation, a failed directory for a logged-out one, and a
    failed directory — rather than a Kodi error dialog — when the server
    refuses the fetch. ``build`` does the route's own work with the Api;
    phase 0's router ``finally`` already guarantees the handle closes on
    any other exception, so this is the same shape the six routes spelled
    one by one, spelled once.

    The root listing is not one of them on purpose: it tolerates a
    logged-out state (Settings must stay reachable) and treats an
    unavailable view list as a warning, not a failure.
    """
    if request.handle < 0:
        return
    api = _api()
    if api is None:
        xbmcplugin.endOfDirectory(request.handle, succeeded=False)
        return
    try:
        build(request, api)
    except JellyfinError as error:
        LOG.warning("%s failed %s: %s", what, dict(request.params), error)
        xbmcplugin.endOfDirectory(request.handle, succeeded=False)


StructuralRow = Tuple[str, str, Dict[str, str]]


def structural_row(
    label: str, icon: str, params: Dict[str, str], folder: bool = True
) -> Tuple[str, xbmcgui.ListItem, bool]:
    """One row that stands for a place to go rather than a piece of media:
    a label, stock art on both keys (:func:`structural_art`), a plugin URL."""
    li = xbmcgui.ListItem(label)
    li.setArt(structural_art(icon))
    return plugin_url(params), li, folder


def structural_rows(
    request: Request, rows: Iterable[StructuralRow], content: str = ""
) -> None:
    """A whole structural menu — the search kinds, the alphabet, a
    library's nodes, its years or tags — from ``(label, icon, params)``
    triples, closed with the empty content type the skins need to draw
    ``setArt(icon)`` instead of watched-status overlays (P2.1)."""
    entries = [structural_row(label, icon, params) for label, icon, params in rows]
    xbmcplugin.addDirectoryItems(request.handle, entries, len(entries))
    xbmcplugin.setContent(request.handle, content)
    xbmcplugin.endOfDirectory(request.handle)


def _who_is_watching_label() -> str:
    """Root label reflecting who is on the session: the base 'Who's watching?'
    plus any additional users.

    Read from the window property the service publishes (state.PROP_WHO_NAMES)
    rather than from /Sessions: the service owns every change to the set (the
    picker worker, the connect-time restore) and publishes as it goes, while
    the round trip here priced every root render — and, against an unreachable
    server, hung the root for the call's whole retry ladder. An empty or stale
    property degrades to the base label, which is also what the round trip's
    error path answered."""
    names = state.watching_names()
    if not names:
        return settings.localized(30041)
    return settings.localized(30046) % ", ".join(names)


def root(request: Request) -> None:
    if request.handle < 0:
        return
    api = _api()
    entries: List[Tuple[str, xbmcgui.ListItem, bool]] = []

    import xbmc

    if api is not None:
        # First, the way the web client leads with it: the one entry that is
        # about what the viewer was in the middle of rather than about where it
        # is filed. Offered without asking the server whether it has anything —
        # every root render would pay for that question, and the answer changes
        # with every playback. Stock art on icon and thumb: Contuary list
        # glyphs bind ListItem.Icon, which prefers thumb over Art(icon).
        entries.append(
            structural_row(
                settings.localized(30049),
                node_icon("", "inprogress"),
                {"mode": "continuewatching"},
            )
        )

        # Search sits with Continue watching, above the libraries: both are
        # ways in that are not a place, and a viewer who knows what they want
        # should not have to pick a library first.
        entries.append(
            structural_row(
                xbmc.getLocalizedString(137),  # Search
                "DefaultAddonsSearch.png",
                {"mode": "search"},
            )
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
            entries.append((plugin_url(params), li, True))

    # "Who's watching?" — gone entirely when the Advanced-tab shortlist has
    # nobody on it, which is how the feature is switched off (adduser.py).
    from kofin.service import whoswatching

    if api is not None and whoswatching.is_enabled():
        entries.append(
            structural_row(
                _who_is_watching_label(),
                _addon_media("person-search.png") or "DefaultUser.png",
                {"mode": "adduser"},
                folder=False,
            )
        )

    # SyncPlay root entry (phase 4): gated on the master toggle, read fresh
    # each listing, and hidden when an external player is configured.
    from kofin.syncplay import offer

    if api is not None and offer.available():
        entries.append(
            structural_row(
                settings.localized(30560),
                _addon_media("syncplay-groups.png") or "DefaultUser.png",
                {"mode": "syncplay"},
                folder=False,
            )
        )
    entries.append(
        structural_row(
            xbmc.getLocalizedString(5),  # "Settings"
            "DefaultAddonService.png",
            {"mode": "settings"},
            folder=False,
        )
    )

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

    def build(request: Request, api: Api) -> None:
        view_id = request.params.get("id", "")
        items = api.next_up(view_id, bounded_fields()).get("Items", [])
        _add_items(request, api, items, view_id, "tvshows")
        xbmcplugin.setContent(request.handle, "episodes")
        xbmcplugin.endOfDirectory(request.handle)

    listing(request, build, "next episodes")


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

    def build(request: Request, api: Api) -> None:
        items = api.resume(bounded_fields()).get("Items", [])
        _add_items(request, api, items, "", "")
        # Movies and episodes in one listing: "videos" is the content type
        # that describes both, and the items carry their own media type for
        # the skin.
        xbmcplugin.setContent(request.handle, "videos")
        xbmcplugin.endOfDirectory(request.handle)

    listing(request, build, "continue watching")


def browse(request: Request) -> None:
    listing(request, _browse, "browse")


def _browse(request: Request, api: Api) -> None:
    view_id = request.params.get("view", "")
    media = request.params.get("type", "")
    folder = request.params.get("folder", "")

    if not folder and media in NODES:
        _node_menu(request, api, media, view_id)
        return
    if folder == "alpha":
        _alpha_menu(request, media, view_id)
        return
    if folder in ("years", "tags"):
        _filter_menu(request, api, media, view_id, folder)
        return
    if folder.startswith("tags-"):
        _tag_menu(request, api, media, view_id, folder.split("-", 1)[1])
        return
    if folder == "extras" and media == "tvshows":
        _extras_node(request, api, view_id)
        return
    items, content = _list_items(api, media, folder or "children", view_id, request)

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


def search(request: Request) -> None:
    """Search the server (mode=search).

    Three shapes, one route:

    * no ``type`` — the menu of what can be searched. It asks nothing, so it
      is safe as a library node, a widget or a favourite.
    * ``type`` with no ``query`` — asks for the term, then lists the results.
      The keyboard is a modal, and a modal fights a directory fetch, so *this*
      shape is the one that must not be a node (kodi-plugin-handles).
    * ``type`` and ``query`` — lists results with nothing to answer. This is
      what lets a skin's own search box address kofin directly, and it is why
      the term is a parameter rather than only a prompt.

    ``person`` replaces both and lists everything one person appears in, which
    is where a result from the Actors leg leads.

    Every label here is a Kodi core string, so search adds no translatable id
    to the 27 locales.
    """
    listing(request, _search, "search")


def _search(request: Request, api: Api) -> None:
    person_id = request.params.get("person", "")
    if person_id:
        _search_person_items(request, api, person_id)
        return

    kind = request.params.get("type", "")
    if kind not in SEARCH_KINDS:
        _search_menu(request)
        return

    query = request.params.get("query", "") or _ask_for_query(kind)
    if not query:
        # Cancelled at the keyboard. A failed fetch is what returns the
        # viewer to where they were; an empty listing would strand them in a
        # results screen they did not ask for.
        xbmcplugin.endOfDirectory(request.handle, succeeded=False)
        return

    if kind == "people":
        items = api.persons(query, SEARCH_LIMIT).get("Items", [])
        _add_person_items(request, api, items)
        xbmcplugin.setContent(request.handle, "")
        xbmcplugin.endOfDirectory(request.handle)
        return
    items = api.items(_search_query(kind, query)).get("Items", [])

    _add_items(request, api, items, "", "")
    xbmcplugin.setContent(request.handle, SEARCH_KINDS[kind][2])
    for method in (
        xbmcplugin.SORT_METHOD_UNSORTED,
        xbmcplugin.SORT_METHOD_LABEL,
        xbmcplugin.SORT_METHOD_VIDEO_YEAR,
    ):
        xbmcplugin.addSortMethod(request.handle, method)
    xbmcplugin.endOfDirectory(request.handle)


def _search_menu(request: Request) -> None:
    """What can be searched. One row per kind, none of which asks anything."""
    import xbmc

    structural_rows(
        request,
        [
            (
                xbmc.getLocalizedString(label_id),
                SEARCH_ICONS[kind],
                {"mode": "search", "type": kind},
            )
            for kind, (label_id, _types, _content) in SEARCH_KINDS.items()
        ],
    )


def _ask_for_query(kind: str) -> str:
    """The search term, from the viewer. Empty when they backed out."""
    import xbmc

    heading = "%s: %s" % (
        xbmc.getLocalizedString(137),  # Search
        xbmc.getLocalizedString(SEARCH_KINDS[kind][0]),
    )
    return xbmcgui.Dialog().input(heading, type=xbmcgui.INPUT_ALPHANUM).strip()


def _search_query(kind: str, query: str) -> JsonDict:
    """The /Items query for one search kind.

    Bounded by SEARCH_LIMIT, so it takes the bounded field list — cast
    included when the viewer asked for it. Stream details ride along for the
    same reason every other bounded listing carries them.
    """
    return {
        "searchTerm": query,
        "IncludeItemTypes": SEARCH_KINDS[kind][1],
        "Recursive": True,
        "Fields": bounded_fields(),
        "ImageTypeLimit": 1,
        "Limit": SEARCH_LIMIT,
    }


def _search_person_items(request: Request, api: Api, person_id: str) -> None:
    """Everything one person appears in."""
    try:
        items = api.items(
            {
                "PersonIds": person_id,
                "Recursive": True,
                "IncludeItemTypes": "Movie,Series,Episode",
                "Fields": bounded_fields(),
                "ImageTypeLimit": 1,
                "SortBy": "PremiereDate,SortName",
                "Limit": SEARCH_LIMIT,
            }
        ).get("Items", [])
    except JellyfinError as error:
        LOG.warning("person listing failed (%s): %s", person_id, error)
        xbmcplugin.endOfDirectory(request.handle, succeeded=False)
        return

    _add_items(request, api, items, "", "")
    xbmcplugin.setContent(request.handle, "videos")
    xbmcplugin.endOfDirectory(request.handle)


def _add_person_items(request: Request, api: Api, items: List[JsonDict]) -> None:
    """People as folders leading to their own filmography.

    Built here rather than through :func:`_add_items`, which would give a
    person a playable path.
    """
    entries = []
    for item in items:
        li = listitems.build(item, api.server)
        if not li.getArt("thumb"):
            li.setArt(structural_art("DefaultActor.png"))
        path = plugin_url({"mode": "search", "person": item.get("Id", "")})
        entries.append((path, li, True))
    xbmcplugin.addDirectoryItems(request.handle, entries, len(entries))


def extras(request: Request) -> None:
    """Special features of a series/season (mode=extras) — a live listing
    over the SpecialFeatures endpoint, no DB writes (plan §2 TV extras)."""

    def build(request: Request, api: Api) -> None:
        items = api.special_features(request.params.get("id", ""))
        _add_items(request, api, items, "", "")
        xbmcplugin.setContent(request.handle, "videos")
        xbmcplugin.endOfDirectory(request.handle)

    listing(request, build, "extras listing")


def _alpha_menu(request: Request, media: str, view_id: str) -> None:
    """The alphabet. Letters are letters, so no server call and no strings."""
    icon = node_icon(media, "alpha")
    structural_rows(
        request,
        [
            (letter, icon, _folder_params(view_id, media, "alpha-%s" % letter))
            for letter in ALPHABET
        ],
    )


def _tag_letters(request: Request, media: str, view_id: str, values: List[Any]) -> None:
    """The initials a library's tags actually use, for a tag list too long to
    show flat. Only letters with tags behind them, so no empty rows."""
    letters = sorted({str(v)[:1].upper() or "#" for v in values})
    icon = node_icon(media, "tags")
    structural_rows(
        request,
        [
            (letter, icon, _folder_params(view_id, media, "tags-%s" % letter))
            for letter in letters
        ],
    )


def _tag_menu(
    request: Request, api: Api, media: str, view_id: str, letter: str
) -> None:
    """One letter's worth of tags."""
    values = api.filters(view_id, _search_item_type(media)).get("Tags") or []
    wanted = sorted(v for v in values if str(v)[:1].upper() == letter.upper())
    icon = node_icon(media, "tags")
    structural_rows(
        request,
        [
            (str(value), icon, _folder_params(view_id, media, "tag-%s" % value))
            for value in wanted
        ],
    )


def _filter_menu(
    request: Request, api: Api, media: str, view_id: str, kind: str
) -> None:
    """Years or tags for one library, from the server's own filter list.

    One call answers both — /Items/Filters returns Years, Tags, Genres and
    OfficialRatings together — so the menu is what the library actually holds
    rather than a fixed range.
    """
    values = (
        api.filters(view_id, _search_item_type(media)).get(
            "Years" if kind == "years" else "Tags"
        )
        or []
    )
    if kind == "years":
        # Newest first: a year list that opens at 1918 is a list nobody wants
        # to page to the end of.
        values = sorted(values, reverse=True)
    elif len(values) > TAG_MENU_MAX:
        # A tag list is only as disciplined as the server's metadata agent.
        # Measured on a real library: 7,794 tags, most of them scraped keywords
        # ("12th century bc"). A flat menu of that is not a menu, and it costs
        # a ListItem per row in the plugin process — so past the threshold the
        # letters come first and the tags sit under them.
        _tag_letters(request, media, view_id, values)
        return
    icon = node_icon(media, kind)
    prefix = "year" if kind == "years" else "tag"
    structural_rows(
        request,
        [
            (
                str(value),
                icon,
                _folder_params(view_id, media, "%s-%s" % (prefix, value)),
            )
            for value in values
        ],
    )


def _search_item_type(media: str) -> str:
    """The Jellyfin item type one collection type is made of."""
    return ITEM_TYPES.get(media, "")


def _folder_params(view_id: str, media: str, folder: str) -> Dict[str, str]:
    """The browse URL for one node of one library — the shape every
    structural menu row links to."""
    return {"mode": "browse", "view": view_id, "type": media, "folder": folder}


def _node_menu(request: Request, api: Api, media: str, view_id: str) -> None:
    nodes = list(NODES[media])
    if media == "tvshows" and _view_has_specials(api, view_id):
        nodes.append(("extras", 30500))
    structural_rows(
        request,
        [
            (
                node_label(label_id),
                node_icon(media, key),
                _folder_params(view_id, media, key),
            )
            for key, label_id in nodes
        ],
    )


def _extras_node(request: Request, api: Api, view_id: str) -> None:
    """The Extras node: series in the view that advertise special features,
    each opening its extras listing."""
    result = api.items(
        {
            "ParentId": view_id,
            "IncludeItemTypes": "Series",
            "Recursive": True,
            "Fields": BROWSE_FIELDS + ",SpecialFeatureCount",
            "SortBy": "SortName",
            "SortOrder": "Ascending",
        }
    )
    entries = []
    resume_offset = settings.resume_offset()
    for item in specials_only(result.get("Items", [])):
        # No apply_backdrop: these are series rows, and a media row keeps
        # whatever backdrop the server gave it or none (MEDIA_TYPES).
        li = listitems.build(item, api.server, resume_offset=resume_offset)
        path = plugin_url({"mode": "extras", "id": item.get("Id", "")})
        entries.append((path, li, True))
    xbmcplugin.addDirectoryItems(request.handle, entries, len(entries))
    xbmcplugin.setContent(request.handle, "tvshows")
    xbmcplugin.endOfDirectory(request.handle)


def specials_only(items: List[JsonDict]) -> List[JsonDict]:
    """The items that actually carry special features.

    Filtered here rather than by the server. ``HasSpecialFeature=true`` does
    not work for series on 10.11: a show with two extras in its folder, which
    /Items/{id}/SpecialFeatures happily returns and whose SpecialFeatureCount
    reads 2, is matched by that filter zero times — before and after a full
    library rescan. Asking for the count and filtering here agrees with the
    listing the entry opens, which is the property that matters.
    """
    return [item for item in items if item.get("SpecialFeatureCount")]


def _view_has_specials(api: Api, view_id: str) -> bool:
    """Whether any series in the view has special features (gates the node)."""
    try:
        result = api.items(
            {
                "ParentId": view_id,
                "IncludeItemTypes": "Series",
                "Recursive": True,
                # Just the counts: no artwork, no userdata. Measured against a
                # 78-series view at 36 KB and 38 ms.
                "Fields": "SpecialFeatureCount",
                "EnableImages": False,
                "EnableUserData": False,
            }
        )
    except JellyfinError:
        return False
    return bool(specials_only(result.get("Items", [])))


def _append_extras_entry(request: Request, api: Api, item_id: str) -> None:
    """An Extras entry at the end of a series/season drill-down when its DTO
    reports special features."""
    try:
        count = api.item(item_id).get("SpecialFeatureCount") or 0
    except JellyfinError:
        return
    if not count:
        return
    row = structural_row(
        settings.localized(30500), "DefaultVideo.png", {"mode": "extras", "id": item_id}
    )
    xbmcplugin.addDirectoryItems(request.handle, [row], 1)


def _list_items(
    api: Api, media: str, folder: str, view_id: str, request: Request
) -> Tuple[List[JsonDict], str]:
    """Fetch the item list for a folder; returns (items, kodi content type)."""
    # Special routes first.
    if folder == "nextup":
        result = api.next_up(view_id, bounded_fields())
        return result.get("Items", []), "episodes"
    if folder == "recentalbums":
        # Latest, not a sort: see the note above PLAY_HISTORY_LIMIT.
        return api.latest(view_id, "Audio", BROWSE_FIELDS, RECENT_LIMIT), "albums"
    if folder == "genres":
        # Empty content, like the node menu above it: genres are structural
        # rows carrying a stock glyph, and "files" is what hides it
        # (structural_art).
        result = api.genres(view_id, _genre_types(media))
        return result.get("Items", []), ""
    if folder == "artists":
        result = api.artists(view_id)
        return result.get("Items", []), "artists"
    if folder == "albumartists":
        result = api.album_artists(view_id)
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
            api.episodes(series, folder, bounded_fields()).get("Items", []),
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
    # One settings read for the whole listing: resume_of per row would build a
    # fresh Addon per item (settings.adjusted_resume), which at library scale
    # was most of the build time.
    resume_offset = settings.resume_offset()
    for item in items:
        li = listitems.build(item, api.server, resume_offset=resume_offset)
        if not is_media_row(item):
            apply_backdrop(li)
        item_type = item.get("Type", "")

        if item_type in ("Genre", "MusicGenre"):
            if not li.getArt("thumb"):
                li.setArt(structural_art("DefaultGenre.png"))
            path = plugin_url(
                {
                    "mode": "browse",
                    "view": view_id,
                    "type": media,
                    "folder": "genre-%s" % item.get("Id", ""),
                }
            )
            entries.append((path, li, True))
            continue

        if item_type == "Playlist":
            # A playlist is a place, not a piece of media, so it gets the same
            # treatment as a library row on the root: the stock glyph on icon
            # *and* thumb for the list, the server's own art kept on poster for
            # the focus pane. Without it the row falls through to Kodi's
            # watched-status square, which is what a playlist listing looked
            # like — thirteen identical dots (Contuary binds the list glyph to
            # ListItem.Icon, which prefers thumb; see structural_art).
            art = listitems.art_for(item, api.server)
            primary = art.get("thumb") or art.get("poster")
            icon = (
                "DefaultMusicPlaylists.png"
                if item.get("MediaType") == "Audio"
                else "DefaultVideoPlaylists.png"
            )
            art["icon"] = icon
            art["thumb"] = icon
            if primary:
                art["poster"] = primary
            li.setArt(with_backdrop(art))
            entries.append((listitems.path_for(item), li, True))
            continue

        if item_type == "Photo":
            tags = item.get("ImageTags") or {}
            path = api.image_url(item.get("Id", ""), "Primary", tags.get("Primary", ""))
            entries.append((path, li, False))
            continue

        path = listitems.path_for(item)
        if item_type == "Season":
            path = plugin_url(
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
    if node in ("lastplayed", "topsongs"):
        return "songs"
    if node == "sets":
        return "movies"
    if node.startswith("alpha-") and media == "music":
        # An alphabet leg over music lists artists, not the albums the rest of
        # the music tree is made of (node_query switches the item type too).
        return "artists"
    return CONTENT_TYPES.get(media, "videos")


def _genre_types(media: str) -> str:
    return ITEM_TYPES.get(media, "")


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
    # Playlists are structural rows, and empty content is what lets their
    # glyph through: with a media content type the skin draws watched-status
    # overlays instead of setArt(icon) (structural_art says the same for the
    # node menus). Checked after the loop so a listing that merely *holds* a
    # playlist beside real media still describes the media.
    if items and all(item.get("Type") == "Playlist" for item in items):
        return ""
    return "videos"
