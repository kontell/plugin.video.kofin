"""Confirmed native operations for every kind through stock Piers public
interfaces.

An accepted RPC is never a commit: every write is confirmed by reading the
owned row back (``readback``), removals wait for that confirmation
(``removal``) and patches go only where the scanner's row differs
(``patch``). This module owns the gates, the waits, the bindings and the
scans, and runs one pass in ``reconcile``.

Music is scanned by directory, a plan of them a part at a time. Which
directories, in which order and under what bar is ``musicpass`` and
``walk``; the scanner they drive is here.
"""

import time
from contextlib import contextmanager
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

import xbmc

from kofin.core import memory, settings, state
from kofin.core.log import Logger
from kofin.sync.catalogue import BackendMismatch
from . import companion, metadata, paths, progress, removal
from .kinds import MUSIC_FIRST, ORDER, rpc, rpc_batch
from .musicpass import MusicPass
from .patch import Applier
from .readback import Readback
from .records import PayloadWindow, Record
from .store import Store

LOG = Logger(__name__)

# A pass scans a movies root once instead of the missing movies' folders by
# name when at least this share of the library is missing. (Music has no
# such choice to make: MusicPass.scan lists exactly the directories that
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
# A directory that is not there. Scanned, it is skipped at once and the scan
# still ends with the announcement home widgets listen for (library_event).
NOWHERE = "special://temp/kofin-no-such-directory/"


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


class VideoScan(NamedTuple):
    """What a pass has Kodi's video scanner list (Native.plan_video)."""

    directories: List[str]
    # Rows the scan of each directory is to file, for Kodi's own count.
    expected: Dict[str, int]
    # (item id, generation, user data) of what the scanner is about to file.
    expectations: List[Tuple[str, int, Dict]]
    # Movie folders scanned by name: bound without noupdate for the scan
    # and with it afterwards (bind_folders).
    movie_folders: List[str]


class Native:
    SCAN_POLL = SCAN_POLL

    def __init__(self, store: Store, abort=lambda: False, bar=None):
        self.store = store
        self.key = store.namespace
        self.abort = abort
        self.monitor = Monitor()
        self.separator = metadata.item_separator()
        self.readback = Readback(self.key)
        self._async_pending = False
        self.payloads = PayloadWindow(self.store)
        # The library thread's own bar when it made this pass (one bar from
        # a viewer's request to the end of the work), else one of its own.
        self.progress = bar if bar is not None else progress.Progress()
        self._server = ""
        # The pass's music work and what it reads once for it; a new one
        # for every pass (reconcile).
        self.music = MusicPass(self)
        # scanner -> when the hold on it was taken.
        self._held: Dict[str, float] = {}
        # Movie folders this pass scans for extras: their extras folder is
        # listed through Kodi just before the scan (_prepare_scan).
        self._extras_folders: Set[str] = set()
        self._extras_allowed = False
        # A held Kodi setting is still to be given back (restore_settings),
        # and the ones whose refusal has been said.
        self.settings_owed = False
        self._refused_settings: Set[str] = set()

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
        without the flag first and with it afterwards (scan_video)."""
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

    def library_event(self):
        """Have Kodi announce that the video library changed, and scan
        nothing for it.

        A scan with no directory walks every bound folder to compare its
        hash: every show of every library that was not being removed, 10 to
        14 s on a desktop each time a library left, and Kodi refused the
        clean asked for straight after it ("CleanLibrary is not possible
        while scanning or cleaning"), so the sets a removed movie library
        had emptied were never dropped. Not issued while a scan runs: the
        builtin behind the call stops a running scan instead of queuing.
        """
        if not self._idle("video"):
            return
        rpc("VideoLibrary.Scan", {"directory": NOWHERE, "showdialogs": False})

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

    def scan_heading(self, location, scanner="video") -> str:
        return "%s: %s" % (
            xbmc.getLocalizedString(progress.SCANNING),
            progress.label(
                location.content if location and location.content else scanner
            ),
        )

    def scan_watch(self, location, heading, goal):
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
        on_poll=None,
    ):
        """Scan each directory in turn and wait for the scanner to go idle.

        ``expected`` maps a directory to the rows its scan should add; while
        it runs the bar shows Kodi's own count climbing towards it, read every
        few seconds, since a scan is the longest phase and the pass counts
        nothing of its own until it ends (20 minutes at "0 %" on the box).
        ``on_poll`` is called while a directory with no such count is
        scanned: the caller's own bar across the run (``MusicPass.scan``).
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
            heading = self.scan_heading(location, scanner)
            goal = int(expected.get(directory) or 0)
            saved = self.progress.phase(goal, heading) if goal else None
            watch = self.scan_watch(location, heading, goal) if saved else on_poll

            if prepare is not None:
                prepare(directory)
            rpc(method, {"directory": directory, "showdialogs": dialogs})
            self._async_pending = True
            try:
                self.wait(
                    lambda serial=serial: finished() > serial and self._idle(scanner),
                    busy=lambda: self._scanning(scanner),
                    on_poll=watch,
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
                    try:
                        rpc(
                            "Settings.SetSettingValue",
                            {"setting": ACTOR_THUMBS_SETTING, "value": False},
                        )
                    except RuntimeError:
                        # Kodi kept its value, so nothing is held. A record
                        # left here would be given back at a later start,
                        # over whatever the user had set by then.
                        self.store.release_setting(ACTOR_THUMBS_SETTING)
                        raise
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
        stays, ``settings_owed`` says so, and every later pass and service
        start asks again, as does the library thread while nothing is
        pending (``Library.apply``). Raised from here it failed the pass
        that called it, and the pass after that, for as long as Kodi
        refused. A refusal is a warning once a setting and generation."""
        owed = False
        for setting, value in self.store.held_settings().items():
            try:
                rpc("Settings.SetSettingValue", {"setting": setting, "value": value})
            except RuntimeError as error:
                owed = True
                say = LOG.debug if setting in self._refused_settings else LOG.warning
                self._refused_settings.add(setting)
                say("Kodi setting %s not given back yet: %s", setting, error)
                continue
            self.store.release_setting(setting)
            self._refused_settings.discard(setting)
            LOG.info("Kodi setting %s back to %r", setting, value)
        self.settings_owed = owed

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

    # -- the pass ------------------------------------------------------------

    def reconcile(self, repair=False):
        """Replay durable operations; an accepted RPC is never a commit."""
        # Until the first scan or the first row is counted, the pass waits
        # for Kodi's scanners, reads the catalogue, removes, binds and plans.
        self.progress.working(progress.PREPARING)
        self.setup()
        # A prior process may have died with its pin held, or left a scan
        # running that outlives it. Wait for Kodi to finish using it before
        # publishing the newer desired view to a scan, however long it runs.
        self.wait(lambda: self._idle("any"), busy=lambda: not self._idle("any"))
        self.restore_settings()
        self.store.pin()
        self.readback.clear()
        self.music = MusicPass(self)
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
            sets = self.store.records(kind="BoxSet")
            boxsets = [r.item for r in sets.values()]
            collections = metadata.collections_of(boxsets)
            self.join_sets(pending, entries, sets, collections)
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

            self.scan_music(upserts, errors, entries)
            video = self.plan_video(upserts, errors)
            # Album and artist artwork does not wait for the video scans
            # (kinds.MUSIC_FIRST). With no video scan to wait for, the kinds
            # keep their order and the music hold is taken once.
            early = (
                [
                    kind
                    for kind in MUSIC_FIRST
                    if any(r.kind == kind for r in upserts.values())
                ]
                if video.directories or video.movie_folders
                else []
            )
            if early:
                applier.run(upserts, {}, set(), repair, errors, kinds=early)
            self.scan_video(video)
            movies = {i for i, e in entries.items() if e.kind == "Movie"}
            applier.run(
                upserts,
                collections,
                movies,
                repair,
                errors,
                kinds=[kind for kind in ORDER if kind not in early],
            )
            # Sets, bindings and the companion's requests: nothing counted.
            self.progress.working(progress.WAITING)
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
            self.music.forget()
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

    def join_sets(self, pending, entries, sets: Dict[str, Record], collections):
        """Put every collection a movie of this pass is filed under into the
        pass, pending before that movie is acknowledged.

        A set is Kodi's to make and to drop. It goes with the last movie
        that names it (the companion deletes the empty row, as a clean does)
        and a scan or a movie's patch makes it again from the name alone,
        with no art and no plot. The collection's payload moves with none of
        that, so nothing but its movies can bring it here: a library
        imported again left every set of it bare, with only the repair pass
        after the next daily enumeration to compare them. Planned, a set is
        compared with its row (Applier.plan), and one found as it was left is
        sent nothing.
        """
        taken = {
            collections[item_id]
            for item_id, (_, operation, _) in pending.items()
            if operation == "upsert"
            and item_id in collections
            and item_id in entries
            and entries[item_id].kind == "Movie"
        }
        joined = {
            item_id: record
            for item_id, record in sets.items()
            if item_id not in pending
            and (record.item.get("Name") or "").strip(metadata.ASCII_SPACE) in taken
        }
        self.store.invalidate(joined)
        for item_id, record in joined.items():
            pending[item_id] = (record.generation, "upsert", {})

    def scan_music(
        self, upserts: Dict[str, Record], errors, entries: Optional[Dict] = None
    ):
        """Have Kodi list the music directories that need it, a library at a
        time. The pass scans music before video (reconcile)."""
        for library in sorted({r.library for r in upserts.values() if r.library}):
            if not any(
                paths.CONTENT.get(r.kind) == "music"
                for r in upserts.values()
                if r.library == library
            ):
                continue
            try:
                if entries is None:
                    entries = self.store.entries()
                self.music.import_missing(upserts, library, entries)
            except InterruptedError:
                raise
            except Exception as error:
                errors.append(error)

    def plan_video(self, upserts: Dict[str, Record], errors) -> VideoScan:
        """Bind whatever the readback shows the video scanner has not filed,
        and name the directories to scan for it and for the movie folders
        whose versions or extras the scanner has not seen."""
        directories: List[str] = []
        expected: Dict[str, int] = {}
        expectations = []
        self._extras_folders = set()
        scanned_movie_folders: List[str] = []
        for library in sorted({r.library for r in upserts.values() if r.library}):
            kinds = {r.kind for r in upserts.values() if r.library == library}
            for content in paths.CONTENTS:
                if not any(paths.CONTENT.get(k) == content for k in kinds):
                    continue
                if content == "music":
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
        return VideoScan(directories, expected, expectations, scanned_movie_folders)

    def scan_video(self, video: VideoScan):
        """Have Kodi's video scanner list what plan_video named."""
        try:
            if video.movie_folders:
                # Without noupdate for the scan: the scanner skips a noupdate
                # folder it was not told to scan everything of.
                self.bind_folders("movies", video.movie_folders, force=True)
            if video.directories:
                self.store.expect_many(video.expectations)
                with self.without_actor_thumbs(sum(video.expected.values())):
                    self.scan(
                        video.directories,
                        expected=video.expected,
                        prepare=self._prepare_scan,
                    )
        finally:
            # The flag goes back whether the scan finished or not, and
            # whether the lift itself finished: without it Kodi's own Update
            # library lists these folders.
            if video.movie_folders:
                self.bind_folders(
                    "movies", video.movie_folders, force=True, noupdate=True
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
