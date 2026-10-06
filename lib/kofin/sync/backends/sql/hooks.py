# -*- coding: utf-8 -*-
"""Writer hooks: what the pipeline adds to a write that the writers do not own.

The transplanted writers (``sync/writers/``) write the rows Kodi needs for
an item. Two things the shell later bolted onto that write are not the
writer's business and pulled the shell into the transplant the wrong way
round -- ``writers/movies.py`` importing ``kofin.downloads`` to stamp the
downloads tag and re-point a downloaded file, ``writers/music.py``
importing ``sync.musicsources`` to link an album to its library's MyMusic
source row (docs/sync-refactor-assessment.md §5). They live here now, as
callables the pipeline registers on the writers it builds.

Two seams per writer, both keyed by the kind of row being written:

* ``extra_tags(kind, writer, obj)`` -- before ``add_tags`` replaces the tag
  set wholesale, the tags the pipeline wants kept on the row.
* ``after_write(kind, writer, obj)`` -- once the row is in writer shape and
  before the page commits, the pipeline's own writes against it.

A writer built with no hooks writes exactly the fork's rows; the L2 suite
pins both worlds. :func:`pipeline_hooks` is the composition the service
and the full sync use, and the only place in ``sync/`` that imports
``kofin.downloads``.
"""

from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

TagHook = Callable[[Any, Dict[str, Any]], Optional[Iterable[str]]]
WriteHook = Callable[[Any, Dict[str, Any]], None]


class WriterHooks:
    """Per-kind tag contributors and post-write callbacks.

    Empty by default: ``WriterHooks()`` is "the fork's rows, nothing more".
    """

    def __init__(
        self,
        tags: Optional[Mapping[str, Iterable[TagHook]]] = None,
        post_write: Optional[Mapping[str, Iterable[WriteHook]]] = None,
    ) -> None:
        self._tags: Dict[str, List[TagHook]] = {
            kind: list(hooks) for kind, hooks in (tags or {}).items()
        }
        self._post_write: Dict[str, List[WriteHook]] = {
            kind: list(hooks) for kind, hooks in (post_write or {}).items()
        }

    def extra_tags(self, kind: str, writer: Any, obj: Dict[str, Any]) -> List[str]:
        extra: List[str] = []
        for hook in self._tags.get(kind, ()):
            extra.extend(hook(writer, obj) or ())
        return extra

    def after_write(self, kind: str, writer: Any, obj: Dict[str, Any]) -> None:
        for hook in self._post_write.get(kind, ()):
            hook(writer, obj)

    def kinds(self) -> Dict[str, List[str]]:
        """What is registered, for the tests and the log."""
        return {
            "tags": sorted(self._tags),
            "post_write": sorted(self._post_write),
        }


# -- the pipeline's composition -------------------------------------------------


def _downloads_tag_movie(writer: Any, obj: Dict[str, Any]) -> Optional[List[str]]:
    # Downloaded items carry the downloads tag through every rewrite the
    # same way favorites do: add_tags replaces the set wholesale, so an
    # out-of-band stamp dies on the next pass without this
    # (docs/offline-downloads-plan.md W1.8; the Downloads node filters on
    # the tag).
    from kofin.downloads import TAG, store

    if store.is_done_on(writer.jellyfin_db.cursor, obj["Id"]):
        return [TAG]
    return None


def _downloads_tag_show(writer: Any, obj: Dict[str, Any]) -> Optional[List[str]]:
    # A show with any downloaded episode carries the tag the way favorites
    # carry theirs; the show's own jellyfin id is the series id the download
    # rows carry.
    from kofin.downloads import TAG, store

    if store.series_done_on(writer.jellyfin_db.cursor, obj["Id"]):
        return [TAG]
    return None


def _downloads_reassert(writer: Any, obj: Dict[str, Any]) -> None:
    # A changed item's rewrite put the file row back in writer shape; a
    # downloaded one is re-pointed at its local file before the page
    # commits, with the fresh URL recaptured for restore (plan W1.8 -- the
    # L2 suite pins both halves).
    from kofin.downloads import repoint

    repoint.reassert_on(writer.cursor, writer.jellyfin_db.cursor, obj["Id"])


def _downloads_reassert_music(writer: Any, obj: Dict[str, Any]) -> None:
    # Downloaded songs get their local location re-asserted inside the same
    # transaction (plan W1.8/W3.2): the write rebuilt the row in writer
    # shape, and committing that would point a downloaded song back at the
    # server until the next reconcile.
    from kofin.downloads import repoint

    repoint.reassert_music_on(writer.cursor, writer.jellyfin_db.cursor, obj["Id"])


def _music_source_link(writer: Any, obj: Dict[str, Any]) -> None:
    """Link the item's album to its library's MyMusic ``source`` row.

    The music side of the video writers' tag injection. MyMusic has no tag
    table, so a per-library node filters on the source name instead, and
    ``album_source`` is the one link that survives a downloaded song being
    repointed at the filesystem (sync/musicsources.py says why).

    Registered for both ``album`` and ``song``: a single's album is created
    on the fly by ``song_add`` and never passes through ``album``, so the
    album leg alone would drop every single out of its library's nodes.
    """
    from kofin.sync import musicsources

    library_id = obj.get("LibraryId")

    if not library_id or not obj.get("AlbumId"):
        return

    source_id = writer.ensure_source(
        library_id, musicsources.source_name(library_id, writer.music_views())
    )
    writer.link_album_source(obj["AlbumId"], source_id)


def pipeline_hooks() -> WriterHooks:
    """The hooks every writer the sync pipeline builds carries."""
    return WriterHooks(
        tags={"movie": [_downloads_tag_movie], "tvshow": [_downloads_tag_show]},
        post_write={
            "movie": [_downloads_reassert],
            "episode": [_downloads_reassert],
            "album": [_music_source_link],
            "song": [_music_source_link, _downloads_reassert_music],
        },
    )
