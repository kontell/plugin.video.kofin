# -*- coding: utf-8 -*-
"""The view table, the server's library listing, and the regeneration of
everything derived from them (fork ``views.py`` port; P2.1 split).

What is generated lives in ``kofin.sync.nodes`` (the video and music node
trees, the skin props) and ``kofin.sync.playlists`` (the smart playlists).
This module decides *when*: nodes and playlists regenerate only when the
view-set hash changed (stored in the hidden ``viewsHash`` setting -- window
props are still refreshed every start, they do not survive Kodi restarts),
and it turns the whitelist into the ordered entries the generators write.
"""

import hashlib

from kofin.core import ipc, settings
from kofin.core.http import Unauthorized
from kofin.core.log import Logger
from kofin.sync import playlists
from kofin.sync.db import Database, get_sync, save_sync
from kofin.sync import kofindb as jellyfin_db
from kofin.sync.nodes import music, props, video

LOG = Logger(__name__)


class Views(object):

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

        playlists.remove_video_playlist_for(view_id)
        video.delete_library(view_id)
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
            # and a library missing from it really is gone -- which is why this
            # case must go on removing, or a non-admin install (most of them)
            # would keep every library the server ever dropped.
            LOG.info("media folders are admin-only here; using the user's own views")
        except Exception as error:
            # Anything else -- a timeout, a 500, a reset -- is the endpoint that
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
            raise IndexError("Unable to retrieve libraries: %s" % error) from error

        return libraries, complete

    def get_views(self):
        """Get the media folders. Add or remove them. Do not proceed if issue getting libraries."""
        try:
            libraries, complete = self.get_libraries()
        except IndexError as error:
            LOG.exception(error)

            return

        # An empty listing against existing references is not a deletion
        # order — the rule the boxsets sweep and the prune already apply,
        # and this is the case with the largest blast radius: the removal
        # below deletes every synced row of every library it names. A user
        # whose access was withdrawn gets a *successful* empty /UserViews
        # (200, zero items — verified on jf12 v12, tests/live/
        # jf12_user_policy.py) and, as a non-admin, a 403 on MediaFolders
        # that ``complete`` rightly ignores; so ``complete`` alone cannot
        # tell that answer from a healthy one. Gate on the whitelist rather
        # than on "no views": a Live TV grant still lists one view for such
        # a user. A library that really left while others stay is still
        # removed (audit F2, fixes plan H3). Before the SortedViews stamp,
        # because an empty stamp would regenerate an empty node tree too.
        synced = {x.replace("Mixed:", "") for x in self.sync["Whitelist"]}
        listed = {x["Id"] for x in libraries}
        if synced and not (synced & listed):
            LOG.warning(
                "the server listed none of the %d synced libraries; an empty "
                "listing is not a deletion order — keeping the view table and "
                "asking again next pass",
                len(synced),
            )
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
        parts.append("layout:%s" % video.NODE_LAYOUT)
        # The Downloads singles exist only while the feature is on, so the
        # toggle must regenerate the tree (docs/offline-downloads-plan.md W1.9).
        parts.append("downloads:%s" % settings.get_bool("downloadsEnabled"))
        # The episodes node embeds the downloads root in its rule, so moving
        # the location has to regenerate the tree (plan W2.6).
        parts.append("downloadspath:%s" % settings.get_str("downloadsPath"))
        return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()

    def get_nodes(self):
        """Set up playlists, video nodes, window props.

        File generation is skipped when nothing feeding it changed (the
        viewsHash guard); window props are session state and always rebuilt.
        """
        current_hash = self.views_hash()

        if settings.get_str("viewsHash") == current_hash:
            LOG.info("--[ nodes ] unchanged (hash match), skipping generation")
            self.window_nodes()
            return

        # Before the whitelist check below: the music tree is its own thing --
        # keyed on the synced *music* libraries and the downloads feature, not
        # on the video whitelist the tree beneath is built from -- and it has
        # its own removal path for the nothing-wanted case.
        music.write_music_nodes()

        # Anything left where the pre-NODE_ROOT layout put it (loose folders
        # and kofin_*.xml in the video library root, loose kofin*.xsp among the
        # user's playlists) belongs to no library any more; the tree below
        # replaces it.
        video.migrate_flat_nodes()
        playlists.migrate_flat_video_playlists()

        if not self.sync["Whitelist"]:
            # Nothing is synced: the whole tree goes, favourites included.
            video.delete_tree()
            playlists.remove_video_playlists()
            settings.set_str("viewsHash", current_hash)
            self.window_nodes()
            return

        entries = self.node_entries()
        singles = video.single_nodes()

        playlists.write_video_playlists(entries)
        video.write_tree(entries, singles)

        settings.set_str("viewsHash", current_hash)
        self.window_nodes()

    def node_entries(self):
        """The whitelist as the generators want it: ``(view, mixed)`` pairs
        in tree order, a mixed library split into its two kinds."""
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
        return entries

    def node_order(self, view):
        """Sort key for one library node: its kind first, the server's order
        within that kind second.

        Kind first because the alternative -- the server's order alone -- reads
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
        rank = video.MEDIA_RANK.get(media, len(video.MEDIA_RANK))

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
            # server did name instead -- offset by sorted-whitelist position
            # so several strays stay stable and distinct -- and let the next
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

    def window_nodes(self):
        """Publish the skin props from the view table and the whitelist."""
        with Database("kofin") as kofin_db:
            libraries = jellyfin_db.JellyfinDatabase(kofin_db.cursor).get_views()

        # Only when there is a server to ask. The listing feeds one thing --
        # the library tiles' artwork -- and that already clears the prop
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

        props.publish(
            libraries, self.sync, video.single_nodes(), self.media_folders, self.server
        )
