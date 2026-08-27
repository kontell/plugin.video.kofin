"""Library removal (P2.2): every synced row of one library out of Kodi's
database, through the writers' own ``remove``, then the library off the
whitelist. The view row and the generated nodes go elsewhere
(``Views.remove_library``); this is the database half.
"""

from kofin.core.log import Logger
from kofin.sync.db import Database
from kofin.sync import kofindb as jellyfin_db
from kofin.sync.kodidb import Music as MusicKodiDb
from kofin.sync.writers import Movies, TVShows, MusicVideos, Music

LOG = Logger(__name__)


def remove_library(host, api, sync, library_id, dialog):
    """Remove a library's rows from Kodi's database and drop it from the
    whitelist. ``sync`` is the loaded sync.json dict; the caller saves it."""
    with Database("kofin") as jellyfindb:

        db = jellyfin_db.JellyfinDatabase(jellyfindb.cursor)
        library = db.get_view(library_id.replace("Mixed:", ""))

        if library is None:
            LOG.info("Library %s is already removed", library_id)

            return

        items = db.get_item_by_media_folder(library_id.replace("Mixed:", ""))
        media = "music" if library.media_type == "music" else "video"

        # A music library's `source` row is not one of its items, so it
        # outlives an item-less removal: the gate has to open for the
        # media type, not just for the count. Every album's own
        # album_source link goes with the album (tgrDeleteAlbum), and
        # what tgrDeleteSource does not cover is exactly this row.
        if items or library.media_type == "music":
            with host.music_database_lock if media == "music" else host.database_lock:
                with Database(media) as kodidb:

                    count = 0

                    if library.media_type == "mixed":

                        movies = [x for x in items if x[1] == "Movie"]
                        tvshows = [x for x in items if x[1] == "Series"]

                        obj = Movies(api, jellyfindb, kodidb, library).remove

                        for item in movies:

                            obj(item[0])
                            dialog.update(
                                int((float(count) / float(len(items)) * 100)),
                                heading="%s: %s" % ("Kofin", library.view_name),
                            )
                            count += 1

                        obj = TVShows(api, jellyfindb, kodidb, library).remove

                        for item in tvshows:

                            obj(item[0])
                            dialog.update(
                                int((float(count) / float(len(items)) * 100)),
                                heading="%s: %s" % ("Kofin", library.view_name),
                            )
                            count += 1
                    else:
                        default_args = (api, jellyfindb, kodidb)
                        for item in items:
                            if item[1] in ("Series", "Season", "Episode"):
                                TVShows(*default_args).remove(item[0])
                            elif item[1] in ("Movie", "BoxSet"):
                                Movies(*default_args).remove(item[0])
                            elif item[1] in (
                                "MusicAlbum",
                                "MusicArtist",
                                "Audio",
                            ):
                                Music(*default_args).remove(item[0])
                            elif item[1] == "MusicVideo":
                                MusicVideos(*default_args).remove(item[0])

                            dialog.update(
                                int((float(count) / float(len(items)) * 100)),
                                heading="%s: %s" % ("Kofin", library.view_name),
                            )
                            count += 1

                    if library.media_type == "music":
                        MusicKodiDb(kodidb.cursor).delete_source_for(
                            library_id.replace("Mixed:", "")
                        )

    if library_id in sync["Whitelist"]:
        sync["Whitelist"].remove(library_id)

    elif "Mixed:%s" % library_id in sync["Whitelist"]:
        sync["Whitelist"].remove("Mixed:%s" % library_id)
