"""SQL implementation, excluded from the official-repository distribution."""

from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Dict, List

from kofin.sync import fields, kofindb, musicsources
from kofin.sync.backend import ApplyResult
from kofin.sync.catalogue import claim_backend
from kofin.sync.db import Database
from kofin.sync.writers import Movies, TVShows, MusicVideos, Music
from .dispatch import (
    UPDATE_DISPATCH,
    USERDATA_DISPATCH,
    ARTWORK_WRITERS,
    _dispatch,
    _already_mapped,
    removal_writer_for,
)
from .hooks import pipeline_hooks


class SQLBatch:
    def __init__(
        self, server, mapping, native, library=None, full_sync=False, hooks=True
    ):
        claim_backend(mapping.cursor, "sql")
        self._mapping = mapping
        self._native = native
        self._pending: List[ApplyResult] = []
        self._writers: Dict[str, Any]
        callbacks = pipeline_hooks() if hooks else None
        args = (server, mapping, native)
        if native.db_file == "video":
            self._writers = {
                "movies": Movies(*args, library=library, hooks=callbacks),
                "tvshows": TVShows(
                    *args, library=library, update_library=full_sync, hooks=callbacks
                ),
                "musicvideos": MusicVideos(*args, library=library, hooks=callbacks),
            }
        elif native.db_file == "music":
            self._writers = {"music": Music(*args, library=library, hooks=callbacks)}
        else:
            raise ValueError("unknown library kind: %s" % native.db_file)

    def _result(self, item, outcome=None, new=False):
        if any(item.item_id in w.refused for w in self._writers.values()):
            return ApplyResult(item, "skipped", outcome=outcome)
        result = ApplyResult(item, "prepared", new, outcome)
        self._pending.append(result)
        return result

    def apply(self, item):
        write = _dispatch(UPDATE_DISPATCH, self._writers, {"Type": item.kind})
        if write is None:
            return ApplyResult(item, "unsupported")
        known = _already_mapped(self._writers, item.item_id)
        return self._result(item, write(item.payload), new=not known)

    def userdata(self, item):
        write = _dispatch(USERDATA_DISPATCH, self._writers, {"Type": item.kind})
        if write is None:
            return ApplyResult(item, "unsupported")
        return self._result(item, write(item.payload))

    def artwork(self, item):
        name = ARTWORK_WRITERS.get(item.kind)
        writer = self._writers.get(name) if name else None
        if writer is None or not fields.artwork_only(
            writer, item.payload, writer.jellyfin_db.get_item_by_id(item.item_id)
        ):
            return ApplyResult(item, "unsupported")
        return self._result(item)

    def remove(self, item):
        remove = removal_writer_for(
            item.kind,
            self._writers.get("movies"),
            self._writers.get("tvshows"),
            self._writers.get("music"),
            self._writers.get("musicvideos"),
        )
        if remove is None:
            return ApplyResult(item, "unsupported")
        return self._result(item, remove(item.item_id))

    def commit(self):
        # Preserve the crash-recovery ordering: never persist an unchanged
        # mapping for a native row whose transaction did not commit.
        self._native.conn.commit()
        self._mapping.conn.commit()
        results = [replace(r, status="applied") for r in self._pending]
        self._pending.clear()
        return results

    def finish_music(self):
        musicsources.reassert(
            self._mapping.cursor,
            self._native.cursor,
            self._writers["music"].music_views(),
        )

    def boxset_ids(self):
        db = kofindb.JellyfinDatabase(self._mapping.cursor)
        return [row[0] for row in db.get_items_by_media("set")]

    def restamp_boxsets(self, guarded):
        self._writers["movies"].restamp_boxset_states(guarded)

    def reset_boxsets(self):
        self._writers["movies"].boxsets_reset()


class SQLBackend:
    name = "sql"

    @contextmanager
    def _connections(self, kind):
        with Database("kofin") as mapping:
            claim_backend(mapping.cursor, self.name)
            with Database(kind) as native:
                yield mapping, native

    @contextmanager
    def batch(self, kind, server, library=None, *, full_sync=False, hooks=True):
        with self._connections(kind) as (mapping, native):
            yield SQLBatch(server, mapping, native, library, full_sync, hooks)

    @contextmanager
    def pages(self, kind, server, lock, library=None, *, full_sync=False):
        """Hold connections across a pass, and lock/commit each complete page."""
        with self._connections(kind) as (mapping, native):

            @contextmanager
            def page():
                with lock:
                    batch = SQLBatch(server, mapping, native, library, full_sync)
                    yield batch
                    batch.commit()

            yield page

    def remove_library(self, host, api, sync, library_id, dialog):
        from .removal import remove_library

        return remove_library(host, api, sync, library_id, dialog)

    def probe_boxset_drift(self, host):
        from . import maintenance

        return maintenance.probe_boxset_drift(host)

    def check_version(self, host):
        from . import maintenance

        return maintenance.check_version(host)

    def test_databases(self, host):
        with Database("kofin") as mapping:
            claim_backend(mapping.cursor, self.name)
        from . import maintenance

        return maintenance.test_databases(host)

    def repoint_ratings(self, host):
        from . import maintenance

        return maintenance.repoint_ratings(host)

    def reassert_music_sources(self, host):
        from . import maintenance

        return maintenance.reassert_music_sources(host)

    def _reconcile_playlists(self, host, api, kinds):
        from . import maintenance

        return maintenance._reconcile_playlists(host, api, kinds)

    def apply_playlist(self, host, data):
        from . import maintenance

        return maintenance.apply_playlist(host, data)

    def gate_status(self, kinds):
        from kofin.sync import schema

        return schema.gate_status(kinds)

    def refresher(self, *args):
        from kofin.sync.refresh import Refresher

        return Refresher(*args)
