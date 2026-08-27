# -*- coding: utf-8 -*-
"""Video nodes, smart playlists and skin window-props for synced libraries
(fork ``views.py`` port).

Adaptations per plan §3/§2: nodes and playlists regenerate only when the
view-set hash changed (stored in the hidden ``viewsHash`` setting — window
props are still refreshed every start, they don't survive Kodi restarts);
node ``<icon>`` elements use Kodi's stock icon names (Default*.png) so every
skin substitutes its own native artwork, never addon-branded icons on
structural entries; the api is passed in instead of a client singleton.
"""

import hashlib
import os
import xml.etree.ElementTree as etree
from urllib.parse import urlencode

import xbmc
import xbmcvfs

from kofin.core import ipc, settings
from kofin.core.http import Unauthorized
from kofin.core.log import Logger
from kofin.sync.db import Database, get_sync, save_sync
from kofin.sync import kofindb as jellyfin_db
from kofin.sync import fields as api
from kofin.sync import musicsources
from kofin.sync.playlists import FOLDER_ICON, FOLDER_NAME, write_folder_icon
from kofin.sync.shims import localized, window_prop

LOG = Logger(__name__)

# Every generated node lives under one folder in the video library root, so
# Kodi shows a single "Kofin" entry instead of one per synced library.
NODE_ROOT = "kofin"

# The generated smart playlists get a folder of their own under Kodi's video
# playlists, for the same reason the nodes do: they used to sit loose among the
# user's own, and a folder is the only thing that can carry the addon's icon —
# a .tbn beside an .xsp does nothing (measured on Piers). Same name as the
# music side, so the two managed folders read as one addon's.
PLAYLIST_FOLDER = FOLDER_NAME

# Shape/label revision of the generated tree, folded into views_hash() so a
# change here regenerates on upgrade even when the view set is untouched.
# 3: the playlists moved into PLAYLIST_FOLDER.
# 7: the Downloaded singles carry the addon's downloaded icon.
# 8: the music tree gained a folder per library, and Downloaded music became
#    a folder of its own rather than one flat node.
NODE_LAYOUT = 8

# Kind ordering for the generated library nodes, following Kodi's own
# top-level video ordering (movies 10, tvshows 20, musicvideos 30). Libraries
# of one kind sit together; see Views.node_order.
MEDIA_RANK = {
    "movies": 0,
    "tvshows": 1,
    "musicvideos": 2,
    "homevideos": 3,
    "episodes": 4,
}

# Order of the "Kofin" parent among Kodi's own top-level video nodes (movies
# 10, tvshows 20, musicvideos 30). Written once, on creation only — the user's
# own ordering is never overwritten (plan §3).
NODE_ROOT_ORDER = 15

# The same idea on the music side, where Kodi's own ordering runs genres 10,
# artists 20, albums 30, singles 40, songs 50: Kofin follows the five ways of
# browsing the library rather than splitting them up.
MUSIC_NODE_ROOT_ORDER = 55

# The Downloaded-music folder inside the music tree. Named with the NODE_ROOT
# prefix so the pruner claims it like everything else kofin writes.
MUSIC_DOWNLOADED_FOLDER = "kofin_Downloaded"

# The flat Downloaded-music node this replaced. The pruner sweeps it by
# prefix like any other stale file; the name survives only for the tests
# that plant one.
MUSIC_DOWNLOADED_FILE = "kofin_DownloadedMusic.xml"

# (file stem, label, content, group) for the sub-nodes inside a music folder.
# Every label is a *Kodi-core* string id — the same ones Kodi's own music
# nodes use (system/library/music/{artists,albums,songs,genres}.xml) — so
# _node_label leaves them numeric and they follow the UI language. Genres is
# content=artists grouped by genre, exactly as Kodi writes it.
MUSIC_NODES = (
    ("artists", 133, "artists", None),
    ("albums", 132, "albums", None),
    ("songs", 134, "songs", None),
    ("genres", 135, "artists", "genres"),
)

# The Downloaded set gets no genres leg: it is three ways into one small pile
# of files, and a genre level over a handful of albums is noise.
MUSIC_DOWNLOAD_NODES = MUSIC_NODES[:3]

# Stock Kodi icons for those sub-nodes, so skins substitute their own.
MUSIC_NODE_ICONS = {
    "artists": "DefaultMusicArtists.png",
    "albums": "DefaultMusicAlbums.png",
    "songs": "DefaultMusicSongs.png",
    "genres": "DefaultMusicGenres.png",
}

# The one structural entry that is allowed addon art: this node *is* the addon,
# so a skin has nothing of its own to substitute. Kodi resolves special:// for
# textures (URIUtils::IsHD translates it), so the path stays valid wherever the
# addon is installed — unlike an absolute one baked into the XML.
NODE_ROOT_ICON = (
    "special://home/addons/plugin.video.kofin/resources/media/kofin-node.png"
)

# The other allowed exception, for the same reason: a Downloaded node has no
# Kodi-native counterpart for a skin to substitute artwork for, and
# DefaultFavourites.png — what it inherited from the favourites singles it
# shares code with — said "favourite", which is a different idea entirely.
NODE_DOWNLOADS_ICON = (
    "special://home/addons/plugin.video.kofin/resources/media/downloaded.png"
)

# (node key, label). Ints are Kodi-core string ids that node XML resolves
# natively; ours are resolved at generation time.
NODES = {
    "tvshows": [
        ("all", None),
        ("recent", 30350),
        ("recentepisodes", 30355),
        ("inprogress", 30351),
        ("inprogressepisodes", 30356),
        ("nextepisodes", 30357),
        ("genres", 135),
        ("random", 30353),
        ("recommended", 30354),
    ],
    "movies": [
        ("all", None),
        ("recent", 30350),
        ("inprogress", 30351),
        ("unwatched", 30352),
        ("sets", 20434),
        ("genres", 135),
        ("random", 30353),
        ("recommended", 30354),
    ],
    "musicvideos": [
        ("all", None),
        ("recent", 30350),
        ("inprogress", 30351),
        ("unwatched", 30352),
    ],
}
# Stock Kodi icon per media type (structural entries never carry addon or
# server art — plan §2).
MEDIA_ICONS = {
    "movies": "DefaultMovies.png",
    "tvshows": "DefaultTVShows.png",
    "musicvideos": "DefaultMusicVideos.png",
    "episodes": "DefaultTVShows.png",
    "music": "DefaultMusicAlbums.png",
}
NODE_ICONS = {
    "recent": {
        "movies": "DefaultRecentlyAddedMovies.png",
        "tvshows": "DefaultRecentlyAddedEpisodes.png",
        "musicvideos": "DefaultRecentlyAddedMusicVideos.png",
    },
    "recentepisodes": "DefaultRecentlyAddedEpisodes.png",
    "inprogress": "DefaultInProgressShows.png",
    "inprogressepisodes": "DefaultInProgressShows.png",
    "nextepisodes": "DefaultInProgressShows.png",
    "genres": "DefaultGenre.png",
    "sets": "DefaultSets.png",
    "favorites": "DefaultFavourites.png",
}


def node_icon(media, node=None):
    icon = NODE_ICONS.get(node or "")
    if isinstance(icon, dict):
        icon = icon.get(media)
    return icon or MEDIA_ICONS.get(media, "DefaultVideo.png")


def _label(value, fallback=""):
    if isinstance(value, int):
        return localized(value) if value >= 30000 else xbmc.getLocalizedString(value)
    return value or fallback


def _node_label(value, fallback=""):
    """``<label>`` text for a node file.

    Kodi resolves a *numeric* node label against its own strings
    (``CGUIControlFactory::FilterLabel``, from ``CLibraryDirectory``), where
    the 30000+ addon range is empty — a bare ``30350`` renders blank in the
    library and in the node editor. Ours therefore go in as text; Kodi-core
    ids stay numeric so they keep following the UI language.
    """
    if isinstance(value, int):
        return localized(value) if value >= 30000 else str(value)
    return value or fallback


def _require(xml, tag):
    """The child element the caller has just ensured exists.

    ``find`` is typed Optional; the callers below all create the element
    when it is missing a few lines earlier, so a miss here is a programming
    error and says so instead of failing on ``.text``.
    """
    element = xml.find(tag)
    if element is None:
        raise ValueError("node XML is missing its <%s> element" % tag)
    return element


def set_node_icon(xml, icon):
    """Set (or add) a node's ``<icon>``, keeping it before the other
    children the way Kodi's own node files write it."""
    element = xml.find("icon")
    if element is None:
        element = etree.Element("icon")
        xml.insert(0, element)
    element.text = icon


def node_root_path():
    """Directory holding every generated node."""
    return os.path.join(
        xbmcvfs.translatePath("special://profile/library/video"), NODE_ROOT
    )


def music_node_root_path():
    """Directory holding the generated *music* nodes.

    Kodi keeps a second, entirely separate node tree for music
    (``CLibraryDirectory`` over ``library://music/``), and nothing in the
    video tree shows up there — which is why the downloads feature had a
    Downloaded-music smart playlist but no node beside the video ones.
    """
    return os.path.join(
        xbmcvfs.translatePath("special://profile/library/music"), NODE_ROOT
    )


def node_folder(view):
    """Per-library folder name inside :data:`NODE_ROOT`."""
    return "kofin%s%s" % (view["Media"], view["Id"])


def music_root_path():
    """The downloads music directory as a music node's rule needs it —
    the same value the Downloaded-music .xsp filters on, so the node and the
    playlist can never disagree about what "downloaded" means."""
    from kofin.downloads import downloads_root
    from kofin.downloads.files import MUSIC_DIR

    return "%s/%s/" % (downloads_root().rstrip("/"), MUSIC_DIR)


def music_node_folder(view):
    """Per-library folder name inside the music tree — the music twin of
    :func:`node_folder`, which cannot be reused because its name folds in a
    ``Media`` that is always ``music`` here."""
    return "kofinmusic%s" % view["Id"]


def music_library_views():
    """The synced music libraries, in the order their folders should sit.

    Ordered by SortedViews like the video entries are, and tolerant of a
    library that is missing from it — this runs on every node pass, and a
    stray view must leave the tree slightly mis-sorted rather than unwritten.
    """
    sync = get_sync()
    whitelist = [
        library.replace("Mixed:", "") for library in sync.get("Whitelist") or []
    ]
    order = sync.get("SortedViews") or []
    views = []

    with Database("kofin") as kofin_db:
        db = jellyfin_db.JellyfinDatabase(kofin_db.cursor)

        for library_id in whitelist:
            view = db.get_view(library_id)

            if view is not None and view.media_type == "music":
                views.append({"Id": library_id, "Name": view.view_name})

    def position(view):
        try:
            return (0, order.index(view["Id"]), "")
        except ValueError:
            return (1, 0, view["Name"])

    views.sort(key=position)

    for view in views:
        view["Source"] = musicsources.source_name(view["Id"], views)

    return views


def write_music_nodes(libraries=None):
    """The ``Kofin`` folder in Kodi's *music* library.

    Kodi keeps music nodes in a tree of their own (``CLibraryDirectory`` over
    ``library://music/``), so none of the video generation above reaches
    here. The tree holds a folder per synced music library — Artists, Albums,
    Songs and Genres, the same ways in Kodi's own music library offers — plus
    a Downloaded folder while the downloads feature is on.

    A library's sub-nodes filter on its MyMusic ``source`` row rather than a
    tag (MyMusic has none) or a path (a downloaded song's path is repointed
    at the filesystem, so it would fall out of its own library). See
    sync/musicsources.py.

    ``libraries`` is read from kofin.db when not supplied, so the downloads
    manager can keep calling this with no arguments. Returns whether the tree
    now exists.
    """
    root = music_node_root_path()
    downloads = settings.get_bool("downloadsEnabled")

    try:
        libraries = music_library_views() if libraries is None else libraries
    except Exception:
        LOG.exception("music library views unavailable")
        return False

    if not libraries and not downloads:
        _delete_music_nodes(root)
        return False

    try:
        if not os.path.isdir(root):
            os.makedirs(root)

        _write_music_parent(root)
        keep = set()

        for index, view in enumerate(libraries):
            _write_music_library_folder(root, view, index)
            keep.add(music_node_folder(view))

        if downloads:
            _write_downloaded_music_folder(root, len(libraries))
            keep.add(MUSIC_DOWNLOADED_FOLDER)

        _prune_music_nodes(root, keep)
    except Exception:
        LOG.exception("music node generation failed")
        return False
    return True


def _write_music_parent(root):
    """The music-side ``Kofin`` folder node. Creation only, exactly like the
    video parent: the order and the label are the user's afterwards."""
    file = os.path.join(root, "index.xml")

    if os.path.isfile(file):
        return

    xml = etree.Element("node", {"order": str(MUSIC_NODE_ROOT_ORDER)})
    etree.SubElement(xml, "icon").text = NODE_ROOT_ICON
    etree.SubElement(xml, "label").text = settings.addon_name()
    etree.ElementTree(xml).write(file)


def _write_music_library_folder(root, view, index):
    """One synced music library's folder and its four ways in."""
    folder = os.path.join(root, music_node_folder(view))

    if not os.path.isdir(folder):
        os.makedirs(folder)

    _write_music_folder_index(folder, index, view["Name"], MUSIC_NODE_ICONS["albums"])

    for order, (stem, label, content, group) in enumerate(MUSIC_NODES):
        rule = etree.Element("rule", {"field": "source", "operator": "is"})
        etree.SubElement(rule, "value").text = view["Source"]
        _write_music_filter_node(folder, order, stem, label, content, group, rule)


def _write_downloaded_music_folder(root, index):
    """The Downloaded-music folder: the same three ways in, filtered on the
    downloads directory instead of a source.

    Path rather than source because a download is not a library — it is a
    slice of one, and the repointed path is the honest signal for it (the
    same rule the Downloaded-music .xsp carries, so node and playlist cannot
    disagree about what "downloaded" means).
    """
    folder = os.path.join(root, MUSIC_DOWNLOADED_FOLDER)

    if not os.path.isdir(folder):
        os.makedirs(folder)

    _write_music_folder_index(
        folder,
        index,
        _node_label(30736, "Downloaded music"),
        NODE_DOWNLOADS_ICON,
    )

    for order, (stem, label, content, group) in enumerate(MUSIC_DOWNLOAD_NODES):
        rule = etree.Element("rule", {"field": "path", "operator": "startswith"})
        etree.SubElement(rule, "value").text = music_root_path()
        _write_music_filter_node(folder, order, stem, label, content, group, rule)


def _write_music_folder_index(folder, order, label, icon):
    """A music folder's own ``index.xml``.

    Rewritten every pass, unlike the tree's parent: a library's name and its
    position are the server's to state, not the user's to keep.
    """
    file = os.path.join(folder, "index.xml")
    xml = etree.Element("node", {"order": str(order)})
    set_node_icon(xml, icon)
    etree.SubElement(xml, "label").text = label
    etree.ElementTree(xml).write(file)


def _write_music_filter_node(folder, order, stem, label, content, group, rule):
    """One filter node inside a music folder.

    Built from scratch every pass rather than parsed and amended the way the
    video nodes are (``add_node``). The rule's value is the library's source
    name, and the server can rename a library at any time: with ``match=all``
    a leftover rule for the old name means the node matches *nothing*, which
    is silent. Nothing in these files is the user's to keep, so rewriting
    whole is both correct and simpler.
    """
    file = os.path.join(folder, "%s.xml" % stem)
    xml = etree.Element("node", {"order": str(order), "type": "filter"})
    set_node_icon(xml, MUSIC_NODE_ICONS[stem])
    etree.SubElement(xml, "label").text = _node_label(label, stem)
    etree.SubElement(xml, "content").text = content

    if group:
        etree.SubElement(xml, "group").text = group

    etree.SubElement(xml, "match").text = "all"
    xml.append(rule)
    etree.ElementTree(xml).write(file)


def _prune_music_nodes(root, keep):
    """Reconcile the music tree against what was just written.

    Prefix-gated on ``kofin`` like every other deletion path here, so a node
    the user dropped into the folder is never ours. ``index.xml`` does not
    carry the prefix and survives — it is creation-only and theirs to
    reorder. The loose ``kofin_DownloadedMusic.xml`` from before the
    Downloaded folder existed is swept here too; that sweep is the migration.
    """
    dirs, files = xbmcvfs.listdir(root)

    for name in dirs:
        if name.startswith(NODE_ROOT) and name not in keep:
            _delete_music_folder(os.path.join(root, name))

    for name in files:
        if name.startswith(NODE_ROOT):
            xbmcvfs.delete(os.path.join(root, name))


def _delete_music_folder(folder):
    """Remove one generated music folder, contents first."""
    try:
        _, files = xbmcvfs.listdir(folder)

        for name in files:
            xbmcvfs.delete(os.path.join(folder, name))

        xbmcvfs.rmdir(folder)
    except Exception:
        LOG.exception("music node folder removal failed for %s", folder)


def _delete_music_nodes(root):
    """Take the whole music tree back out when nothing wants it.

    Prefix-gated like the pruner, plus the parent ``index.xml`` — this is the
    teardown, not a reconcile. The folder itself only goes when nothing is
    left in it, so a hand-made node keeps it alive.
    """
    if not os.path.isdir(root):
        return
    try:
        dirs, files = xbmcvfs.listdir(root)
        for name in dirs:
            if name.startswith(NODE_ROOT):
                _delete_music_folder(os.path.join(root, name))
        for name in files:
            if name.startswith(NODE_ROOT) or name == "index.xml":
                xbmcvfs.delete(os.path.join(root, name))
        remaining_dirs, remaining_files = xbmcvfs.listdir(root)
        if not remaining_dirs and not remaining_files:
            xbmcvfs.rmdir(root)
    except Exception:
        LOG.exception("music node removal failed")


def downloads_root_path():
    """The downloads root as the episodes node's rule needs it: absolute,
    with a trailing separator so ``startswith`` cannot match a sibling
    directory that merely shares the prefix."""
    from kofin.downloads import downloads_root

    return downloads_root().rstrip("/") + "/"


def playlists_path():
    """Kodi's own video playlist directory — the user's, not ours."""
    return xbmcvfs.translatePath("special://profile/playlists/video")


def playlist_root_path():
    """Directory holding every generated smart playlist."""
    return os.path.join(playlists_path(), PLAYLIST_FOLDER)


class Views(object):

    limit = 25
    media_folders = None

    def __init__(self, server=None):
        """``server`` is the kofin Api (may be None for local-only paths
        like remove_library)."""
        self.sync = get_sync()
        self.server = server

    def add_library(self, view):
        """Add entry to view table in kofin database."""
        with Database("kofin") as kofin_db:
            jellyfin_db.JellyfinDatabase(kofin_db.cursor).add_view(
                view["Id"], view["Name"], view["Media"]
            )

    def remove_library(self, view_id):
        """Remove entry from view table in kofin database."""
        with Database("kofin") as kofin_db:
            jellyfin_db.JellyfinDatabase(kofin_db.cursor).remove_view(view_id)

        self.delete_playlist_by_id(view_id)
        self.delete_node_by_id(view_id)
        # The view set changed shape; force regeneration next pass.
        settings.set_str("viewsHash", "")

    def get_libraries(self):
        """The libraries to sync, and whether the answer is the whole truth.

        The second value is what stops a bad minute from deleting a library.
        A listing missing /Library/MediaFolders is not merely shorter: the two
        endpoints report *different ids for the same library* (Playlists is
        one id under MediaFolders and another under UserViews, verified
        against 10.11), so every view that came from the richer endpoint reads
        as deleted. See get_views.
        """

        if self.server is None:
            # Stated rather than tripped over: ``server`` is optional on this
            # class for the local-only paths (remove_library and kin), so
            # reaching here without one is a caller error. Unguarded it
            # surfaced as an AttributeError on NoneType, twice logged with a
            # traceback, naming neither the cause nor the caller.
            raise IndexError("Views has no server to list libraries from")

        # /Library/MediaFolders is admin-only (403 for a normal user). It is
        # worth asking for because it carries OriginalCollectionType and the
        # physical folders behind grouped views, but it must not be required:
        # the fork only ever ran as an admin, so a 403 there took the whole
        # view table down with it, and an empty view table silently breaks
        # node generation and fast_sync's media-type filter.
        libraries = []
        complete = True
        try:
            libraries = self.server.media_folders()["Items"]
        except Unauthorized:
            # A 403 is not a failure, it is the answer: this user is not an
            # administrator, so that endpoint is not theirs to see and never
            # will be. Their /UserViews listing is the whole truth for them,
            # and a library missing from it really is gone — which is why this
            # case must go on removing, or a non-admin install (most of them)
            # would keep every library the server ever dropped.
            LOG.info("media folders are admin-only here; using the user's own views")
        except Exception as error:
            # Anything else — a timeout, a 500, a reset — is the endpoint that
            # usually answers failing to. Nothing below may treat what is
            # missing as deleted.
            complete = False
            LOG.warning(
                "media folders unavailable (%s); listing is incomplete this pass",
                error,
            )

        try:
            library_ids = [x["Id"] for x in libraries]
            for view in self.server.views().get("Items", []):
                if view["Id"] not in library_ids:
                    libraries.append(view)

        except Exception as error:
            LOG.exception(error)
            raise IndexError("Unable to retrieve libraries: %s" % error)

        return libraries, complete

    def get_views(self):
        """Get the media folders. Add or remove them. Do not proceed if issue getting libraries."""
        try:
            libraries, complete = self.get_libraries()
        except IndexError as error:
            LOG.exception(error)

            return

        # An incomplete listing may add, never reorder or remove. Stamping
        # SortedViews from it would also reshuffle the generated node tree,
        # which now takes its ordering from here.
        if complete:
            self.sync["SortedViews"] = [x["Id"] for x in libraries]

        for library in libraries:

            if library["Type"] == "Channel":
                library["Media"] = "channels"
            else:
                library["Media"] = library.get(
                    "OriginalCollectionType", library.get("CollectionType", "mixed")
                )

            self.add_library(library)

        if complete:
            with Database("kofin") as kofin_db:

                views = jellyfin_db.JellyfinDatabase(kofin_db.cursor).get_views()
                removed = []

                for view in views:
                    if view.view_id not in self.sync["SortedViews"]:
                        removed.append(view.view_id)

                if removed:
                    # Not a listing tweak: remove_library deletes every synced
                    # row for these out of Kodi's database. It only ever runs
                    # off an answer we know to be whole.
                    ipc.notify(ipc.REMOVE_LIBRARY, {"Id": ",".join(removed)})

        save_sync(self.sync)

    def views_hash(self):
        """Fingerprint of everything the generated files depend on."""
        with Database("kofin") as kofin_db:
            views = jellyfin_db.JellyfinDatabase(kofin_db.cursor).get_views()

        parts = sorted(
            "%s|%s|%s" % (view.view_id, view.view_name, view.media_type)
            for view in views
        )
        parts.append("whitelist:%s" % ",".join(sorted(self.sync["Whitelist"])))
        parts.append("order:%s" % ",".join(self.sync["SortedViews"]))
        # Without this a change to the generated tree would never reach an
        # install whose view set happens to be unchanged.
        parts.append("layout:%s" % NODE_LAYOUT)
        # The Downloads singles exist only while the feature is on, so the
        # toggle must regenerate the tree (docs/offline-downloads-plan.md W1.9).
        parts.append("downloads:%s" % settings.get_bool("downloadsEnabled"))
        # The episodes node embeds the downloads root in its rule, so moving
        # the location has to regenerate the tree (plan W2.6).
        parts.append("downloadspath:%s" % settings.get_str("downloadsPath"))
        return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()

    def get_nodes(self):
        """Set up playlists, video nodes, window prop.

        File generation is skipped when nothing feeding it changed (the
        viewsHash guard); window props are session state and always rebuilt.
        """
        current_hash = self.views_hash()

        if settings.get_str("viewsHash") == current_hash:
            LOG.info("--[ nodes ] unchanged (hash match), skipping generation")
            self.window_nodes()
            return

        # Before the whitelist check below: the music tree is its own thing —
        # keyed on the synced *music* libraries and the downloads feature, not
        # on the video whitelist the tree beneath is built from — and it has
        # its own removal path for the nothing-wanted case.
        write_music_nodes()

        playlist_path = playlist_root_path()
        index = 0

        # Anything left where the pre-NODE_ROOT layout put it (loose folders
        # and kofin_*.xml in the video library root, loose kofin*.xsp among the
        # user's playlists) belongs to no library any more; the tree below
        # replaces it.
        self.migrate_flat_nodes()
        self.migrate_flat_playlists()

        if not self.sync["Whitelist"]:
            # Nothing is synced: the whole tree goes, favourites included.
            self.delete_nodes()
            self.delete_playlists()
            settings.set_str("viewsHash", current_hash)
            self.window_nodes()
            return

        node_path = node_root_path()

        if not os.path.isdir(node_path):
            os.makedirs(node_path)

        self.node_parent(node_path)
        self.playlist_parent(playlist_path)

        entries = []

        with Database("kofin") as kofin_db:
            db = jellyfin_db.JellyfinDatabase(kofin_db.cursor)

            for library in self.sync["Whitelist"]:

                library = library.replace("Mixed:", "")
                view = db.get_view(library)

                if view:
                    view = {
                        "Id": library,
                        "Name": view.view_name,
                        "Tag": view.view_name,
                        "Media": view.media_type,
                    }

                    if view["Media"] == "mixed":
                        # A mixed library is two entries and sorts as two, so
                        # its halves join their own kind rather than travelling
                        # together in the middle of everything else.
                        for media in ("movies", "tvshows"):
                            entries.append((dict(view, Media=media), True))
                    else:
                        entries.append((view, False))

        entries.sort(key=lambda entry: self.node_order(entry[0]))

        # Counted on the nodes actually written, not on the entries walked: a
        # music library is whitelisted and sorted like the rest but has no
        # video node, and numbering it anyway leaves holes that the favourites
        # then start after.
        index = 0

        for view, mixed in entries:

            if view["Media"] in ("movies", "tvshows", "musicvideos"):
                self.add_playlist(playlist_path, view, mixed)

            if view["Media"] not in ("music",):
                self.add_nodes(node_path, view, mixed, index)
                index += 1

        for single in self.single_nodes():

            self.add_single_node(
                node_path, index, single.get("Type", "favorites"), single
            )
            index += 1

        # A library can leave the whitelist by a route that never called
        # remove_library (server-side deletion, a settings diff, an install
        # that leaked before this pass existed). The tree above is the whole
        # truth, so anything else under it is stale.
        self.prune_nodes(node_path)

        settings.set_str("viewsHash", current_hash)
        self.window_nodes()

    def node_parent(self, node_path):
        """The ``Kofin`` folder node itself.

        Written on creation only: ``order`` and ``label`` are the user's to
        change afterwards (the fork pinned its own layout; kofin does not).
        """
        file = os.path.join(node_path, "index.xml")

        if os.path.isfile(file):
            return

        xml = self.node_root("main", NODE_ROOT_ORDER, NODE_ROOT_ICON)
        etree.SubElement(xml, "label").text = settings.addon_name()
        etree.ElementTree(xml).write(file)

    def playlist_parent(self, playlist_path):
        """The managed playlist folder, with the addon's icon on it.

        Unlike ``node_parent`` this runs every generation: the folder holds
        only generated files, so there is no user ordering or label to
        preserve, and the icon write is a no-op once it is there.
        """
        if not os.path.isdir(playlist_path):
            os.makedirs(playlist_path)

        write_folder_icon(playlist_path)

    def add_playlist(self, path, view, mixed=False):
        """Create or update the xps file."""
        file = os.path.join(path, "kofin%s%s.xsp" % (view["Media"], view["Id"]))

        try:
            if os.path.isfile(file):
                xml = etree.parse(file).getroot()
            else:
                xml = etree.Element("smartplaylist", {"type": view["Media"]})
                etree.SubElement(xml, "name")
                etree.SubElement(xml, "match")
        except Exception:
            LOG.warning("Unable to parse file '%s'", file)
            xml = etree.Element("smartplaylist", {"type": view["Media"]})
            etree.SubElement(xml, "name")
            etree.SubElement(xml, "match")

        name = _require(xml, "name")
        name.text = (
            view["Name"] if not mixed else "%s (%s)" % (view["Name"], view["Media"])
        )

        match = _require(xml, "match")
        match.text = "all"

        for rule in xml.findall(".//value"):
            if rule.text == view["Tag"]:
                break
        else:
            rule = etree.SubElement(xml, "rule", {"field": "tag", "operator": "is"})
            etree.SubElement(rule, "value").text = view["Tag"]

        tree = etree.ElementTree(xml)
        tree.write(file)

    def add_nodes(self, path, view, mixed=False, index=0):
        """Create or update the video node file."""
        folder = os.path.join(path, node_folder(view))

        if not xbmcvfs.exists(folder):
            xbmcvfs.mkdir(folder)

        self.node_index(folder, view, mixed, index)

        if view["Media"] == "tvshows":
            self.node_tvshow(folder, view)
        else:
            self.node(folder, view)

    def single_nodes(self):
        """The tag-filtered singles beside the library nodes: the favorites
        trio always, the Downloads pair while the feature is on. One list
        for both the node files and the window properties, so the two
        surfaces cannot disagree."""
        singles = [
            {
                "Name": localized(30358),
                "Tag": "Favorite movies",
                "Media": "movies",
            },
            {
                "Name": localized(30359),
                "Tag": "Favorite tvshows",
                "Media": "tvshows",
            },
            {
                "Name": localized(30360),
                "Tag": "Favorite episodes",
                "Media": "episodes",
            },
        ]
        if settings.get_bool("downloadsEnabled"):
            from kofin.downloads import TAG as DOWNLOADS_TAG

            singles.append(
                {
                    "Name": localized(30718),
                    "Tag": DOWNLOADS_TAG,
                    "Media": "movies",
                    "File": "DownloadedMovies",
                    "Type": "downloads",
                    "Icon": NODE_DOWNLOADS_ICON,
                }
            )
            singles.append(
                {
                    "Name": localized(30719),
                    "Tag": DOWNLOADS_TAG,
                    "Media": "tvshows",
                    "File": "DownloadedShows",
                    "Type": "downloads",
                    "Icon": NODE_DOWNLOADS_ICON,
                }
            )
            # Episodes cannot be filtered by tag: Kodi compiles a tag rule on
            # an episodes node against ``episode_view.idShow``
            # (SmartPlayList.cpp), so it answers with every episode of every
            # tagged show — verified live, 25+ rows for two downloads. Their
            # *path* is the honest signal, since a downloaded episode's row
            # points into the downloads root (plan W2.6).
            singles.append(
                {
                    "Name": localized(30721),
                    "Tag": DOWNLOADS_TAG,
                    "Path": downloads_root_path(),
                    "Media": "episodes",
                    "File": "DownloadedEpisodes",
                    "Type": "downloads",
                    "Icon": NODE_DOWNLOADS_ICON,
                }
            )
        return singles

    def add_single_node(self, path, index, item_type, view):

        # ``File`` names the node file when the tag cannot: the two Downloads
        # singles share one tag and would otherwise collide on this name.
        file = os.path.join(
            path, "kofin_%s.xml" % view.get("File", view["Tag"].replace(" ", ""))
        )

        icon = view.get("Icon") or node_icon(view["Media"], "favorites")

        try:
            if os.path.isfile(file):
                xml = etree.parse(file).getroot()
                # Rewritten every pass, like the library nodes' own index: an
                # install that predates the grouped ordering keeps its stale
                # number otherwise, and the stale numbers are the bug — they
                # were counted in a different space from the libraries', so
                # they collided with them and sorted among them.
                xml.set("order", str(index))
            else:
                xml = self.node_root(
                    (
                        "folder"
                        if item_type == "favorites" and view["Media"] == "episodes"
                        else "filter"
                    ),
                    index,
                    icon,
                )
                etree.SubElement(xml, "label")
                etree.SubElement(xml, "match")
                etree.SubElement(xml, "content")
        except Exception:
            LOG.warning("Unable to parse file '%s'", file)
            xml = self.node_root(
                (
                    "folder"
                    if item_type == "favorites" and view["Media"] == "episodes"
                    else "filter"
                ),
                index,
                icon,
            )
            etree.SubElement(xml, "label")
            etree.SubElement(xml, "match")
            etree.SubElement(xml, "content")

        # Every pass, for the same reason as ``order`` above: the icon only
        # ever reached the create branch, so an install that already had
        # these files kept whatever icon the release that made them chose,
        # and a NODE_LAYOUT bump changed nothing visible.
        set_node_icon(xml, icon)

        label = _require(xml, "label")
        label.text = view["Name"]

        content = _require(xml, "content")
        content.text = view["Media"]

        match = _require(xml, "match")
        match.text = "all"

        if view["Media"] != "episodes":

            for rule in xml.findall(".//value"):
                if rule.text == view["Tag"]:
                    break
            else:
                rule = etree.SubElement(xml, "rule", {"field": "tag", "operator": "is"})
                etree.SubElement(rule, "value").text = view["Tag"]

        elif view.get("Path"):
            # An episodes node filters on the file's location, because a tag
            # rule here resolves against the *show* (see single_nodes).
            for rule in xml.findall(".//value"):
                if rule.text == view["Path"]:
                    break
            else:
                rule = etree.SubElement(
                    xml, "rule", {"field": "path", "operator": "startswith"}
                )
                etree.SubElement(rule, "value").text = view["Path"]

        if item_type == "favorites" and view["Media"] == "episodes":
            path = self.window_browse(view, "FavEpisodes")
            self.node_favepisodes(xml, path)
        else:
            self.node_all(xml)

        tree = etree.ElementTree(xml)
        tree.write(file)

    def node_root(self, root, index, icon):
        """Create the root element"""
        if root == "main":
            element = etree.Element("node", {"order": str(index)})
        elif root == "filter":
            element = etree.Element("node", {"order": str(index), "type": "filter"})
        else:
            element = etree.Element("node", {"order": str(index), "type": "folder"})

        # Stock icon name: the skin substitutes its own native artwork.
        etree.SubElement(element, "icon").text = icon

        return element

    def node_order(self, view):
        """Sort key for one library node: its kind first, the server's order
        within that kind second.

        Kind first because the alternative — the server's order alone — reads
        as shuffled the moment a user has two libraries of one type: the
        Jellyfin view list interleaves them freely, and Kodi renders whatever
        order it is handed. Grouping is also what makes the favourites block
        below land after the libraries instead of among them; the two used to
        be numbered in different spaces (libraries by their position in the
        *whole* server view list, favourites by a count of the *whitelisted*
        ones), which is how "Favorite shows" ended up between two libraries
        and sharing an order with a third.

        MEDIA_RANK follows Kodi's own top-level ordering (movies before
        tvshows before musicvideos); anything the server names that is not in
        it sorts after, by name, so the answer stays stable.
        """
        media = view["Media"]
        rank = MEDIA_RANK.get(media, len(MEDIA_RANK))

        try:
            within = self.sync["SortedViews"].index(view["Id"])
        except ValueError:
            # A whitelisted view the ordering answer did not carry:
            # get_libraries degrades to a views-only listing when
            # /Library/MediaFolders 403s or times out, and a view can leave
            # /UserViews while still whitelisted. Raising here aborted the
            # whole generation *before* the viewsHash stamp, so every
            # startup and library command re-ran and re-crashed it, forever
            # (healing-loops-plan F5). Order the node after everything the
            # server did name instead — offset by sorted-whitelist position
            # so several strays stay stable and distinct — and let the next
            # full answer correct it: this attribute is rewritten every
            # pass.
            whitelist = sorted(x.replace("Mixed:", "") for x in self.sync["Whitelist"])
            offset = whitelist.index(view["Id"]) if view["Id"] in whitelist else 0
            within = len(self.sync["SortedViews"]) + offset
            LOG.debug(
                "view %s missing from SortedViews; ordering it at %s",
                view["Id"],
                within,
            )

        return (rank, media, within, view["Name"])

    def node_index(self, folder, view, mixed=False, index=0):

        file = os.path.join(folder, "index.xml")

        try:
            if os.path.isfile(file):
                xml = etree.parse(file).getroot()
                xml.set("order", str(index))
            else:
                xml = self.node_root("main", index, node_icon(view["Media"]))
                etree.SubElement(xml, "label")
        except Exception as error:
            LOG.exception(error)
            xml = self.node_root("main", index, node_icon(view["Media"]))
            etree.SubElement(xml, "label")

        label = _require(xml, "label")
        label.text = (
            view["Name"]
            if not mixed
            else "%s (%s)" % (view["Name"], _label(view["Media"]))
        )

        tree = etree.ElementTree(xml)
        tree.write(file)

    def node(self, folder, view):

        for node in NODES[view["Media"]]:

            xml_name = node[0]
            xml_label = node[1] or view["Name"]
            file = os.path.join(folder, "%s.xml" % xml_name)
            self.add_node(
                NODES[view["Media"]].index(node), file, view, xml_name, xml_label
            )

    def node_tvshow(self, folder, view):

        for node in NODES[view["Media"]]:

            xml_name = node[0]
            xml_label = node[1] or view["Name"]
            xml_index = NODES[view["Media"]].index(node)
            file = os.path.join(folder, "%s.xml" % xml_name)

            if xml_name == "nextepisodes":
                path = self.window_nextepisodes(view)
                self.add_dynamic_node(xml_index, file, view, xml_name, xml_label, path)
            else:
                self.add_node(xml_index, file, view, xml_name, xml_label)

    def add_node(self, index, file, view, node, name):

        try:
            if os.path.isfile(file):
                xml = etree.parse(file).getroot()
            else:
                xml = self.node_root("filter", index, node_icon(view["Media"], node))
                etree.SubElement(xml, "label")
                etree.SubElement(xml, "match")
                etree.SubElement(xml, "content")

        except Exception:
            LOG.warning("Unable to parse file '%s'", file)
            xml = self.node_root("filter", index, node_icon(view["Media"], node))
            etree.SubElement(xml, "label")
            etree.SubElement(xml, "match")
            etree.SubElement(xml, "content")

        label = _require(xml, "label")
        label.text = _node_label(name)

        content = _require(xml, "content")
        content.text = view["Media"]

        match = _require(xml, "match")
        match.text = "all"

        for rule in xml.findall(".//value"):
            if rule.text == view["Tag"]:
                break
        else:
            rule = etree.SubElement(xml, "rule", {"field": "tag", "operator": "is"})
            etree.SubElement(rule, "value").text = view["Tag"]

        getattr(self, "node_" + node)(xml)  # get node function based on node type
        tree = etree.ElementTree(xml)
        tree.write(file)

    def add_dynamic_node(self, index, file, view, node, name, path):

        try:
            if os.path.isfile(file):
                xml = etree.parse(file).getroot()
            else:
                xml = self.node_root("folder", index, node_icon(view["Media"], node))
                etree.SubElement(xml, "label")
                etree.SubElement(xml, "content")
        except Exception:
            LOG.warning("Unable to parse file '%s'", file)
            xml = self.node_root("folder", index, node_icon(view["Media"], node))
            etree.SubElement(xml, "label")
            etree.SubElement(xml, "content")

        label = _require(xml, "label")
        label.text = _node_label(name)

        getattr(self, "node_" + node)(xml, path)
        tree = etree.ElementTree(xml)
        tree.write(file)

    def node_all(self, root):

        for rule in root.findall(".//order"):
            if rule.text == "sorttitle":
                break
        else:
            etree.SubElement(root, "order", {"direction": "ascending"}).text = (
                "sorttitle"
            )

    def node_nextepisodes(self, root, path):

        for rule in root.findall(".//path"):
            rule.text = path
            break
        else:
            etree.SubElement(root, "path").text = path

        for rule in root.findall(".//content"):
            rule.text = "episodes"
            break
        else:
            etree.SubElement(root, "content").text = "episodes"

    def node_recent(self, root):

        for rule in root.findall(".//order"):
            if rule.text == "dateadded":
                break
        else:
            etree.SubElement(root, "order", {"direction": "descending"}).text = (
                "dateadded"
            )

        for rule in root.findall(".//limit"):
            rule.text = str(self.limit)
            break
        else:
            etree.SubElement(root, "limit").text = str(self.limit)

        for rule in root.findall(".//rule"):
            if rule.attrib["field"] == "playcount":
                rule.find("value").text = "0"
                break
        else:
            rule = etree.SubElement(
                root, "rule", {"field": "playcount", "operator": "is"}
            )
            etree.SubElement(rule, "value").text = "0"

    def node_inprogress(self, root):

        for rule in root.findall(".//rule"):
            if rule.attrib["field"] == "inprogress":
                break
        else:
            etree.SubElement(root, "rule", {"field": "inprogress", "operator": "true"})

        for rule in root.findall(".//limit"):
            rule.text = str(self.limit)
            break
        else:
            etree.SubElement(root, "limit").text = str(self.limit)

    def node_genres(self, root):

        for rule in root.findall(".//order"):
            if rule.text == "sorttitle":
                break
        else:
            etree.SubElement(root, "order", {"direction": "ascending"}).text = (
                "sorttitle"
            )

        for rule in root.findall(".//group"):
            rule.text = "genres"
            break
        else:
            etree.SubElement(root, "group").text = "genres"

    def node_unwatched(self, root):

        for rule in root.findall(".//order"):
            if rule.text == "sorttitle":
                break
        else:
            etree.SubElement(root, "order", {"direction": "ascending"}).text = (
                "sorttitle"
            )

        for rule in root.findall(".//rule"):
            if rule.attrib["field"] == "playcount":
                rule.find("value").text = "0"
                break
        else:
            rule = etree.SubElement(
                root, "rule", {"field": "playcount", "operator": "is"}
            )
            etree.SubElement(rule, "value").text = "0"

    def node_sets(self, root):

        for rule in root.findall(".//order"):
            if rule.text == "sorttitle":
                break
        else:
            etree.SubElement(root, "order", {"direction": "ascending"}).text = (
                "sorttitle"
            )

        for rule in root.findall(".//group"):
            rule.text = "sets"
            break
        else:
            etree.SubElement(root, "group").text = "sets"

    def node_random(self, root):

        for rule in root.findall(".//order"):
            if rule.text == "random":
                break
        else:
            etree.SubElement(root, "order", {"direction": "ascending"}).text = "random"

        for rule in root.findall(".//limit"):
            rule.text = str(self.limit)
            break
        else:
            etree.SubElement(root, "limit").text = str(self.limit)

    def node_recommended(self, root):

        for rule in root.findall(".//order"):
            if rule.text == "rating":
                break
        else:
            etree.SubElement(root, "order", {"direction": "descending"}).text = "rating"

        for rule in root.findall(".//limit"):
            rule.text = str(self.limit)
            break
        else:
            etree.SubElement(root, "limit").text = str(self.limit)

        for rule in root.findall(".//rule"):
            if rule.attrib["field"] == "playcount":
                rule.find("value").text = "0"
                break
        else:
            rule = etree.SubElement(
                root, "rule", {"field": "playcount", "operator": "is"}
            )
            etree.SubElement(rule, "value").text = "0"

        for rule in root.findall(".//rule"):
            if rule.attrib["field"] == "rating":
                rule.find("value").text = "7"
                break
        else:
            rule = etree.SubElement(
                root, "rule", {"field": "rating", "operator": "greaterthan"}
            )
            etree.SubElement(rule, "value").text = "7"

    def node_recentepisodes(self, root):

        for rule in root.findall(".//order"):
            if rule.text == "dateadded":
                break
        else:
            etree.SubElement(root, "order", {"direction": "descending"}).text = (
                "dateadded"
            )

        for rule in root.findall(".//limit"):
            rule.text = str(self.limit)
            break
        else:
            etree.SubElement(root, "limit").text = str(self.limit)

        for rule in root.findall(".//rule"):
            if rule.attrib["field"] == "playcount":
                rule.find("value").text = "0"
                break
        else:
            rule = etree.SubElement(
                root, "rule", {"field": "playcount", "operator": "is"}
            )
            etree.SubElement(rule, "value").text = "0"

        content = root.find("content")
        content.text = "episodes"

    def node_inprogressepisodes(self, root):

        for rule in root.findall(".//limit"):
            rule.text = str(self.limit)
            break
        else:
            etree.SubElement(root, "limit").text = str(self.limit)

        for rule in root.findall(".//rule"):
            if rule.attrib["field"] == "inprogress":
                break
        else:
            etree.SubElement(root, "rule", {"field": "inprogress", "operator": "true"})

        content = root.find("content")
        content.text = "episodes"

    def node_favepisodes(self, root, path):

        for rule in root.findall(".//path"):
            rule.text = path
            break
        else:
            etree.SubElement(root, "path").text = path

        for rule in root.findall(".//content"):
            rule.text = "episodes"
            break
        else:
            etree.SubElement(root, "content").text = "episodes"

    def order_media_folders(self, folders):
        """The media folders in SortedViews order, unknown ones last. Pure: it
        neither reads nor writes anything but its argument and the stored
        order."""
        if not folders:
            return folders

        sorted_views = list(self.sync["SortedViews"])
        unordered = [x[0] for x in folders]
        grouped = [x for x in unordered if x not in sorted_views]

        for library in grouped:
            sorted_views.append(library)

        sorted_folders = [x for x in sorted_views if x in unordered]

        return [folders[unordered.index(x)] for x in sorted_folders]

    def window_nodes(self):
        """Just read from the database and populate based on SortedViews
        Set up the window properties that reflect the jellyfin server views and more.
        """
        self.window_clear()
        self.window_clear("Kofin.wnodes")

        with Database("kofin") as kofin_db:
            libraries = jellyfin_db.JellyfinDatabase(kofin_db.cursor).get_views()

        libraries = self.order_media_folders(libraries or [])
        index = 0
        windex = 0

        # Only when there is a server to ask. The listing feeds one thing —
        # window_artwork's library tile — and that already clears the prop
        # outright when self.server is None, so a serverless pass has nothing
        # to gain here and used to pay two logged tracebacks for it (the
        # settings-apply node rebuild goes through Views() with no server).
        if self.server is not None:
            try:
                # Window props are cosmetic and rebuilt every start, so a
                # listing that came back short only costs this pass its
                # labels.
                self.media_folders, _ = self.get_libraries()
            except IndexError as error:
                LOG.exception(error)

        for library in libraries:
            view = {
                "Id": library.view_id,
                "Name": library.view_name,
                "Tag": library.view_name,
                "Media": library.media_type,
            }

            if library.view_id in [
                x.replace("Mixed:", "") for x in self.sync["Whitelist"]
            ]:  # Synced libraries

                if view["Media"] in ("movies", "tvshows", "musicvideos", "mixed"):

                    if view["Media"] == "mixed":
                        for media in ("movies", "tvshows"):

                            for node in NODES[media]:

                                temp_view = dict(view)
                                temp_view["Media"] = media
                                temp_view["Name"] = "%s (%s)" % (
                                    view["Name"],
                                    _label(media),
                                )
                                self.window_node(index, temp_view, *node)
                                self.window_wnode(windex, temp_view, *node)

                            # Add one to compensate for the duplicate.
                            index += 1
                            windex += 1
                    else:
                        for node in NODES[view["Media"]]:

                            self.window_node(index, view, *node)

                            if view["Media"] in ("movies", "tvshows"):
                                self.window_wnode(windex, view, *node)

                        if view["Media"] in ("movies", "tvshows"):
                            windex += 1

                elif view["Media"] == "music":
                    self.window_node(index, view, "music")
            else:  # Dynamic entry
                if view["Media"] in ("homevideos", "books", "playlists"):
                    self.window_wnode(windex, view, "browse")
                    windex += 1

                self.window_node(index, view, "browse")

            index += 1

        for single in self.single_nodes():

            self.window_single_node(index, single.get("Type", "favorites"), single)
            index += 1

        window_prop("Kofin.nodes.total", str(index))
        window_prop("Kofin.wnodes.total", str(windex))

    def window_node(self, index, view, node=None, node_label=None):
        """Leads to another listing of nodes."""
        if view["Media"] in ("homevideos", "photos"):
            path = self.window_browse(view, None if node in ("all", "browse") else node)
        elif node == "nextepisodes":
            path = self.window_nextepisodes(view)
        elif node == "music":
            path = self.window_music(view)
        elif node == "browse":
            path = self.window_browse(view)
        else:
            path = self.window_path(view, node)

        if node == "music":
            window_path = "ActivateWindow(Music,%s,return)" % path
        elif node in ("browse", "homevideos", "photos"):
            window_path = path
        else:
            window_path = "ActivateWindow(Videos,%s,return)" % path

        node_label = _label(node_label)
        node_label = node_label or view["Name"]

        if node in ("all", "music"):

            window_prop_name = "Kofin.nodes.%s" % index
            window_prop("%s.index" % window_prop_name, path.replace("all.xml", ""))
            window_prop("%s.title" % window_prop_name, view["Name"])
            window_prop("%s.content" % window_prop_name, path)

        elif node == "browse":

            window_prop_name = "Kofin.nodes.%s" % index
            window_prop("%s.title" % window_prop_name, view["Name"])
        else:
            window_prop_name = "Kofin.nodes.%s.%s" % (index, node)
            window_prop("%s.title" % window_prop_name, node_label)
            window_prop("%s.content" % window_prop_name, path)

        window_prop("%s.id" % window_prop_name, view["Id"])
        window_prop("%s.path" % window_prop_name, window_path)
        window_prop("%s.type" % window_prop_name, view["Media"])
        self.window_artwork(window_prop_name, view["Id"])

    def window_single_node(self, index, item_type, view):
        """Single destination node."""
        path = "library://video/%s/kofin_%s.xml" % (
            NODE_ROOT,
            view.get("File", view["Tag"].replace(" ", "")),
        )
        window_path = "ActivateWindow(Videos,%s,return)" % path

        window_prop_name = "Kofin.nodes.%s" % index
        window_prop("%s.title" % window_prop_name, view["Name"])
        window_prop("%s.path" % window_prop_name, window_path)
        window_prop("%s.content" % window_prop_name, path)
        window_prop("%s.type" % window_prop_name, item_type)

    def window_wnode(self, index, view, node=None, node_label=None):
        """Similar to window_node, but does not contain music, musicvideos.
        Contains books, audiobooks.
        """
        if view["Media"] in ("homevideos", "photos", "books", "playlists"):
            path = self.window_browse(view, None if node in ("all", "browse") else node)
        else:
            path = self.window_path(view, node)

        if node in ("browse", "homevideos", "photos", "books", "playlists"):
            window_path = path
        else:
            window_path = "ActivateWindow(Videos,%s,return)" % path

        node_label = _label(node_label)
        node_label = node_label or view["Name"]

        if node == "all":

            window_prop_name = "Kofin.wnodes.%s" % index
            window_prop("%s.index" % window_prop_name, path.replace("all.xml", ""))
            window_prop("%s.title" % window_prop_name, view["Name"])
        elif node == "browse":

            window_prop_name = "Kofin.wnodes.%s" % index
            window_prop("%s.title" % window_prop_name, view["Name"])
        else:
            window_prop_name = "Kofin.wnodes.%s.%s" % (index, node)
            window_prop("%s.title" % window_prop_name, node_label)
        window_prop("%s.content" % window_prop_name, path)

        window_prop("%s.id" % window_prop_name, view["Id"])
        window_prop("%s.path" % window_prop_name, window_path)
        window_prop("%s.type" % window_prop_name, view["Media"])
        self.window_artwork(window_prop_name, view["Id"])

        LOG.debug(
            "--[ wnode/%s/%s ] %s",
            index,
            window_prop("%s.title" % window_prop_name),
            window_prop("%s.artwork" % window_prop_name),
        )

    def window_artwork(self, prop, view_id):
        """Server artwork for the library tile, when the view has any.

        This is a real media image (the library's Primary), not a structural
        icon, so a server URL is correct here; skins fall back to their own
        art when the prop is empty.
        """
        if self.server is None:
            window_prop("%s.artwork" % prop, clear=True)

        elif self.media_folders is not None:
            for library in self.media_folders:

                if library["Id"] == view_id and "Primary" in library.get(
                    "ImageTags", {}
                ):
                    artwork = api.API(None, self.server.server).get_artwork(
                        view_id, "Primary"
                    )
                    window_prop("%s.artwork" % prop, artwork)

                    break
            else:
                window_prop("%s.artwork" % prop, clear=True)

    def window_path(self, view, node):
        return "library://video/%s/%s/%s.xml" % (
            NODE_ROOT,
            node_folder(view),
            node,
        )

    def window_music(self, view):
        return "library://music/"

    def window_nextepisodes(self, view):

        params = {"id": view["Id"], "mode": "nextepisodes", "limit": self.limit}
        return "%s?%s" % ("plugin://plugin.video.kofin/", urlencode(params))

    def window_browse(self, view, node=None):

        params = {"mode": "browse", "type": view["Media"]}

        if view.get("Id"):
            params["view"] = view["Id"]

        if node:
            params["folder"] = node

        return "%s?%s" % ("plugin://plugin.video.kofin/", urlencode(params))

    def window_clear(self, name=None):
        """Clearing window prop setup for Views."""
        total = int(window_prop((name or "Kofin.nodes") + ".total") or 0)
        props = [
            "index",
            "id",
            "path",
            "artwork",
            "title",
            "content",
            "type",
            "inprogress.title",
            "inprogress.content",
            "inprogress.path",
            "nextepisodes.title",
            "nextepisodes.content",
            "nextepisodes.path",
            "unwatched.title",
            "unwatched.content",
            "unwatched.path",
            "recent.title",
            "recent.content",
            "recent.path",
            "recentepisodes.title",
            "recentepisodes.content",
            "recentepisodes.path",
            "inprogressepisodes.title",
            "inprogressepisodes.content",
            "inprogressepisodes.path",
        ]
        for i in range(total):
            for prop in props:
                window_prop(
                    "%s.%s.%s" % (name or "Kofin.nodes", str(i), prop), clear=True
                )

        for prop in props:
            window_prop("%s.%s" % (name or "Kofin.nodes", prop), clear=True)

    def delete_playlist(self, path):

        xbmcvfs.delete(path)
        LOG.info("DELETE playlist %s", path)

    def delete_playlists(self):
        """Remove all kofin playlists, the managed folder with them.

        Name-gated inside the folder the way ``delete_nodes`` is: the
        generated files and the folder's own icon go, the folder goes once it
        is empty, and anything else in there is not ours to remove.
        """
        path = playlist_root_path()

        if os.path.isdir(path):
            _, files = xbmcvfs.listdir(path)

            for file in files:
                if file.startswith("kofin") or file == FOLDER_ICON:
                    self.delete_playlist(os.path.join(path, file))

            dirs, files = xbmcvfs.listdir(path)

            if not dirs and not files:
                xbmcvfs.rmdir(path)

        self.migrate_flat_playlists()

    def delete_playlist_by_id(self, view_id):
        """Remove playlist based on view_id.

        Both homes: a library removed between the upgrade and the next
        generation still has its playlist out in the old flat layout.
        """
        for path in (playlist_root_path(), playlists_path()):

            if not os.path.isdir(path):
                continue

            _, files = xbmcvfs.listdir(path)

            for file in files:
                if file.startswith("kofin") and file.endswith("%s.xsp" % view_id):
                    self.delete_playlist(os.path.join(path, file))

    def delete_node(self, path):

        xbmcvfs.delete(path)
        LOG.info("DELETE node %s", path)

    def delete_node_folder(self, path):
        """Delete a generated node folder and everything in it."""
        _, files = xbmcvfs.listdir(path)

        for file in files:
            self.delete_node(os.path.join(path, file))

        xbmcvfs.rmdir(path)

    def delete_nodes(self):
        """Remove the whole generated tree.

        Name-gated throughout: only ``kofin``-prefixed entries and the
        parent's own index.xml go, so hand-made node files living beside them
        survive (they are the user's, not ours).
        """
        path = node_root_path()

        if not os.path.isdir(path):
            return

        dirs, files = xbmcvfs.listdir(path)

        for file in files:
            if file.startswith("kofin") or file == "index.xml":
                self.delete_node(os.path.join(path, file))

        for directory in dirs:
            if directory.startswith("kofin"):
                self.delete_node_folder(os.path.join(path, directory))

        # Only ours were in there; an empty parent is ours to remove too.
        dirs, files = xbmcvfs.listdir(path)

        if not dirs and not files:
            xbmcvfs.rmdir(path)

    def delete_node_by_id(self, view_id):
        """Remove node and children files based on view_id."""
        path = node_root_path()

        if not os.path.isdir(path):
            return

        dirs, _ = xbmcvfs.listdir(path)

        for directory in dirs:

            if directory.startswith("kofin") and directory.endswith(view_id):
                self.delete_node_folder(os.path.join(path, directory))

    def prune_nodes(self, node_path):
        """Drop node folders for libraries that are no longer whitelisted.

        A library can leave the whitelist without ``remove_library`` running
        (deleted server-side, dropped by a settings diff, or leaked by an
        older build), so generation ends by reconciling the tree against the
        whitelist rather than trusting every removal path to have cleaned up.
        """
        wanted = {library.replace("Mixed:", "") for library in self.sync["Whitelist"]}
        dirs, files = xbmcvfs.listdir(node_path)

        for directory in dirs:

            if not directory.startswith("kofin"):
                continue

            if not any(directory.endswith(view_id) for view_id in wanted):
                LOG.info("--[ nodes ] pruning stale folder %s", directory)
                self.delete_node_folder(os.path.join(node_path, directory))

        # The single nodes are files, not folders, and their set is no longer
        # fixed: the Downloads pair exists only while that feature is on, so
        # a toggle-off leaves its files behind unless they are reconciled too
        # (docs/offline-downloads-plan.md W1.9). Gated on the ``kofin_``
        # prefix like every other deletion path here — hand-made node files
        # live in this tree and are never ours to remove.
        keep = {
            "kofin_%s.xml" % single.get("File", single["Tag"].replace(" ", ""))
            for single in self.single_nodes()
        }
        for name in files:
            if not name.startswith("kofin_") or name in keep:
                continue
            LOG.info("--[ nodes ] pruning stale single node %s", name)
            self.delete_node(os.path.join(node_path, name))

    def migrate_flat_nodes(self):
        """Clear out the pre-:data:`NODE_ROOT` layout.

        Before the ``kofin`` parent, every library node folder and the three
        ``kofin_Favorite*.xml`` sat directly in the video library root. They
        are regenerated under the parent, so the old copies are dead weight
        that would also show up twice in the library.
        """
        path = xbmcvfs.translatePath("special://profile/library/video/")

        if not os.path.isdir(path):
            return

        dirs, files = xbmcvfs.listdir(path)

        for file in files:
            if file.startswith("kofin"):
                self.delete_node(os.path.join(path, file))

        for directory in dirs:
            # NODE_ROOT itself is the new home, not a leftover.
            if directory.startswith("kofin") and directory != NODE_ROOT:
                self.delete_node_folder(os.path.join(path, directory))

    def migrate_flat_playlists(self):
        """Clear out the pre-:data:`PLAYLIST_FOLDER` layout.

        The generated smart playlists used to sit directly in the user's
        ``playlists/video/``. They are regenerated inside the managed folder,
        so the old copies are dead weight — and, being smart playlists over
        the same tag, would show up twice under two names.

        Narrower than ``migrate_flat_nodes``: that sweeps a directory only
        kofin writes to, this one sweeps the user's, so only a generated
        ``kofin*.xsp`` qualifies. The managed folder is a directory and is
        never in ``files``.
        """
        path = playlists_path()

        if not os.path.isdir(path):
            return

        _, files = xbmcvfs.listdir(path)

        for file in files:
            if file.startswith("kofin") and file.endswith(".xsp"):
                self.delete_playlist(os.path.join(path, file))
