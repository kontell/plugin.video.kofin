"""Legacy writer dispatch, confined to the SQL adapter."""

from typing import Any, Dict, Optional

UPDATE_DISPATCH = {
    "Movie": ("movies", "movie"),
    "BoxSet": ("movies", "boxset"),
    "Series": ("tvshows", "tvshow"),
    "Season": ("tvshows", "season"),
    "Episode": ("tvshows", "episode"),
    "MusicVideo": ("musicvideos", "musicvideo"),
    "MusicAlbum": ("music", "album"),
    "MusicArtist": ("music", "artist"),
    "Audio": ("music", "song"),
}

USERDATA_DISPATCH = {
    "Movie": ("movies", "userdata"),
    "Series": ("tvshows", "userdata"),
    "Season": ("tvshows", "userdata"),
    "Episode": ("tvshows", "userdata"),
    "MusicAlbum": ("music", "album"),
    "MusicArtist": ("music", "artist"),
    "Audio": ("music", "userdata"),
}

ARTWORK_WRITERS = {
    "Movie": "movies",
    "Series": "tvshows",
    "Season": "tvshows",
    "Episode": "tvshows",
    "MusicVideo": "musicvideos",
}


def _dispatch(table, writers, item):
    """The bound writer method for an item, or None when nothing handles
    the kind on this database."""
    entry = table.get(item["Type"])
    if entry is None:
        return None
    writer = writers.get(entry[0])
    return None if writer is None else getattr(writer, entry[1])


def _already_mapped(writers: Dict[str, Any], item_id: Optional[str]) -> bool:
    """Whether kofin.db already holds this id.

    The writers on one worker share the database opened for the drain.
    Call this before the write: the write inserts the reference, so a row
    this item just created is not evidence it was already in the library.
    """
    if not item_id:
        return False

    writer = next(iter(writers.values()), None)
    if writer is None:
        return False

    return writer.jellyfin_db.get_item_by_id(item_id) is not None


REMOVAL_WRITERS = {
    "Movie": "movies",
    "BoxSet": "movies",
    "Series": "tvshows",
    "Season": "tvshows",
    "Episode": "tvshows",
    "MusicAlbum": "music",
    "MusicArtist": "music",
    "Audio": "music",
    "MusicVideo": "musicvideos",
}


def removal_writer_for(item_type, movies, tvshows, music, musicvideos):
    """The bound ``remove`` for this kind, or None when nothing handles it."""
    writer = {
        "movies": movies,
        "tvshows": tvshows,
        "music": music,
        "musicvideos": musicvideos,
    }.get(REMOVAL_WRITERS.get(item_type or "", ""))

    return None if writer is None else writer.remove
