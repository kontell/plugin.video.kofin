"""Distribution identity. Changed at build time, never by a user setting."""

BACKEND = "sql"


def native_sync() -> bool:
    """The API distribution's first native vertical slice arrives in phase 3."""
    return BACKEND == "sql"


def legacy_features() -> bool:
    """Downloads, texture writes and native repair still require the SQL port."""
    return BACKEND == "sql"
