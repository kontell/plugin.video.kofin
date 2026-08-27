"""The generated video node tree: a ``Kofin`` folder in Kodi's video
library holding one folder per synced library and a few single nodes.

Every file is written whole on every pass (the music tree's shape). The
fork parsed and amended what was there, and that is what left a renamed
library with its old tag rule beside the new one -- under ``match=all`` a
node that matches nothing, silently. Nothing under the tree is the user's
to keep except the parent's own ``index.xml`` (its order and label), which
stays creation-only; the per-library ``index.xml`` restates what the
server says about the library and is rewritten like the rest.

Structural entries carry Kodi's stock icon names (Default*.png) so every
skin substitutes its own artwork; the two exceptions (the parent, the
Downloaded singles) are the addon itself and have nothing to substitute.
"""

import os
import xml.etree.ElementTree as etree
from urllib.parse import urlencode

import xbmc
import xbmcvfs

from kofin.core import settings
from kofin.core.log import Logger
from kofin.sync.nodes import fs
from kofin.sync.shims import localized

LOG = Logger(__name__)

# Every generated node lives under one folder in the video library root, so
# Kodi shows a single "Kofin" entry instead of one per synced library. The
# folder name is the prefix every managed name carries.
NODE_ROOT = fs.PREFIX

# Shape/label revision of the generated tree, folded into views_hash() so a
# change here regenerates on upgrade even when the view set is untouched.
# 3: the playlists moved into the managed playlist folder.
# 7: the Downloaded singles carry the addon's downloaded icon.
# 8: the music tree gained a folder per library, and Downloaded music became
#    a folder of its own rather than one flat node.
# 9: node files are written whole (P2.1), so every install gets one pass
#    that replaces whatever parse-and-amend had accumulated.
NODE_LAYOUT = 9

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
# 10, tvshows 20, musicvideos 30). Written once, on creation only -- the
# user's own ordering is never overwritten (plan §3).
NODE_ROOT_ORDER = 15

# The one structural entry that is allowed addon art: this node *is* the
# addon, so a skin has nothing of its own to substitute. Kodi resolves
# special:// for textures (URIUtils::IsHD translates it), so the path stays
# valid wherever the addon is installed -- unlike an absolute one baked into
# the XML.
NODE_ROOT_ICON = (
    "special://home/addons/plugin.video.kofin/resources/media/kofin-node.png"
)

# The other allowed exception, for the same reason: a Downloaded node has no
# Kodi-native counterpart for a skin to substitute artwork for, and
# DefaultFavourites.png -- what it inherited from the favourites singles it
# shares code with -- said "favourite", which is a different idea entirely.
NODE_DOWNLOADS_ICON = (
    "special://home/addons/plugin.video.kofin/resources/media/downloaded.png"
)

# (node key, label) per library kind. Ints are Kodi-core string ids that node
# XML resolves natively; ours (30xxx) are resolved at generation time.
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

# What each filter node carries after its tag rule, in the order the file
# is written. ("order", by, direction) / ("limit",) / ("group", name) /
# ("rule", field, operator, value-or-None). The two episode-level nodes of a
# show library list episodes rather than shows.
NODE_PARTS = {
    "all": (("order", "sorttitle", "ascending"),),
    "recent": (
        ("order", "dateadded", "descending"),
        ("limit",),
        ("rule", "playcount", "is", "0"),
    ),
    "inprogress": (("rule", "inprogress", "true", None), ("limit",)),
    "unwatched": (
        ("order", "sorttitle", "ascending"),
        ("rule", "playcount", "is", "0"),
    ),
    "sets": (("order", "sorttitle", "ascending"), ("group", "sets")),
    "genres": (("order", "sorttitle", "ascending"), ("group", "genres")),
    "random": (("order", "random", "ascending"), ("limit",)),
    "recommended": (
        ("order", "rating", "descending"),
        ("limit",),
        ("rule", "playcount", "is", "0"),
        ("rule", "rating", "greaterthan", "7"),
    ),
    "recentepisodes": (
        ("order", "dateadded", "descending"),
        ("limit",),
        ("rule", "playcount", "is", "0"),
    ),
    "inprogressepisodes": (("limit",), ("rule", "inprogress", "true", None)),
}
EPISODE_CONTENT = ("recentepisodes", "inprogressepisodes")

# The one node that is a plugin listing rather than a library filter: next
# episodes are a server question (what follows what was watched), which no
# Kodi rule expresses.
DYNAMIC_NODES = ("nextepisodes",)

# Rows a node lists at most, where it lists a selection.
LIMIT = 25

# Stock Kodi icon per media type (structural entries never carry addon or
# server art -- plan §2).
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
    the 30000+ addon range is empty -- a bare ``30350`` renders blank in the
    library and in the node editor. Ours therefore go in as text; Kodi-core
    ids stay numeric so they keep following the UI language.
    """
    if isinstance(value, int):
        return localized(value) if value >= 30000 else str(value)
    return value or fallback


def set_node_icon(xml, icon):
    """Set (or add) a node's ``<icon>``, keeping it before the other
    children the way Kodi's own node files write it."""
    element = xml.find("icon")
    if element is None:
        element = etree.Element("icon")
        xml.insert(0, element)
    element.text = icon


def write_xml(file, element):
    etree.ElementTree(element).write(file)


# --- paths ---------------------------------------------------------------------


def video_library_path():
    """Kodi's video library node root -- the user's, where the parent sits."""
    return xbmcvfs.translatePath("special://profile/library/video/")


def node_root_path():
    """Directory holding every generated video node."""
    return os.path.join(
        xbmcvfs.translatePath("special://profile/library/video"), NODE_ROOT
    )


def node_folder(view):
    """Per-library folder name inside :data:`NODE_ROOT`."""
    return "%s%s%s" % (fs.PREFIX, view["Media"], view["Id"])


def single_file(single):
    """A single node's file name. ``File`` names it when the tag cannot:
    the two Downloads singles share one tag and would otherwise collide."""
    return "%s_%s.xml" % (fs.PREFIX, single.get("File", single["Tag"].replace(" ", "")))


def downloads_root_path():
    """The downloads root as the episodes node's rule needs it: absolute,
    with a trailing separator so ``startswith`` cannot match a sibling
    directory that merely shares the prefix."""
    from kofin.downloads import downloads_root

    return downloads_root().rstrip("/") + "/"


def library_node_path(view, node):
    return "library://video/%s/%s/%s.xml" % (NODE_ROOT, node_folder(view), node)


def single_node_path(single):
    return "library://video/%s/%s" % (NODE_ROOT, single_file(single))


def browse_url(view, node=None):
    params = {"mode": "browse", "type": view["Media"]}

    if view.get("Id"):
        params["view"] = view["Id"]

    if node:
        params["folder"] = node

    return "%s?%s" % ("plugin://plugin.video.kofin/", urlencode(params))


def nextepisodes_url(view):
    params = {"id": view["Id"], "mode": "nextepisodes", "limit": LIMIT}
    return "%s?%s" % ("plugin://plugin.video.kofin/", urlencode(params))


# --- what goes in the tree ------------------------------------------------------


def single_nodes():
    """The tag-filtered singles beside the library nodes: the favorites
    trio always, the Downloads set while the feature is on. One list for
    both the node files and the window properties, so the two surfaces
    cannot disagree."""
    singles = [
        {"Name": localized(30358), "Tag": "Favorite movies", "Media": "movies"},
        {"Name": localized(30359), "Tag": "Favorite tvshows", "Media": "tvshows"},
        {"Name": localized(30360), "Tag": "Favorite episodes", "Media": "episodes"},
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
        # tagged show -- verified live, 25+ rows for two downloads. Their
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


# --- building one file ----------------------------------------------------------


def _node(kind, order, icon):
    attributes = {"order": str(order)}
    if kind != "main":
        attributes["type"] = kind
    element = etree.Element("node", attributes)
    etree.SubElement(element, "icon").text = icon
    return element


def _tag_rule(xml, tag):
    rule = etree.SubElement(xml, "rule", {"field": "tag", "operator": "is"})
    etree.SubElement(rule, "value").text = tag


def _parts(xml, parts):
    for part in parts:
        if part[0] == "order":
            etree.SubElement(xml, "order", {"direction": part[2]}).text = part[1]
        elif part[0] == "limit":
            etree.SubElement(xml, "limit").text = str(LIMIT)
        elif part[0] == "group":
            etree.SubElement(xml, "group").text = part[1]
        elif part[0] == "rule":
            rule = etree.SubElement(
                xml, "rule", {"field": part[1], "operator": part[2]}
            )
            if part[3] is not None:
                etree.SubElement(rule, "value").text = part[3]


def build_parent():
    xml = _node("main", NODE_ROOT_ORDER, NODE_ROOT_ICON)
    etree.SubElement(xml, "label").text = settings.addon_name()
    return xml


def build_index(view, mixed, order):
    """A library folder's own ``index.xml``: its position and its name."""
    xml = _node("main", order, node_icon(view["Media"]))
    etree.SubElement(xml, "label").text = (
        view["Name"] if not mixed else "%s (%s)" % (view["Name"], _label(view["Media"]))
    )
    return xml


def build_node(view, key, label, order):
    """One filter node of a library folder."""
    xml = _node("filter", order, node_icon(view["Media"], key))
    etree.SubElement(xml, "label").text = _node_label(label or view["Name"])
    etree.SubElement(xml, "match").text = "all"
    etree.SubElement(xml, "content").text = (
        "episodes" if key in EPISODE_CONTENT else view["Media"]
    )
    _tag_rule(xml, view["Tag"])
    _parts(xml, NODE_PARTS[key])
    return xml


def build_dynamic(view, key, label, order, path):
    """A folder node that opens a plugin listing (next episodes)."""
    xml = _node("folder", order, node_icon(view["Media"], key))
    etree.SubElement(xml, "label").text = _node_label(label or view["Name"])
    etree.SubElement(xml, "content").text = "episodes"
    etree.SubElement(xml, "path").text = path
    return xml


def build_single(single, order, item_type):
    """A single node: a favourites or a Downloads listing."""
    episodes = single["Media"] == "episodes"
    favourite_episodes = item_type == "favorites" and episodes
    icon = single.get("Icon") or node_icon(single["Media"], "favorites")
    xml = _node("folder" if favourite_episodes else "filter", order, icon)
    etree.SubElement(xml, "label").text = single["Name"]
    etree.SubElement(xml, "match").text = "all"
    content = etree.SubElement(xml, "content")
    content.text = single["Media"]

    if not episodes:
        _tag_rule(xml, single["Tag"])
    elif single.get("Path"):
        # An episodes node filters on the file's location, because a tag
        # rule here resolves against the *show* (see single_nodes).
        rule = etree.SubElement(
            xml, "rule", {"field": "path", "operator": "startswith"}
        )
        etree.SubElement(rule, "value").text = single["Path"]

    if favourite_episodes:
        etree.SubElement(xml, "path").text = browse_url(single, "FavEpisodes")
    else:
        _parts(xml, NODE_PARTS["all"])
    return xml


# --- the tree ----------------------------------------------------------------


def write_parent(root):
    """The ``Kofin`` folder node itself. Written on creation only: ``order``
    and ``label`` are the user's to change afterwards (the fork pinned its
    own layout; kofin does not)."""
    file = os.path.join(root, "index.xml")
    if not os.path.isfile(file):
        write_xml(file, build_parent())


def write_library(root, view, mixed, order):
    """One library's folder: its index and every node of its kind."""
    folder = os.path.join(root, node_folder(view))
    if not os.path.isdir(folder):
        os.makedirs(folder)

    write_xml(os.path.join(folder, "index.xml"), build_index(view, mixed, order))

    for position, (key, label) in enumerate(NODES[view["Media"]]):
        file = os.path.join(folder, "%s.xml" % key)
        if key in DYNAMIC_NODES:
            xml = build_dynamic(view, key, label, position, nextepisodes_url(view))
        else:
            xml = build_node(view, key, label, position)
        write_xml(file, xml)


def write_single(root, single, order):
    item_type = single.get("Type", "favorites")
    write_xml(
        os.path.join(root, single_file(single)), build_single(single, order, item_type)
    )


def write_tree(entries, singles):
    """The whole tree from what should be in it: ``entries`` are the sorted
    ``(view, mixed)`` pairs of the whitelist, ``singles`` the single nodes.

    Numbered on the nodes actually written, not on the entries walked: a
    music library is whitelisted and sorted like the rest but has no video
    node, and numbering it anyway leaves holes that the singles then start
    after. Ends by reconciling the tree against what was written -- a
    library can leave the whitelist by a route that never called
    remove_library (server-side deletion, a settings diff, an install that
    leaked before this pass existed), and a single can go with its feature.
    """
    root = node_root_path()
    if not os.path.isdir(root):
        os.makedirs(root)

    write_parent(root)
    keep = set()
    order = 0

    for view, mixed in entries:
        if view["Media"] not in NODES:
            # A music library is whitelisted like the rest and has no video
            # node; the same is true of any kind the table does not know
            # (a boxsets view that reached the whitelist through update
            # mode, live on 2026-08-27). Skipping is the only answer that
            # keeps the rest of the tree -- raising here killed the library
            # thread at startup, and with it every sync until a restart.
            if view["Media"] != "music":
                LOG.warning(
                    "--[ nodes ] no node kind for %s (%s); skipped",
                    view["Id"],
                    view["Media"],
                )
            continue
        write_library(root, view, mixed, order)
        keep.add(node_folder(view))
        order += 1

    for single in singles:
        write_single(root, single, order)
        keep.add(single_file(single))
        order += 1

    for name in fs.remove_managed_entries(root, keep=keep):
        LOG.info("--[ nodes ] pruned stale %s", name)


# --- taking it down ------------------------------------------------------------


def delete_tree():
    """Remove the whole generated tree.

    Name-gated throughout: only ``kofin``-prefixed entries and the parent's
    own index.xml go, so hand-made node files living beside them survive
    (they are the user's, not ours), and the parent stays while they do.
    """
    root = node_root_path()
    fs.remove_managed_entries(root, also=("index.xml",))
    fs.remove_empty(root)


def delete_library(view_id):
    """Remove one library's folder, whatever kind it was written as."""
    root = node_root_path()
    dirs, _ = fs.listdir(root)
    for name in dirs:
        if fs.is_managed(name) and name.endswith(view_id):
            fs.remove_folder(os.path.join(root, name))


def migrate_flat_nodes():
    """Clear out the pre-:data:`NODE_ROOT` layout.

    Before the ``kofin`` parent, every library node folder and the three
    ``kofin_Favorite*.xml`` sat directly in the video library root. They are
    regenerated under the parent, so the old copies are dead weight that
    would also show up twice in the library. NODE_ROOT itself is the new
    home, not a leftover.
    """
    fs.remove_managed_entries(video_library_path(), keep=(NODE_ROOT,))
