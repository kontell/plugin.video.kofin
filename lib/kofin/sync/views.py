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
from kofin.core.log import Logger
from kofin.sync.db import Database, get_sync, save_sync
from kofin.sync import kofindb as jellyfin_db
from kofin.sync import fields as api
from kofin.sync.shims import localized, window_prop

LOG = Logger(__name__)

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
DYNNODES = {
    "tvshows": [
        ("all", None),
        ("RecentlyAdded", 30350),
        ("recentepisodes", 30355),
        ("InProgress", 30351),
        ("inprogressepisodes", 30356),
        ("nextepisodes", 30357),
        ("Genres", 135),
        ("Random", 30353),
        ("recommended", 30354),
    ],
    "movies": [
        ("all", None),
        ("RecentlyAdded", 30350),
        ("InProgress", 30351),
        ("Boxsets", 20434),
        ("Favorite", 30361),
        ("FirstLetter", 30362),
        ("Genres", 135),
        ("Random", 30353),
    ],
    "musicvideos": [
        ("all", None),
        ("RecentlyAdded", 30350),
        ("InProgress", 30351),
        ("Unwatched", 30352),
    ],
    "homevideos": [
        ("all", None),
        ("RecentlyAdded", 30350),
        ("InProgress", 30351),
        ("Favorite", 30361),
    ],
    "books": [
        ("all", None),
        ("RecentlyAdded", 30350),
        ("InProgress", 30351),
        ("Favorite", 30361),
    ],
    "audiobooks": [
        ("all", None),
        ("RecentlyAdded", 30350),
        ("InProgress", 30351),
        ("Favorite", 30361),
    ],
    "music": [
        ("all", None),
        ("RecentlyAdded", 30350),
        ("Favorite", 30361),
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

        # /Library/MediaFolders is admin-only (403 for a normal user). It is
        # worth asking for because it carries OriginalCollectionType and the
        # physical folders behind grouped views, but it must not be required:
        # the fork only ever ran as an admin, so a 403 there took the whole
        # view table down with it, and an empty view table silently breaks
        # node generation and fast_sync's media-type filter.
        libraries = []
        try:
            libraries = self.server.media_folders()["Items"]
        except Exception as error:
            LOG.info(
                "media folders unavailable (%s); using the user's own views", error
            )

        try:
            library_ids = [x["Id"] for x in libraries]
            for view in self.server.views().get("Items", []):
                if view["Id"] not in library_ids:
                    libraries.append(view)

        except Exception as error:
            LOG.exception(error)
            raise IndexError("Unable to retrieve libraries: %s" % error)

        return libraries

    def get_views(self):
        """Get the media folders. Add or remove them. Do not proceed if issue getting libraries."""
        try:
            libraries = self.get_libraries()
        except IndexError as error:
            LOG.exception(error)

            return

        self.sync["SortedViews"] = [x["Id"] for x in libraries]

        for library in libraries:

            if library["Type"] == "Channel":
                library["Media"] = "channels"
            else:
                library["Media"] = library.get(
                    "OriginalCollectionType", library.get("CollectionType", "mixed")
                )

            self.add_library(library)

        with Database("kofin") as kofin_db:

            views = jellyfin_db.JellyfinDatabase(kofin_db.cursor).get_views()
            removed = []

            for view in views:
                if view.view_id not in self.sync["SortedViews"]:
                    removed.append(view.view_id)

            if removed:
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

        node_path = xbmcvfs.translatePath("special://profile/library/video")
        playlist_path = xbmcvfs.translatePath("special://profile/playlists/video")
        index = 0

        if not os.path.isdir(node_path):
            os.makedirs(node_path)

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
                        for media in ("movies", "tvshows"):

                            temp_view = dict(view)
                            temp_view["Media"] = media
                            self.add_playlist(playlist_path, temp_view, True)
                            self.add_nodes(node_path, temp_view, True)

                        index += 1  # Compensate for the duplicate.
                    else:
                        if view["Media"] in ("movies", "tvshows", "musicvideos"):
                            self.add_playlist(playlist_path, view)

                        if view["Media"] not in ("music",):
                            self.add_nodes(node_path, view)

                    index += 1

        for single in [
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
        ]:

            self.add_single_node(node_path, index, "favorites", single)
            index += 1

        settings.set_str("viewsHash", current_hash)
        self.window_nodes()

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

        name = xml.find("name")
        name.text = (
            view["Name"] if not mixed else "%s (%s)" % (view["Name"], view["Media"])
        )

        match = xml.find("match")
        match.text = "all"

        for rule in xml.findall(".//value"):
            if rule.text == view["Tag"]:
                break
        else:
            rule = etree.SubElement(xml, "rule", {"field": "tag", "operator": "is"})
            etree.SubElement(rule, "value").text = view["Tag"]

        tree = etree.ElementTree(xml)
        tree.write(file)

    def add_nodes(self, path, view, mixed=False):
        """Create or update the video node file."""
        folder = os.path.join(path, "kofin%s%s" % (view["Media"], view["Id"]))

        if not xbmcvfs.exists(folder):
            xbmcvfs.mkdir(folder)

        self.node_index(folder, view, mixed)

        if view["Media"] == "tvshows":
            self.node_tvshow(folder, view)
        else:
            self.node(folder, view)

    def add_single_node(self, path, index, item_type, view):

        file = os.path.join(path, "kofin_%s.xml" % view["Tag"].replace(" ", ""))

        try:
            if os.path.isfile(file):
                xml = etree.parse(file).getroot()
            else:
                xml = self.node_root(
                    (
                        "folder"
                        if item_type == "favorites" and view["Media"] == "episodes"
                        else "filter"
                    ),
                    index,
                    node_icon(view["Media"], "favorites"),
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
                node_icon(view["Media"], "favorites"),
            )
            etree.SubElement(xml, "label")
            etree.SubElement(xml, "match")
            etree.SubElement(xml, "content")

        label = xml.find("label")
        label.text = view["Name"]

        content = xml.find("content")
        content.text = view["Media"]

        match = xml.find("match")
        match.text = "all"

        if view["Media"] != "episodes":

            for rule in xml.findall(".//value"):
                if rule.text == view["Tag"]:
                    break
            else:
                rule = etree.SubElement(xml, "rule", {"field": "tag", "operator": "is"})
                etree.SubElement(rule, "value").text = view["Tag"]

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

    def node_index(self, folder, view, mixed=False):

        file = os.path.join(folder, "index.xml")
        index = self.sync["SortedViews"].index(view["Id"])

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

        label = xml.find("label")
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

        label = xml.find("label")
        label.text = str(name) if isinstance(name, int) else name

        content = xml.find("content")
        content.text = view["Media"]

        match = xml.find("match")
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

        label = xml.find("label")
        label.text = _label(name)

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
        """Returns a list of sorted media folders based on the Jellyfin views.
        Insert them in SortedViews and remove Views that are not in media folders.
        """
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

        try:
            self.media_folders = self.get_libraries()
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

        for single in [
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
        ]:

            self.window_single_node(index, "favorites", single)
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
        path = "library://video/kofin_%s.xml" % view["Tag"].replace(" ", "")
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
        return "library://video/kofin%s%s/%s.xml" % (view["Media"], view["Id"], node)

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
        """Remove all kofin playlists."""
        path = xbmcvfs.translatePath("special://profile/playlists/video/")
        _, files = xbmcvfs.listdir(path)
        for file in files:
            if file.startswith("kofin"):
                self.delete_playlist(os.path.join(path, file))

    def delete_playlist_by_id(self, view_id):
        """Remove playlist based on view_id."""
        path = xbmcvfs.translatePath("special://profile/playlists/video/")
        _, files = xbmcvfs.listdir(path)
        for file in files:
            file = file

            if file.startswith("kofin") and file.endswith("%s.xsp" % view_id):
                self.delete_playlist(os.path.join(path, file))

    def delete_node(self, path):

        xbmcvfs.delete(path)
        LOG.info("DELETE node %s", path)

    def delete_nodes(self):
        """Remove node and children files."""
        path = xbmcvfs.translatePath("special://profile/library/video/")
        dirs, files = xbmcvfs.listdir(path)

        for file in files:

            if file.startswith("kofin"):
                self.delete_node(os.path.join(path, file))

        for directory in dirs:

            if directory.startswith("kofin"):
                _, files = xbmcvfs.listdir(os.path.join(path, directory))

                for file in files:
                    self.delete_node(os.path.join(path, directory, file))

                xbmcvfs.rmdir(os.path.join(path, directory))

    def delete_node_by_id(self, view_id):
        """Remove node and children files based on view_id."""
        path = xbmcvfs.translatePath("special://profile/library/video/")
        dirs, files = xbmcvfs.listdir(path)

        for directory in dirs:

            if directory.startswith("kofin") and directory.endswith(view_id):
                _, files = xbmcvfs.listdir(os.path.join(path, directory))

                for file in files:
                    self.delete_node(os.path.join(path, directory, file))

                xbmcvfs.rmdir(os.path.join(path, directory))
