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

from contextlib import contextmanager
from typing import Dict, Iterator, List

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
    names: Dict[str, List[str]] = {}

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
    is three no-op statements per library. Returns ``{library id: source id}``.

    The song leg is not redundant with the album leg: a single's album is
    created by the writer on the fly (``writers/music.py`` ``single``) and
    has no kofin.db reference of its own, so walking the album mappings
    alone drops every single out of the library's nodes.

    Done entirely in SQL against an ATTACHed kofin.db. The mapping rows used
    to be fetched into Python and fed back a library at a time, and that
    boundary was the whole cost of the reconcile — see the measurements on
    the statements in ``queries_music``. kofin.db is only ever *read* here,
    so the fact that a cross-database transaction is not atomic while the
    main database is in WAL mode does not apply: every write lands in
    MyMusic.
    """
    sources = {}

    with mapped(kofin_cursor, music_cursor) as music:
        for view in views:
            view_id = _view_id(view)
            source_id = music.ensure_source(view_id, source_name(view_id, views))
            sources[view_id] = source_id

            music.link_library_albums(view_id, source_id)
            music.link_library_song_albums(view_id, source_id)

        removed = music.prune_sources(sources)

    if removed:
        LOG.info("removed %d music source(s) for unsynced libraries", removed)

    return sources


def prune_orphan_paths(kofin_cursor, music_cursor) -> int:
    """Reclaim the path rows kofin abandoned; how many went.

    The statement needs the mapping in-engine — a downloaded song's server
    row is referenced by nothing in MyMusic and must be spared — so it runs
    inside the same ATTACH window as the reconcile. ``check_version`` calls
    it once per service start.
    """
    with mapped(kofin_cursor, music_cursor) as music:
        return int(music.prune_orphan_paths())


@contextmanager
def mapped(kofin_cursor, music_cursor) -> Iterator[Music]:
    """kofin.db ATTACHed to the music connection for the block's duration.

    ATTACH is refused inside a transaction and every caller has usually
    opened one — ``check_version`` arrives with its blank-artist repair
    pending, ``full_sync`` at the end of a library write pass. Flushing
    theirs is safe rather than merely convenient: the repair paths are
    idempotent, and the writers already commit per page, so the most this
    can promote from "would have rolled back" to "committed" is the tail of
    a pass the resume machinery re-runs anyway.

    DETACH is refused inside a transaction too, so the block's own writes
    commit here rather than at the caller's context exit — and on the error
    path they roll back first, because ``Database.__exit__`` rolls back on
    purpose (audit finding #15) and the commit DETACH forces must not
    quietly convert that into "half a reconcile, persisted".
    """
    music = Music(music_cursor)
    music_cursor.connection.commit()
    music.attach_mapping(_mapping_path(kofin_cursor))

    try:
        yield music
    except BaseException:
        music_cursor.connection.rollback()
        raise
    else:
        music_cursor.connection.commit()
    finally:
        # Either arm above ended the transaction, so this can always run.
        music.detach_mapping()


def _mapping_path(kofin_cursor):
    """The file behind the kofin connection the caller handed us.

    Asked of the connection rather than re-derived from settings, so the
    reconcile can only ever attach the database it was given — which is also
    what makes it follow a test's path override without knowing about one.
    """
    kofin_cursor.execute("PRAGMA database_list")

    for _seq, name, path in kofin_cursor.fetchall():
        if name == "main":
            return path

    raise RuntimeError("kofin connection has no main database to attach")


def _view_id(view):
    return str(view["Id"] if isinstance(view, dict) else view.view_id)


def _view_name(view):
    return str(view["Name"] if isinstance(view, dict) else view.view_name)
