"""Confirmed movie operations through stock Piers public interfaces."""

import json
import time
from urllib.parse import unquote

import xbmc

from kofin.core import kodirpc
from kofin.sync.catalogue import BackendMismatch
from . import metadata
from .store import MovieStore, directory, identity, playback_url

PROPERTIES = [
    "file",
    "uniqueid",
    "title",
    "plot",
    "playcount",
    "lastplayed",
    "resume",
    "tag",
    "art",
    "ratings",
    "originaltitle",
    "sorttitle",
    "plotoutline",
    "tagline",
    "year",
    "premiered",
    "mpaa",
    "runtime",
    "genre",
    "studio",
    "country",
    "director",
    "writer",
    "dateadded",
    "trailer",
]


def rpc(method, params=None):
    result = kodirpc.call(method, params or {})
    if result is None or result is kodirpc.FAILED:
        raise RuntimeError("Kodi refused " + method)
    return result


class Monitor(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        self.finished = 0

    def onScanFinished(self, library):
        if library.lower() == "video":
            self.finished += 1


class Movies:
    def __init__(self, store: MovieStore, abort=lambda: False):
        self.store = store
        self.key = store.namespace
        self.abort = abort
        self.monitor = Monitor()
        self._async_pending = False

    def setup(self):
        owner = self.store.prepared()
        if owner == self.key:
            return
        if owner:
            raise BackendMismatch(
                "native library belongs to another server/user; use a fresh profile"
            )
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

    def read(self):
        reply = rpc(
            "VideoLibrary.GetMovies",
            {
                "properties": PROPERTIES,
                "filter": {
                    "field": "path",
                    "operator": "startswith",
                    "value": directory(self.key),
                },
            },
        )
        if not isinstance(reply, dict) or "limits" not in reply:
            raise RuntimeError("incomplete native movie readback")
        rows = reply.get("movies", [])
        if len(rows) != reply["limits"]["total"]:
            raise RuntimeError("partial native movie readback")
        result = {}
        for row in rows:
            uid = row.get("uniqueid", {}).get("kofin", "")
            if not uid.startswith(self.key + ":"):
                continue
            item_id = uid[len(self.key) + 1 :]
            if row.get("file") != playback_url(self.key, item_id):
                continue
            if item_id in result:
                raise RuntimeError("duplicate owned movie identity")
            result[item_id] = row
        return result

    def owned(self, kodi_id, item_id):
        row = rpc(
            "VideoLibrary.GetMovieDetails",
            {"movieid": kodi_id, "properties": PROPERTIES},
        ).get("moviedetails", {})
        if row.get("file") != playback_url(self.key, item_id) or row.get(
            "uniqueid", {}
        ).get("kofin") != identity(self.key, item_id):
            raise RuntimeError("movie ownership changed")
        return row

    def _idle(self):
        return not xbmc.getCondVisibility("Library.IsScanningVideo")

    def wait(self, predicate, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.abort() or self.monitor.waitForAbort(0.1):
                raise InterruptedError("movie operation interrupted")
            value = predicate()
            if value:
                return value
        raise TimeoutError("native movie operation did not converge")

    def scan(self, records):
        self.wait(self._idle)
        rpc(
            "VideoLibrary.SetSourceContent",
            {
                "path": directory(self.key),
                "content": "movies",
                "scraperid": "metadata.local",
                "scanrecursive": False,
                "refresh": False,
            },
        )
        for item_id, record in records.items():
            self.store.expect(
                item_id, record["generation"], metadata.userdata(record["item"])
            )
        serial = self.monitor.finished
        rpc(
            "VideoLibrary.Scan",
            {"directory": directory(self.key), "showdialogs": False},
        )
        self._async_pending = True
        self.wait(lambda: self.monitor.finished > serial and self._idle())
        self._async_pending = False

    def reconcile(self, repair=False):
        """Replay durable operations; an accepted RPC is never a commit."""
        self.setup()
        # A prior process may have died with its pin held. Wait for Kodi to
        # finish using it before publishing the newer desired view to a scan.
        self.wait(self._idle)
        records, server, _ = self.store.pin()
        completed = False
        try:
            pending = {
                item.item_id: (generation, operation, item.payload)
                for item, generation, operation in self.store.pending()
            }
            if repair:
                for i, r in records.items():
                    pending[i] = (r["generation"], "upsert", r["item"])
            native = self.read_pending(pending, repair)
            if any(
                i not in native
                for i, (_, op, _) in pending.items()
                if op == "upsert" and i in records
            ):
                self.scan(records)
                native = self.read()
            errors = []
            local = {item_id for item_id, _ in self.store.local_pending()}
            for item_id, (generation, operation, item) in pending.items():
                try:
                    if self.abort():
                        raise InterruptedError("movie sync stopped")
                    if operation == "remove":
                        row = native.get(item_id)
                        if row:
                            self.owned(row["movieid"], item_id)
                            rpc("VideoLibrary.RemoveMovie", {"movieid": row["movieid"]})
                            if item_id in self.read():
                                raise RuntimeError("movie removal not confirmed")
                        self.store.remember(item_id, generation, None, {})
                        continue
                    record = records.get(item_id)
                    if not record:
                        continue
                    # Finish the immutable generation Kodi may still be
                    # consuming after a timeout. A stale acknowledgement is
                    # refused; the newer desired state runs after unpinning.
                    generation, item = record["generation"], record["item"]
                    row = native.get(item_id)
                    if not row:
                        raise RuntimeError("scanner did not import movie")
                    desired = metadata.details(
                        item, server, self.key, record["library"]
                    )
                    if item_id in local:
                        # Deliver the real user edit first. Never overwrite it
                        # with an older server snapshot during a retry/refresh.
                        continue
                    mapping = self.store.mapping(item_id)
                    previous_item = (
                        json.loads(mapping[1]) if mapping and mapping[1] else {}
                    )
                    previous_details = metadata.details(
                        dict(previous_item, Id=item_id),
                        server,
                        self.key,
                        record["library"],
                    )
                    old_tags = set(previous_details["tag"]) | {
                        tag
                        for tag in row.get("tag", [])
                        if tag.startswith("kofin.library.")
                    }
                    desired["tag"] = sorted(
                        set(desired["tag"]) | (set(row.get("tag", [])) - old_tags)
                    )
                    for field in ("art", "ratings", "uniqueid"):
                        # Setters merge maps. Clear only keys previously owned
                        # by Kofin, retaining Kodi defaults and local additions.
                        owned = set(previous_details[field])
                        desired[field].update(
                            {key: None for key in owned if key not in desired[field]}
                        )
                        desired[field].update(
                            {
                                key: value
                                for key, value in row.get(field, {}).items()
                                if key not in desired[field]
                                and key not in owned
                                and key != "icon"
                            }
                        )
                    desired["art"] = {
                        key: (
                            unquote(value[8:].rstrip("/"))
                            if isinstance(value, str) and value.startswith("image://")
                            else value
                        )
                        for key, value in desired["art"].items()
                    }
                    if mapping and mapping[1]:
                        previous = metadata.userdata(json.loads(mapping[1]))
                        edits = {}
                        if (
                            row.get("playcount", 0) != previous["playcount"]
                            and row.get("playcount", 0) != desired["playcount"]
                        ):
                            edits["playcount"] = row.get("playcount", 0)
                        position = row.get("resume", {}).get("position", 0)
                        if (
                            abs(position - previous["resume"]["position"]) >= 1
                            and abs(position - desired["resume"]["position"]) >= 1
                        ):
                            edits["position"] = position
                        if edits:
                            self.store.local(item_id, edits)
                            continue
                    if (
                        repair
                        and mapping
                        and mapping[1]
                        == json.dumps(item, sort_keys=True, separators=(",", ":"))
                        and self.matches(row, desired)
                    ):
                        self.store.remember(item_id, generation, row["movieid"], item)
                        continue
                    self.store.expect(item_id, generation, metadata.userdata(item))
                    if row.get("uniqueid", {}).get(
                        "kofinrefresh"
                    ) != metadata.refresh_token(item):
                        self.owned(row["movieid"], item_id)
                        rpc(
                            "VideoLibrary.RefreshMovie",
                            {"movieid": row["movieid"], "ignorenfo": False},
                        )
                        self._async_pending = True

                        def refreshed(item_id=item_id, item=item):
                            found = self.read().get(item_id)
                            return (
                                found
                                if found
                                and found.get("uniqueid", {}).get("kofinrefresh")
                                == metadata.refresh_token(item)
                                else None
                            )

                        row = self.wait(refreshed)
                        self._async_pending = False
                    self.owned(row["movieid"], item_id)
                    # Art updates merge on Kodi; null explicitly clears keys
                    # omitted from the new server metadata.
                    rpc(
                        "VideoLibrary.SetMovieDetails",
                        dict(desired, movieid=row["movieid"]),
                    )
                    checked = self.owned(row["movieid"], item_id)
                    if not self.matches(checked, desired):
                        fields = [
                            key
                            for key, value in desired.items()
                            if not self.matches(checked, {key: value})
                        ]
                        raise RuntimeError(
                            "movie detail readback differs: " + ", ".join(fields)
                        )
                    self.store.remember(item_id, generation, row["movieid"], item)
                except InterruptedError:
                    raise
                except Exception as error:
                    self.store.failed(
                        item_id, generation, type(error).__name__ + ": " + str(error)
                    )
                    errors.append(error)
            if errors:
                raise RuntimeError(
                    "%d movie operations remain pending: %s" % (len(errors), errors[0])
                )
            if not records and not self.read():
                rpc(
                    "VideoLibrary.SetSourceContent",
                    {
                        "path": directory(self.key),
                        "content": "none",
                        "clearmode": "clear",
                        "refresh": False,
                    },
                )
            completed = True
        finally:
            # A timed-out scan must retain its snapshot until Kodi is idle.
            if (completed or not self._async_pending) and self._idle():
                self.store.unpin()

    @staticmethod
    def matches(row, desired):
        for key, value in desired.items():
            actual = row.get(key)
            if key == "resume":
                if abs((actual or {}).get("position", 0) - value["position"]) >= 1:
                    return False
            elif key in ("ratings", "uniqueid", "art"):
                actual = actual or {}
                for name, wanted in value.items():
                    got = actual.get(name)
                    if (
                        key == "art"
                        and isinstance(got, str)
                        and got.startswith("image://")
                    ):
                        got = unquote(got[8:].rstrip("/"))
                    if key == "ratings" and wanted:
                        if (
                            not got
                            or abs(got["rating"] - wanted["rating"]) >= 0.01
                            or got.get("votes", 0) != wanted["votes"]
                        ):
                            return False
                    elif (got or None) != (wanted or None):
                        return False
            elif isinstance(value, list):
                if sorted(actual or []) != sorted(value):
                    return False
            elif (actual or "") != (value or ""):
                return False
        return True

    def read_pending(self, pending, repair=False):
        """Small deltas use validated cached IDs; only misses enumerate."""
        if repair or len(pending) > 100:
            return self.read()
        result = {}
        for item_id in pending:
            mapping = self.store.mapping(item_id)
            if not mapping or mapping[0] is None:
                return self.read()
            try:
                result[item_id] = self.owned(mapping[0], item_id)
            except RuntimeError:
                return self.read()
        return result


def current_store():
    from kofin.core.settings import Credentials
    from .store import namespace

    creds = Credentials.load()
    return MovieStore(namespace(creds.server_id, creds.user_id))


def mapped_item(kodi_id, media):
    if media != "movie":
        return None
    store = current_store()
    row = rpc(
        "VideoLibrary.GetMovieDetails",
        {"movieid": kodi_id, "properties": ["file", "uniqueid"]},
    ).get("moviedetails", {})
    uid = row.get("uniqueid", {}).get("kofin", "")
    prefix = store.namespace + ":"
    if not uid.startswith(prefix):
        return None
    item_id = uid[len(prefix) :]
    if (
        row.get("file") != playback_url(store.namespace, item_id)
        or store.mapping(item_id) is None
    ):
        return None
    return item_id


def native_id_for(item_id):
    """A cache hit still needs ownership verification; browsing needs no hit."""
    store = current_store()
    mapping = store.mapping(item_id)
    if not mapping or mapping[0] is None:
        return None
    try:
        if mapped_item(mapping[0], "movie") == item_id:
            return mapping[0]
    except RuntimeError:
        pass
    found = Movies(store).read().get(item_id)
    return found["movieid"] if found else None
