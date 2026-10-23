"""The ``Kofin`` folder in Kodi's *music* library.

Kodi keeps music nodes in a tree of their own (``CLibraryDirectory`` over
``library://music/``), so nothing in the video tree reaches here. The tree
holds a folder per synced music library -- Artists, Albums, Songs and
Genres, the same ways in Kodi's own music library offers.

A library's sub-nodes filter on the folder its songs are filed under
(``library_rule``): a music smart playlist's ``path`` rule takes an album or
an artist by its songs' paths. Every file is written whole on every pass:
nothing in them is the user's to keep, and a leftover rule for a renamed
library would match nothing under ``match=all`` -- silently.
"""

import os
import xml.etree.ElementTree as etree

import xbmcvfs

from kofin.core import settings
from kofin.core.log import Logger
from kofin.sync.nodes import fs
from kofin.sync.nodes.video import (
    NODE_ROOT,
    NODE_ROOT_ICON,
    _node_label,
    set_node_icon,
    write_xml,
)

LOG = Logger(__name__)

# The same idea as the video parent, where Kodi's own ordering runs genres
# 10, artists 20, albums 30, singles 40, songs 50: Kofin follows the five
# ways of browsing the library rather than splitting them up.
MUSIC_NODE_ROOT_ORDER = 55

# The Downloaded-music folder inside the music tree. Named with the prefix
# so the pruner claims it like everything else kofin writes.
MUSIC_DOWNLOADED_FOLDER = fs.PREFIX + "_Downloaded"

# The flat Downloaded-music node this replaced. The pruner sweeps it by
# prefix like any other stale file; the name survives only for the tests
# that plant one.
MUSIC_DOWNLOADED_FILE = fs.PREFIX + "_DownloadedMusic.xml"

# (file stem, label, content, group) for the sub-nodes inside a music folder.
# Every label is a *Kodi-core* string id -- the same ones Kodi's own music
# nodes use (system/library/music/{artists,albums,songs,genres}.xml) -- so
# _node_label leaves them numeric and they follow the UI language. Genres is
# content=artists grouped by genre, exactly as Kodi writes it.
MUSIC_NODES = (
    ("artists", 133, "artists", None),
    ("albums", 132, "albums", None),
    ("songs", 134, "songs", None),
    ("genres", 135, "artists", "genres"),
)


# Stock Kodi icons for those sub-nodes, so skins substitute their own.
MUSIC_NODE_ICONS = {
    "artists": "DefaultMusicArtists.png",
    "albums": "DefaultMusicAlbums.png",
    "songs": "DefaultMusicSongs.png",
    "genres": "DefaultMusicGenres.png",
}


def music_node_root_path():
    """Directory holding the generated *music* nodes."""
    return os.path.join(
        xbmcvfs.translatePath("special://profile/library/music"), NODE_ROOT
    )


def music_node_folder(view):
    """Per-library folder name inside the music tree -- the music twin of
    :func:`video.node_folder`, which cannot be reused because its name folds
    in a ``Media`` that is always ``music`` here."""
    return "%smusic%s" % (fs.PREFIX, view["Id"])


def library_rule(view):
    """The rule a library's sub-nodes filter on: the folder its songs are
    filed under (``Path``)."""
    rule = etree.Element("rule", {"field": "path", "operator": "startswith"})
    etree.SubElement(rule, "value").text = view["Path"]
    return rule


def write_music_nodes(libraries):
    """Write (or take down) the music tree. Returns whether it now exists.

    ``libraries`` are the whitelisted music libraries, each with the folder
    its songs are filed under (``dynamic.music_entries``); an empty list
    takes the tree down. There is no Downloaded folder: a download keeps its
    row's plugin URL, so a path rule on the downloads folder would match
    nothing.
    """
    root = music_node_root_path()

    if not libraries:
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

        # Reconcile: a library that left the whitelist, and the loose
        # kofin_DownloadedMusic.xml from before the Downloaded folder
        # existed -- that sweep is the migration. index.xml carries no
        # prefix and survives; it is creation-only and the user's to reorder.
        fs.remove_managed_entries(root, keep=keep)
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
    write_xml(file, xml)


def _write_music_library_folder(root, view, index):
    """One synced music library's folder and its four ways in."""
    folder = os.path.join(root, music_node_folder(view))

    if not os.path.isdir(folder):
        os.makedirs(folder)

    _write_music_folder_index(folder, index, view["Name"], MUSIC_NODE_ICONS["albums"])

    for order, (stem, label, content, group) in enumerate(MUSIC_NODES):
        rule = library_rule(view)
        _write_music_filter_node(folder, order, stem, label, content, group, rule)


def _write_music_folder_index(folder, order, label, icon):
    """A music folder's own ``index.xml``.

    Rewritten every pass, unlike the tree's parent: a library's name and its
    position are the server's to state, not the user's to keep.
    """
    xml = etree.Element("node", {"order": str(order)})
    set_node_icon(xml, icon)
    etree.SubElement(xml, "label").text = label
    write_xml(os.path.join(folder, "index.xml"), xml)


def _write_music_filter_node(folder, order, stem, label, content, group, rule):
    """One filter node inside a music folder."""
    xml = etree.Element("node", {"order": str(order), "type": "filter"})
    set_node_icon(xml, MUSIC_NODE_ICONS[stem])
    etree.SubElement(xml, "label").text = _node_label(label, stem)
    etree.SubElement(xml, "content").text = content

    if group:
        etree.SubElement(xml, "group").text = group

    etree.SubElement(xml, "match").text = "all"
    xml.append(rule)
    write_xml(os.path.join(folder, "%s.xml" % stem), xml)


def _delete_music_nodes(root):
    """Take the whole music tree back out when nothing wants it.

    Prefix-gated like the reconcile, plus the parent ``index.xml`` -- this
    is the teardown. The folder itself only goes when nothing is left in it,
    so a hand-made node keeps it alive.
    """
    if not os.path.isdir(root):
        return
    try:
        fs.remove_managed_entries(root, also=("index.xml",))
        fs.remove_empty(root)
    except Exception:
        LOG.exception("music node removal failed")
