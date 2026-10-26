"""Lookups between Kodi ids and Jellyfin ids.

These serve the play route, the userdata watcher and the Jellyfin actions
menu, not the reconciler: a cache hit still needs ownership verification, and
browsing needs no hit.
"""

from kofin.core.settings import Credentials

from . import paths
from .kinds import CONTAINERS, FILED, KINDS, MEDIA, rpc
from .metadata import ASCII_SPACE
from .readback import Readback, artist_key
from .store import Store, namespace

# Kinds whose rows carry no id of ours, found through what the row holds.
FOUND = ("Season", "BoxSet", "MusicAlbum", "MusicArtist")


def current_store():
    creds = Credentials.load()
    return Store(namespace(creds.server_id, creds.user_id))


def mapped_item(kodi_id, media):
    """The item id behind an owned native row, or None for anything else."""
    kind = MEDIA.get(media) or CONTAINERS.get(media)
    if kind is None:
        return None
    return _mapped(current_store(), kind, kodi_id)


def _details(kind, kodi_id, properties):
    """Kodi's row, or None when it has no such row."""
    table = KINDS[kind]
    try:
        return rpc(
            table.getter, {table.id_param: kodi_id, "properties": properties}
        ).get(table.result_key, {})
    except RuntimeError:
        return None


def _mapped(store, kind, kodi_id):
    if kind in FOUND:
        return _found(store, kind, kodi_id)
    properties = ["file", "uniqueid"] if kind in FILED else ["file"]
    row = _details(kind, kodi_id, properties)
    if row is None:
        # No such row for that media type: not ours, whatever it is.
        return None
    if kind == "Audio":
        # A song has no unique id; the URL it is filed under is its identity.
        location, item_id = paths.parse_item(row.get("file") or "")
        if location is None or location.key != store.namespace or not item_id:
            return None
        placed = store.entry(item_id)
        if placed is None or store.mapping(item_id) is None:
            return None
        if location.library != placed.library or location.folder != placed.parent_id:
            return None
        return item_id
    uid = (row.get("uniqueid") or {}).get("kofin", "")
    prefix = store.namespace + ":"
    if not uid.startswith(prefix):
        return None
    item_id = uid[len(prefix) :]
    placed = store.entry(item_id)
    if placed is None or store.mapping(item_id) is None:
        return None
    if kind == "Series":
        # A show is filed as its folder (Readback.owns).
        filed = (row.get("file") or "").rstrip("/") == paths.show_dir(
            store.namespace, placed.library, item_id
        ).rstrip("/")
    else:
        filed = row.get("file") == paths.playback_url(
            store.namespace, kind, placed.library, item_id, placed.parent_id
        )
    return item_id if filed else None


def _found(store, kind, kodi_id):
    """The item a season, set, album or artist row stands for.

    The pass finds each one's row through what it holds
    (patch.Applier.row_for), and this reads it back the other way: a season
    by its show and number, a set by its title, an album by the directory its
    songs are filed in, an artist by name. Where Kodi keeps one row for
    several of ours, the first by id is the one the row stands for, as the
    pass's owner of its art is.
    """
    if kind == "MusicAlbum":
        try:
            songs = rpc(
                "AudioLibrary.GetSongs",
                {"filter": {"albumid": kodi_id}, "properties": ["file"]},
            ).get("songs", [])
        except RuntimeError:
            return None
        folders = set()
        for song in songs:
            location, _ = paths.parse_item(song.get("file") or "")
            if location is not None and location.key == store.namespace:
                folders.add(location.folder)
        # A song's directory is its album's id, or singles/<artist id>.
        for folder in sorted(f for f in folders if f):
            placed = store.entry(folder)
            if placed is not None and placed.kind == kind:
                return folder
        return None
    if kind == "Season":
        row = _details(kind, kodi_id, ["season", "tvshowid"])
        show = _mapped(store, "Series", row.get("tvshowid")) if row else None
        if show is None:
            return None
        numbered = sorted(
            item_id
            for item_id, record in store.records(kind=kind, parent_id=show).items()
            if record.item.get("IndexNumber") is not None
            and int(record.item["IndexNumber"]) == row.get("season")
        )
        return numbered[0] if numbered else None
    if kind == "BoxSet":
        row = _details(kind, kodi_id, ["title"])
        if not row:
            return None
        named = sorted(
            item_id
            for item_id, record in store.records(kind=kind).items()
            if (record.item.get("Name") or "").strip(ASCII_SPACE) == row.get("title")
        )
        if len(named) > 1:
            # Kodi keeps one set per title: of our collections sharing it,
            # the one the pass acknowledged against this row.
            named = [
                item_id
                for item_id in named
                if getattr(store.mapping(item_id), "kodi_id", None) == kodi_id
            ]
        return named[0] if named else None
    # An artist: Kodi keeps one per name, and every artist of ours by that
    # name is acknowledged against it, owner or not.
    row = _details(kind, kodi_id, [])
    if not row:
        return None
    name = artist_key(row.get("artist") or row.get("label"))
    candidates = store.mapped_to(kind, kodi_id)
    live = store.known(candidates)
    named = sorted(
        item_id
        for item_id, item in store.payloads(candidates).items()
        if item_id in live and artist_key(item.get("Name")) == name
    )
    return named[0] if named else None


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
    readback = Readback(store.namespace)
    if kind == "Audio":
        # One directory, not the library: a lookup must not page 22,000 rows.
        found = readback.directory(placed.library, placed.parent_id)
    else:
        found = readback.scope(kind, placed.library, placed.parent_id)
    row = found.get(item_id)
    return row[KINDS[kind].id_param] if row else None


def library_url(item_id):
    """The resolver URL of an imported item, or None when it has no row."""
    store = current_store()
    mapping = store.mapping(item_id)
    placed = store.entry(item_id)
    if not mapping or mapping.kodi_id is None or placed is None:
        return None
    if placed.kind in ("MusicAlbum", "MusicArtist"):
        return None
    container = ""
    if placed.kind == "Audio":
        container = paths.container_of(store.item(item_id) or {})
    return paths.playback_url(
        store.namespace,
        placed.kind,
        placed.library,
        item_id,
        placed.parent_id,
        container,
    )
