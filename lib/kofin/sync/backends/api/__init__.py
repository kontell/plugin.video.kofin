"""Public-API backend: complete video directories and durable staged batches."""

import os
import shutil
from contextlib import contextmanager

from kofin.core.log import Logger
from kofin.sync import private
from kofin.sync.backend import ApplyResult
from kofin.sync.catalogue import claim_backend
from kofin.sync.private import Database

LOG = Logger(__name__)

SUPPORTED = (
    "Movie",
    "Series",
    "Season",
    "Episode",
    "MusicVideo",
    "BoxSet",
    "Audio",
    "MusicAlbum",
    "MusicArtist",
)


class APIBackend:
    name = "api"

    def initialize(self):
        with Database() as db:
            claim_backend(db.cursor, self.name)
        forget_companion()

    @contextmanager
    def batch(self, kind, server, library=None, **kwargs):
        from .identity import current_store

        store = current_store()
        store.initialize(server.server)
        batch = Batch(store, library)
        yield batch
        batch.commit()

    def capabilities(self):
        from .capabilities import inspect

        return inspect()


def forget_companion():
    """Delete what the Piers build kept for the companion add-on: its request
    document, its set journal and its staged chapter images. A companion
    still installed reads a missing document as one asking nothing, and on
    Kodi 23 it refuses every database request anyway (its schema gate opens
    at MyVideos149)."""
    folder = os.path.join(private.addon_data_path(), "companion")
    if os.path.isdir(folder):
        shutil.rmtree(folder, ignore_errors=True)
        LOG.info("removed the companion add-on's requests")


class Batch:
    def __init__(self, store, library):
        self.store = store
        self.library = library
        self.items = []
        self.removed = []
        self.results = []

    def apply(self, item):
        supported = item.kind in SUPPORTED
        result = ApplyResult(item, "staged" if supported else "unsupported")
        if supported:
            self.items.append(item.payload)
        self.results.append(result)
        return result

    userdata = apply
    artwork = apply

    def remove(self, item):
        supported = item.kind in SUPPORTED
        result = ApplyResult(item, "staged" if supported else "unsupported")
        if supported:
            self.removed.append(item.item_id)
        self.results.append(result)
        return result

    def commit(self):
        if self.items or self.removed:
            self.store.publish(
                self.items,
                removed=self.removed,
                membership=(self.library or {}).get("Id"),
            )
            self.items.clear()
            self.removed.clear()
        results, self.results = self.results, []
        return results


MovieBatch = Batch
