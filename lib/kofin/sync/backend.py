"""The native-library adapter the service runs."""

from typing import Any


def create_backend() -> Any:
    # Lazy: a plugin invocation pays for the backend's imports only on the
    # routes that reach it.
    from kofin.sync.backends.api import APIBackend

    return APIBackend()
