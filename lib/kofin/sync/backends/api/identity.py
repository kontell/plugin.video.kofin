"""Playback and userdata lookups between Kodi ids and Jellyfin ids.

These serve the play route and the userdata watcher, not the reconciler: a
cache hit still needs ownership verification, and browsing needs no hit.
"""

from kofin.core.settings import Credentials

from . import paths
from .kinds import KINDS, MEDIA, rpc
from .readback import Readback
from .store import Store, namespace


def current_store():
    creds = Credentials.load()
    return Store(namespace(creds.server_id, creds.user_id))


def mapped_item(kodi_id, media):
    """The item id behind an owned native row, or None for anything else."""
    kind = MEDIA.get(media)
    if kind is None:
        return None
    store = current_store()
    table = KINDS[kind]
    try:
        row = rpc(
            table.getter, {table.id_param: kodi_id, "properties": ["file", "uniqueid"]}
        ).get(table.result_key, {})
    except RuntimeError:
        # No such row for that media type: not ours, whatever it is.
        return None
    uid = (row.get("uniqueid") or {}).get("kofin", "")
    prefix = store.namespace + ":"
    if not uid.startswith(prefix):
        return None
    item_id = uid[len(prefix) :]
    placed = store.entry(item_id)
    if placed is None or store.mapping(item_id) is None:
        return None
    if row.get("file") != paths.playback_url(
        store.namespace, kind, placed.library, item_id, placed.parent_id
    ):
        return None
    return item_id


def native_id_for(item_id, media="movie"):
    """The Kodi id of an imported item, verified; None when it has no row."""
    store = current_store()
    mapping = store.mapping(item_id)
    if not mapping or mapping.kodi_id is None:
        return None
    try:
        if mapped_item(mapping.kodi_id, media) == item_id:
            return mapping.kodi_id
    except RuntimeError:
        pass
    placed = store.entry(item_id)
    kind = MEDIA.get(media)
    if placed is None or kind is None:
        return None
    found = Readback(store.namespace).scope(kind, placed.library, placed.parent_id)
    row = found.get(item_id)
    return row[KINDS[kind].id_param] if row else None


def library_url(item_id):
    """The resolver URL of an imported item, or None when it has no row."""
    store = current_store()
    mapping = store.mapping(item_id)
    placed = store.entry(item_id)
    if not mapping or mapping.kodi_id is None or placed is None:
        return None
    return paths.playback_url(
        store.namespace, placed.kind, placed.library, item_id, placed.parent_id
    )
