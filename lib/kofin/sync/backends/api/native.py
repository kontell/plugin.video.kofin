"""Confirmed native operations for every kind through stock Piers public
interfaces.

An accepted RPC is never a commit: every write is confirmed by reading the
owned row back (``readback``), removals wait for that confirmation
(``removal``) and patches go only where the scanner's row differs
(``patch``). This module owns the gates, the waits, the bindings and the
scans, and runs one pass in ``reconcile``.

Music is scanned by directory. A song is missing, or its tags have moved,
or it has moved album: each means its album directory is listed again, and
a pass with many such directories walks the library's music root once
instead -- Kodi lists every album folder either way, and one scan job costs
less than hundreds.
"""

import time
from typing import Dict, List, Optional, Set, Tuple

import xbmc

from kofin.core.log import Logger
from kofin.sync.catalogue import BackendMismatch
from . import metadata, paths, removal
from .kinds import rpc
from .patch import Applier
from .readback import Readback
from .store import Entry, Record, Store

LOG = Logger(__name__)

# A pass walks the library's music root once instead of scanning changed
# album directories by name when at least this share of the directories
# changed. Measured on the P1D: a directory scan is 60-80 ms, and a root walk
# re-lists every unchanged directory at about 100 ms each because Kodi
# re-creates the Python interpreter when it has no database work between two
# listings -- so the walk pays only when most directories have work.
ROOT_SCAN_SHARE = 0.5


class Monitor(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        self.finished = 0
        self.music_finished = 0

    def onScanFinished(self, library):
        if library.lower() == "video":
            self.finished += 1
        elif library.lower() == "music":
            self.music_finished += 1


class Native:
    def __init__(self, store: Store, abort=lambda: False):
        self.store = store
        self.key = store.namespace
        self.abort = abort
        self.monitor = Monitor()
        self.separator = metadata.item_separator()
        self.readback = Readback(self.key)
        self._async_pending = False
        self._server = ""
        # (library, folder) pairs whose directory this pass has re-listed,
        # or (library, "*") when the music root was walked.
        self.rescanned: Set[Tuple[str, str]] = set()

    # -- gates ---------------------------------------------------------------

    def setup(self):
        owner = self.store.prepared()
        if owner and owner != self.key:
            raise BackendMismatch(
                "native library belongs to another server/user; use a fresh profile"
            )
        if self.store.legacy_present():
            self.retire_legacy()
        if owner == self.key:
            return
        for kind in (
            "Movies",
            "TVShows",
            "Episodes",
            "MusicVideos",
            "Songs",
            "Albums",
            "Artists",
        ):
            library = (
                "AudioLibrary"
                if kind in ("Songs", "Albums", "Artists")
                else "VideoLibrary"
            )
            reply = rpc(library + ".Get" + kind, {"limits": {"start": 0, "end": 1}})
            if not isinstance(reply, dict) or not isinstance(
                reply.get("limits", {}).get("total"), int
            ):
                raise RuntimeError("cannot verify empty native library")
            if reply["limits"]["total"]:
                raise BackendMismatch(
                    "fresh native library required; choose a clean profile or reset before switching to OR"
                )
        self.store.prepare_empty()

    def retire_legacy(self):
        """Take the 0.90.0 movie rows out of Kodi and the old tables with them.

        The ownership URL changed with the per-library layout, so the old
        rows can never be matched again; the next enumeration re-imports
        every selected library under the new one.
        """
        LOG.info("retiring the 0.90.0 movie layout; selected libraries re-import")
        self.wait(self._idle)
        rpc(
            "VideoLibrary.SetSourceContent",
            {
                "path": paths.root(self.key),
                "content": "none",
                "clearmode": "remove",
                "refresh": False,
            },
        )
        rows = rpc(
            "VideoLibrary.GetMovies",
            {
                "properties": ["uniqueid"],
                "filter": {
                    "field": "path",
                    "operator": "startswith",
                    "value": paths.root(self.key),
                },
            },
        ).get("movies", [])
        if rows:
            raise RuntimeError("legacy clear left %d owned movies" % len(rows))
        xbmc.executebuiltin("UpdateLibrary(video)")
        self.store.retire_legacy()

    # -- waits ---------------------------------------------------------------

    def _idle(self, scanner="video"):
        if scanner == "music":
            return not xbmc.getCondVisibility("Library.IsScanningMusic")
        if scanner == "any":
            return self._idle("video") and self._idle("music")
        return not xbmc.getCondVisibility("Library.IsScanningVideo")

    def _scanning(self, scanner="video"):
        return not self._idle(scanner)

    def wait(self, predicate, timeout=60, busy=lambda: False):
        """``timeout`` is idle time: the deadline moves while ``busy`` holds."""
        deadline = time.monotonic() + timeout
        while True:
            if self.abort() or self.monitor.waitForAbort(0.1):
                raise InterruptedError("native operation interrupted")
            value = predicate()
            if value:
                return value
            if busy():
                deadline = time.monotonic() + timeout
            elif time.monotonic() >= deadline:
                raise TimeoutError("native operation did not converge")

    def server_url(self):
        if not self._server:
            self._server = self.store.server()
        return self._server

    # -- bindings and scans --------------------------------------------------

    def bind(self, library, content):
        path = paths.library_dir(self.key, library, content)
        if self.store.bindings().get(path) == content:
            return
        rpc(
            "VideoLibrary.SetSourceContent",
            {
                "path": path,
                "content": content,
                "scraperid": "metadata.local",
                "scanrecursive": False,
                "refresh": False,
            },
        )
        self.store.bind(path, content)

    def bind_show(self, library, series_id):
        """A show folder needs its own binding: Kodi derives a plugin path's
        parent as the plugin root, so the library binding is never found
        from beneath it (URIUtils::GetParentPath), and a folder without one
        is skipped by the scanner."""
        path = paths.show_dir(self.key, library, series_id)
        if path in self.store.bindings():
            return
        rpc(
            "VideoLibrary.SetSourceContent",
            {
                "path": path,
                "content": "tvshows",
                "scraperid": "metadata.local",
                "containssingleitem": True,
                "refresh": False,
            },
        )
        self.store.bind(path, "tvshows")

    def unbind_show(self, library, series_id):
        path = paths.show_dir(self.key, library, series_id)
        try:
            rpc(
                "VideoLibrary.SetSourceContent",
                {
                    "path": path,
                    "content": "none",
                    "clearmode": "clear",
                    "refresh": False,
                },
            )
        except RuntimeError as error:
            LOG.warning("show binding not cleared: %s", error)
        self.store.unbind(path)

    def scan(self, directories: List[str], scanner="video"):
        """Queue one scan per directory and wait for the scanner to go idle."""
        if not directories:
            return
        self.wait(lambda: self._idle(scanner))

        def finished():
            return (
                self.monitor.music_finished
                if scanner == "music"
                else self.monitor.finished
            )

        serial = finished()
        method = "AudioLibrary.Scan" if scanner == "music" else "VideoLibrary.Scan"
        began = time.monotonic()
        for directory in directories:
            rpc(method, {"directory": directory, "showdialogs": False})
        self._async_pending = True
        try:
            self.wait(
                lambda: finished() >= serial + len(directories) and self._idle(scanner),
                busy=lambda: self._scanning(scanner),
            )
            LOG.info(
                "%s scan of %d director%s took %.1f s",
                scanner,
                len(directories),
                "y" if len(directories) == 1 else "ies",
                time.monotonic() - began,
            )
        except TimeoutError:
            if finished() == serial:
                raise
            LOG.warning(
                "%d of %d scans reported finished; reading back anyway",
                finished() - serial,
                len(directories),
            )
        finally:
            self._async_pending = not self._idle(scanner)
        if scanner == "music":
            for directory in directories:
                location = paths.parse(directory)
                if location is not None and location.library:
                    self.readback.forget_music(location.library)
        else:
            self.readback.clear()

    def scan_music(self, library, folders: Set[str]):
        """List the changed album directories again: each by name, or the
        library's music root once when most of them changed."""
        if not folders:
            return
        total = len(set(self.store.folders("Audio", library)) | folders)
        if len(folders) > total * ROOT_SCAN_SHARE:
            LOG.info(
                "music: %d of %d directories changed; walking the library root",
                len(folders),
                total,
            )
            self.scan([paths.library_dir(self.key, library, "music")], "music")
            self.rescanned.add((library, "*"))
            return
        LOG.info("music: scanning %d of %d directories by name", len(folders), total)
        self.scan(
            [paths.music_dir(self.key, library, folder) for folder in sorted(folders)],
            "music",
        )
        self.rescanned.update((library, folder) for folder in folders)

    # -- the pass ------------------------------------------------------------

    def reconcile(self, repair=False):
        """Replay durable operations; an accepted RPC is never a commit."""
        self.setup()
        # A prior process may have died with its pin held, or left a scan
        # running that outlives it. Wait for Kodi to finish using it before
        # publishing the newer desired view to a scan, however long it runs.
        self.wait(lambda: self._idle("any"), busy=lambda: not self._idle("any"))
        self.store.pin()
        self.readback.clear()
        self.rescanned = set()
        applier = Applier(self)
        completed = False
        try:
            entries = self.store.entries()
            pending: Dict[str, Tuple[int, str, Dict]] = {
                item.item_id: (generation, operation, item.payload)
                for item, generation, operation in self.store.pending()
            }
            if repair:
                for item_id, record in self.store.records().items():
                    if item_id not in pending:
                        pending[item_id] = (record.generation, "upsert", record.item)
            errors: List[Exception] = []
            # A removed collection's movies must let go of its set before the
            # set can; they rejoin this pass as upserts.
            self.store.invalidate(removal.set_members(pending))
            removal.remove(self, pending, entries, self.store.tombstones(), errors)
            # A whole-library clear puts other libraries' shows back to
            # pending, and the set members above are pending now too; both
            # join this pass rather than wait for the next.
            for item, generation, operation in self.store.pending():
                if operation == "upsert" and item.item_id not in pending:
                    pending[item.item_id] = (generation, operation, item.payload)
            upserts: Dict[str, Record] = {}
            for item_id, (generation, operation, payload) in pending.items():
                entry = entries.get(item_id)
                if operation != "upsert" or entry is None:
                    continue
                upserts[item_id] = Record(
                    item_id,
                    entry.kind,
                    entry.library,
                    entry.parent_id,
                    payload,
                    generation,
                )
            self.import_missing(upserts, errors, entries)
            boxsets = [r.item for r in self.store.records(kind="BoxSet").values()]
            collections = metadata.collections_of(boxsets)
            movies = {i for i, e in entries.items() if e.kind == "Movie"}
            applier.run(upserts, collections, movies, repair, errors)
            removal.finish_sets(self, pending, boxsets, errors)
            if errors:
                raise RuntimeError(
                    "%d native operations remain pending: %s" % (len(errors), errors[0])
                )
            completed = True
        finally:
            applier.commit()
            # A timed-out scan must retain its snapshot until Kodi is idle.
            if (completed or not self._async_pending) and self._idle("any"):
                self.store.unpin()

    def import_missing(
        self, upserts: Dict[str, Record], errors, entries: Optional[Dict] = None
    ):
        """Bind and scan whatever the readback shows the scanner has not filed."""
        directories: List[str] = []
        expectations = []
        for library in sorted({r.library for r in upserts.values() if r.library}):
            kinds = {r.kind for r in upserts.values() if r.library == library}
            for content in paths.CONTENTS:
                if not any(paths.CONTENT.get(k) == content for k in kinds):
                    continue
                if content == "music":
                    try:
                        if entries is None:
                            entries = self.store.entries()
                        folders = self._music_directories(upserts, library, entries)
                        self.scan_music(library, folders)
                    except InterruptedError:
                        raise
                    except Exception as error:
                        errors.append(error)
                    continue
                try:
                    self.bind(library, content)
                except InterruptedError:
                    raise
                except Exception as error:
                    errors.append(error)
                    continue
                if content == "tvshows":
                    missing = self._missing_tv(upserts, library)
                    for record in missing:
                        if record.kind == "Series":
                            self.bind_show(library, record.item_id)
                else:
                    kind = "Movie" if content == "movies" else "MusicVideo"
                    present = self.readback.scope(kind, library)
                    missing = [
                        r
                        for r in upserts.values()
                        if r.kind == kind
                        and r.library == library
                        and r.item_id not in present
                    ]
                if not missing:
                    continue
                directories.append(paths.library_dir(self.key, library, content))
                for record in missing:
                    if record.kind in ("Movie", "Episode", "MusicVideo"):
                        expectations.append(
                            (
                                record.item_id,
                                record.generation,
                                metadata.userdata(record.item),
                            )
                        )
        if directories:
            self.store.expect_many(expectations)
            self.scan(directories)

    def _missing_tv(self, upserts, library) -> List[Record]:
        shows = self.readback.scope("Series", library)
        missing = [
            r
            for r in upserts.values()
            if r.kind == "Series" and r.library == library and r.item_id not in shows
        ]
        for record in upserts.values():
            if record.kind != "Episode" or record.library != library:
                continue
            if metadata.episode_numbers(record.item) is None:
                # Never listed, never imported: not a reason to scan.
                continue
            if record.parent_id not in shows:
                missing.append(record)
                continue
            if record.item_id not in self.readback.scope(
                "Episode", library, record.parent_id
            ):
                missing.append(record)
        return missing

    def _music_directories(
        self, upserts: Dict[str, Record], library, entries: Dict[str, Entry]
    ) -> Set[str]:
        """The album directories whose listing Kodi must read again.

        A song with no row is missing; one whose tag hash moved since its
        row was acknowledged needs its tags re-read, which the scanner does
        only for a directory whose listing changed; one that moved album
        leaves its old directory only when that directory is listed without
        it. And a directory holding rows no live song of ours claims -- the
        old half of a move, or a listing that failed half way -- is settled
        by its complete listing too.
        """
        songs = {
            r.item_id: r
            for r in upserts.values()
            if r.kind == "Audio" and r.library == library
        }
        folders: Set[str] = set()
        if songs:
            albums = {
                i: r.item
                for i, r in self.store.records(
                    kind="MusicAlbum", library=library
                ).items()
            }
            mappings = self.store.mappings(kind="Audio", library=library)
            for item_id, record in songs.items():
                folder = record.parent_id
                row = self.readback.scope("Audio", library, folder).get(item_id)
                mapping = mappings.get(item_id)
                applied = mapping.applied if mapping else {}
                if row is None:
                    folders.add(folder)
                elif mapping is None:
                    # Imported but never acknowledged (a userdata patch
                    # failed): the row has the tags of the listing that made
                    # it unless the payload has moved since.
                    if record.generation > 1:
                        folders.add(folder)
                elif applied.get("tag") != metadata.tag_hash(
                    record.item, albums.get(folder)
                ):
                    folders.add(folder)
                previous = applied.get("dir")
                if previous and previous != folder:
                    folders.add(previous)
        for folder, rows in self.readback.folders(library).items():
            for item_id in rows:
                placed = entries.get(item_id)
                if (
                    placed is None
                    or placed.kind != "Audio"
                    or placed.library != library
                    or placed.parent_id != folder
                ):
                    folders.add(folder)
                    break
        return folders
