"""The MyMusic ``source`` row behind each synced Jellyfin music library.

Kodi's music library has no tag table, so the video side's per-library rule
(``tag is <library name>``) has no counterpart here, and a ``path`` rule
cannot stand in: a downloaded song's path is repointed at the filesystem, so
every download would fall out of its library's node. Kodi's *source* surface
can carry it. The smart-playlist ``source`` rule compiles to an EXISTS over
``album_source`` joined to ``source.strName`` for artists, albums and songs
alike (xbmc/playlists/SmartPlayList.cpp), and ``album_source`` is a link
table — repointing a song never touches it.

So: one source row per whitelisted music library, every album kofin writes
for it linked, and the node files filter on the source name. The writers do
the linking as they go (writers/music.py); this module owns the name both
sides must agree on, and the reconcile that heals the table after Kodi has
been through it.

That reconcile is not belt-and-braces. ``CMusicDatabase::UpdateSources``
runs ``DELETE FROM source`` whenever the table disagrees with sources.xml,
and with an empty sources.xml it disagrees the moment kofin writes a row —
so any user-triggered music scan empties every node this feature draws.
"""

from kofin.core.log import Logger
from kofin.sync.kodidb.music import Music

LOG = Logger(__name__)

# How much of the library id disambiguates two libraries sharing a name.
NAME_SUFFIX_LENGTH = 8


def source_name(view_id, views):
    """The ``source.strName`` for a library — its name, made unique.

    The name is the *whole* of what a node's rule matches, so two music
    libraries both called "Music" would each show the other's contents. The
    suffix is the library id's first characters, applied only on a real
    clash, and both the node writer and the database writer derive it from
    the same view list so the two cannot disagree about it.
    """
    names = {}

    for view in views:
        names.setdefault(_view_name(view), []).append(_view_id(view))

    for view in views:
        if _view_id(view) != view_id:
            continue

        name = _view_name(view)

        if len(names.get(name, ())) > 1:
            return "%s (%s)" % (name, view_id[:NAME_SUFFIX_LENGTH])

        return name

    return view_id


def reassert(kofin_cursor, music_cursor, views):
    """Rewrite every kofin source row and its album links from scratch.

    Idempotent by construction — ``ensure_source`` renames in place and the
    links are INSERT OR IGNORE against a unique index — so the healthy case
    is a few hundred no-op statements. Returns ``{library id: source id}``.

    The song leg is not redundant with the album leg: a single's album is
    created by the writer on the fly (``writers/music.py`` ``single``) and
    has no kofin.db reference of its own, so walking the album mappings
    alone drops every single out of the library's nodes.
    """
    from kofin.sync.kofindb import JellyfinDatabase

    mapping = JellyfinDatabase(kofin_cursor)
    music = Music(music_cursor)
    sources = {}

    for view in views:
        view_id = _view_id(view)
        source_id = music.ensure_source(view_id, source_name(view_id, views))
        sources[view_id] = source_id

        for album_id in mapping.get_kodi_ids_by_media_folder("album", view_id):
            music.link_album_source(album_id, source_id)

        music.link_song_albums_source(
            mapping.get_kodi_ids_by_media_folder("song", view_id), source_id
        )

    removed = music.prune_sources(sources)

    if removed:
        LOG.info("removed %d music source(s) for unsynced libraries", removed)

    return sources


def _view_id(view):
    return str(view["Id"] if isinstance(view, dict) else view.view_id)


def _view_name(view):
    return str(view["Name"] if isinstance(view, dict) else view.view_name)
