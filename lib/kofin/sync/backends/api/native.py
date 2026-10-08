"""Confirmed native operations for every video kind through stock Piers
public interfaces.

An accepted RPC is never a commit: every write is confirmed by reading the
owned row back, and a row is owned only when its namespaced unique id and
its exact resolver URL both say so. Readback is scoped -- one library's
movies, one show's episodes -- and a refresh is confirmed by the id it
produced, so converging on a change never re-reads the whole catalogue.
"""

import time
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote

import xbmc

from kofin.core import kodirpc
from kofin.core.log import Logger
from kofin.sync.catalogue import BackendMismatch
from . import metadata, paths
from .store import Entry, Record, Store, payload_hash

LOG = Logger(__name__)

# Independent setters travel 25 to a JSON-RPC array: the feasibility report's
# fastest measured shape (7.4), and small enough that one failed call costs
# little to retry.
BATCH = 25
# A root scan covers new shows and new episodes alike; Kodi skips an
# unchanged show by the folder's hash property without listing it.
MANY = 10

PROPERTIES = {
    "Movie": [
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
        "set",
    ],
    "Series": [
        "file",
        "uniqueid",
        "title",
        "originaltitle",
        "sorttitle",
        "plot",
        "premiered",
        "mpaa",
        "genre",
        "studio",
        "tag",
        "art",
        "ratings",
        "dateadded",
        "trailer",
        "status",
        "runtime",
    ],
    "Season": ["season", "title", "art", "tvshowid"],
    "Episode": [
        "file",
        "uniqueid",
        "title",
        "originaltitle",
        "plot",
        "firstaired",
        "season",
        "episode",
        "runtime",
        "director",
        "writer",
        "art",
        "ratings",
        "dateadded",
        "playcount",
        "lastplayed",
        "resume",
        "tvshowid",
    ],
    "MusicVideo": [
        "file",
        "uniqueid",
        "title",
        "plot",
        "runtime",
        "director",
        "studio",
        "year",
        "premiered",
        "genre",
        "album",
        "artist",
        "track",
        "tag",
        "art",
        "rating",
        "dateadded",
        "playcount",
        "lastplayed",
        "resume",
    ],
    "BoxSet": ["title", "plot", "art"],
}

# kind -> (list method, list key, id parameter, details method, details key,
#          setter, refresh method, remove method)
METHODS = {
    "Movie": (
        "VideoLibrary.GetMovies",
        "movies",
        "movieid",
        "VideoLibrary.GetMovieDetails",
        "moviedetails",
        "VideoLibrary.SetMovieDetails",
        "VideoLibrary.RefreshMovie",
        "VideoLibrary.RemoveMovie",
    ),
    "Series": (
        "VideoLibrary.GetTVShows",
        "tvshows",
        "tvshowid",
        "VideoLibrary.GetTVShowDetails",
        "tvshowdetails",
        "VideoLibrary.SetTVShowDetails",
        "VideoLibrary.RefreshTVShow",
        "VideoLibrary.RemoveTVShow",
    ),
    "Season": (
        "VideoLibrary.GetSeasons",
        "seasons",
        "seasonid",
        "VideoLibrary.GetSeasonDetails",
        "seasondetails",
        "VideoLibrary.SetSeasonDetails",
        None,
        None,
    ),
    "Episode": (
        "VideoLibrary.GetEpisodes",
        "episodes",
        "episodeid",
        "VideoLibrary.GetEpisodeDetails",
        "episodedetails",
        "VideoLibrary.SetEpisodeDetails",
        "VideoLibrary.RefreshEpisode",
        "VideoLibrary.RemoveEpisode",
    ),
    "MusicVideo": (
        "VideoLibrary.GetMusicVideos",
        "musicvideos",
        "musicvideoid",
        "VideoLibrary.GetMusicVideoDetails",
        "musicvideodetails",
        "VideoLibrary.SetMusicVideoDetails",
        "VideoLibrary.RefreshMusicVideo",
        "VideoLibrary.RemoveMusicVideo",
    ),
    "BoxSet": (
        "VideoLibrary.GetMovieSets",
        "sets",
        "setid",
        "VideoLibrary.GetMovieSetDetails",
        "setdetails",
        "VideoLibrary.SetMovieSetDetails",
        None,
        None,
    ),
}
# Kinds whose rows carry a resolver URL and a namespaced unique id.
FILED = ("Movie", "Series", "Episode", "MusicVideo")
# Application order: a season or episode patch needs its show's row first.
ORDER = ("Series", "Season", "Episode", "Movie", "MusicVideo", "BoxSet")
MEDIA = {"movie": "Movie", "episode": "Episode", "musicvideo": "MusicVideo"}


def rpc(method, params=None):
    result = kodirpc.call(method, params or {})
    if result is None or result is kodirpc.FAILED:
        raise RuntimeError("Kodi refused " + method)
    return result


def rpc_batch(requests):
    """Every reply, as a result or the RuntimeError that call would raise."""
    results = kodirpc.batch(requests)
    return [
        (
            RuntimeError("Kodi refused " + method)
            if result is None or result is kodirpc.FAILED
            else result
        )
        for (method, _), result in zip(requests, results)
    ]


class Monitor(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        self.finished = 0

    def onScanFinished(self, library):
        if library.lower() == "video":
            self.finished += 1


class _Patch:
    def __init__(self, record, kodi_id, desired, compare, applied):
        self.record = record
        self.kodi_id = kodi_id
        self.desired = desired
        self.compare = compare
        self.applied = applied


class Native:
    def __init__(self, store: Store, abort=lambda: False):
        self.store = store
        self.key = store.namespace
        self.abort = abort
        self.monitor = Monitor()
        self.separator = metadata.item_separator()
        self._async_pending = False
        self._scopes: Dict[Tuple[Any, ...], Dict[Any, Dict[str, Any]]] = {}
        self._server = ""
        # Per-pass write batches: thousands of one-row commits cost more than
        # the pass itself (measured: 3,685 database opens for a 1,788-movie
        # pass that sent no patch at all).
        self._acks: List[Tuple[str, int, Optional[int], Dict[str, Any], str]] = []
        self._expectations: List[Tuple[str, int, Dict[str, Any]]] = []
        self._mappings: Dict[str, Any] = {}
        self._mappings_loaded = False

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

    # -- readback ------------------------------------------------------------

    def _server_url(self):
        if not self._server:
            self._server = self.store.server()
        return self._server

    def _check_complete(self, reply, key):
        if not isinstance(reply, dict) or "limits" not in reply:
            raise RuntimeError("incomplete native readback")
        rows = reply.get(key, [])
        if len(rows) != reply["limits"]["total"]:
            raise RuntimeError("partial native readback")
        return rows

    def _owns(self, kind, row, library, parent_id=""):
        """The item id of an owned row, or None."""
        uid = (row.get("uniqueid") or {}).get("kofin", "")
        if not uid.startswith(self.key + ":"):
            return None
        item_id = uid[len(self.key) + 1 :]
        if kind == "Series":
            expected = paths.show_dir(self.key, library, item_id).rstrip("/")
            if (row.get("file") or "").rstrip("/") != expected:
                return None
        elif row.get("file") != paths.playback_url(
            self.key, kind, library, item_id, parent_id
        ):
            return None
        return item_id

    def scope(self, kind, library="", parent_id="", tvshowid=None):
        """Owned rows of one scanner scope, cached for the pass."""
        key = (kind, library, parent_id)
        if key in self._scopes:
            return self._scopes[key]
        listing, list_key, _, _, _, _, _, _ = METHODS[kind]
        params: Dict[str, Any] = {"properties": PROPERTIES[kind]}
        if kind in ("Movie", "MusicVideo"):
            params["filter"] = {
                "field": "path",
                "operator": "startswith",
                "value": paths.library_dir(self.key, library, paths.CONTENT[kind]),
            }
        elif kind == "Series":
            params["filter"] = {
                "field": "path",
                "operator": "startswith",
                "value": paths.library_dir(self.key, library, "tvshows"),
            }
        elif kind in ("Episode", "Season"):
            if tvshowid is None:
                show = self.scope("Series", library).get(parent_id)
                if not show:
                    self._scopes[key] = {}
                    return {}
                tvshowid = show["tvshowid"]
            params["tvshowid"] = tvshowid
        rows = self._check_complete(rpc(listing, params), list_key)
        result: Dict[Any, Dict[str, Any]] = {}
        for row in rows:
            if kind == "Season":
                result[int(row.get("season", -1))] = row
                continue
            if kind == "BoxSet":
                result[row.get("title", "")] = row
                continue
            item_id = self._owns(kind, row, library, parent_id)
            if item_id is None:
                continue
            if item_id in result:
                raise RuntimeError("duplicate owned %s identity" % kind)
            result[item_id] = row
        self._scopes[key] = result
        return result

    def forget_scope(self, kind, library="", parent_id=""):
        self._scopes.pop((kind, library, parent_id), None)
        if kind == "Series":
            # A show's episodes and seasons are read through its row.
            for key in [k for k in self._scopes if k[0] in ("Episode", "Season")]:
                self._scopes.pop(key, None)

    def details(self, kind, kodi_id):
        _, _, id_param, getter, result_key, _, _, _ = METHODS[kind]
        return rpc(getter, {id_param: kodi_id, "properties": PROPERTIES[kind]}).get(
            result_key, {}
        )

    def owned(self, kind, kodi_id, item_id, library, parent_id=""):
        row = self.details(kind, kodi_id)
        if self._owns(kind, row, library, parent_id) != item_id:
            raise RuntimeError("%s ownership changed" % kind)
        return row

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
        self._scopes.clear()

    # -- removal -------------------------------------------------------------

    def remove(self, pending, entries, tombstones, errors) -> Set[str]:
        """Apply tombstones; returns the libraries cleared whole.

        A library none of whose members survive goes in one call: Kodi's
        ``RemoveContentForPath`` on the library root deletes every row filed
        beneath it. Everything else is removed row by row and confirmed with
        one readback per scope.
        """
        removals = {i for i, (_, op, _) in pending.items() if op == "remove"}
        if not removals:
            return set()
        placement: Dict[str, Entry] = dict(entries)
        placement.update(tombstones)
        by_library: Dict[str, Set[str]] = {}
        for item_id, entry in placement.items():
            by_library.setdefault(entry.library, set()).add(item_id)
        cleared: Set[str] = set()
        for library, members in sorted(by_library.items()):
            if not library or not members <= removals:
                continue
            kinds = {placement[i].kind for i in members}
            had_sets = any(
                (self.store.mapping(i) or Entry(i, "", "", "")) is not None
                and (self.store.mapping(i).applied.get("owned", {}).get("set"))  # type: ignore[union-attr]
                for i in members
                if placement[i].kind == "Movie"
            )
            try:
                self.clear_library(library, kinds)
            except InterruptedError:
                raise
            except Exception as error:
                for item_id in members:
                    self.store.failed(
                        item_id,
                        pending[item_id][0],
                        type(error).__name__ + ": " + str(error),
                    )
                errors.append(error)
                continue
            for item_id in members:
                self.store.forget(item_id, pending[item_id][0])
            cleared.add(library)
            # SetSourceContent announces nothing where RemoveX notified per
            # row; the directory is unbound, so this scan walks nothing and
            # only fires the library event the home widgets listen for.
            xbmc.executebuiltin("UpdateLibrary(video)")
            if had_sets:
                # No public call removes a set; Kodi's own clean drops the
                # empty ones, scoped to a root with no files left to check.
                rpc(
                    "VideoLibrary.Clean",
                    {
                        "showdialogs": False,
                        "directory": paths.library_root(self.key, library),
                    },
                )
            if "Series" in kinds or "Episode" in kinds:
                # Kodi merges shows with one title and premiere across
                # libraries (GetMatchingTvShow), so another library's copy
                # may have gone with this one: have the others re-read.
                self.store.invalidate_where(
                    ("Series", "Season", "Episode"), except_library=library
                )
        touched: Set[Tuple[Any, ...]] = set()
        removed_rows: List[Tuple[str, int, str, str, str]] = []
        for item_id in sorted(removals):
            generation = pending[item_id][0]
            placed = placement.get(item_id)
            if (
                placed is None
                or placed.library in cleared
                or placed.kind
                in (
                    "Season",
                    "BoxSet",
                )
            ):
                # Seasons and sets have no removal call; a season row leaves
                # with its episodes or its show, a set with Kodi's clean.
                self.store.forget(item_id, generation)
                continue
            if placed.kind == "Episode" and placed.parent_id in removals:
                # RemoveTVShow takes the show's episodes with it.
                self.store.forget(item_id, generation)
                continue
            mapping = self.store.mapping(item_id)
            if mapping is None or mapping.kodi_id is None:
                self.store.forget(item_id, generation)
                continue
            try:
                if self.abort():
                    raise InterruptedError("native sync stopped")
                self.owned(
                    placed.kind,
                    mapping.kodi_id,
                    item_id,
                    placed.library,
                    placed.parent_id,
                )
                rpc(METHODS[placed.kind][7], {METHODS[placed.kind][2]: mapping.kodi_id})
                removed_rows.append(
                    (item_id, generation, placed.kind, placed.library, placed.parent_id)
                )
            except InterruptedError:
                raise
            except Exception as error:
                self.store.failed(
                    item_id, generation, type(error).__name__ + ": " + str(error)
                )
                errors.append(error)
        if removed_rows:
            self._scopes.clear()
            for item_id, generation, kind, library, parent_id in removed_rows:
                if kind == "Episode" and parent_id in {
                    r[0] for r in removed_rows if r[2] == "Series"
                }:
                    survivors: Dict[Any, Any] = {}
                else:
                    survivors = self.scope(kind, library, parent_id)
                if item_id in survivors:
                    unconfirmed = RuntimeError("%s removal not confirmed" % kind)
                    self.store.failed(item_id, generation, str(unconfirmed))
                    errors.append(unconfirmed)
                else:
                    self.store.forget(item_id, generation)
                touched.add((kind, library, parent_id))
        return cleared

    def clear_library(self, library, kinds):
        root = paths.library_root(self.key, library)
        rpc(
            "VideoLibrary.SetSourceContent",
            {"path": root, "content": "none", "clearmode": "remove", "refresh": False},
        )
        self._scopes.clear()
        left = []
        if "Movie" in kinds:
            left += list(self.scope("Movie", library))
        if "MusicVideo" in kinds:
            left += list(self.scope("MusicVideo", library))
        if kinds & {"Series", "Season", "Episode"}:
            left += list(self.scope("Series", library))
        if left:
            raise RuntimeError("native clear left %d owned rows" % len(left))
        self.store.unbind(root)

    # -- reconcile -----------------------------------------------------------

    def reconcile(self, repair=False):
        """Replay durable operations; an accepted RPC is never a commit."""
        self.setup()
        # A prior process may have died with its pin held. Wait for Kodi to
        # finish using it before publishing the newer desired view to a scan.
        self.wait(self._idle)
        self.store.pin()
        self._scopes.clear()
        completed = False
        try:
            entries = self.store.entries()
            pending: Dict[str, Tuple[int, str, Dict[str, Any]]] = {
                item.item_id: (generation, operation, item.payload)
                for item, generation, operation in self.store.pending()
            }
            if repair:
                for item_id, record in self.store.records().items():
                    if item_id not in pending:
                        pending[item_id] = (record.generation, "upsert", record.item)
            errors: List[Exception] = []
            self.remove(pending, entries, self.store.tombstones(), errors)
            # A whole-library clear puts other libraries' shows back to
            # pending; they join this pass rather than wait for the next.
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
            collections = metadata.collections_of(
                r.item for r in self.store.records(kind="BoxSet").values()
            )
            self.apply(upserts, collections, repair, errors)
            if errors:
                raise RuntimeError(
                    "%d native operations remain pending: %s" % (len(errors), errors[0])
                )
            completed = True
        finally:
            self._commit_acks()
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
                else:
                    kind = "Movie" if content == "movies" else "MusicVideo"
                    present = self.scope(kind, library)
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
        shows = self.scope("Series", library)
        missing = [
            r
            for r in upserts.values()
            if r.kind == "Series" and r.library == library and r.item_id not in shows
        ]
        for record in upserts.values():
            if record.kind != "Episode" or record.library != library:
                continue
            if record.parent_id not in shows:
                missing.append(record)
                continue
            if record.item_id not in self.scope("Episode", library, record.parent_id):
                missing.append(record)
        return missing

    def _ack(self, item_id, generation, kodi_id, applied, kind):
        self._acks.append((item_id, generation, kodi_id, applied, kind))
        if len(self._acks) >= 200:
            self._commit_acks()

    def _commit_acks(self):
        if self._expectations:
            self.store.expect_many(self._expectations)
            self._expectations = []
        if self._acks:
            acks, self._acks = self._acks, []
            self.store.remember_many(acks)
            if self._mappings_loaded:
                self._mappings = self.store.mappings()

    def _mapping(self, item_id):
        if self._mappings_loaded:
            return self._mappings.get(item_id)
        return self.store.mapping(item_id)

    def apply(self, upserts: Dict[str, Record], collections, repair, errors):
        local = {item_id for item_id, _ in self.store.local_pending()}
        self._mappings = self.store.mappings()
        self._mappings_loaded = True
        queue: List[_Patch] = []
        for kind in ORDER:
            for item_id in sorted(i for i, r in upserts.items() if r.kind == kind):
                record = upserts[item_id]
                try:
                    if self.abort():
                        raise InterruptedError("native sync stopped")
                    patch = self.plan(record, collections, repair, local, upserts)
                    if patch is not None:
                        queue.append(patch)
                        if len(queue) >= BATCH:
                            self.flush(queue, errors)
                except InterruptedError:
                    raise
                except Exception as error:
                    self.store.failed(
                        record.item_id,
                        record.generation,
                        type(error).__name__ + ": " + str(error),
                    )
                    errors.append(error)
            self.flush(queue, errors)
            self._commit_acks()

    def _row_for(self, record: Record):
        kind = record.kind
        if kind == "Season":
            number = record.item.get("IndexNumber")
            if number is None:
                return None
            return self.scope("Season", record.library, record.parent_id).get(
                int(number)
            )
        if kind == "BoxSet":
            name = (record.item.get("Name") or "").strip(metadata.ASCII_SPACE)
            return self.scope("BoxSet").get(name)
        return self.scope(kind, record.library, record.parent_id).get(record.item_id)

    def _kodi_id(self, kind, row):
        return row[METHODS[kind][2]]

    def plan(
        self, record: Record, collections, repair, local, upserts
    ) -> Optional[_Patch]:
        kind = record.kind
        row = self._row_for(record)
        if row is None:
            if kind in ("Season", "BoxSet"):
                # A season or a set exists only through its episodes or movies.
                # Nothing native to confirm yet; the next pass finds it.
                if kind == "BoxSet" and not any(
                    m in collections for m in record.item.get("KofinMembers") or []
                ):
                    self._ack(record.item_id, record.generation, None, {}, kind)
                    return None
                raise RuntimeError("%s has no native row yet" % kind)
            raise RuntimeError("scanner did not import %s" % kind)
        seasons = (
            [
                r.item
                for r in self.store.records(
                    kind="Season", parent_id=record.item_id
                ).values()
            ]
            if kind == "Series"
            else ()
        )
        set_name = collections.get(record.item_id, "") if kind == "Movie" else None
        desired = metadata.details(
            record.item,
            self._server_url(),
            self.key,
            record.library,
            self.separator,
            seasons,
            set_name,
        )
        mapping = self._mapping(record.item_id)
        previous = mapping.applied if mapping else {}
        if record.item_id in local:
            # Deliver the real user edit first. Never overwrite it with an
            # older server snapshot during a retry or a refresh.
            return None
        if kind in ("Movie", "Episode", "MusicVideo") and previous.get("userdata"):
            edits = self._local_edits(row, previous["userdata"], desired)
            if edits:
                self.store.local(record.item_id, edits)
                return None
        kodi_id = self._kodi_id(kind, row)
        if kind in FILED:
            token = desired["uniqueid"]["kofinrefresh"]
            if (row.get("uniqueid") or {}).get("kofinrefresh") != token:
                row = self.refresh(record, kodi_id, token, upserts)
                kodi_id = self._kodi_id(kind, row)
        applied = {
            "hash": payload_hash(record.item),
            "owned": metadata.owned(desired),
        }
        compare = self._merge(kind, desired, row, previous.get("owned", {}))
        if kind in ("Movie", "Episode", "MusicVideo"):
            applied["userdata"] = metadata.userdata(record.item)
            self._expectations.append(
                (record.item_id, record.generation, applied["userdata"])
            )
        # A withdrawn season name is a clear the readback cannot show.
        clearing = kind == "Season" and desired.get("title") == ""
        if (
            not clearing
            and self.matches(row, compare)
            and (not repair or previous.get("hash") == applied["hash"])
        ):
            self._ack(record.item_id, record.generation, kodi_id, applied, kind)
            return None
        return _Patch(record, kodi_id, desired, compare, applied)

    def _local_edits(self, row, previous, desired):
        edits = {}
        playcount = row.get("playcount", 0) or 0
        if playcount != previous.get("playcount") and playcount != desired["playcount"]:
            edits["playcount"] = playcount
        position = (row.get("resume") or {}).get("position", 0) or 0
        if (
            abs(position - previous.get("resume", {}).get("position", 0)) >= 1
            and abs(position - desired["resume"]["position"]) >= 1
        ):
            edits["position"] = position
        return edits

    def _merge(self, kind, desired, row, owned_before):
        """Setters merge maps and replace tags. Clear only keys previously
        owned by Kofin, retaining Kodi defaults and local additions."""
        compare = dict(desired)
        if "tag" in compare:
            old_tags = set(owned_before.get("tag", [])) | {
                tag for tag in row.get("tag", []) if tag.startswith("kofin.library.")
            }
            compare["tag"] = sorted(
                set(compare["tag"]) | (set(row.get("tag", [])) - old_tags)
            )
            desired["tag"] = compare["tag"]
        for field in ("art", "ratings", "uniqueid"):
            if field not in compare:
                continue
            owned = set(owned_before.get(field, []))
            merged = dict(compare[field])
            merged.update({key: None for key in owned if key not in merged})
            merged.update(
                {
                    key: value
                    for key, value in (row.get(field) or {}).items()
                    if key not in merged and key not in owned and key != "icon"
                }
            )
            if field == "art":
                merged = {
                    key: (
                        unquote(value[8:].rstrip("/"))
                        if isinstance(value, str) and value.startswith("image://")
                        else value
                    )
                    for key, value in merged.items()
                }
            desired[field] = merged
            compare[field] = merged
        if kind == "Season":
            if not desired.get("title"):
                if "title" in owned_before:
                    desired["title"] = ""
                else:
                    desired.pop("title", None)
                compare.pop("title", None)
            compare.pop("uniqueid", None)
            desired.pop("uniqueid", None)
        return compare

    def refresh(self, record: Record, kodi_id, token, upserts):
        """Re-import a row whose cast or streams changed; confirmed by token.

        A show refresh re-creates every episode of the show, so their rows,
        identities and userdata all need another pass; they are put back to
        pending and joined to this one.
        """
        kind = record.kind
        _, _, id_param, _, _, _, refresh_method, _ = METHODS[kind]
        self.owned(kind, kodi_id, record.item_id, record.library, record.parent_id)
        params: Dict[str, Any] = {id_param: kodi_id, "ignorenfo": False}
        if kind == "Series":
            # The refresh deletes every episode and re-creates it from the
            # supplied tag, so a watched mark or position the viewer set in
            # Kodi since the last sync is captured first, as a local edit.
            self._capture_local_edits(record)
            params["refreshepisodes"] = True
        rpc(refresh_method, params)
        self._async_pending = True

        def refreshed():
            self.forget_scope(kind, record.library, record.parent_id)
            found = self.scope(kind, record.library, record.parent_id).get(
                record.item_id
            )
            if found and (found.get("uniqueid") or {}).get("kofinrefresh") == token:
                return found
            return None

        row = self.wait(refreshed, busy=self._scanning)
        self._async_pending = False
        if kind == "Series":
            children = self.store.records(
                kind=("Season", "Episode"), parent_id=record.item_id
            )
            self.store.invalidate(children)
            for child in children.values():
                upserts.setdefault(child.item_id, child)
            self.forget_scope("Series", record.library)
        return row

    def _capture_local_edits(self, show: Record):
        rows = self.scope("Episode", show.library, show.item_id)
        for item_id, row in rows.items():
            mapping = self.store.mapping(item_id)
            item = self.store.item(item_id)
            if not mapping or not mapping.applied.get("userdata") or item is None:
                continue
            edits = self._local_edits(
                row, mapping.applied["userdata"], metadata.userdata(item)
            )
            if edits:
                self.store.local(item_id, edits)

    def flush(self, queue: List[_Patch], errors):
        if not queue:
            return
        patches, queue[:] = list(queue), []
        # Expectations go in ahead of the setters they cover.
        if self._expectations:
            self.store.expect_many(self._expectations)
            self._expectations = []
        setters = []
        for patch in patches:
            kind = patch.record.kind
            _, _, id_param, _, _, setter, _, _ = METHODS[kind]
            params = dict(patch.desired)
            params[id_param] = patch.kodi_id
            setters.append((setter, params))
        replies = rpc_batch(setters)
        confirmations = []
        pending_confirm = []
        for patch, reply in zip(patches, replies):
            if isinstance(reply, Exception):
                self.store.failed(
                    patch.record.item_id, patch.record.generation, str(reply)
                )
                errors.append(reply)
                continue
            kind = patch.record.kind
            _, _, id_param, getter, _, _, _, _ = METHODS[kind]
            confirmations.append(
                (getter, {id_param: patch.kodi_id, "properties": PROPERTIES[kind]})
            )
            pending_confirm.append(patch)
        for patch, reply in zip(pending_confirm, rpc_batch(confirmations)):
            record = patch.record
            try:
                if isinstance(reply, Exception):
                    raise reply
                row = (
                    reply.get(METHODS[record.kind][4], {})
                    if isinstance(reply, dict)
                    else {}
                )
                if record.kind in FILED:
                    if (
                        self._owns(record.kind, row, record.library, record.parent_id)
                        != record.item_id
                    ):
                        raise RuntimeError("%s ownership changed" % record.kind)
                if not self.matches(row, patch.compare):
                    fields = [
                        key
                        for key, value in patch.compare.items()
                        if not self.matches(row, {key: value})
                    ]
                    raise RuntimeError(
                        "%s detail readback differs: %s"
                        % (record.kind, ", ".join(fields))
                    )
                self._ack(
                    record.item_id,
                    record.generation,
                    patch.kodi_id,
                    patch.applied,
                    record.kind,
                )
                scope_key = (record.kind, record.library, record.parent_id)
                if scope_key in self._scopes:
                    cached_key: Any = record.item_id
                    if record.kind == "Season":
                        cached_key = int(record.item.get("IndexNumber") or 0)
                    elif record.kind == "BoxSet":
                        cached_key = row.get("title", "")
                    self._scopes[scope_key][cached_key] = row
            except InterruptedError:
                raise
            except Exception as error:
                self.store.failed(
                    record.item_id,
                    record.generation,
                    type(error).__name__ + ": " + str(error),
                )
                errors.append(error)

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
            elif key == "rating":
                if abs(float(actual or 0) - float(value or 0)) >= 0.01:
                    return False
            elif isinstance(value, list):
                if sorted(actual or []) != sorted(value):
                    return False
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                if (actual or 0) != value:
                    return False
            elif (actual or "") != (value or ""):
                return False
        return True


# -- identity lookups for playback and userdata --------------------------------


def current_store():
    from kofin.core.settings import Credentials
    from .store import namespace

    creds = Credentials.load()
    return Store(namespace(creds.server_id, creds.user_id))


def mapped_item(kodi_id, media):
    """The item id behind an owned native row, or None for anything else."""
    kind = MEDIA.get(media)
    if kind is None:
        return None
    store = current_store()
    _, _, id_param, getter, result_key, _, _, _ = METHODS[kind]
    try:
        row = rpc(getter, {id_param: kodi_id, "properties": ["file", "uniqueid"]}).get(
            result_key, {}
        )
    except RuntimeError:
        # No such row for that media type: not ours, whatever it is.
        return None
    uid = (row.get("uniqueid") or {}).get("kofin", "")
    prefix = store.namespace + ":"
    if not uid.startswith(prefix):
        return None
    item_id = uid[len(prefix) :]
    placed = store.entry(item_id)
    if placed is None or store.mapping(item_id) is None:
        return None
    if row.get("file") != paths.playback_url(
        store.namespace, kind, placed.library, item_id, placed.parent_id
    ):
        return None
    return item_id


def native_id_for(item_id, media="movie"):
    """A cache hit still needs ownership verification; browsing needs no hit."""
    store = current_store()
    mapping = store.mapping(item_id)
    if not mapping or mapping.kodi_id is None:
        return None
    try:
        if mapped_item(mapping.kodi_id, media) == item_id:
            return mapping.kodi_id
    except RuntimeError:
        pass
    placed = store.entry(item_id)
    kind = MEDIA.get(media)
    if placed is None or kind is None:
        return None
    found = Native(store).scope(kind, placed.library, placed.parent_id).get(item_id)
    return found[METHODS[kind][2]] if found else None


def library_url(item_id):
    """The resolver URL of an imported item, or None when it has no row."""
    store = current_store()
    mapping = store.mapping(item_id)
    placed = store.entry(item_id)
    if not mapping or mapping.kodi_id is None or placed is None:
        return None
    return paths.playback_url(
        store.namespace, placed.kind, placed.library, item_id, placed.parent_id
    )
