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

import threading
import time
from typing import Any, Dict, List, Set, Tuple

from kofin.core.log import Logger
from kofin.sync import private
from . import metadata, paths, progress
from . import companion
from .kinds import KINDS, rpc
from .store import Entry

LOG = Logger(__name__)


def _fail(store, pending, item_id, error, errors):
    store.failed(item_id, pending[item_id][0], type(error).__name__ + ": " + str(error))
    errors.append(error)


def _merged_shows(store, pending, placement, members) -> List[str]:
    """The shows of other libraries that Kodi may have filed on one row
    with a show of the library just cleared, and their seasons and episodes.

    Kodi files a show onto an existing row with the same default unique id,
    or the same title and premiere date (GetMatchingTvShow), whatever
    library either came from, so a copy in another library may have gone
    with this one and has to be read again. Every show of every other
    library used to be: removing a library of one show re-read 79 others
    and their 4,316 episodes, two minutes on a desktop. A show sharing a
    name or any provider id with one that left is every show Kodi could
    have merged, and in most libraries that is none.
    """
    names: Set[str] = set()
    provider_ids: Set[Tuple[str, str]] = set()
    for item_id in members:
        if placement[item_id].kind != "Series":
            continue
        payload = pending[item_id][2] or {}
        names.add((payload.get("Name") or "").strip())
        provider_ids.update(_provider_ids(payload))
    names.discard("")
    if not names and not provider_ids:
        return []
    gone = set(members)
    merged = [
        record.item_id
        for record in store.records(kind="Series", pinned=False).values()
        if record.item_id not in gone
        and (
            (record.item.get("Name") or "").strip() in names
            or provider_ids & _provider_ids(record.item)
        )
    ]
    for series_id in list(merged):
        merged.extend(
            store.records(kind=("Season", "Episode"), parent_id=series_id, pinned=False)
        )
    return merged


def _provider_ids(item) -> Set[Tuple[str, str]]:
    return {
        (str(name).lower(), str(value))
        for name, value in (item.get("ProviderIds") or {}).items()
        if value
    }


# The kinds whose rows Kodi's listings can count while a library is cleared.
COUNTED = ("Movie", "Episode", "MusicVideo")
# Seconds between reads of Kodi's row counts during a clear.
CLEAR_POLL = 3.0


def library_label(library) -> str:
    """The library's name as the viewer selected it, or ''."""
    try:
        with private.Database() as db:
            row = db.cursor.execute(
                "SELECT view_name FROM view WHERE view_id=?", (library,)
            ).fetchone()
    except Exception:
        return ""
    return str(row[0]) if row and row[0] else ""


def removing(library) -> str:
    """The bar's text while a library leaves."""
    name = library_label(library)
    heading = progress.stage(progress.REMOVING)
    return "%s: %s" % (heading, name) if name else heading


def _rows(kinds) -> int:
    return sum(
        rpc(KINDS[kind].listing, {"limits": {"start": 0, "end": 1}})["limits"]["total"]
        for kind in COUNTED
        if kind in kinds
    )


def clear_library(native, library, kinds, rows=0):
    """One call removes every row Kodi filed under the library root.

    The call returns when the last row is gone: three minutes for 1,787
    movies on a desktop, a quarter of an hour on a Raspberry Pi 3, and
    until this said so the screen showed nothing in that time. It is made
    on a thread of its own, and this one reads how many of the library's
    ``rows`` Kodi still lists and moves the bar.
    """
    root = paths.library_root(native.key, library)
    heading = removing(library)
    saved = native.progress.phase(rows, heading)
    failure: List[BaseException] = []

    def clear():
        try:
            rpc(
                "VideoLibrary.SetSourceContent",
                {
                    "path": root,
                    "content": "none",
                    "clearmode": "remove",
                    "refresh": False,
                },
            )
        except BaseException as error:  # noqa: BLE001 - handed to the caller
            failure.append(error)

    try:
        try:
            before = _rows(kinds) if rows else 0
        except Exception:
            # The count is the bar's alone. A listing Kodi refuses here must
            # not be what keeps the library from being cleared.
            before = rows = 0
        worker = threading.Thread(target=clear, name="kofin-library-clear")
        worker.daemon = True
        worker.start()
        while worker.is_alive():
            if native.abort() or native.monitor.waitForAbort(CLEAR_POLL):
                # The call cannot be taken back, and it is this thread's:
                # reported stopped while Kodi is still clearing, the thread
                # would let the next service generation issue the same
                # clear beside it. It waits as it did when it made the call
                # itself, and no longer paints.
                worker.join()
                raise InterruptedError("native sync stopped")
            if rows and worker.is_alive():
                try:
                    gone = before - _rows(kinds)
                except Exception:
                    continue  # a busy Kodi misses a reading, not the clear
                native.progress.at(
                    gone, "%s %d / %d" % (heading, max(0, min(gone, rows)), rows)
                )
        worker.join()
    finally:
        native.progress.restore(saved)
    if failure:
        raise failure[0]
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
            began = time.monotonic()
            clear_library(
                native,
                library,
                kinds,
                rows=sum(1 for i in members if placement[i].kind in COUNTED),
            )
            LOG.info(
                "library %s: %d item(s) cleared from Kodi in %.1f s",
                library,
                len(members),
                time.monotonic() - began,
            )
            if "Movie" in kinds:
                # No public call removes a set and no mapping can be trusted
                # to say which movie had one. Kodi's own clean drops the
                # empty sets; scoped to a root with no files left, it is
                # cheap next to a dangling set, and its end is announced to
                # the home widgets like a scan's.
                rpc(
                    "VideoLibrary.Clean",
                    {
                        "showdialogs": False,
                        "directory": paths.library_root(native.key, library),
                    },
                )
            else:
                native.library_event()
        except InterruptedError:
            raise
        except Exception as error:
            for item_id in members:
                _fail(store, pending, item_id, error, errors)
            continue
        # One transaction: let go of one at a time, a library of 6,000 items
        # was 6,000 commits, each collecting the catalogue's closed intervals.
        store.forget_many([(item_id, pending[item_id][0]) for item_id in members])
        cleared.add(library)
        if kinds & {"Series", "Episode"}:
            store.invalidate(_merged_shows(store, pending, placement, members))

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
    settled: List[Tuple[str, int]] = []
    for item_id in sorted(removals):
        placed = placement.get(item_id)
        if placed is None or placed.library in cleared:
            settled.append((item_id, pending[item_id][0]))
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
    store.forget_many(settled)

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
                # Version files Kodi imported as movies of their own, which
                # the user has not grouped yet, leave with the movie.
                for version in native.readback.version_rows(placed.library, item_id):
                    rpc(table.remove, {table.id_param: version[table.id_param]})
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
        if item_id in survivors or (
            placed.kind == "Movie"
            and native.readback.version_rows(placed.library, item_id)
        ):
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
            if native.progress.attended:
                # A viewer took a library out: the scans that empty its
                # directories count nothing the bar could show.
                native.progress.note(removing(library))
            native.music.scan(library, folders)
            if (library, "*") in native.music.rescanned:
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
    # The companion deletes the one empty row by title (companion.REMOVE_SET);
    # without it the clean of the root is what drops it, at the cost of a
    # check_exists per file under the root.
    delegated = companion.present() is not None
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
            if title not in kept and delegated:
                companion.journal_set(title)
            elif not cleaned and title not in kept:
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
