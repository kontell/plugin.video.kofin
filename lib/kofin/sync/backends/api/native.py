"""Confirmed native operations for every video kind through stock Piers
public interfaces.

An accepted RPC is never a commit: every write is confirmed by reading the
owned row back (``readback``), removals wait for that confirmation
(``removal``) and patches go only where the scanner's row differs
(``patch``). This module owns the gates, the waits, the bindings and the
scans, and runs one pass in ``reconcile``.
"""

import time
from typing import Dict, List, Tuple

import xbmc

from kofin.core.log import Logger
from kofin.sync.catalogue import BackendMismatch
from . import metadata, paths, removal
from .kinds import rpc
from .patch import Applier
from .readback import Readback
from .store import Record, Store

LOG = Logger(__name__)


class Monitor(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        self.finished = 0

    def onScanFinished(self, library):
        if library.lower() == "video":
            self.finished += 1


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

    def _idle(self):
        return not xbmc.getCondVisibility("Library.IsScanningVideo")

    def _scanning(self):
        return not self._idle()

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

    def scan(self, directories: List[str]):
        """Queue one scan per directory and wait for the scanner to go idle."""
        if not directories:
            return
        self.wait(self._idle)
        serial = self.monitor.finished
        for directory in directories:
            rpc("VideoLibrary.Scan", {"directory": directory, "showdialogs": False})
        self._async_pending = True
        try:
            self.wait(
                lambda: self.monitor.finished >= serial + len(directories)
                and self._idle(),
                busy=self._scanning,
            )
        except TimeoutError:
            if self.monitor.finished == serial:
                raise
            LOG.warning(
                "%d of %d scans reported finished; reading back anyway",
                self.monitor.finished - serial,
                len(directories),
            )
        finally:
            self._async_pending = not self._idle()
        self.readback.clear()

    # -- the pass ------------------------------------------------------------

    def reconcile(self, repair=False):
        """Replay durable operations; an accepted RPC is never a commit."""
        self.setup()
        # A prior process may have died with its pin held. Wait for Kodi to
        # finish using it before publishing the newer desired view to a scan.
        self.wait(self._idle)
        self.store.pin()
        self.readback.clear()
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
            self.import_missing(upserts, errors)
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
            if (completed or not self._async_pending) and self._idle():
                self.store.unpin()

    def import_missing(self, upserts: Dict[str, Record], errors):
        """Bind and scan whatever the readback shows the scanner has not filed."""
        directories: List[str] = []
        expectations = []
        for library in sorted({r.library for r in upserts.values() if r.library}):
            kinds = {r.kind for r in upserts.values() if r.library == library}
            for content in paths.CONTENTS:
                if not any(paths.CONTENT.get(k) == content for k in kinds):
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
