"""Distribution identity. Changed at build time, never by a user setting."""

BACKEND = "api"


def native_sync() -> bool:
    """Select the legacy multi-media coordinator and native node generators.

    The API movie worker has its own capability and fresh-library gates.
    """
    return BACKEND == "sql"


def legacy_features() -> bool:
    """Downloads, texture writes and native repair still require the SQL port."""
    return BACKEND == "sql"
