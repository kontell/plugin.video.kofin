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

from kofin.core import memory, state
from kofin.core.log import Logger
from kofin.sync.catalogue import BackendMismatch
from . import metadata, paths, progress, removal
from .kinds import rpc, rpc_batch
from .patch import Applier
from .readback import Readback
from .records import PayloadWindow, Record
from .store import Entry, Store

LOG = Logger(__name__)

# A pass walks the library's music root once instead of scanning changed
# album directories by name when at least this share of the directories
# changed. Measured on the P1D: a directory scan is 60-80 ms, and a root walk
# re-lists every unchanged directory at about 100 ms each because Kodi
# re-creates the Python interpreter when it has no database work between two
# listings -- so the walk pays only when most directories have work.
ROOT_SCAN_SHARE = 0.5
# A hold is renewed before the provider's own limit on it runs out.
HOLD_RENEW = 480.0
# Kodi drops an extras folder from a movies listing while this is on (its
# default); the pass turns it off when a folder with extras is first scanned.
EXTRAS_SETTING = "videolibrary.ignorevideoextras"
SCAN_POLL = 3.0  # seconds between reads of Kodi's row count during a scan


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
    SCAN_POLL = SCAN_POLL

    def __init__(self, store: Store, abort=lambda: False):
        self.store = store
        self.key = store.namespace
        self.abort = abort
        self.monitor = Monitor()
        self.separator = metadata.item_separator()
        self.readback = Readback(self.key)
        self._async_pending = False
        self.payloads = PayloadWindow(self.store)
        self.progress = progress.Progress()
        self._server = ""
        # (library, folder) pairs whose directory this pass has re-listed,
        # or (library, "*") when the music root was walked.
        self.rescanned: Set[Tuple[str, str]] = set()
        # scanner -> when the hold on it was taken.
        self._held: Dict[str, float] = {}
        # Movie folders this pass scans for extras: their extras folder is
        # listed through Kodi just before the scan (_prepare_scan).
        self._extras_folders: Set[str] = set()
        self._extras_allowed = False

    # -- gates ---------------------------------------------------------------

    def setup(self):
        owner = self.store.prepared()
        if owner and owner != self.key:
            raise BackendMismatch(
                "native library belongs to another server/user; use a fresh profile"
            )
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

    # -- waits ---------------------------------------------------------------

    def _idle(self, scanner="video"):
        if scanner == "music":
            return not xbmc.getCondVisibility("Library.IsScanningMusic")
        if scanner == "any":
            return self._idle("video") and self._idle("music")
        return not xbmc.getCondVisibility("Library.IsScanningVideo")

    def _scanning(self, scanner="video"):
        return not self._idle(scanner)

    def wait(self, predicate, timeout=60, busy=lambda: False, on_poll=None):
        """``timeout`` is idle time: the deadline moves while ``busy`` holds.

        ``on_poll`` runs once a loop, for a bar that reads progress off Kodi.
        """
        deadline = time.monotonic() + timeout
        while True:
            if self.abort() or self.monitor.waitForAbort(0.1):
                raise InterruptedError("native operation interrupted")
            value = predicate()
            if value:
                return value
            if on_poll is not None:
                on_poll()
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

    def bind_folders(self, content, folders, force=False):
        """A folder under a root needs its own binding: Kodi derives a plugin
        path's parent as the plugin root, so the library binding is never
        found from beneath it (URIUtils::GetParentPath), and a folder without
        one is skipped by the scanner. A show folder is bound before the
        root scan that lists it; a movie folder only before a scan by name
        (``containssingleitem`` would make the folder the movie). The calls
        go 25 to a batch and the store learns them in one transaction.

        A movie folder is bound with folder names on: the scanner reads an
        extras folder only from a directory whose settings say its movies
        are in folders of their own (AddVideoExtras, ``parent_name``).
        ``force`` binds a folder the store already knows, for the flag."""
        bound = self.store.bindings()
        wanted = list(
            dict.fromkeys(folders if force else [p for p in folders if p not in bound])
        )
        for start in range(0, len(wanted), 25):
            chunk = wanted[start : start + 25]
            replies = rpc_batch(
                [
                    (
                        "VideoLibrary.SetSourceContent",
                        {
                            "path": path,
                            "content": content,
                            "scraperid": "metadata.local",
                            "containssingleitem": content == "tvshows",
                            "usedirectorynames": content == "movies",
                            "refresh": False,
                        },
                    )
                    for path in chunk
                ]
            )
            done = [
                path
                for path, reply in zip(chunk, replies)
                if not isinstance(reply, Exception)
            ]
            self.store.bind_many((path, content) for path in done)
            failed = [r for r in replies if isinstance(r, Exception)]
            if failed:
                raise failed[0]

    def bind_show(self, library, series_id):
        self.bind_folders("tvshows", [paths.show_dir(self.key, library, series_id)])

    def unbind_show(self, library, series_id):
        self.unbind_folder(paths.show_dir(self.key, library, series_id))

    def unbind_movie(self, library, item_id):
        self.unbind_folder(paths.movie_dir(self.key, library, item_id))

    def unbind_folder(self, path):
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
            LOG.warning("folder binding not cleared: %s", error)
        self.store.unbind(path)

    def _count(self, location) -> Optional[int]:
        """Kodi's row count under a scanner directory's library, or None."""
        if location is None or not location.library or not location.content:
            return None
        library_dir = paths.library_dir(self.key, location.library, location.content)
        method, key = {
            "music": ("AudioLibrary.GetSongs", "songs"),
            "movies": ("VideoLibrary.GetMovies", "movies"),
            "tvshows": ("VideoLibrary.GetEpisodes", "episodes"),
            "musicvideos": ("VideoLibrary.GetMusicVideos", "musicvideos"),
        }[location.content]
        reply = rpc(
            method,
            {
                "filter": {
                    "field": "path",
                    "operator": "startswith",
                    "value": library_dir,
                },
                "limits": {"start": 0, "end": 0},
            },
        )
        total = (reply or {}).get("limits", {}).get("total")
        return total if isinstance(total, int) else None

    def _scan_watch(self, location, heading, goal):
        """A poll callback that reads Kodi's row count under the scan's
        library every few seconds and moves the bar towards ``goal``."""
        if not self.progress.open:
            return None
        try:
            baseline = self._count(location)
        except Exception:
            return None
        if baseline is None:
            return None
        polled = [time.monotonic()]

        def on_poll():
            if time.monotonic() - polled[0] < self.SCAN_POLL:
                return
            polled[0] = time.monotonic()
            try:
                now = self._count(location)
            except Exception:
                return
            if now is not None:
                done = max(0, now - baseline)
                self.progress.at(done, "%s %d / %d" % (heading, done, goal))

        return on_poll

    def scan(
        self, directories: List[str], scanner="video", expected=None, prepare=None
    ):
        """Scan each directory in turn and wait for the scanner to go idle.

        ``expected`` maps a directory to the rows its scan should add; while
        it runs the bar shows Kodi's own count climbing towards it, read every
        few seconds, since a scan is the longest phase and the pass counts
        nothing of its own until it ends (20 minutes at "0 %" on the box).
        ``prepare`` is called with each directory just before its scan.

        One scan at a time: a second ``VideoLibrary.Scan`` queued while the
        first runs never starts (observed on 22.0b2: of two scans issued
        together only the first ran, every time, and a pass that selected
        two video libraries at once lost one of them on every retry).
        """
        if not directories:
            return

        def finished():
            return (
                self.monitor.music_finished
                if scanner == "music"
                else self.monitor.finished
            )

        method = "AudioLibrary.Scan" if scanner == "music" else "VideoLibrary.Scan"
        # Kodi's own scanning dialog shows the import item by item; the bar
        # above it names the phase. Never for music: with a dialog the music
        # scanner starts its MusicFileCounter thread, which lists every
        # directory a second time, concurrently, to size the bar
        # (MusicInfoScanner.cpp; two root listings of 62 s each on the
        # LibreELEC box). The video scanner has no such thread.
        dialogs = progress.show_dialogs() and scanner != "music"
        expected = expected or {}
        for directory in directories:
            self.wait(lambda: self._idle(scanner), busy=lambda: self._scanning(scanner))
            serial = finished()
            began = time.monotonic()
            location = paths.parse(directory)
            heading = "%s: %s" % (
                xbmc.getLocalizedString(progress.SCANNING),
                progress.label(
                    location.content if location and location.content else scanner
                ),
            )
            goal = int(expected.get(directory) or 0)
            saved = self.progress.phase(goal, heading) if goal else None
            on_poll = self._scan_watch(location, heading, goal) if saved else None

            if prepare is not None:
                prepare(directory)
            rpc(method, {"directory": directory, "showdialogs": dialogs})
            self._async_pending = True
            try:
                self.wait(
                    lambda serial=serial: finished() > serial and self._idle(scanner),
                    busy=lambda: self._scanning(scanner),
                    on_poll=on_poll,
                )
                LOG.info(
                    "%s scan of %s took %.1f s",
                    scanner,
                    paths.describe(directory),
                    time.monotonic() - began,
                )
            except TimeoutError:
                if finished() == serial:
                    raise
            finally:
                self._async_pending = not self._idle(scanner)
                if saved is not None:
                    self.progress.restore(saved)
        if scanner == "music":
            for directory in directories:
                location = paths.parse(directory)
                if location is not None and location.library:
                    self.readback.forget_music(location.library)
        else:
            self.readback.clear()

    # -- movie versions and extras -------------------------------------------

    def _asset_folders(self, upserts, library) -> List[str]:
        """Movie folders whose listing Kodi has not seen: the assets token
        acknowledged for the movie (the listing a scan presented) differs
        from its payload's (metadata.assets_token). The root walk lists the
        movie's own file alone, so these are scanned by name -- a movie that
        lost its last version or extra too, so that the empty token the
        apply pass then acknowledges is one a scan presented. The scan adds
        and re-reads and never removes (AddVideoExtras only adds): a row the
        listing no longer names leaves through Clean, whose per-file
        check_exists is provider.exists."""
        movies = [
            r for r in upserts.values() if r.kind == "Movie" and r.library == library
        ]
        if not movies:
            return []
        mappings = self.store.mappings(kind="Movie", library=library)
        folders: List[str] = []
        for record in self.payloads.walk(movies):
            token = metadata.assets_token(record.item)
            mapping = mappings.get(record.item_id)
            previous = mapping.applied.get("assets", "") if mapping is not None else ""
            if previous == token:
                continue
            folder = paths.movie_dir(self.key, library, record.item_id)
            folders.append(folder)
            if metadata.special_features(record.item):
                self._extras_folders.add(folder)
        return folders

    def allow_extras(self):
        """Turn Kodi's "Ignore video extras on scan" off, once: while it is
        on the scanner drops an extras folder from the listing before
        anything looks at it (VideoInfoScanner.cpp, m_ignoreVideoExtras)."""
        if self._extras_allowed:
            return
        reply = rpc("Settings.GetSettingValue", {"setting": EXTRAS_SETTING})
        if isinstance(reply, dict) and reply.get("value") is True:
            rpc("Settings.SetSettingValue", {"setting": EXTRAS_SETTING, "value": False})
            LOG.info(
                "Kodi setting %s turned off: movie extras are imported", EXTRAS_SETTING
            )
            # Kodi's own setting, for every source: the user is told once.
            from kofin.core import settings, toast

            toast.show(settings.localized(30858))
        self._extras_allowed = True

    def _prepare_scan(self, directory):
        """List a movie's extras folder through Kodi just before the folder
        is scanned, with the disc folders the scanner probes for below it.

        Before the scanner looks at a listing, every folder in it is probed
        for a disc structure (CFileItemList::Stack, GetOpticalMediaPath), and
        CPluginFile::Exists says yes to any plugin URL, so an extras folder
        is otherwise turned into a phantom VIDEO_TS.IFO. The probe asks the
        directory cache first (CFile::Exists), which holds the last fifty
        directories Kodi listed: listed here, the folder and its VIDEO_TS/
        and BDMV/ answer "no such file" and the folder stays a folder."""
        if directory not in self._extras_folders:
            return
        extras = directory + paths.EXTRAS
        for probe in (extras, extras + "VIDEO_TS/", extras + "BDMV/"):
            rpc("Files.GetDirectory", {"directory": probe, "media": "video"})

    # -- the scanner hold ----------------------------------------------------

    def hold(self, scanner="music"):
        """Keep Kodi's scanner busy while the pass writes, so the writes are
        announced as a transaction and the home widgets stay quiet.

        The scanner lists the hold directory; the provider keeps that
        listing open until ``release`` clears the token (``provider._hold``).
        Only the music scanner marks its announcements this way -- the
        video database's ``AnnounceUpdate`` carries no flag.
        """
        since = self._held.get(scanner)
        if since is not None and time.monotonic() - since < HOLD_RENEW:
            return
        if since is not None:
            self.release(scanner)
        self.wait(lambda: self._idle(scanner), busy=lambda: self._scanning(scanner))
        state.set_native_hold(scanner)
        method = "AudioLibrary.Scan" if scanner == "music" else "VideoLibrary.Scan"
        rpc(
            method,
            {"directory": paths.hold_dir(self.key, scanner), "showdialogs": False},
        )
        try:
            self.wait(lambda: self._scanning(scanner), timeout=10)
        except TimeoutError:
            LOG.warning("%s scanner did not take the hold; writing without it", scanner)
        self._held[scanner] = time.monotonic()

    def release(self, scanner="music"):
        if scanner not in self._held:
            return
        state.clear_native_hold(scanner)
        self._held.pop(scanner, None)
        self.wait(lambda: self._idle(scanner), busy=lambda: self._scanning(scanner))

    def release_all(self):
        for scanner in list(self._held):
            self.release(scanner)

    def scan_music(self, library, folders: Set[str], expected=0):
        """List the changed album directories again: each by name, or the
        library's music root once when most of them changed. ``expected`` is
        the number of songs the scan should add, for the bar."""
        if not folders:
            return
        total = len(set(self.store.folders("Audio", library)) | folders)
        if len(folders) > total * ROOT_SCAN_SHARE:
            LOG.info(
                "music: %d of %d directories changed; walking the library root",
                len(folders),
                total,
            )
            root = paths.library_dir(self.key, library, "music")
            self.scan([root], "music", expected={root: expected})
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
            # Payloads are read on demand, a chunk at a time: every pending
            # payload at once was 200 MB of Python for one video catalogue.
            self.payloads = PayloadWindow(self.store)
            pending: Dict[str, Tuple[int, str, Dict]] = {
                item_id: (generation, operation, payload)
                for item_id, generation, operation, payload in self.store.pending_work()
            }
            if repair:
                for item_id, record in self.store.records(payloads=False).items():
                    if item_id not in pending:
                        pending[item_id] = (record.generation, "upsert", {})
            errors: List[Exception] = []
            # A removed collection's movies must let go of its set before the
            # set can; they rejoin this pass as upserts.
            self.store.invalidate(removal.set_members(pending))
            removal.remove(self, pending, entries, self.store.tombstones(), errors)
            # A whole-library clear puts other libraries' shows back to
            # pending, and the set members above are pending now too; both
            # join this pass rather than wait for the next.
            for item_id, generation, operation, _ in self.store.pending_work():
                if operation == "upsert" and item_id not in pending:
                    pending[item_id] = (generation, operation, {})
            upserts: Dict[str, Record] = {}
            for item_id, (generation, operation, _payload) in pending.items():
                entry = entries.get(item_id)
                if operation != "upsert" or entry is None:
                    continue
                upserts[item_id] = Record(
                    item_id,
                    entry.kind,
                    entry.library,
                    entry.parent_id,
                    None,
                    generation,
                    loader=self.payloads,
                )
            self.progress.begin(len(upserts))
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
            self.progress.close()
            self.payloads = PayloadWindow(self.store)
            # The pass's tables are the largest Python objects in the
            # process; hand the heap back before Kodi's scanner needs the
            # page cache (core/memory.py).
            del applier
            memory.release()
            try:
                self.release_all()
            except Exception:
                LOG.exception("scanner hold not released")
                state.clear_native_hold("music")
            # A timed-out scan must retain its snapshot until Kodi is idle.
            if (completed or not self._async_pending) and self._idle("any"):
                self.store.unpin()

    def import_missing(
        self, upserts: Dict[str, Record], errors, entries: Optional[Dict] = None
    ):
        """Bind and scan whatever the readback shows the scanner has not filed,
        and the movie folders whose versions or extras it has not seen."""
        directories: List[str] = []
        expected: Dict[str, int] = {}
        expectations = []
        self._extras_folders = set()
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
                        songs_expected = sum(
                            1
                            for r in upserts.values()
                            if r.kind == "Audio"
                            and r.library == library
                            and r.parent_id in folders
                        )
                        self.scan_music(library, folders, expected=songs_expected)
                        lost = self._lost_songs(library, folders, entries)
                        if lost:
                            self.store.invalidate(lost)
                            LOG.warning(
                                "music: %d acknowledged songs missing after the"
                                " scan; pending again",
                                len(lost),
                            )
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
                    self.bind_folders(
                        "tvshows",
                        [
                            paths.show_dir(self.key, library, r.item_id)
                            for r in missing
                            if r.kind == "Series"
                        ],
                    )
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
                asset_folders: List[str] = []
                if content == "movies":
                    asset_folders = self._asset_folders(upserts, library)
                if not missing and not asset_folders:
                    continue
                root = paths.library_dir(self.key, library, content)
                if content == "movies":
                    # A movie's URL sits in a folder of its own. A few new
                    # movies are scanned by folder, each bound first (Kodi
                    # finds a plugin folder's scraper only through the
                    # folder's own binding); a first import, or a library
                    # mostly missing, scans the root once, which lists every
                    # movie as a file under its folder's URL.
                    if missing:
                        total = len(present) + len(missing)
                        if len(missing) > total * ROOT_SCAN_SHARE:
                            directories.append(root)
                            expected[root] = len(missing)
                        else:
                            movie_folders = [
                                paths.movie_dir(self.key, library, r.item_id)
                                for r in missing
                            ]
                            self.bind_folders("movies", movie_folders)
                            directories.extend(movie_folders)
                            expected.update((folder, 1) for folder in movie_folders)
                    # A folder whose version files or extras Kodi has not
                    # seen is scanned by name, after the root when the root
                    # walks (the root lists the movie's own file alone), and
                    # bound again with folder names on, which the extras need.
                    if asset_folders:
                        self.bind_folders("movies", asset_folders, force=True)
                        directories.extend(
                            f for f in asset_folders if f not in directories
                        )
                        if self._extras_folders:
                            self.allow_extras()
                else:
                    directories.append(root)
                    expected[root] = sum(
                        1 for r in missing if r.kind in ("Episode", "MusicVideo")
                    )
                for record in self.payloads.walk(missing):
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
            self.scan(directories, expected=expected, prepare=self._prepare_scan)

    def _missing_tv(self, upserts, library) -> List[Record]:
        shows = self.readback.scope("Series", library)
        missing = [
            r
            for r in upserts.values()
            if r.kind == "Series" and r.library == library and r.item_id not in shows
        ]
        episodes = [
            r for r in upserts.values() if r.kind == "Episode" and r.library == library
        ]
        for record in self.payloads.walk(episodes):
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

    def _lost_songs(self, library, folders: Set[str], entries) -> List[str]:
        """Acknowledged songs the scan just run should have re-filed and did
        not: a listing that failed half way, or a scan stopped between its
        delete and its re-add. Nothing pending names them, so they are found
        by comparing what was acknowledged with what Kodi holds, and go back
        to pending for the next pass to re-list their directory (salted, if
        Kodi kept the hash of the listing that lost them)."""
        everywhere = (library, "*") in self.rescanned
        held = self.readback.folders(library)
        lost: List[str] = []
        for item_id, known in self.store.mappings(
            kind="Audio", library=library
        ).items():
            if known.kodi_id is None:
                continue
            placed = entries.get(item_id)
            if placed is None or placed.kind != "Audio" or placed.library != library:
                continue
            if not everywhere and placed.parent_id not in folders:
                continue
            if item_id not in held.get(placed.parent_id, {}):
                lost.append(item_id)
        return lost

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

        Two kinds of applied song join the comparison uninvited. One Kodi
        no longer holds -- a listing that failed half way, a scan stopped
        between its delete and its re-add, a user's rescan interrupted:
        nothing pending names it, so it is found by comparing what was
        acknowledged with what Kodi has. And one whose album's payload moved:
        its tags take the album's artists and MusicBrainz ids, which its own
        payload hash never sees. Either goes back to pending and into this
        pass, so the scan re-files it and the applier acknowledges it again.
        """
        songs = {
            r.item_id: r
            for r in upserts.values()
            if r.kind == "Audio" and r.library == library
        }
        folders: Set[str] = set()
        mappings = self.store.mappings(kind="Audio", library=library)
        held = self.readback.folders(library)
        changed_albums = {
            r.item_id
            for r in upserts.values()
            if r.kind == "MusicAlbum" and r.library == library
        }
        uninvited: List[str] = []
        for item_id, known in mappings.items():
            if item_id in songs or known.kodi_id is None:
                continue
            placed = entries.get(item_id)
            if placed is None or placed.kind != "Audio" or placed.library != library:
                continue
            if (
                item_id not in held.get(placed.parent_id, {})
                or placed.parent_id in changed_albums
            ):
                uninvited.append(item_id)
        if uninvited:
            songs.update(
                self.store.records(
                    kind="Audio", library=library, item_ids=uninvited, payloads=False
                )
            )
        revived: Dict[str, Record] = {}
        if songs:
            # Six minutes on a Raspberry Pi for 22,000 songs (a payload parse
            # and a tag hash each): the bar names the phase and the log times it.
            began = time.monotonic()
            self.progress.note(progress.label("Audio"))
            albums = {
                i: r.item
                for i, r in self.store.records(
                    kind="MusicAlbum", library=library
                ).items()
            }
            for record in self.payloads.walk(songs.values()):
                item_id = record.item_id
                folder = record.parent_id
                row = self.readback.scope("Audio", library, folder).get(item_id)
                mapping = mappings.get(item_id)
                applied = mapping.applied if mapping else {}
                changed = False
                if row is None:
                    changed = True
                elif mapping is None:
                    # Imported but never acknowledged (a userdata patch
                    # failed): the row has the tags of the listing that made
                    # it unless the payload has moved since.
                    changed = record.generation > 1
                elif applied.get("tag") != metadata.tag_hash(
                    record.item, albums.get(folder)
                ):
                    changed = True
                if changed:
                    folders.add(folder)
                    if item_id not in upserts:
                        revived[item_id] = record
                previous = applied.get("dir")
                if previous and previous != folder:
                    folders.add(previous)
            if revived:
                self.store.invalidate(revived)
                upserts.update(revived)
            LOG.info(
                "music: %d songs compared in %.1f s; %d directories to scan,"
                " %d applied songs put back",
                len(songs),
                time.monotonic() - began,
                len(folders),
                len(revived),
            )
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
