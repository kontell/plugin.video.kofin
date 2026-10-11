"""Confirmed native operations for every kind through stock Piers public
interfaces.

An accepted RPC is never a commit: every write is confirmed by reading the
owned row back (``readback``), removals wait for that confirmation
(``removal``) and patches go only where the scanner's row differs
(``patch``). This module owns the gates, the waits, the bindings and the
scans, and runs one pass in ``reconcile``.

Music is scanned by directory. A song is missing, or its tags have moved,
or it has moved album: each means its album directory is listed again. The
directories a pass wants listed are planned in order and scanned a part at
a time (``scan_music``, ``walk``), whether they are every album of a first
import or the one album a song was renamed in.
"""

import time
from contextlib import contextmanager
from typing import Dict, List, Optional, Set, Tuple

import xbmc

from kofin.core import memory, settings, state
from kofin.core.log import Logger
from kofin.sync.catalogue import BackendMismatch
from . import companion, metadata, paths, progress, removal, walk
from .kinds import rpc, rpc_batch
from .patch import Applier
from .readback import Readback
from .records import PayloadWindow, Record
from .store import Entry, Store

LOG = Logger(__name__)

# A pass scans a movies root once instead of the missing movies' folders by
# name when at least this share of the library is missing. (Music has no
# such choice to make: scan_music lists exactly the directories that
# changed, however many, a part at a time.)
ROOT_SCAN_SHARE = 0.5
# A hold is renewed before the provider's own limit on it runs out.
HOLD_RENEW = 480.0
# Kodi drops an extras folder from a movies listing while this is on (its
# default); the pass turns it off when a folder with extras is first scanned.
EXTRAS_SETTING = "videolibrary.ignorevideoextras"
ACTOR_THUMBS_SETTING = "videolibrary.actorthumbs"
# A video scan expected to file at least this many rows runs with Kodi's
# "Download actor thumbnails" off (Native.without_actor_thumbs).
ACTOR_THUMBS_FLOOR = 100
# The profile has had every movie folder bound with noupdate
# (Native.bind_movie_folders); a hidden string setting, since the store
# does not record a binding's flag.
MOVIE_BINDINGS_SETTING = "apiMovieBindings"
MOVIE_BINDINGS_STATE = "noupdate"
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
        # and (library, "*") when that was every directory of the library.
        self.rescanned: Set[Tuple[str, str]] = set()
        # scanner -> when the hold on it was taken.
        self._held: Dict[str, float] = {}
        # Movie folders this pass scans for extras: their extras folder is
        # listed through Kodi just before the scan (_prepare_scan).
        self._extras_folders: Set[str] = set()
        self._extras_allowed = False
        # Per pass: a library's album payloads and the tag hash of every
        # song compared so far. The directory picker and the applier both
        # need them, and a song's hash is a payload walk, a tag build and a
        # digest (album_items, tag_hash).
        self._album_items: Dict[str, Dict[str, Dict]] = {}
        self._tag_hashes: Dict[str, str] = {}

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

    def pump(self):
        """Run the callbacks Kodi has queued for this thread.

        Kodi queues one for every announcement on every Monitor object, in
        one queue for the whole process, and they leave it only when the
        thread that made the object waits on Kodi; every other Python thread
        walks past them on each wait of its own (core/ws.py has the measure
        of it). The pass's setters announce every row they write, thousands
        in a burst with no wait between them, so the burst takes its own
        callbacks off as it goes. One millisecond, since a zero timeout is
        Kodi's spelling of "for ever".
        """
        if self.abort() or self.monitor.waitForAbort(0.001):
            raise InterruptedError("native operation interrupted")

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

    def bind_folders(self, content, folders, force=False, noupdate=False):
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
        ``force`` binds a folder the store already knows, for a flag.

        ``noupdate`` is the binding every movie folder ends up with: Kodi's
        info dialog loads a library movie's full details (its cast among
        them) only when it finds a scraper on the movie's own folder, and a
        plugin path's parent walk never reaches the bound library root
        (CGUIWindowVideoBase::ShowInfo, CVideoDatabase::GetScraperForPath),
        so every folder is bound; with ``noupdate`` Kodi's own Update library
        lists none of them. The scanner skips a ``noupdate`` folder it was
        not told to scan everything of, so a folder scanned by name is bound
        without the flag first and with it afterwards (import_missing)."""
        bound = self.store.bindings()
        wanted = list(
            dict.fromkeys(folders if force else [p for p in folders if p not in bound])
        )
        if wanted:
            LOG.info(
                "binding %d %s folder(s)%s",
                len(wanted),
                content,
                " noupdate" if noupdate else "",
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
                            "noupdate": noupdate,
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

    def bind_movie_folders(self):
        """Every movie folder carries its binding (bind_folders), the folders
        of an older profile's rows included: a pass plans pending rows only,
        so this runs on the catalogue, after a pass and once per service
        generation when nothing is pending (library.apply). Once per
        profile every movie folder is bound again with ``noupdate``, for the
        folders an earlier build bound without it -- the store does not
        record the flag -- and MOVIE_BINDINGS_SETTING remembers that."""
        folders = sorted(
            {
                paths.movie_dir(self.key, e.library, e.item_id)
                for e in self.store.entries(kind="Movie").values()
            }
        )
        if not folders:
            return
        force = settings.get_str(MOVIE_BINDINGS_SETTING) != MOVIE_BINDINGS_STATE
        if force:
            LOG.info("binding every movie folder with noupdate, once")
        self.bind_folders("movies", folders, force=force, noupdate=True)
        if force:
            settings.set_str(MOVIE_BINDINGS_SETTING, MOVIE_BINDINGS_STATE)

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
        self,
        directories: List[str],
        scanner="video",
        expected=None,
        prepare=None,
        span=0,
    ):
        """Scan each directory in turn and wait for the scanner to go idle.

        ``expected`` maps a directory to the rows its scan should add; while
        it runs the bar shows Kodi's own count climbing towards it, read every
        few seconds, since a scan is the longest phase and the pass counts
        nothing of its own until it ends (20 minutes at "0 %" on the box).
        ``span`` is that count for the whole run of directories instead: one
        phase of the bar across the parts of a music walk.
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

        def heading_of(location):
            return "%s: %s" % (
                xbmc.getLocalizedString(progress.SCANNING),
                progress.label(
                    location.content if location and location.content else scanner
                ),
            )

        spanned = None
        span_poll = None
        if span:
            first = paths.parse(directories[0])
            spanned = self.progress.phase(int(span), heading_of(first))
            span_poll = self._scan_watch(first, heading_of(first), int(span))
        try:
            for directory in directories:
                self.wait(
                    lambda: self._idle(scanner), busy=lambda: self._scanning(scanner)
                )
                serial = finished()
                began = time.monotonic()
                location = paths.parse(directory)
                heading = heading_of(location)
                goal = 0 if span else int(expected.get(directory) or 0)
                saved = self.progress.phase(goal, heading) if goal else None
                on_poll = self._scan_watch(location, heading, goal) if saved else None
                if span:
                    on_poll = span_poll

                if prepare is not None:
                    prepare(directory)
                rpc(method, {"directory": directory, "showdialogs": dialogs})
                self._async_pending = True
                try:
                    self.wait(
                        lambda serial=serial: finished() > serial
                        and self._idle(scanner),
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
        finally:
            if spanned is not None:
                self.progress.restore(spanned)
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

    @contextmanager
    def without_actor_thumbs(self, rows):
        """Run a large video scan with Kodi's "Download actor thumbnails" off,
        and put the setting back.

        With it on the scanner asks the texture cache for every cast
        member's picture before it files the item (FetchActorThumbs,
        VideoInfoScanner.cpp): a lookup in the texture database behind the
        lock its one worker holds while it writes, then a search of the
        whole job queue for a duplicate (CJobQueue::AddJob), twenty times a
        movie, into a queue that a first import fills faster than the worker
        empties it. The pictures are not lost by the hold: the listing gives
        every actor's URL, AddActor stores it, and Kodi fetches a picture the
        first time something shows it.

        The value is recorded before it is changed (store.hold_setting), so
        a Kodi stopped mid-scan gets it back at the next start
        (restore_settings)."""
        held = False
        if rows >= ACTOR_THUMBS_FLOOR:
            try:
                reply = rpc(
                    "Settings.GetSettingValue", {"setting": ACTOR_THUMBS_SETTING}
                )
                if isinstance(reply, dict) and reply.get("value") is True:
                    self.store.hold_setting(ACTOR_THUMBS_SETTING, True)
                    rpc(
                        "Settings.SetSettingValue",
                        {"setting": ACTOR_THUMBS_SETTING, "value": False},
                    )
                    held = True
                    LOG.info(
                        "Kodi setting %s off for a scan of %d rows",
                        ACTOR_THUMBS_SETTING,
                        rows,
                    )
            except RuntimeError as error:
                # The scan is the work; the hold only makes it shorter.
                LOG.warning("actor thumbnails left on for the scan: %s", error)
        try:
            yield
        finally:
            if held:
                self.restore_settings()

    def restore_settings(self):
        """Give back every Kodi setting a scan was holding: the scan that
        just ended, or one a stopped Kodi never finished. The record goes
        only after Kodi took the value.

        A value Kodi will not take now is no reason to stop: the record
        stays and every later pass and service start asks again. Raised
        from here it failed the pass that called it, and the pass after
        that, for as long as Kodi refused."""
        for setting, value in self.store.held_settings().items():
            try:
                rpc("Settings.SetSettingValue", {"setting": setting, "value": value})
            except RuntimeError as error:
                LOG.warning("Kodi setting %s not given back yet: %s", setting, error)
                continue
            self.store.release_setting(setting)
            LOG.info("Kodi setting %s back to %r", setting, value)

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
        """Have Kodi list the changed album directories again.

        The directories are planned in listing order (``walk.rows``) and
        scanned a part at a time: each scan is of a directory that lists
        ``paths.MUSIC_PART`` of them, and the scanner goes into each. That
        is one scan job for every thirty-two directories where a scan by
        name was one for each, and it lists no directory that did not
        change, where a walk of the library root listed them all. It is
        also the only one of the three that is safe from Kodi's directory
        cache: a directory scanned by name "does not exist" to Kodi when a
        cached listing of its parent fails to name it, a new album under a
        root listed before it arrived (``paths.MUSIC_PART`` has the rest).
        ``expected`` is the number of songs the scan should add, for the bar.
        """
        if not folders:
            return
        every = walk.root_folders(self.store, library)
        rows = walk.rows(self.store, library, folders, self.album_items(library))
        # The applier reads the album payloads again after the scan; held
        # across it they are memory Kodi's scanner wants for its page cache.
        self._album_items.pop(library, None)
        self.store.plan_walk(library, rows)
        parts = walk.parts(len(rows))
        LOG.info(
            "music: %d of %d directories changed; scanning them in %d part(s)",
            len(rows),
            len(every | set(folders)),
            parts,
        )
        began = time.monotonic()
        self.scan(
            [paths.music_part(self.key, library, n) for n in range(parts)],
            "music",
            span=expected,
        )
        LOG.info(
            "music: %d directories scanned in %.1f s",
            len(rows),
            time.monotonic() - began,
        )
        self.rescanned.update((library, folder) for folder in folders)
        if every <= set(folders):
            self.rescanned.add((library, "*"))

    # -- the pass ------------------------------------------------------------

    def reconcile(self, repair=False):
        """Replay durable operations; an accepted RPC is never a commit."""
        self.setup()
        # A prior process may have died with its pin held, or left a scan
        # running that outlives it. Wait for Kodi to finish using it before
        # publishing the newer desired view to a scan, however long it runs.
        self.wait(lambda: self._idle("any"), busy=lambda: not self._idle("any"))
        self.restore_settings()
        self.store.pin()
        self.readback.clear()
        self.rescanned = set()
        self._album_items = {}
        self._tag_hashes = {}
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
            try:
                self.bind_movie_folders()
            except Exception:
                LOG.exception("movie folder bindings not completed")
            # The requests are built from the stored mappings, and what the
            # companion reports done is written onto them: every
            # acknowledgement of this pass is in the store before either.
            # Applier.run leaves none queued; this is that, said here.
            applier.commit()
            try:
                # What this pass could not do and the companion add-on can:
                # published for it, confirmed by the next readback.
                companion.publish(self)
            except Exception:
                LOG.exception("companion requests not published")
            if errors:
                raise RuntimeError(
                    "%d native operations remain pending: %s" % (len(errors), errors[0])
                )
            completed = True
        finally:
            applier.commit()
            self.progress.close()
            self.payloads = PayloadWindow(self.store)
            self._album_items = {}
            self._tag_hashes = {}
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
        # Movie folders scanned by name this pass: bound without noupdate
        # for the scan and with it afterwards (bind_folders).
        scanned_movie_folders: List[str] = []
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
                            scanned_movie_folders.extend(movie_folders)
                            directories.extend(movie_folders)
                            expected.update((folder, 1) for folder in movie_folders)
                    # A folder whose version files or extras Kodi has not
                    # seen is scanned by name, after the root when the root
                    # walks (the root lists the movie's own file alone), and
                    # bound again with folder names on, which the extras need.
                    if asset_folders:
                        scanned_movie_folders.extend(
                            f for f in asset_folders if f not in scanned_movie_folders
                        )
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
        try:
            if scanned_movie_folders:
                # Without noupdate for the scan: the scanner skips a noupdate
                # folder it was not told to scan everything of.
                self.bind_folders("movies", scanned_movie_folders, force=True)
            if directories:
                self.store.expect_many(expectations)
                with self.without_actor_thumbs(sum(expected.values())):
                    self.scan(
                        directories, expected=expected, prepare=self._prepare_scan
                    )
        finally:
            # The flag goes back whether the scan finished or not, and
            # whether the lift itself finished: without it Kodi's own Update
            # library lists these folders.
            if scanned_movie_folders:
                self.bind_folders(
                    "movies", scanned_movie_folders, force=True, noupdate=True
                )

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

    def album_items(self, library) -> Dict[str, Dict]:
        """The library's album payloads, read once a pass: a song's tag hash
        takes its album's artists and MusicBrainz ids, and 22,000 songs must
        not read their album 22,000 times."""
        if library not in self._album_items:
            self._album_items[library] = {
                i: r.item
                for i, r in self.store.records(
                    kind="MusicAlbum", library=library
                ).items()
            }
        return self._album_items[library]

    def tag_hash(self, record: Record) -> str:
        """A song's tag hash, computed once a pass."""
        found = self._tag_hashes.get(record.item_id)
        if found is None:
            found = self._tag_hashes[record.item_id] = metadata.tag_hash(
                record.item, self.album_items(record.library).get(record.parent_id)
            )
        return found

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

            def moved(record):
                folders.add(record.parent_id)
                if record.item_id not in upserts:
                    revived[record.item_id] = record

            # Only a song with a row and an acknowledgement has tags to
            # compare, and only its payload is read: a first import has
            # neither for any song, and walking 22,000 payloads to decide
            # that every directory is new cost a Raspberry Pi 21 s.
            tagged: List[Record] = []
            for record in songs.values():
                item_id = record.item_id
                folder = record.parent_id
                row = self.readback.scope("Audio", library, folder).get(item_id)
                mapping = mappings.get(item_id)
                applied = mapping.applied if mapping else {}
                if row is None:
                    moved(record)
                elif mapping is None:
                    # Imported but never acknowledged (a userdata patch
                    # failed): the row has the tags of the listing that made
                    # it unless the payload has moved since.
                    if record.generation > 1:
                        moved(record)
                else:
                    tagged.append(record)
                previous = applied.get("dir")
                if previous and previous != folder:
                    folders.add(previous)
            for record in self.payloads.walk(tagged):
                if mappings[record.item_id].applied.get("tag") != self.tag_hash(record):
                    moved(record)
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
