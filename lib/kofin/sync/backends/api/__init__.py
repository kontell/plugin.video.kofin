"""Public-API backend: complete movie directories and durable staged batches."""

from contextlib import contextmanager

from kofin.sync.backend import ApplyResult
from kofin.sync.catalogue import claim_backend
from kofin.sync.private import Database


class APIBackend:
    name = "api"

    def initialize(self):
        with Database() as db:
            claim_backend(db.cursor, self.name)

    @contextmanager
    def batch(self, kind, server, library=None, **kwargs):
        from .movies import current_store

        store = current_store()
        store.initialize(server.server)
        batch = MovieBatch(store, library)
        yield batch
        batch.commit()

    def capabilities(self):
        from .capabilities import inspect

        return inspect()


class MovieBatch:
    def __init__(self, store, library):
        self.store = store
        self.library = library
        self.items = []
        self.removed = []
        self.results = []

    def apply(self, item):
        result = ApplyResult(item, "staged" if item.kind == "Movie" else "unsupported")
        if item.kind == "Movie":
            self.items.append(item.payload)
        self.results.append(result)
        return result

    userdata = apply
    artwork = apply

    def remove(self, item):
        result = ApplyResult(item, "staged" if item.kind == "Movie" else "unsupported")
        if item.kind == "Movie":
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
