"""Applying tombstones: a library whole, or one row at a time, every removal
confirmed before the store lets go of it.

The kind table says how each kind leaves. A row kind has its own remove call
and one readback per scope confirms it. A parent kind -- a season, or an
episode whose show is going too -- leaves with the show, and is acknowledged
only once the show's removal is confirmed. A members kind -- a set -- leaves
once the movies that filed it have been patched off it and Kodi's clean has
dropped the empty row; that happens after the apply pass, in
``finish_sets``. A rescan kind -- a song, and the album and artist Kodi
derives from it -- leaves through the complete listing of its directory: the
audio API has no remove call, so the directory is scanned without the song
and one readback of the directory confirms it; the derived kinds are let go
with their songs, since Kodi's own cleanup drops their empty rows. Nothing is
forgotten on the strength of an accepted call.
"""

from typing import Any, Dict, List, Set, Tuple

import xbmc

from kofin.core.log import Logger
from . import metadata, paths
from .kinds import KINDS, rpc
from .store import Entry

LOG = Logger(__name__)


def _fail(store, pending, item_id, error, errors):
    store.failed(item_id, pending[item_id][0], type(error).__name__ + ": " + str(error))
    errors.append(error)


def clear_library(native, library, kinds):
    """One call removes every row Kodi filed under the library root."""
    root = paths.library_root(native.key, library)
    rpc(
        "VideoLibrary.SetSourceContent",
        {"path": root, "content": "none", "clearmode": "remove", "refresh": False},
    )
    native.readback.clear()
    left = []
    if "Movie" in kinds:
        left += list(native.readback.scope("Movie", library))
    if "MusicVideo" in kinds:
        left += list(native.readback.scope("MusicVideo", library))
    if kinds & {"Series", "Season", "Episode"}:
        left += list(native.readback.scope("Series", library))
    if left:
        raise RuntimeError("native clear left %d owned rows" % len(left))
    native.store.unbind(root)


def remove(native, pending, entries, tombstones, errors) -> Set[str]:
    """Apply every tombstone but the sets'; returns the libraries cleared whole."""
    store = native.store
    removals = {i for i, (_, op, _) in pending.items() if op == "remove"}
    if not removals:
        return set()
    placement: Dict[str, Entry] = dict(entries)
    placement.update(tombstones)
    by_library: Dict[str, Set[str]] = {}
    for item_id, entry in placement.items():
        by_library.setdefault(entry.library, set()).add(item_id)

    # 1. Whole video libraries. A library none of whose members survive goes
    #    in one call; Kodi's RemoveContentForPath deletes every row filed
    #    beneath its root. Music has no such call and is handled below.
    cleared: Set[str] = set()
    for library, members in sorted(by_library.items()):
        members = {i for i in members if KINDS[placement[i].kind].removal != "rescan"}
        if not library or not members or not members <= removals:
            continue
        kinds = {placement[i].kind for i in members}
        try:
            clear_library(native, library, kinds)
            # SetSourceContent announces nothing where the row removers
            # notify per row; the root is unbound, so this scan walks nothing
            # and only fires the library event the home widgets listen for.
            xbmc.executebuiltin("UpdateLibrary(video)")
            if "Movie" in kinds:
                # No public call removes a set and no mapping can be trusted
                # to say which movie had one. Kodi's own clean drops the
                # empty sets; scoped to a root with no files left, it is
                # cheap next to a dangling set.
                rpc(
                    "VideoLibrary.Clean",
                    {
                        "showdialogs": False,
                        "directory": paths.library_root(native.key, library),
                    },
                )
        except InterruptedError:
            raise
        except Exception as error:
            for item_id in members:
                _fail(store, pending, item_id, error, errors)
            continue
        for item_id in members:
            store.forget(item_id, pending[item_id][0])
        cleared.add(library)
        if kinds & {"Series", "Episode"}:
            # Kodi merges shows with one title and premiere across libraries
            # (GetMatchingTvShow), so another library's copy may have gone
            # with this one: have the others re-read.
            store.invalidate_where(
                ("Series", "Season", "Episode"), except_library=library
            )

    # 2. Music: every song tombstone is its album directory listed without
    #    it. The directories are scanned (the root once, when there are
    #    many) and one readback per directory confirms the songs are gone;
    #    albums and artists are derived and leave with their songs.
    remove_music(native, pending, placement, removals, errors)

    # 3. Rows. Children of a show that is going leave with it and are
    #    acknowledged only when the show's removal is confirmed, so they are
    #    gathered first, whatever order the ids sort in.
    children: Dict[str, List[str]] = {}
    rows: List[Tuple[str, Entry]] = []
    for item_id in sorted(removals):
        placed = placement.get(item_id)
        if placed is None or placed.library in cleared:
            store.forget(item_id, pending[item_id][0])
            continue
        table = KINDS[placed.kind]
        if table.removal in ("members", "rescan"):
            # Sets wait for the apply pass (finish_sets below); music was
            # settled above.
            continue
        if placed.parent_id in removals:
            children.setdefault(placed.parent_id, []).append(item_id)
            continue
        if table.removal == "parent" or table.remove is None:
            # A season of a surviving show has no removal call; its row leaves
            # with its episodes, or stays as Kodi's own empty season.
            store.forget(item_id, pending[item_id][0])
            continue
        rows.append((item_id, placed))

    def forget_with_children(item_id):
        store.forget(item_id, pending[item_id][0])
        for child in children.pop(item_id, []):
            store.forget(child, pending[child][0])

    def fail_with_children(item_id, error):
        _fail(store, pending, item_id, error, errors)
        for child in children.pop(item_id, []):
            _fail(store, pending, child, error, errors)

    attempted: List[Tuple[str, Entry]] = []
    for item_id, placed in rows:
        table = KINDS[placed.kind]
        if table.remove is None:
            continue
        try:
            if native.abort():
                raise InterruptedError("native sync stopped")
            # The scoped readback is the source of truth, not the stored
            # mapping: Kodi may have deleted that row already -- a Clean
            # answered False for this very tombstone -- or reissued its id
            # to a foreign row, since the video tables' ids are plain
            # INTEGER PRIMARY KEYs. A row the scope owns is removed by the
            # id it has now; an item absent from the scope is already gone,
            # and so are a vanished show's episodes.
            row = native.readback.scope(
                placed.kind, placed.library, placed.parent_id
            ).get(item_id)
            if row is None:
                forget_with_children(item_id)
                continue
            rpc(table.remove, {table.id_param: row[table.id_param]})
            if placed.kind == "Series":
                native.unbind_show(placed.library, item_id)
            elif placed.kind == "Movie":
                native.unbind_movie(placed.library, item_id)
            attempted.append((item_id, placed))
        except InterruptedError:
            raise
        except Exception as error:
            fail_with_children(item_id, error)

    # 4. Confirm: one readback per scope. Anything still there stays pending,
    #    children included.
    if attempted:
        native.readback.clear()
    removed_shows = {i for i, p in attempted if p.kind == "Series"}
    for item_id, placed in attempted:
        if placed.kind == "Episode" and placed.parent_id in removed_shows:
            survivors: Dict[Any, Any] = {}
        else:
            survivors = native.readback.scope(
                placed.kind, placed.library, placed.parent_id
            )
        if item_id in survivors:
            fail_with_children(
                item_id, RuntimeError("%s removal not confirmed" % placed.kind)
            )
            continue
        forget_with_children(item_id)
    for parent, orphans in children.items():
        # The parent was settled before its children were gathered, or its
        # removal never got as far as a call this pass.
        state = store.state(parent)
        gone = (
            state is not None
            and state.operation == "remove"
            and state.status == "applied"
        )
        for child in orphans:
            if gone:
                store.forget(child, pending[child][0])
            else:
                _fail(
                    store,
                    pending,
                    child,
                    RuntimeError("show removal not confirmed"),
                    errors,
                )
    return cleared


def remove_music(native, pending, placement, removals, errors):
    store = native.store
    by_library: Dict[str, List[Tuple[str, Entry]]] = {}
    for item_id in sorted(removals):
        placed = placement.get(item_id)
        if placed is None or KINDS[placed.kind].removal != "rescan":
            continue
        by_library.setdefault(placed.library, []).append((item_id, placed))
    for library, members in sorted(by_library.items()):
        folders = {p.parent_id for _, p in members if p.kind == "Audio" and p.parent_id}
        try:
            if native.abort():
                raise InterruptedError("native sync stopped")
            native.scan_music(library, folders)
            if (library, "*") in native.rescanned:
                survivors = native.readback.folders(library)
            else:
                survivors = {
                    folder: native.readback.directory(library, folder)
                    for folder in folders
                }
        except InterruptedError:
            raise
        except Exception as error:
            for item_id, _ in members:
                _fail(store, pending, item_id, error, errors)
            continue
        confirmed = []
        for item_id, placed in members:
            if placed.kind == "Audio" and item_id in survivors.get(
                placed.parent_id, {}
            ):
                _fail(
                    store,
                    pending,
                    item_id,
                    RuntimeError("song removal not confirmed"),
                    errors,
                )
                continue
            confirmed.append((item_id, pending[item_id][0]))
        store.forget_many(confirmed)


def set_members(pending) -> Set[str]:
    """Movies that filed a set now being removed; they need re-patching."""
    members: Set[str] = set()
    for _, operation, payload in pending.values():
        if operation == "remove" and payload.get("Type") == "BoxSet":
            members.update(payload.get("KofinMembers") or [])
    return members


def finish_sets(native, pending, live_boxsets, errors):
    """Acknowledge set removals once no owned movie files them any more.

    Runs after the apply pass, which re-patched the former members. A title
    still listed by Kodi while no live collection of ours carries it means a
    member has not let go yet, so the removal stays pending. Once the title
    is gone from the listing, one clean of the namespace root drops the
    empty set row Kodi keeps, and only then is the store told.
    """
    store = native.store
    waiting = [
        (item_id, payload)
        for item_id, (_, operation, payload) in pending.items()
        if operation == "remove" and payload.get("Type") == "BoxSet"
    ]
    if not waiting:
        return
    native.readback.forget("BoxSet")
    listed = native.readback.scope("BoxSet")
    kept = {(b.get("Name") or "").strip(metadata.ASCII_SPACE) for b in live_boxsets}
    cleaned = False
    for item_id, payload in waiting:
        title = (payload.get("Name") or "").strip(metadata.ASCII_SPACE)
        if title in listed and title not in kept:
            _fail(
                store,
                pending,
                item_id,
                RuntimeError("collection still has a member filed"),
                errors,
            )
            continue
        try:
            if not cleaned and title not in kept:
                rpc(
                    "VideoLibrary.Clean",
                    {"showdialogs": False, "directory": paths.root(native.key)},
                )
                cleaned = True
        except InterruptedError:
            raise
        except Exception as error:
            _fail(store, pending, item_id, error, errors)
            continue
        store.forget(item_id, pending[item_id][0])
