"""Applying tombstones: a library whole, or one row at a time, every removal
confirmed before the store lets go of it.

The kind table says how each kind leaves. A row kind has its own remove call
and one readback per scope confirms it. A parent kind -- a season, or an
episode whose show is going too -- leaves with the show, and is acknowledged
only once the show's removal is confirmed. A members kind -- a set -- leaves
once the movies that filed it have been patched off it and its empty row has
been removed by its id; that happens after the apply pass, in
``finish_sets``. A rescan kind -- a song, and the album and artist Kodi
derives from it -- leaves through the complete listing of its directory: the
audio API has no remove call, so the directory is scanned without the song
and one readback of the directory confirms it; the derived kinds are let go
with their songs, since Kodi's own cleanup drops their empty rows. Nothing is
forgotten on the strength of an accepted call.
"""

import threading
import time

from kofin.core import kodirpc
from kofin.core.log import Logger
from kofin.sync import private
from . import paths, progress
from .kinds import KINDS, rpc
from .store import Entry

LOG = Logger(__name__)


def _fail(store, pending, item_id, error, errors):
    store.failed(item_id, pending[item_id][0], type(error).__name__ + ": " + str(error))
    errors.append(error)


def other_sources(native, library, members, placement) -> list[str]:
    """The video sources whose shows Kodi filed on one row with a show of the
    library about to be cleared.

    Kodi files a show from any source onto the row it already holds for
    that show (GetMatchingTvShow), and the clear deletes the row with every
    episode filed on it, theirs included. It forgets the folders' listings
    as it does (DeleteTvShow), so a scan of the source files their show
    again, watched states and all, from its own folder. A source is found by
    the episodes it filed on our shows' rows; a show of theirs with no
    episode comes back with Kodi's next library update.
    """
    rows = [
        native.readback.show(library, item_id)
        for item_id in members
        if placement[item_id].kind == "Series"
    ]
    shows = {row["tvshowid"] for row in rows if row and "tvshowid" in row}
    found = sources_of(native, shows)
    if found:
        LOG.debug(
            "library %s: %d source(s) to scan after the clear", library, len(found)
        )
    return found


def sources_of(native, shows) -> list[str]:
    """The video sources holding episodes another source filed on the rows
    ``shows`` (Kodi's tvshowids)."""
    files = [
        episode["file"]
        for tvshowid in sorted(shows)
        for episode in native.readback.foreign_episodes(tvshowid)
        if episode.get("file")
    ]
    return sources_holding(files)


def sources_holding(files) -> list[str]:
    """The video sources the files are in, the deepest one for each."""
    if not files:
        return []
    sources = [
        str(source["file"])
        for source in (rpc("Files.GetSources", {"media": "video"}) or {}).get("sources")
        or []
        if source.get("file")
    ]
    found: set[str] = set()
    unmatched = 0
    for path in files:
        within = [source for source in sources if path.startswith(source)]
        if within:
            found.add(max(within, key=len))
        else:
            unmatched += 1
    if unmatched:
        LOG.debug(
            "%d episode(s) of other sources on shows leaving, in no source: back"
            " with Kodi's next library update",
            unmatched,
        )
    return sorted(found)


def grouped_movies(native, library, members, placement) -> tuple[list[int], list[str]]:
    """The movies of the library Kodi grouped with another source's copy and
    lists under its file (Readback.movie), and those files."""
    ids: list[int] = []
    files: list[str] = []
    for item_id in sorted(members):
        if placement[item_id].kind != "Movie":
            continue
        row = native.readback.movie(library, item_id)
        if row is None or native.readback.own_file(library, item_id, row):
            continue
        ids.append(row["movieid"])
        files.append(row.get("file") or "")
    return ids, files


def _merged_shows(store, pending, placement, members) -> list[str]:
    """The shows of other libraries that Kodi may have filed on one row
    with a show of the library just cleared, and their seasons and episodes.

    Kodi files a show onto an existing row with the same default unique id,
    or the same title and premiere date (GetMatchingTvShow), whatever
    library either came from, so a copy in another library may have gone
    with this one and has to be read again. A show sharing a name or any
    provider id with one that left is every show Kodi could have merged.
    """
    names: set[str] = set()
    provider_ids: set[tuple[str, str]] = set()
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


def _provider_ids(item) -> set[tuple[str, str]]:
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

    The call returns when the last row is gone, which can take minutes, so
    it is made on a thread of its own while this one moves the bar. Kodi may
    remove the rows in one transaction, and then lists every one of them
    until it commits; it announces each row as it goes all the same
    (``Monitor.removed``), and those announcements reach this thread while
    it waits. The bar takes whichever of the two counts is further on. Its
    waits are timed, to show a stall of the ``OnRemove`` callbacks.
    """
    root = paths.library_root(native.key, library)
    heading = removing(library)
    saved = native.progress.phase(rows, heading)
    failure: list[BaseException] = []

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
        except BaseException as error:  # handed to the caller, below
            failure.append(error)

    try:
        try:
            before = _rows(kinds) if rows else 0
        except Exception:
            # The count is the bar's alone. A listing Kodi refuses here must
            # not be what keeps the library from being cleared.
            before = rows = 0
        announced = native.monitor.removed
        shown = 0
        worker = threading.Thread(target=clear, name="kofin-library-clear")
        worker.daemon = True
        began = time.monotonic()
        worker.start()
        longest = 0.0
        while worker.is_alive():
            asked = time.monotonic()
            aborted = native.abort() or native.monitor.waitForAbort(CLEAR_POLL)
            longest = max(longest, time.monotonic() - asked)
            if aborted:
                # The call cannot be taken back, and it is this thread's:
                # reported stopped while Kodi is still clearing, the thread
                # would let the next service generation issue the same
                # clear beside it.
                worker.join()
                raise InterruptedError("native sync stopped")
            if rows and worker.is_alive():
                # Neither count goes back, and a reading Kodi missed leaves
                # the bar to the announcements.
                gone = max(shown, native.monitor.removed - announced)
                try:
                    gone = max(gone, before - _rows(kinds))
                except Exception:
                    if gone == shown:
                        continue  # a busy Kodi misses a reading, not the clear
                shown = min(gone, rows)
                native.progress.at(shown, "%s %d / %d" % (heading, shown, rows))
        worker.join()
        LOG.debug(
            "library %s cleared in %.1f s; the longest %.0f s wait took %.1f s",
            library,
            time.monotonic() - began,
            CLEAR_POLL,
            longest,
        )
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


def _owed(library, what, call):
    """Do something a library is owed once its rows are gone, and never
    fail its removal for it: failed, the library would be cleared a second
    time on the next pass."""
    try:
        call()
    except InterruptedError:
        raise
    except Exception as error:
        LOG.warning("library %s: left without %s: %s", library, what, error)


def remove(native, pending, entries, tombstones, errors) -> set[str]:
    """Apply every tombstone but the sets'; returns the libraries cleared whole."""
    store = native.store
    removals = {i for i, (_, op, _) in pending.items() if op == "remove"}
    if not removals:
        return set()
    placement: dict[str, Entry] = dict(entries)
    placement.update(tombstones)
    by_library: dict[str, set[str]] = {}
    for item_id, entry in placement.items():
        by_library.setdefault(entry.library, set()).add(item_id)

    # 1. Whole video libraries. A library none of whose members survive goes
    #    in one call; Kodi's RemoveContentForPath deletes every row filed
    #    beneath its root. Music has no such call and is handled below.
    cleared: set[str] = set()
    for library, members in sorted(by_library.items()):
        members = {i for i in members if KINDS[placement[i].kind].removal != "rescan"}
        if not library or not members or not members <= removals:
            continue
        kinds = {placement[i].kind for i in members}
        others: list[str] = []
        grouped: list[int] = []
        try:
            if kinds & {"Series", "Episode"}:
                others = other_sources(native, library, members, placement)
            if "Movie" in kinds:
                grouped, files = grouped_movies(native, library, members, placement)
                others = sorted(set(others) | set(sources_holding(files)))
        except InterruptedError:
            raise
        except Exception as error:
            LOG.warning(
                "library %s: rows shared with other sources not read: %s",
                library,
                error,
            )
        try:
            began = time.monotonic()
            # The clear finds a movie by the file its row lists, which for a
            # movie grouped with another source's copy is theirs: each goes by
            # its id, their copy with it, and their source files it again.
            for movieid in grouped:
                rpc("VideoLibrary.RemoveMovie", {"movieid": movieid})
            # A Kodi carrying kontell/xbmc #3 deletes the sets the removal
            # empties, in the same transaction.
            clear_library(
                native,
                library,
                kinds,
                rows=sum(1 for i in members if placement[i].kind in COUNTED),
            )
            LOG.debug(
                "library %s: %d item(s) cleared from Kodi in %.1f s",
                library,
                len(members),
                time.monotonic() - began,
            )
        except InterruptedError:
            raise
        except Exception as error:
            for item_id in members:
                _fail(store, pending, item_id, error, errors)
            continue
        _owed(library, "its event", native.library_event)
        if others:
            _owed(
                library,
                "the other sources' shows",
                lambda others=others: native.scan(others, "video"),
            )
        # One transaction for the whole library.
        store.forget_many([(item_id, pending[item_id][0]) for item_id in members])
        cleared.add(library)
        if kinds & {"Series", "Episode"}:
            store.invalidate(_merged_shows(store, pending, placement, members))

    # 2. Music: every song tombstone is its album directory listed without
    #    it. The directories are scanned and one readback per directory
    #    confirms the songs are gone; albums and artists are derived and
    #    leave with their songs.
    remove_music(native, pending, placement, removals, errors)

    # 3. Rows. Children of a show that is going leave with it and are
    #    acknowledged only when the show's removal is confirmed, so they are
    #    gathered first, whatever order the ids sort in.
    children: dict[str, list[str]] = {}
    rows: list[tuple[str, Entry]] = []
    settled: list[tuple[str, int]] = []
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

    attempted: list[tuple[str, Entry]] = []
    # The other sources of the shows about to go, scanned once they have.
    rescan: set[str] = set()
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
            row = _row(native, placed, item_id)
            if row is None:
                forget_with_children(item_id)
                continue
            try:
                if placed.kind == "Series":
                    rescan.update(sources_of(native, {row["tvshowid"]}))
                elif placed.kind == "Movie" and not native.readback.own_file(
                    placed.library, item_id, row
                ):
                    rescan.update(sources_holding([row.get("file") or ""]))
            except InterruptedError:
                raise
            except Exception as error:
                LOG.warning("%s: other sources not read: %s", item_id, error)
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
            survived = False
        else:
            survived = _row(native, placed, item_id) is not None
        if survived or (
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
    if rescan:
        _owed(
            "", "the other sources' shows", lambda: native.scan(sorted(rescan), "video")
        )
    return cleared


def remove_music(native, pending, placement, removals, errors):
    store = native.store
    by_library: dict[str, list[tuple[str, Entry]]] = {}
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


def set_members(pending) -> set[str]:
    """Movies that filed a set now being removed; they need re-patching."""
    members: set[str] = set()
    for _, operation, payload in pending.values():
        if operation == "remove" and payload.get("Type") == "BoxSet":
            members.update(payload.get("KofinMembers") or [])
    return members


def finish_sets(native, pending, live_boxsets, errors):
    """Acknowledge set removals once no owned movie files them any more.

    Runs after the apply pass, which re-patched the former members. A title
    still listed by Kodi while no live collection of ours carries it means a
    member has not let go yet, so the removal stays pending. Once the title
    is gone from the listing, the empty set row Kodi keeps is removed by its
    id (``_remove_empty_set``), and only then is the store told.
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
    kept = {(b.get("Name") or "").strip(paths.ASCII_SPACE) for b in live_boxsets}
    for item_id, payload in waiting:
        title = (payload.get("Name") or "").strip(paths.ASCII_SPACE)
        if title in listed and title not in kept and not _ours_in_set(native, title):
            # Only another source's movies file the set: ours have left it,
            # and the set is theirs.
            LOG.debug(
                "collection %s: its set is left to another source's movies", item_id
            )
            store.forget(item_id, pending[item_id][0])
            continue
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
            if title not in kept:
                _remove_empty_set(native.store, item_id, listed, native.features)
        except InterruptedError:
            raise
        except Exception as error:
            _fail(store, pending, item_id, error, errors)
            continue
        store.forget(item_id, pending[item_id][0])


def _ours_in_set(native, title) -> bool:
    """Whether a movie Kofin filed is in the set named ``title``."""
    reply = rpc(
        "VideoLibrary.GetMovies",
        {
            "filter": {"field": "set", "operator": "is", "value": title},
            "properties": ["file"],
        },
    )
    for row in (reply or {}).get("movies") or []:
        location = paths.parse(row.get("file") or "")
        if location is not None and location.key == native.key:
            return True
    return False


def _row(native, placed, item_id):
    """The row an item to remove has now: a show's through its episodes as
    well (Readback.show), since another source's folder may list it."""
    if placed.kind == "Series":
        return native.readback.show(placed.library, item_id)
    if placed.kind == "Movie":
        return native.readback.movie(placed.library, item_id)
    return native.readback.scope(placed.kind, placed.library, placed.parent_id).get(
        item_id
    )


def _remove_empty_set(store, item_id, listed, features=frozenset()):
    """Remove the empty row Kodi keeps for a set, by the id the collection
    was acknowledged under.

    Only with the remove_set feature (``VideoLibrary.RemoveMovieSet``);
    without it the empty row stays for Kodi's own Clean library.

    An emptied set is in no listing: GetMovieSets and GetMovieSetDetails
    both read sets through the movies that file them (GetSetsByWhere), so
    the row cannot be read back to confirm it is ours. It is removed only
    while this pass's set readback (``listed``) shows no set with movies
    holding the id, so a set of anyone's with a movie is never touched,
    even after Kodi gave the id to a new set (``sets.idSet`` is a plain
    rowid). A row already gone, or never made (no movie of the collection
    was filed), leaves nothing to do."""
    if "remove_set" not in features:
        return
    mapping = store.mapping(item_id)
    if mapping is None or not mapping.kodi_id:
        return
    setid = int(mapping.kodi_id)
    if any(row.get("setid") == setid for row in listed.values()):
        return
    # Refused (None) when the row is gone already: nothing is left to
    # remove. FAILED is a Kodi that could not be asked, and the collection
    # stays pending for the next pass rather than being forgotten.
    if kodirpc.call("VideoLibrary.RemoveMovieSet", {"setid": setid}) is kodirpc.FAILED:
        raise RuntimeError("Kodi could not be asked to remove set %d" % setid)
