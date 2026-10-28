"""API coordinator. Network enumeration precedes publication.

One worker serializes publication and native jobs. Callbacks enqueue intent
and never wait on Kodi scans. A complete enumeration -- startup without a
watermark, Update library, a sync-queue retention overrun, the scheduled full
update when one is set -- is the only listing that may imply deletion;
between passes the websocket and a watermarked catch-up on
the shared change-feed contract carry the changes. Native operations already
published are replayed even if Jellyfin is offline.
"""

import collections
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Set, Tuple

import xbmc

from kofin.core import memory, settings
from kofin.core.http import HttpError
from kofin.core.log import Logger
from kofin.core.settings import Credentials
from kofin.sync import changefeed, private
from kofin.sync.catalogue import BackendMismatch
from kofin.sync.downloader import info, library_filter
from . import announce, capabilities, metadata, progress
from . import playlists as api_playlists
from . import status as apistatus
from .native import Native
from .store import REBUILDING, Store, namespace

LOG = Logger(__name__)

KINDS_BY_COLLECTION = {
    "movies": ("Movie",),
    "tvshows": ("Series", "Season", "Episode"),
    "musicvideos": ("MusicVideo",),
    "mixed": ("Movie", "Series", "Season", "Episode", "MusicVideo"),
    "music": ("MusicArtist", "MusicAlbum", "Audio"),
}
SUPPORTED = ("movies", "tvshows", "musicvideos", "mixed", "music")
# SpecialFeatureCount says which movies have extras to fetch (attach_extras).
FIELDS = info() + ",MediaStreams,SpecialFeatureCount"
# Music asks for what its tags and art need and nothing the server has to
# count (RecursiveItemCount is slow); People, trailers and streams have no
# music setter.
MUSIC_FIELDS = (
    "DateCreated,Genres,ProviderIds,MediaSources,Overview,SortName,Etag,"
    "ParentId,PremiereDate,Tags"
)
MUSIC_KINDS = ("MusicArtist", "MusicAlbum", "Audio")
# What a listing asks the server for, recorded per library when its listing
# is published (store.remember_listing). A catch-up fetches changed items
# alone, so rows filed from a listing that lacked a field would never get
# it: a library whose record differs is listed again by name, at the first
# catch-up and then at each one until it has been (stale_listings).
LISTING = FIELDS + "|" + MUSIC_FIELDS
CHANGEFEED_TYPES = ("movies", "tvshows", "boxsets", "musicvideos", "music")
# Between the websocket and the sync-queue feed the catch-up is a safety
# net. A full enumeration on a clock is the viewer's choice
# (fullUpdateInterval, in days, 0 for none). Without the sync queue the
# catch-up reads save dates and cannot see a deletion.
CATCHUP_INTERVAL = 1800
DAY = 86400


def full_interval() -> Optional[float]:
    """Seconds between scheduled complete enumerations, or None for none."""
    days = settings.get_int("fullUpdateInterval")
    return days * DAY if days > 0 else None


PAGE = 200
# A song's DTO is about a tenth of a movie's, so its pages are larger.
MUSIC_PAGE = 500
# Pages of one listing in flight at once: a page's wait is the wire, not the
# server or the client.
PAGERS = 3
# How long after a playback stopped the user data held for its item is
# applied when the server has sent nothing newer. The server's own entry
# for the stop arrives within a second or two and replaces the held one.
HELD_USERDATA_GRACE = 20.0
# What a viewer asks for by name: the bar is up from the request to the end
# of the work (progress.Progress.attend).
ATTENDED = ("SyncLibrary", "UpdateLibrary", "RepairLibrary", "RemoveLibrary")
HAS_CONTENT = {
    "Movie": "Movies",
    "Series": "TVShows",
    "MusicVideo": "MusicVideos",
    "Audio": "Music",
}


def status(value, listing=(), namespace=""):
    """What the sync is doing, for the status dialog (status.py), under the
    account whose thread says so."""
    apistatus.write(value, listing, namespace)


def collection_kinds(view):
    collection = view.get("CollectionType") or "mixed"
    return KINDS_BY_COLLECTION.get(collection, ())


def fetch_kind(
    api,
    library,
    kind,
    abort=lambda: False,
    extra=None,
    on_page=None,
    api_factory=None,
):
    """Only a complete, stable, duplicate-free pagination can imply deletion.

    ``on_page(fetched, total)`` is told after every page, for the bar.

    With ``api_factory`` the pages after the first are asked for PAGERS at
    a time, each worker on a client of its own, and taken in listing order.
    Nothing is relaxed for it: every page reports the first page's total
    and is exactly as long as its place in the listing says, or the
    enumeration fails as a whole.
    """
    result: List[dict] = []
    seen = set()
    limit = MUSIC_PAGE if kind in MUSIC_KINDS else PAGE

    def ask(client, start):
        if abort():
            raise InterruptedError("enumeration stopped")
        params = {
            **library_filter(client, library, kind),
            "Fields": MUSIC_FIELDS if kind in MUSIC_KINDS else FIELDS,
            "StartIndex": start,
            "Limit": limit,
            "SortBy": "DateCreated,SortName",
            "SortOrder": "Ascending,Ascending",
            "EnableTotalRecordCount": True,
        }
        params.update(extra or {})
        page = client.items(params)
        count = page.get("TotalRecordCount")
        rows = page.get("Items")
        if not isinstance(count, int) or count < 0 or not isinstance(rows, list):
            raise ValueError("incomplete %s page" % kind)
        return count, rows

    def take(rows, total):
        for item in rows:
            if (
                not isinstance(item, dict)
                or not item.get("Id")
                or item.get("Type") != kind
                or not item.get("Name")
                or item["Id"] in seen
                or (kind in ("Season", "Episode") and not item.get("SeriesId"))
            ):
                raise ValueError("invalid or duplicate %s" % kind)
            seen.add(item["Id"])
            result.append(metadata.compact(item))
        if len(result) > total or (not rows and len(result) < total):
            raise ValueError("short %s page" % kind)
        if on_page is not None:
            on_page(len(result), total)

    total, rows = ask(api, 0)
    take(rows, total)
    starts = list(range(limit, total, limit))
    if api_factory is not None and len(starts) > 1 and len(rows) == limit:
        _fetch_pages(ask, take, starts, total, limit, kind, api_factory, api)
    while len(result) < total:
        count, rows = ask(api, len(result))
        if count != total:
            raise ValueError("%s listing changed during enumeration" % kind)
        take(rows, total)
    if kind == "Movie":
        attach_extras(api, result, abort)
    return result


def _account(client):
    return getattr(client, "server", None), getattr(client, "user_id", None)


def _fetch_pages(ask, take, starts, total, limit, kind, api_factory, api):
    """The pages at ``starts``, PAGERS in flight, taken in listing order.

    At most PAGERS pages wait to be taken, so a slow first page never lets
    the rest of a catalogue pile up behind it in memory.

    Every page client answers for the account ``api`` asked the first page
    as. The factory reads the stored credentials when it is called, so a
    sign-in changed while the enumeration runs fails it as a whole.
    """
    local = threading.local()
    clients: List[Any] = []
    account = _account(api)

    def page(start):
        client = getattr(local, "client", None)
        if client is None:
            client = api_factory()
            clients.append(client)
            if _account(client) != account:
                raise ValueError("account changed during %s enumeration" % kind)
            local.client = client
        return ask(client, start)

    pending = iter(starts)
    window: "collections.deque" = collections.deque()
    pool = ThreadPoolExecutor(
        max_workers=min(PAGERS, len(starts)), thread_name_prefix="kofin-page"
    )

    def top_up():
        while len(window) < PAGERS:
            start = next(pending, None)
            if start is None:
                return
            window.append((start, pool.submit(page, start)))

    try:
        top_up()
        while window:
            start, future = window.popleft()
            count, rows = future.result()
            if count != total:
                raise ValueError("%s listing changed during enumeration" % kind)
            if len(rows) != min(limit, total - start):
                raise ValueError("short %s page" % kind)
            top_up()
            take(rows, total)
    finally:
        for _, future in window:
            future.cancel()
        pool.shutdown(wait=True)
        for client in clients:
            try:
                client.close()
            except Exception as error:
                LOG.debug("page client not closed: %s", error)


fetch_movies = fetch_kind


def attach_extras(api, items, abort=lambda: False):
    """A movie's special features travel in its payload: the scanner's
    listings never contact the server, and the movie folder's extras folder
    is built from them. Only a movie whose count says it has any is asked.

    Every path that publishes a movie goes through here -- ``fetch_kind``
    for an enumeration or a catch-up, ``fetch_items`` and ``flush_local``
    for a single re-fetch -- because a payload without its features reads as
    a movie without extras: the token goes empty and Clean, asking
    ``provider.exists`` per file, would drop rows the server still has."""
    for item in items:
        if item.get("Type") != "Movie" or not item.get("SpecialFeatureCount"):
            continue
        if abort():
            raise InterruptedError("enumeration stopped")
        features = api.special_features(item["Id"])
        item["SpecialFeatures"] = [
            metadata.compact_extra(feature)
            for feature in features
            if isinstance(feature, dict) and feature.get("Id") and feature.get("Name")
        ]


def fetch_boxsets(api, abort=lambda: False, ids=None):
    """Collections with their movie members; sets live outside any library."""
    params: Dict[str, Any] = {
        "userId": api.user_id,
        "IncludeItemTypes": "BoxSet",
        "Recursive": True,
        "Fields": FIELDS,
        "EnableTotalRecordCount": True,
    }
    if ids is not None:
        if not ids:
            return []
        params["Ids"] = ",".join(ids)
    page = api.items(params)
    rows = page.get("Items")
    if not isinstance(rows, list) or (
        ids is None and page.get("TotalRecordCount") != len(rows)
    ):
        raise ValueError("incomplete collection listing")
    result = []
    for boxset in rows:
        if abort():
            raise InterruptedError("enumeration stopped")
        if not isinstance(boxset, dict) or boxset.get("Type") != "BoxSet":
            raise ValueError("invalid collection")
        members = api.items(
            {
                "userId": api.user_id,
                "ParentId": boxset["Id"],
                "IncludeItemTypes": "Movie",
                "Fields": "Etag",
                "EnableTotalRecordCount": True,
            }
        )
        items = members.get("Items")
        if not isinstance(items, list) or members.get("TotalRecordCount") != len(items):
            raise ValueError("incomplete collection membership")
        compacted = metadata.compact(boxset)
        compacted["KofinMembers"] = sorted(
            i["Id"] for i in items if isinstance(i, dict) and i.get("Id")
        )
        result.append(compacted)
    return result


# Seconds before the idle binding backfill is tried again after a failure.
BINDINGS_RETRY = 300.0
# And between two idle attempts to give a held Kodi setting back.
SETTINGS_RETRY = 300.0


class Library(threading.Thread):
    def __init__(self, api, player, api_factory):
        super().__init__(name="kofin-api", daemon=True)
        creds = Credentials.load()
        self.store = Store(namespace(creds.server_id, creds.user_id))
        self.progress = progress.Progress()
        self.api = api
        self.player = player
        self.api_factory = api_factory
        self.startup_done = False
        self._reload_owed: Set[str] = set()
        self.stop_thread = False
        self._stop_event = threading.Event()
        # (command, data), and a third member on an entry that is a retry.
        self._queue: queue.Queue[Tuple[Any, ...]] = queue.Queue()
        self._catchup_due = 0.0
        self._full_due = False
        self._repair = False
        self.changefeed: Any = None
        # Libraries selected while no worker ran, enumerated once each: a
        # library the server no longer lists never reaches the whitelist and
        # must not be retried on every tick ahead of the catch-up.
        self._unsynced_tried: Set[str] = set()
        # Libraries a repair is rebuilding: their rows are leaving Kodi, and
        # nothing is enumerated until they have left (rebuild_stale, refresh).
        self._rebuilding: Set[str] = set()
        self._first_attended = False
        # The server's user data for the item this Kodi is playing, kept
        # back until it has stopped (apply_userdata): item id -> the newest
        # entry and when the item was first seen no longer playing.
        self._held_userdata: Dict[str, Tuple[dict, Optional[float]]] = {}
        self._playlists_due = False
        # What catch-ups bring that the catalogue did not hold, until the
        # pass has filed it and a toast may go up (announce.Arrivals).
        self.arrivals = announce.Arrivals()
        # When the idle binding backfill is next due: now, then never once
        # it succeeded, or a pause after a failure (apply).
        self._bindings_due = 0.0
        # When a Kodi setting a scan held and Kodi would not take back is
        # next asked for while nothing is pending (apply).
        self._settings_due = 0.0

    def stop_client(self):
        self.stop_thread = True
        self._stop_event.set()

    def workers_alive(self):
        return False

    def enqueue_command(self, command, data=None):
        if command in (
            "FastSync",
            "Reconnected",
            "SyncLibrary",
            "UpdateLibrary",
            "RepairLibrary",
            "RemoveLibrary",
            "SyncPlaylists",
            "SyncMusicPlaylists",
            "CleanupMusicPlaylists",
        ):
            self._queue.put((command, data or {}))

    def added(self, ids):
        self._queue.put(("changed", list(ids)))

    updated = added
    artwork = added

    def removed(self, ids):
        self._queue.put(("removed", list(ids)))

    def userdata(self, data):
        self._queue.put(("userdata", list(data)))

    def refresh_libraries(self, databases, force_reload=False):
        self.enqueue_command("FastSync")

    # -- the loop ----------------------------------------------------------------

    def run(self):
        monitor = xbmc.Monitor()
        try:
            # One connection to kofin.db for as long as this thread runs: it
            # is the thread that asks the store for work twice a second and
            # the one every pass runs on (private.hold_connections).
            with private.hold_connections():
                self._run(monitor)
        except BackendMismatch as error:
            self._status(settings.localized(30420) % str(error))
            LOG.warning("API sync blocked: %s", error)
        except Exception:
            LOG.exception("API video worker stopped")
        finally:
            self.stop_thread = True
            # A stop in the middle of a request leaves nobody to finish its
            # bar: the tick that would have settled it never ran.
            try:
                self._bar().finish()
            except Exception:
                LOG.debug("progress bar not taken down at the stop")

    def _run(self, monitor):
        self.store.initialize(self.api.server)
        # A rebuild a stopped service left half done is still one.
        self._rebuilding.update(self.store.rebuilding())
        native = Native(self.store, self._stop_event.is_set, bar=self._bar())
        native.setup()
        # A Kodi stopped mid-scan never gave back the setting the scan was
        # holding (Native.without_actor_thumbs); a pass would, but none may
        # be due.
        try:
            native.restore_settings()
        except Exception:
            LOG.exception("held Kodi settings not restored; the next pass retries")
        self._settings_due = time.monotonic() + SETTINGS_RETRY
        self.startup_done = True
        # Recover native work before trying to reach Jellyfin.
        while not self.stop_thread and not monitor.abortRequested():
            failed = False
            try:
                self.apply(native)
            except InterruptedError:
                break
            except Exception:
                # A failed native job must not stop a newer server edit
                # from replacing its payload, or poison jobs never heal.
                LOG.exception("API native work remains pending")
                failed = True
            # Local delivery and individual events can fail permanently
            # (e.g. an item deleted before its update event is processed).
            # Neither may prevent enumeration from repairing the snapshot.
            for work in (
                self.flush_local,
                self.commands,
                self.release_userdata,
                self.refresh,
                lambda: self.apply(native),
                self.announce_new_content,
                self.playlists_pass,
                self.flush_pending_reload,
            ):
                try:
                    work()
                except InterruptedError:
                    return
                except Exception:
                    LOG.exception("API video sync will retry")
                    self._status(settings.localized(30403))
                    self._catchup_due = min(self._catchup_due, time.monotonic() + 30)
                    failed = True
            self.settle_progress(failed)
            if failed and self._stop_event.wait(5):
                break
            if monitor.waitForAbort(0.5):
                break

    def commands(self):
        failed = False
        # Only visit each queued command once per tick, including failures.
        for _ in range(min(100, self._queue.qsize())):
            entry = self._queue.get_nowait()
            command, data = entry[0], entry[1]
            try:
                self.command(command, data, retry=len(entry) > 2)
            except InterruptedError:
                raise
            except Exception:
                LOG.exception("API video command will retry: %s", command)
                # Marked as a retry: it is not the viewer asking again, and
                # the bar must not come back up for each one while the
                # server is away (settle_progress).
                self._queue.put((command, data, True))
                failed = True
        if failed:
            raise RuntimeError("API video commands remain pending")

    def settle_progress(self, failed=False):
        """Take the bar down when what the viewer asked for is done: nothing
        queued, nothing to list, nothing pending. A tick that failed takes
        it down too; the retries that follow are not what was asked for
        and may go on for as long as the server is away."""
        bar = self._bar()
        if not bar.attended:
            return
        if failed or not (
            self._queue.qsize()
            or self._full_due
            or self._rebuilding
            or self.store.has_pending()
        ):
            bar.finish()

    def refresh(self):
        if self._rebuilding:
            if self.store.removing(self._rebuilding):
                # Listed again while its rows are still in Kodi, a library
                # is only compared with them, and they are the old rows.
                return
            self._rebuilding.clear()
        watermark, enumerated = self.store.watermark()
        unsynced = sorted(
            set(settings.get_list("librarySelection"))
            - set(private.get_sync()["Whitelist"])
            - self._unsynced_tried
        )
        if unsynced and enumerated and watermark and not self._full_due:
            # Selected while no worker was running to hear the setting change.
            self._unsynced_tried.update(unsynced)
            self._catchup_due = time.monotonic() + 30
            self._bar().attend()
            self.full_sync(unsynced)
            return
        if (
            self._full_due
            or not enumerated
            or not watermark
            or self._schedule_due(enumerated)
        ):
            # Advance before the call so failure gets a bounded retry rather
            # than keeping the already-expired deadline on every tick.
            self._catchup_due = time.monotonic() + 30
            if (
                not enumerated
                and not self._first_attended
                and settings.get_list("librarySelection")
            ):
                # The first listing of a profile is its first import, and
                # attended once: a listing that failed is tried again every
                # half minute.
                self._first_attended = True
                self._bar().attend()
            self.full_sync()
            self._full_due = False
            self._catchup_due = time.monotonic() + CATCHUP_INTERVAL
            return
        if time.monotonic() >= self._catchup_due:
            self._catchup_due = time.monotonic() + 30
            stale = self.stale_listings()
            if stale:
                # A library missing from the server's views is skipped by
                # the listing and stays stale for the next catch-up.
                LOG.info(
                    "listing %d librar%s again for fields their last listing lacked",
                    len(stale),
                    "y" if len(stale) == 1 else "ies",
                )
                self.full_sync(stale)
            self.catch_up()
            self._catchup_due = time.monotonic() + CATCHUP_INTERVAL

    def stale_listings(self) -> List[str]:
        """The synced, selected libraries whose last published listing asked
        the server for less than LISTING, or was never recorded."""
        selected = set(settings.get_list("librarySelection"))
        recorded = self.store.listings()
        return sorted(
            library
            for library in private.get_sync()["Whitelist"]
            if library in selected and recorded.get(library) != LISTING
        )

    @staticmethod
    def _schedule_due(enumerated) -> bool:
        interval = full_interval()
        return interval is not None and time.time() - enumerated >= interval

    # -- enumeration -----------------------------------------------------------------

    def _supported_views(self):
        views = self.api.views().get("Items")
        if not isinstance(views, list):
            raise ValueError("incomplete library listing")
        return {
            v["Id"]: v
            for v in views
            if v.get("Id") and (v.get("CollectionType") or "mixed") in SUPPORTED
        }

    def _status(self, value, listing=()):
        status(value, listing, self.store.namespace)

    def _changefeed(self):
        if self.changefeed is None:
            try:
                self.changefeed = changefeed.detect(self.api)
            except Exception as error:
                LOG.info("no change-feed companion detected (%s)", error)
                self.changefeed = None
            # For the status dialog: a found feed is kept for the thread's
            # life, a missing one is looked for again on every call, and the
            # record moves with the answer.
            feed = self.changefeed
            apistatus.record_feed(
                feed.tier if feed is not None else changefeed.TIER_NONE,
                self.store.namespace,
            )
        return self.changefeed

    def _server_now(self):
        feed = self._changefeed()
        if feed is not None:
            try:
                return feed.server_now()
            except Exception as error:
                LOG.warning("server clock unavailable: %s", error)
        # Without a companion the catch-up is MinDateLastSaved against the
        # local clock; a few minutes of overlap costs nothing but re-fetches.
        return int(time.time()) - 300

    def _bar(self) -> "progress.Progress":
        """The enumeration's progress bar, made on first use."""
        bar = getattr(self, "progress", None)
        if bar is None:
            bar = self.progress = progress.Progress()
        return bar

    def full_sync(self, libraries=None):
        """Enumerate the selected libraries (or the named ones) completely."""
        bar = self._bar()
        try:
            self._full_sync(libraries, bar)
        finally:
            bar.close()
        # The pass that follows says when the update is done; a listing that
        # left it nothing to do ends here. One that failed is the tick's to
        # report.
        if not (self.store.has_pending() or self._repair):
            self._status(xbmc.getLocalizedString(20177))
        else:
            self._status(settings.localized(30401))

    def _full_sync(self, libraries, bar):
        # Until the first page of the first kind arrives nothing is counted:
        # the libraries are asked for, the server's clock is read.
        bar.working(progress.PREPARING)
        selected = settings.get_list("librarySelection")
        supported = self._supported_views()
        # A library being rebuilt is listed when its old rows have left Kodi,
        # whoever asks: refresh waits for that, and a listing asked for by
        # name (SyncLibrary) must not publish it over them either.
        held = {i for i in self._rebuilding if self.store.removing([i])}
        targets = [
            library
            for library in sorted(libraries if libraries is not None else selected)
            if library in supported and library in selected and library not in held
        ]
        complete = libraries is None
        # A listing is minutes of a first import, and until it is published
        # the catalogue has no row of a library selected for the first time.
        whitelist = private.get_sync()["Whitelist"]
        self._status(
            settings.localized(30401),
            listing=[
                supported[library].get("Name", library)
                for library in targets
                if library not in whitelist
            ],
        )
        started = self._server_now()
        fetched: Dict[str, List[dict]] = {}
        for library in targets:
            rows: List[dict] = []
            for kind in collection_kinds(supported[library]):
                began = time.monotonic()
                bar.asking(kind)
                found = fetch_kind(
                    self.api,
                    library,
                    kind,
                    self._stop_event.is_set,
                    on_page=lambda done, total, kind=kind: bar.track(kind, done, total),
                    api_factory=getattr(self, "api_factory", None),
                )
                LOG.info(
                    "enumerated %d %s in %.1f s",
                    len(found),
                    kind,
                    time.monotonic() - began,
                )
                rows.extend(found)
            fetched[library] = rows
        # The collections, the catalogue and the node tree follow, with
        # nothing to count.
        bar.working(progress.SAVING)
        movies_selected = any(
            "Movie" in collection_kinds(supported[library])
            for library in selected
            if library in supported
        )
        boxsets = (
            fetch_boxsets(self.api, self._stop_event.is_set)
            if movies_selected
            and (
                complete
                or any(
                    "Movie" in collection_kinds(supported[library])
                    for library in targets
                )
            )
            else []
        )
        # Enumeration takes minutes on a real catalogue. A library deselected
        # meanwhile is not published: its RemoveLibrary is already queued, and
        # publishing it here would import every item only to remove it again.
        selected = settings.get_list("librarySelection")
        committed = [library for library in fetched if library in selected]
        if not committed and not (complete and boxsets):
            return
        items: List[dict] = []
        membership: Dict[str, str] = {}
        for library in committed:
            for item in fetched[library]:
                if item["Id"] in membership:
                    continue
                membership[item["Id"]] = library
                items.append(item)
        invalidate = self._collection_changes(boxsets, complete)
        began = time.monotonic()
        self.publish(
            items + boxsets,
            membership=membership,
            complete_libraries=committed,
            complete_boxsets=complete and movies_selected,
        )
        LOG.info(
            "published %d items in %.1f s",
            len(items) + len(boxsets),
            time.monotonic() - began,
        )
        del items, fetched
        memory.release()
        self.store.invalidate(invalidate)
        # Only what was listed and published: a selected library the server
        # did not show, or one being rebuilt, keeps its old record.
        self.store.remember_listing(committed, LISTING)
        if complete:
            self.store.set_watermark(
                watermark=changefeed.unix_to_watermark(started), enumerated=time.time()
            )
            self._repair = True
        for library in committed:
            state = private.get_sync()
            if library not in state["Whitelist"]:
                state["Whitelist"].append(library)
                if not self.store.has_rows(library):
                    # Nothing of it is in Kodi yet, so every row it gets is
                    # filed by this build's listing.
                    self.store.set_scanner_format(
                        library, metadata.scanner_format(capabilities.features())
                    )
            private.save_sync(state)
            with private.Database() as db:
                db.cursor.execute(
                    "INSERT OR REPLACE INTO view VALUES (?,?,?)",
                    (
                        library,
                        supported[library].get("Name", library),
                        supported[library].get("CollectionType") or "mixed",
                    ),
                )
        self.publish_selection()
        # The playlists follow the catalogue they point into.
        self._playlists_due = True

    def _collection_changes(self, boxsets, complete):
        """Movies whose one native set changes with these collections."""
        before = {
            r.item_id: set(r.item.get("KofinMembers") or [])
            for r in self.store.records(kind="BoxSet", pinned=False).values()
        }
        after = {b["Id"]: set(b.get("KofinMembers") or []) for b in boxsets}
        affected: Set[str] = set()
        for boxset_id, members in after.items():
            affected |= members ^ before.get(boxset_id, set())
        if complete:
            for boxset_id, members in before.items():
                if boxset_id not in after:
                    affected |= members
        return affected

    def _removed_collection_members(self, ids):
        """Movies filed under a collection that is going: their one set changes."""
        boxsets = self.store.records(kind="BoxSet", item_ids=list(ids), pinned=False)
        members: Set[str] = set()
        for record in boxsets.values():
            members.update(record.item.get("KofinMembers") or [])
        return members

    def catch_up(self):
        """Changes since the watermark, on the shared change-feed contract."""
        watermark, _ = self.store.watermark()
        if not watermark:
            self.full_sync()
            return
        selected = settings.get_list("librarySelection")
        supported = self._supported_views()
        selected = [library for library in selected if library in supported]
        feed = self._changefeed()
        if feed is None:
            self._catch_up_by_date(watermark, selected, supported)
            return
        change_set = feed.changes(watermark, CHANGEFEED_TYPES, selected)
        if changefeed.retention_overrun(
            watermark, change_set.envelope.retention_cutoff
        ):
            LOG.warning("sync queue retention exceeded; scheduling a full enumeration")
            self._full_due = True
        removed = [r.id for r in change_set.records if r.status == "Removed"]
        self.store.invalidate(self._removed_collection_members(removed))
        wanted = {}
        for record in change_set.records:
            if record.status == "Removed":
                continue
            if record.library_ids is not None and not set(record.library_ids) & set(
                selected
            ):
                if self.store.entry(record.id) is None:
                    continue
            wanted[record.id] = record
        items, membership = self._resolve(list(wanted), selected, supported, wanted)
        self.arrivals.published(
            items, self.store.known(i["Id"] for i in items), membership
        )
        self.publish(items, membership=membership, removed=removed)
        self.apply_userdata(change_set.userdata)
        if change_set.envelope.server_time:
            self.store.set_watermark(
                watermark=changefeed.unix_to_watermark(change_set.envelope.server_time)
            )

    def _catch_up_by_date(self, watermark, selected, supported):
        started = int(time.time()) - 300
        items: List[dict] = []
        membership: Dict[str, str] = {}
        for library in sorted(selected):
            for kind in collection_kinds(supported[library]):
                for item in fetch_kind(
                    self.api,
                    library,
                    kind,
                    self._stop_event.is_set,
                    {"MinDateLastSaved": watermark},
                ):
                    if item["Id"] not in membership:
                        membership[item["Id"]] = library
                        items.append(item)
        boxsets: List[dict] = []
        if any("Movie" in collection_kinds(supported[library]) for library in selected):
            changed = self.api.items(
                {
                    "userId": self.api.user_id,
                    "IncludeItemTypes": "BoxSet",
                    "Recursive": True,
                    "MinDateLastSaved": watermark,
                    "Fields": "Etag",
                }
            ).get("Items")
            if isinstance(changed, list):
                boxsets = fetch_boxsets(
                    self.api,
                    self._stop_event.is_set,
                    [b["Id"] for b in changed if isinstance(b, dict) and b.get("Id")],
                )
        invalidate = self._collection_changes(boxsets, False)
        self.arrivals.published(
            items, self.store.known(i["Id"] for i in items), membership
        )
        self.publish(items + boxsets, membership=membership)
        self.store.invalidate(invalidate)
        self.store.set_watermark(watermark=changefeed.unix_to_watermark(started))

    def fetch_items(self, ids):
        """Full DTOs by id; the server lists only what this user may see."""
        result: List[dict] = []
        ids = list(ids)
        for start in range(0, len(ids), 50):
            chunk = ids[start : start + 50]
            page = self.api.items(
                {"userId": self.api.user_id, "Ids": ",".join(chunk), "Fields": FIELDS}
            )
            rows = page.get("Items")
            if not isinstance(rows, list):
                raise ValueError("incomplete item fetch")
            result.extend(
                metadata.compact(i)
                for i in rows
                if isinstance(i, dict) and i.get("Id") and i.get("Name")
            )
        attach_extras(self.api, result)
        return result

    def _library_of(self, item, selected, hints=None):
        """The selected library an item belongs to, or None."""
        entry = self.store.entry(item["Id"])
        if entry is not None and entry.library in selected:
            return entry.library
        for hint in hints or ():
            if hint in selected:
                return hint
        if item.get("SeriesId"):
            parent = self.store.entry(item["SeriesId"])
            if parent is not None and parent.library in selected:
                return parent.library
        if item.get("AlbumId"):
            parent = self.store.entry(item["AlbumId"])
            if parent is not None and parent.library in selected:
                return parent.library
        for ancestor in self.api.ancestors(item["Id"]):
            if isinstance(ancestor, dict) and ancestor.get("Id") in selected:
                return ancestor["Id"]
        return None

    def _resolve(self, ids, selected, supported, hints=None):
        """Fetch changed items and place each in its selected library."""
        items = []
        membership: Dict[str, str] = {}
        boxsets = []
        for item in self.fetch_items([i for i in ids]):
            kind = item.get("Type")
            if kind == "BoxSet":
                boxsets.append(item["Id"])
                continue
            if kind not in (
                "Movie",
                "Series",
                "Season",
                "Episode",
                "MusicVideo",
                "MusicArtist",
                "MusicAlbum",
                "Audio",
            ):
                continue
            hint = hints.get(item["Id"]) if hints else None
            library = self._library_of(
                item, selected, hint.library_ids if hint is not None else None
            )
            if library is None or kind not in collection_kinds(supported[library]):
                continue
            membership[item["Id"]] = library
            items.append(item)
        if boxsets:
            sets = fetch_boxsets(self.api, self._stop_event.is_set, boxsets)
            self.store.invalidate(self._collection_changes(sets, False))
            items.extend(sets)
        return items, membership

    def _playing(self) -> str:
        """The item this Kodi is playing through kofin, or ''."""
        item = self.player.current_item() if self.player is not None else None
        return str((item or {}).get("Id") or "")

    def apply_userdata(self, data):
        """Merge the server's user data into the catalogue, except for the
        item this Kodi is playing.

        Kodi's player owns the row of what it plays. That something came
        for it is kept, and the entry the server sends when the playback
        stops is applied in its place; when none comes, the server is asked
        a while after the stop (release_userdata).
        """
        playing = self._playing()
        items = []
        settled = []
        for entry in data:
            item_id = entry.get("ItemId") if isinstance(entry, dict) else entry
            if not item_id:
                continue
            if isinstance(entry, dict) and item_id == playing:
                self._held_userdata[item_id] = (entry, None)
                continue
            settled.append(item_id)
            item = self.store.item(item_id)
            if item is None:
                continue
            item["UserData"] = dict(item.get("UserData") or {}, **entry)
            items.append(item)
        if items:
            self.store.publish(items)
        # Only now. A release has no queue behind it: dropped before a
        # publish that failed, its entry was gone for good.
        for item_id in settled:
            self._held_userdata.pop(item_id, None)

    def publish(self, items, **placement):
        """Publish full items as the server sent them, all but the user data
        of the item this Kodi is playing, which is held like an entry sent
        for it (apply_userdata) while the item keeps what the catalogue has.
        """
        playing = self._playing()
        if playing:
            for item in items:
                if item.get("Id") != playing:
                    continue
                known = self.store.item(playing)
                if known is not None and isinstance(item.get("UserData"), dict):
                    self._held_userdata[playing] = (
                        dict(item["UserData"], ItemId=playing),
                        None,
                    )
                    item["UserData"] = known.get("UserData") or {}
                break
        self.store.publish(items, **placement)

    def announce_new_content(self):
        """Announce what the pass filed of the new content catch-ups brought."""
        self.arrivals.flush(self.store, self.api, self.player)

    def release_userdata(self):
        """Once an item has stopped and the server has had time to settle
        the play, ask it what the play made of the item.

        The last entry heard may be a progress report, and the entry the
        stop sent may never arrive.
        """
        if not self._held_userdata:
            return
        playing = self._playing()
        now = time.monotonic()
        due = []
        for item_id, (entry, stopped) in list(self._held_userdata.items()):
            if item_id == playing:
                self._held_userdata[item_id] = (entry, None)
            elif stopped is None:
                self._held_userdata[item_id] = (entry, now)
            elif now - stopped >= HELD_USERDATA_GRACE:
                due.append(item_id)
        if not due:
            return
        try:
            page = self.api.items({"userId": self.api.user_id, "Ids": ",".join(due)})
            rows = page.get("Items")
            if not isinstance(rows, list):
                raise ValueError("incomplete item fetch")
        except Exception as error:
            # Held, and asked again a grace period on: the server may stay
            # away for as long as it likes, and this is no failure of a tick.
            LOG.debug("user data of %d finished play(s) not read: %s", len(due), error)
            for item_id in due:
                self._held_userdata[item_id] = (self._held_userdata[item_id][0], now)
            return
        fresh = {
            row["Id"]: dict(row.get("UserData") or {}, ItemId=row["Id"])
            for row in rows
            if isinstance(row, dict) and row.get("Id")
        }
        for item_id in due:
            if item_id not in fresh:
                # The server no longer lists it; its removal is another path's.
                self._held_userdata.pop(item_id, None)
        # apply_userdata drops each from the held set once it is published.
        self.apply_userdata([fresh[item_id] for item_id in due if item_id in fresh])

    def publish_selection(self):
        """The selection moved: the node tree and the skin entries follow it."""
        whitelist = private.get_sync()["Whitelist"]
        with private.Database() as db:
            names = [
                name
                for key, name in db.cursor.execute(
                    "SELECT view_id, view_name FROM view ORDER BY view_name"
                ).fetchall()
                if key in whitelist
            ]
        LOG.info("synced libraries: %s", ", ".join(names) or "none")
        try:
            from kofin.sync.dynamic import publish

            publish(self.api.views().get("Items", []), self.api)
        except Exception:
            LOG.exception("library nodes not republished")

    # -- commands ----------------------------------------------------------------------

    def drop_libraries(self, libraries):
        """Tombstone every item of these libraries and take them off the
        whitelist; the next pass removes their rows."""
        for library in libraries:
            self.store.publish([], library=library)
            state = private.get_sync()
            state["Whitelist"] = [i for i in state["Whitelist"] if i != library]
            private.save_sync(state)
        self.publish_selection()
        # The playlists of a side whose last library left are pruned
        # by the next pass (backends/api/playlists.reconcile).
        self._playlists_due = True

    def rebuild_stale(self, picked):
        """Remove the rows of every picked video library filed under an
        older scanner format, so the enumeration that follows imports them
        again; returns those libraries.

        What the format numbers (metadata.SCANNER_FORMAT) has no setter, so
        a row filed without it gets it only by being filed again, and
        removing the rows and scanning the root is a first import, far
        cheaper than a refresh per row. It is the viewer's to ask for, never
        a pass's: the library is gone from Kodi until the import has run. A
        music library has no such rows and is repaired by comparison.
        """
        whitelist = private.get_sync()["Whitelist"]
        with private.Database() as db:
            media = dict(
                db.cursor.execute("SELECT view_id, media_type FROM view").fetchall()
            )
        # The format this Kodi's listing reaches: a library filed at a lower
        # one is stale here, never one filed at the format this Kodi lacks.
        current = metadata.scanner_format(capabilities.features())
        stale = [
            library
            for library in sorted(picked or whitelist)
            if library in whitelist
            and media.get(library) != "music"
            and self.store.scanner_format(library) < current
        ]
        rebuild = [library for library in stale if self.store.has_rows(library)]
        for library in stale:
            if library not in rebuild:
                # Nothing was ever filed for it; whatever comes is current.
                self.store.set_scanner_format(library, current)
        if rebuild:
            LOG.info(
                "repair: %d library(ies) filed before scanner format %d;"
                " their rows are removed and imported again: %s",
                len(rebuild),
                current,
                ", ".join(rebuild),
            )
            # Recorded before the rows are tombstoned: a service stopped
            # between the two must not list the library over its old rows.
            for library in rebuild:
                self.store.set_scanner_format(library, REBUILDING)
            # And off the whitelist before a row is tombstoned: a service
            # stopped after this write leaves it selected and not synced,
            # listed again over the rows it still has, and still asking for
            # its repair.
            state = private.get_sync()
            state["Whitelist"] = [i for i in state["Whitelist"] if i not in rebuild]
            private.save_sync(state)
            self.drop_libraries(rebuild)
            self._rebuilding.update(rebuild)
        return rebuild

    def command(self, command, data, retry=False):
        if command in ATTENDED and not retry:
            self._bar().attend()
        if command == "RemoveLibrary":
            self.drop_libraries([i for i in data.get("Id", "").split(",") if i])
            return
        if command == "removed":
            self.store.invalidate(self._removed_collection_members(data))
            self.store.publish([], removed=data)
            return
        if command == "userdata":
            self.apply_userdata(data)
            return
        if command == "changed":
            known = [i for i in data if self.store.entry(i) is not None]
            if len(known) < len(data):
                # A new item: its library is the catch-up's to decide.
                self._catchup_due = 0
            if known:
                selected = settings.get_list("librarySelection")
                items, membership = self._resolve(
                    known, selected, self._supported_views()
                )
                if items:
                    self.publish(items, membership=membership)
            return
        if command == "SyncLibrary":
            if not self.store.watermark()[1]:
                # Nothing has ever been listed whole, so the refresh that
                # follows in this tick lists every selected library, this
                # one among them.
                self._full_due = True
                return
            self.full_sync([i for i in data.get("Id", "").split(",") if i])
            return
        if command == "SyncPlaylists":
            playlist_id = (data or {}).get("Id") or ""
            if playlist_id:
                api_playlists.apply(self.api, self.store, playlist_id)
                self.reconcile_subscriptions(playlist_id)
            else:
                self._playlists_due = True
            return
        if command == "SyncMusicPlaylists":
            self._playlists_due = True
            return
        if command == "CleanupMusicPlaylists":
            # Queued by the switch going off; by the time it runs the switch
            # may be on again and the pass may have written the playlists
            # back, so the setting is read again here.
            if api_playlists.wanted():
                LOG.info("playlist cleanup skipped: the switch is on again")
                return
            api_playlists.cleanup()
            return
        if command == "FastSync":
            self._catchup_due = 0
            return
        if command == "Reconnected":
            # The server that came back may have gained or lost a companion
            # (a plugin install is a server restart), and a found feed is
            # otherwise kept for the thread's life: the catch-up that runs
            # now detects it again, and the status row moves with it.
            self.changefeed = None
            self._catchup_due = 0
            return
        rebuilt = (
            self.rebuild_stale([i for i in (data or {}).get("Id", "").split(",") if i])
            if command == "RepairLibrary"
            else []
        )
        self._full_due = True
        # A rebuild's first pass only removes; the pass after the complete
        # enumeration that follows is the repair (full_sync).
        self._repair = self._repair or (command == "RepairLibrary" and not rebuilt)

    # -- native application -----------------------------------------------------------

    def apply(self, native):
        if self.store.has_pending() or self._repair:
            self._status(settings.localized(30401))
            populated = {kind: self.store.populated(kind) for kind in HAS_CONTENT}
            began = time.monotonic()
            try:
                native.reconcile(repair=self._repair)
                LOG.info("native pass done in %.1f s", time.monotonic() - began)
            finally:
                # A home widget whose last fetch found nothing is deaf to
                # every later library announcement, and the skin's widget
                # sections bake their Library.HasContent gate at window load.
                # Only a skin reload shows content that arrived into an empty
                # library, so the empty -> populated transition owes one.
                for kind, before in populated.items():
                    if not before and self.store.populated(kind):
                        self._reload_owed.add(kind)
            self._repair = False
            self._status(xbmc.getLocalizedString(20177))
            # The playlists and the skin's reload follow, counting nothing.
            self._bar().working(progress.WAITING)
            self.flush_pending_reload()
        elif time.monotonic() >= self._bindings_due:
            # Nothing pending, so no pass: the movie folders an older
            # profile's rows never had bound are bound here, once per
            # service generation (Native.bind_movie_folders); a failure
            # (Kodi's RPC away) is retried after a pause, not every tick.
            try:
                native.bind_movie_folders()
            except Exception:
                LOG.exception("movie folder bindings not completed")
                self._bindings_due = time.monotonic() + BINDINGS_RETRY
            else:
                self._bindings_due = float("inf")
        elif native.settings_owed and time.monotonic() >= self._settings_due:
            # A pass gives a held setting back when it starts, and none may
            # come for hours: the user's "Download actor thumbnails" must
            # not stay off for that long because one call was refused.
            self._settings_due = time.monotonic() + SETTINGS_RETRY
            native.restore_settings()

    def playlists_pass(self):
        """Rewrite the managed playlist files after an enumeration or a
        request, once the pass has filed the rows they point at."""
        if not self._playlists_due:
            return
        self._playlists_due = False
        try:
            stats = api_playlists.reconcile(self.api, self.store)
        except Exception:
            LOG.exception("playlists not reconciled; next enumeration retries")
        else:
            if stats:
                LOG.info("playlists reconciled: %s", stats)
        self.reconcile_subscriptions()

    def reconcile_subscriptions(self, playlist_id=""):
        """Bring the music-library and playlist download subscriptions to the
        server's listing: one playlist's when a request names it, every
        subscription's after a complete enumeration and when the viewer
        subscribes."""
        from kofin.downloads import subscriptions

        try:
            if playlist_id:
                subscriptions.reconcile_playlist_direct(self.api, playlist_id)
            else:
                subscriptions.reconcile_music_libraries(self.api)
                subscriptions.reconcile_playlists_direct(self.api)
        except Exception:
            LOG.exception("download subscriptions not reconciled")

    def flush_pending_reload(self):
        """Fire the owed first-content skin reload once nothing is playing."""
        if not self._reload_owed:
            return
        if self.player is not None and self.player.isPlayingVideo():
            # A reload rebuilds the OSD under the viewer; the tick retries.
            return
        monitor = xbmc.Monitor()
        # The scan cycle re-samples Kodi's cached HasContent bool; a reload
        # against the stale value becomes right only on the next one.
        for kind in sorted(self._reload_owed):
            flag = "Library.HasContent(%s)" % HAS_CONTENT[kind]
            for _ in range(40):
                if xbmc.getCondVisibility(flag):
                    break
                if monitor.waitForAbort(0.25):
                    return
            else:
                LOG.warning("%s did not flip; reloading anyway", flag)
        self._reload_owed.clear()
        LOG.info("first content synced; reloading skin for home widgets")
        xbmc.executebuiltin("ReloadSkin()")

    def flush_local(self):
        """Tell the server what was marked or played in Kodi.

        Each edit on its own: an edit of an item the server does not have
        (404) is dropped, and any other failure is raised once the rest have
        been delivered.
        """
        failure: Optional[Exception] = None
        for item_id, values in self.store.local_pending():
            try:
                self._deliver_local(item_id, values)
            except InterruptedError:
                raise
            except HttpError as error:
                if error.status == 404:
                    LOG.info(
                        "play state of %s not sent: the server no longer has it",
                        item_id,
                    )
                    self.store.local_done(item_id, values)
                    continue
                failure = failure or error
            except Exception as error:
                failure = failure or error
        if failure is not None:
            raise failure

    def _deliver_local(self, item_id, values):
        payload: Dict[str, Any] = {}
        if "playcount" in values:
            # The watched mark and never the count: the server and Kodi each
            # count their own plays.
            payload["Played"] = values["playcount"] > 0
        if "position" in values:
            payload["PlaybackPositionTicks"] = int(values["position"] * 10000000)
        # The partial setter is idempotent across an interrupted reply;
        # MarkPlayed would increment the server count again on retry.
        self.api.update_user_data(item_id, payload)
        # Re-fetch the authoritative full DTO before acknowledging the
        # local outbox; a failed read remains replayable.
        item = metadata.compact(self.api.item(item_id))
        attach_extras(self.api, [item])
        if self.store.entry(item_id) is not None:
            self.publish([item])
        self.store.local_done(item_id, values)
