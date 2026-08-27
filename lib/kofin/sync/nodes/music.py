"""The ``Kofin`` folder in Kodi's *music* library.

Kodi keeps music nodes in a tree of their own (``CLibraryDirectory`` over
``library://music/``), so nothing in the video tree reaches here. The tree
holds a folder per synced music library -- Artists, Albums, Songs and
Genres, the same ways in Kodi's own music library offers -- plus a
Downloaded folder while the downloads feature is on.

A library's sub-nodes filter on its MyMusic ``source`` row rather than a
tag (MyMusic has none) or a path (a downloaded song's path is repointed at
the filesystem, so it would fall out of its own library). See
sync/musicsources.py. Every file is written whole on every pass: nothing in
them is the user's to keep, and a leftover rule for a renamed library would
match nothing under ``match=all`` -- silently.
"""

import os
import xml.etree.ElementTree as etree

import xbmcvfs

from kofin.core import settings
from kofin.core.log import Logger
from kofin.sync import musicsources
from kofin.sync.db import Database, get_sync
from kofin.sync import kofindb as jellyfin_db
from kofin.sync.nodes import fs
from kofin.sync.nodes.video import (
    NODE_DOWNLOADS_ICON,
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


def music_node_root_path():
    """Directory holding the generated *music* nodes."""
    return os.path.join(
        xbmcvfs.translatePath("special://profile/library/music"), NODE_ROOT
    )


def music_root_path():
    """The downloads music directory as a music node's rule needs it --
    the same value the Downloaded-music .xsp filters on, so the node and the
    playlist can never disagree about what "downloaded" means."""
    from kofin.downloads import downloads_root
    from kofin.downloads.files import MUSIC_DIR

    return "%s/%s/" % (downloads_root().rstrip("/"), MUSIC_DIR)


def music_node_folder(view):
    """Per-library folder name inside the music tree -- the music twin of
    :func:`video.node_folder`, which cannot be reused because its name folds
    in a ``Media`` that is always ``music`` here."""
    return "%smusic%s" % (fs.PREFIX, view["Id"])


def music_library_views():
    """The synced music libraries, in the order their folders should sit.

    Ordered by SortedViews like the video entries are, and tolerant of a
    library that is missing from it -- this runs on every node pass, and a
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
    """Write (or take down) the music tree. Returns whether it now exists.

    ``libraries`` is read from kofin.db when not supplied, so the downloads
    manager can keep calling this with no arguments.
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
        rule = etree.Element("rule", {"field": "source", "operator": "is"})
        etree.SubElement(rule, "value").text = view["Source"]
        _write_music_filter_node(folder, order, stem, label, content, group, rule)


def _write_downloaded_music_folder(root, index):
    """The Downloaded-music folder: the same three ways in, filtered on the
    downloads directory instead of a source.

    Path rather than source because a download is not a library -- it is a
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
