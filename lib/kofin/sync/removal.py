"""Library removal through the selected native-library adapter."""

from kofin.sync.backend import create_backend


def remove_library(host, api, sync, library_id, dialog):
    backend = getattr(host, "backend", None) or create_backend()
    return backend.remove_library(host, api, sync, library_id, dialog)
