"""Movie-only API coordinator. Network enumeration precedes publication.

One worker serializes snapshots and native jobs. Callbacks enqueue intent and
never wait on Kodi scans. Every restart reconciles complete selected libraries;
native operations already published are replayed even if Jellyfin is offline.
"""

import queue
import threading
import time
from typing import Any

import xbmc

from kofin.core import settings
from kofin.core.log import Logger
from kofin.core.settings import Credentials
from kofin.sync import private
from kofin.sync.catalogue import BackendMismatch
from kofin.sync.downloader import info, library_filter
from .movies import Movies
from .store import MovieStore, namespace

LOG = Logger(__name__)


def status(value):
    # Every setting write rewrites settings.xml and fires a settings callback.
    if settings.get_str("syncStatus") != value:
        settings.set_str("syncStatus", value)


def fetch_movies(api, library, abort=lambda: False):
    """Only a complete, stable, duplicate-free pagination can imply deletion."""
    result: list[dict] = []
    seen = set()
    total = None
    while total is None or len(result) < total:
        if abort():
            raise InterruptedError("movie enumeration stopped")
        page = api.items(
            {
                **library_filter(api, library, "Movie"),
                "Fields": info() + ",MediaStreams",
                "StartIndex": len(result),
                "Limit": 200,
                "SortBy": "Id",
                "SortOrder": "Ascending",
                "EnableTotalRecordCount": True,
            }
        )
        count = page.get("TotalRecordCount")
        rows = page.get("Items")
        if not isinstance(count, int) or count < 0 or not isinstance(rows, list):
            raise ValueError("incomplete movie page")
        if total is not None and count != total:
            raise ValueError("movie listing changed during enumeration")
        total = count
        for item in rows:
            if (
                not isinstance(item, dict)
                or not item.get("Id")
                or item.get("Type") != "Movie"
                or not item.get("Name")
                or item["Id"] in seen
            ):
                raise ValueError("invalid or duplicate movie")
            seen.add(item["Id"])
            result.append(item)
        if len(result) > total or (not rows and len(result) < total):
            raise ValueError("short movie page")
    return result


class Library(threading.Thread):
    def __init__(self, api, player, api_factory):
        super().__init__(name="kofin-api-movies", daemon=True)
        creds = Credentials.load()
        self.store = MovieStore(namespace(creds.server_id, creds.user_id))
        self.api = api
        self.api_factory = api_factory
        self.startup_done = False
        self.stop_thread = False
        self._stop_event = threading.Event()
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._refresh_due = 0.0
        self._repair = False

    def stop_client(self):
        self.stop_thread = True
        self._stop_event.set()

    def workers_alive(self):
        return False

    def enqueue_command(self, command, data=None):
        if command in (
            "FastSync",
            "SyncLibrary",
            "UpdateLibrary",
            "RepairLibrary",
            "RemoveLibrary",
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

    def run(self):
        monitor = xbmc.Monitor()
        try:
            self.store.initialize(self.api.server)
            movies = Movies(self.store, self._stop_event.is_set)
            movies.setup()
            self.startup_done = True
            # Recover native work before trying to reach Jellyfin.
            while not self.stop_thread and not monitor.abortRequested():
                failed = False
                try:
                    self.apply(movies)
                except InterruptedError:
                    break
                except Exception:
                    # A failed native job must not stop a newer server edit
                    # from replacing its payload, or poison jobs never heal.
                    LOG.exception("API native movie work remains pending")
                    failed = True
                try:
                    self.flush_local()
                    for _ in range(100):
                        try:
                            command, data = self._queue.get_nowait()
                        except queue.Empty:
                            break
                        try:
                            self.command(command, data)
                        except Exception:
                            self._queue.put((command, data))
                            raise
                    if time.monotonic() >= self._refresh_due:
                        self.full_sync()
                        self._refresh_due = time.monotonic() + 300
                    self.apply(movies)
                except InterruptedError:
                    break
                except Exception:
                    LOG.exception("API movie sync will retry")
                    status(settings.localized(30403))
                    # Replay errors leave durable desired state intact. Network
                    # failures schedule a complete enumeration after backoff.
                    self._refresh_due = time.monotonic() + 30
                    failed = True
                if failed and self._stop_event.wait(5):
                    break
                if monitor.waitForAbort(0.5):
                    break
        except BackendMismatch as error:
            status(settings.localized(30420) % str(error))
            LOG.warning("API sync blocked: %s", error)
        except Exception:
            LOG.exception("API movie worker stopped")
        finally:
            self.stop_thread = True

    def full_sync(self):
        selected = settings.get_list("librarySelection")
        views = self.api.views().get("Items")
        if not isinstance(views, list):
            raise ValueError("incomplete library listing")
        supported = {
            v["Id"]: v
            for v in views
            if v.get("CollectionType") in ("movies", "mixed", None, "")
        }
        complete = {}
        membership: dict[str, str] = {}
        committed = []
        for library in sorted(selected):
            if library not in supported:
                # Absence/access loss is not consent to remove a native library.
                continue
            items = fetch_movies(self.api, library, self._stop_event.is_set)
            for item in items:
                complete[item["Id"]] = item
                membership.setdefault(item["Id"], library)
            committed.append(library)
        if not committed:
            return
        # A later selected library may contain a movie moved out of an earlier
        # one. Publish their union only after every enumeration succeeded.
        self.store.publish(
            list(complete.values()), membership=membership, complete_libraries=committed
        )
        self._repair = True
        for library in committed:
            state = private.get_sync()
            if library not in state["Whitelist"]:
                state["Whitelist"].append(library)
            private.save_sync(state)
            with private.Database() as db:
                db.cursor.execute(
                    "INSERT OR REPLACE INTO view VALUES (?,?,?)",
                    (library, supported[library].get("Name", library), "movies"),
                )
        self.update_selection_label()

    def update_selection_label(self):
        whitelist = private.get_sync()["Whitelist"]
        with private.Database() as db:
            names = [
                name
                for key, name in db.cursor.execute(
                    "SELECT view_id, view_name FROM view ORDER BY view_name"
                ).fetchall()
                if key in whitelist
            ]
        label = ", ".join(names)
        if settings.get_str("syncedLibraries") != label:
            settings.set_str("syncedLibraries", label)

    def command(self, command, data):
        if command == "RemoveLibrary":
            # Reassign shared movies to their remaining selected library before
            # deleting a membership. A failed fetch leaves the old view intact.
            self.full_sync()
            for library in data.get("Id", "").split(","):
                if library:
                    self.store.publish([], library=library)
                    state = private.get_sync()
                    state["Whitelist"] = [i for i in state["Whitelist"] if i != library]
                    private.save_sync(state)
            self.update_selection_label()
            return
        if command == "removed":
            self.store.publish([], removed=data)
            return
        if command in ("changed", "userdata"):
            records, _, _ = self.store.snapshot(pinned=False)
            items = []
            for entry in data:
                item_id = entry.get("ItemId") if isinstance(entry, dict) else entry
                if item_id in records:
                    if command == "userdata":
                        item = dict(records[item_id]["item"])
                        item["UserData"] = dict(item.get("UserData") or {}, **entry)
                    else:
                        item = self.api.item(item_id)
                    if item.get("Type") == "Movie":
                        items.append(item)
                else:
                    self._refresh_due = 0
            if items:
                self.store.publish(items)
            return
        self._refresh_due = 0
        self._repair = self._repair or command == "RepairLibrary"

    def apply(self, movies):
        if self.store.pending() or self._repair:
            status(settings.localized(30401))
            movies.reconcile(repair=self._repair)
            self._repair = False
            status(xbmc.getLocalizedString(20177))

    def flush_local(self):
        for item_id, values in self.store.local_pending():
            payload: dict[str, Any] = {}
            if "playcount" in values:
                payload.update(
                    Played=values["playcount"] > 0, PlayCount=values["playcount"]
                )
            if "position" in values:
                payload["PlaybackPositionTicks"] = int(values["position"] * 10000000)
            # The partial setter is idempotent across an interrupted reply;
            # MarkPlayed would increment the server count again on retry.
            self.api.update_user_data(item_id, payload)
            # Re-fetch the authoritative full DTO before acknowledging the
            # local outbox; a failed read remains replayable.
            item = self.api.item(item_id)
            self.store.publish([item])
            self.store.local_done(item_id, values)
