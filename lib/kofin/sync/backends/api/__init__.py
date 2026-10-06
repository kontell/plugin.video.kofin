"""Public-API backend foundation. Native sync remains disabled in phase 1."""

from kofin.sync.backend import CompatibilityError
from kofin.sync.catalogue import claim_backend
from kofin.sync.private import Database


class APIBackend:
    name = "api"

    def initialize(self):
        with Database() as db:
            claim_backend(db.cursor, self.name)

    def batch(self, *args, **kwargs):
        raise CompatibilityError(
            "native API sync is not enabled in this internal build"
        )

    def capabilities(self):
        from .capabilities import inspect

        return inspect()
