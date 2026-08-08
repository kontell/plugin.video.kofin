"""The service-side download manager (docs/offline-downloads-plan.md W1.5).

A worker pool draining the kofin.db queue oldest-first: fetch to ``.part``
over :meth:`Api.download_stream`, verify the size, pull the external
subtitle sidecars (W1.6), rename, repoint, refresh. Owned by the service the
way the library manager is — built when enabled, degrade-don't-die, stopped
in ``_shutdown`` with bounded joins — and fed by the guarded download IPC
messages, whose handlers run on Kodi's notification thread and therefore
only enqueue here (the ops queue) rather than touch a database or a socket.

Stop discipline (the thread-stop doctrine, and the reason every worker gets
its own ``_new_api`` session): the transport abort predicate bounds retries,
the per-chunk check in ``StreamedResponse.chunks`` bounds a live body, and
the read timeout bounds a dead one — so a stop request is honored within one
chunk plus one read timeout, never a file's worth of transfer. A download a
stop interrupts stays ``active`` and is re-queued by ``recover_interrupted``
at the next start, resuming originals from ``bytes_done`` with a Range.
"""

import os
import threading
import time
from queue import Empty, Queue
from typing import Any, Callable, Dict, List, Optional, Tuple

from kofin.core import settings, state, toast
from kofin.core.http import JellyfinError, StreamedResponse, Unauthorized
from kofin.core.log import Logger
from kofin.downloads import downloads_root, files, repoint, store

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

MEDIA_TYPE_BY_DTO = {"Movie": "movie", "Episode": "episode"}

# Per-item attempts before the row settles as failed. The store keeps
# bytes_done across the requeues, so original attempts resume with a Range.
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 5.0

# How long an idle worker sleeps between queue polls when nothing wakes it.
IDLE_POLL_SECONDS = 30.0

# Same, while the server is unreachable: shorter, because nothing wakes a
# worker when the connection comes back — the service raises the online flag
# and this is what notices.
OFFLINE_POLL_SECONDS = 10.0

# One join per worker at stop: a chunk-loop abort plus one full read timeout,
# with a little grace (core.http.DEFAULT_TIMEOUT read budget is 30 s).
JOIN_SECONDS = 35.0

# Store progress roughly every 8 MiB rather than every chunk.
PROGRESS_EVERY_CHUNKS = 32

# ffmpeg-style codec -> sidecar extension, where they differ. Anything not
# listed uses the codec name itself, which is right for ass/ssa/vtt/sup.
SUBTITLE_EXTENSIONS = {"subrip": "srt", "webvtt": "vtt"}


class _Cancelled(Exception):
    """The item was cancelled mid-transfer (never an error)."""


def worker_count() -> int:
    configured = settings.get_int("downloadsMaxParallel")
    return max(1, min(4, configured or 2))


class DownloadManager:
    def __init__(
        self,
        api_factory: Callable[[], Any],
        refresh: Callable[[], None],
        stopping: "threading.Event",
    ) -> None:
        self._api_factory = api_factory
        self._refresh = refresh
        # The service generation's own event (never state.should_stop —
        # the successor lowers that on its way up; see service/main.py).
        self._stopping = stopping
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._ops: "Queue[Tuple[str, str, str]]" = Queue()
        self._cancels: set = set()
        self._cancels_lock = threading.Lock()
        self._attempts: Dict[str, int] = {}
        self._workers: List[threading.Thread] = []
        self._reconciler: Optional[threading.Thread] = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        recovered = store.recover_interrupted()
        if recovered:
            self._wake.set()
        self._reconciler = threading.Thread(
            target=self._run_reconcile, name="kofin-downloads-reconcile", daemon=True
        )
        self._reconciler.start()
        for index in range(worker_count()):
            worker = threading.Thread(
                target=self._run_worker,
                name="kofin-downloads-%d" % index,
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)
        LOG.info("download manager started (%d worker(s))", len(self._workers))

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for worker in [self._reconciler] + self._workers:
            if worker is None or not worker.is_alive():
                continue
            worker.join(timeout=JOIN_SECONDS)
            if worker.is_alive():  # pragma: no cover - watchdog logging only
                LOG.warning("%s did not stop within its deadline", worker.name)
        LOG.info("download manager stopped")

    def _should_stop(self) -> bool:
        return self._stop.is_set() or self._stopping.is_set()

    # -- the IPC surface (notification thread: enqueue only) -------------------

    def submit(self, item_ids: List[str], origin: str = store.ORIGIN_USER) -> None:
        for item_id in item_ids:
            if item_id:
                self._ops.put(("add", str(item_id), origin))
        self._wake.set()

    def cancel(self, item_id: str) -> None:
        with self._cancels_lock:
            self._cancels.add(item_id)
        self._ops.put(("cancel", item_id, ""))
        self._wake.set()

    def remove(self, item_id: str) -> None:
        self._ops.put(("remove", item_id, ""))
        self._wake.set()

    def _cancelled(self, item_id: str) -> bool:
        with self._cancels_lock:
            return item_id in self._cancels

    def _clear_cancel(self, item_id: str) -> None:
        with self._cancels_lock:
            self._cancels.discard(item_id)

    # -- workers ---------------------------------------------------------------

    def _run_worker(self) -> None:
        api = self._api_factory()
        try:
            while not self._should_stop():
                self._drain_ops()
                if state.is_offline():
                    # Hold the queue rather than burn it: every claim while
                    # the server is gone would fail its three attempts and
                    # settle, so a user who queued a season offline came back
                    # to a list of failures instead of a list of downloads
                    # (the gap phase 1's gates exposed). Ops still drain
                    # above, so a cancel or a remove works offline.
                    if self._wake.wait(timeout=OFFLINE_POLL_SECONDS):
                        self._wake.clear()
                    continue
                row = store.claim()
                if row is None:
                    if self._wake.wait(timeout=IDLE_POLL_SECONDS):
                        self._wake.clear()
                    continue
                try:
                    self._process(api, row)
                except Exception:
                    # Contain per item: a store hiccup must not kill the
                    # worker for the rest of the generation.
                    LOG.exception("download processing died for %s", row.jellyfin_id)
                    store.fail(row.jellyfin_id, "internal error")
        except Exception:  # pragma: no cover - the backstop, never expected
            LOG.exception("download worker died")
        finally:
            api.close()

    def _drain_ops(self) -> None:
        while True:
            try:
                op, item_id, origin = self._ops.get_nowait()
            except Empty:
                return
            try:
                if op == "add":
                    self._apply_add(item_id, origin)
                elif op == "cancel":
                    self._apply_cancel(item_id)
                elif op == "remove":
                    self._apply_remove(item_id)
            except Exception:
                LOG.exception("download op %s failed for %s", op, item_id)

    def _apply_add(self, item_id: str, origin: str) -> None:
        if store.queue(store.Download(jellyfin_id=item_id, origin=origin)):
            self._attempts.pop(item_id, None)
            LOG.info("download queued: %s", item_id)

    def _apply_cancel(self, item_id: str) -> None:
        row = store.get(item_id)
        if row is None or row.state == store.DONE:
            # Done rows are removed, not cancelled; active ones abort at the
            # next chunk and clean themselves up.
            self._clear_cancel(item_id)
            return
        if row.state in (store.QUEUED, store.FAILED):
            self._delete_media(row)
            store.remove(item_id)
            self._clear_cancel(item_id)
            LOG.info("download cancelled: %s", item_id)

    def _apply_remove(self, item_id: str) -> None:
        row = store.get(item_id)
        if row is None:
            return
        if row.state != store.DONE:
            self._apply_cancel(item_id)
            return
        root = downloads_root()
        repoint.restore(row, root)
        self._delete_media(row)
        # The store row goes before the tag check: an episode's unstamp asks
        # whether any sibling download still holds the show in the node, and
        # the row being removed must not count itself as that sibling.
        store.remove(item_id)
        repoint.unstamp_tag(row)
        self._refresh_quietly()
        LOG.info("download removed: %s", item_id)

    # -- the transfer ----------------------------------------------------------

    def _process(self, api: Any, row: "store.Download") -> None:
        item_id = row.jellyfin_id
        try:
            item = api.item(item_id)
            self._transfer(api, row, item)
        except _Cancelled:
            self._delete_part(store.get(item_id) or row)
            store.remove(item_id)
            self._clear_cancel(item_id)
            LOG.info("download cancelled mid-transfer: %s", item_id)
        except Unauthorized as error:
            # The server's EnableContentDownloading gate: retrying cannot fix
            # a permission, so the row settles immediately (feasibility V1).
            store.fail(item_id, "download not permitted: %s" % error)
            self._toast(30713, item_id)
        except JellyfinError as error:
            self._retry_or_fail(row, str(error))
        except OSError as error:
            self._retry_or_fail(row, self._explain_write_error(error))

    def _transfer(self, api: Any, row: "store.Download", item: JsonDict) -> None:
        item_id = row.jellyfin_id
        media_type = MEDIA_TYPE_BY_DTO.get(str(item.get("Type")))
        if media_type is None:
            store.fail(item_id, "unsupported type %r" % item.get("Type"))
            return
        source = (item.get("MediaSources") or [{}])[0]
        container = str(source.get("Container") or "").split(",")[0]
        size_expected = int(source.get("Size") or 0)
        store.record_details(
            item_id,
            media_type,
            str(item.get("SeriesId") or ""),
            size_expected,
            _userdata_json(item.get("UserData")),
        )

        root = downloads_root()
        if not row.rel_path and not files.free_space_ok(root, size_expected):
            store.fail(item_id, "not enough free space")
            self._toast(30715, item.get("Name", item_id))
            return

        owner_id = str(item.get("SeriesId") or "") or item_id
        rel_path = row.rel_path
        start = 0
        if rel_path:
            part_existing = os.path.join(root, rel_path) + ".part"
            if os.path.exists(part_existing):
                start = os.path.getsize(part_existing)
        stream = api.download_stream(item_id, start=start)
        try:
            if not rel_path:
                # First contact decides the name: the server states it in
                # Content-Disposition on this very response (V1), so one open
                # serves both the headers and the body.
                rel_path = self._decide_target(
                    item,
                    item_id,
                    owner_id,
                    container,
                    stream.header("Content-Disposition"),
                )
            absolute = os.path.join(root, rel_path)
            part = absolute + ".part"
            os.makedirs(os.path.dirname(absolute), exist_ok=True)
            expected_total = _expected_total(stream, start, size_expected)
            if not stream.already_complete:
                if stream.status == 200 and start:
                    # The server ignored the range; write fresh.
                    start = 0
                self._write_body(stream, part, start, item_id)
        finally:
            stream.close()

        actual = os.path.getsize(part) if os.path.exists(part) else 0
        if expected_total and actual != expected_total:
            store.record_progress(item_id, actual)
            raise JellyfinError(
                "size mismatch: %d of %d bytes" % (actual, expected_total)
            )

        self._download_subtitles(api, item, source, os.path.dirname(absolute), rel_path)
        os.replace(part, absolute)
        store.finish(item_id, rel_path, container, actual)
        self._attempts.pop(item_id, None)
        finished = store.get(item_id)
        if finished is not None:
            repoint.repoint(finished, root)
            repoint.stamp_tag(finished)
        self._refresh_quietly()
        self._toast(30712, item.get("Name", item_id))
        LOG.info("download complete: %s (%d bytes) at %s", item_id, actual, rel_path)

    def _decide_target(
        self,
        item: JsonDict,
        item_id: str,
        owner_id: str,
        container: str,
        disposition: str,
    ) -> str:
        """Freeze the target path on first contact (files.py owns the rules).

        The uniqueness test is on the *owning* directory — a film's own, a
        show's — never the season leaf, so siblings share one season folder
        (files.unique_dir).
        """
        owning, leaf = files.item_dirs(item)
        owning = files.unique_dir(owning, owner_id, _dir_taken_by_other(owner_id))
        directory = owning if leaf is None else "%s/%s" % (owning, leaf)
        fallback = "%s.%s" % (
            files.sanitize(str(item.get("Name") or item_id)),
            container or "bin",
        )
        filename = files.filename_from_disposition(disposition, fallback)
        rel_path = "%s/%s" % (directory, filename)
        store.record_target(item_id, rel_path, container)
        return rel_path

    def _write_body(
        self, stream: StreamedResponse, part: str, start: int, item_id: str
    ) -> None:
        written = start
        chunk_count = 0
        try:
            with open(part, "ab" if start else "wb") as handle:
                for chunk in stream.chunks():
                    if self._cancelled(item_id):
                        raise _Cancelled()
                    handle.write(chunk)
                    written += len(chunk)
                    chunk_count += 1
                    if chunk_count % PROGRESS_EVERY_CHUNKS == 0:
                        store.record_progress(item_id, written)
        finally:
            # Also on the way out through an abort or a dead socket: the
            # resume reads the .part's real size, but a watermark left at
            # whatever the last 8 MiB tick happened to catch (or at 0)
            # misreports an interrupted download to everything that renders
            # one.
            store.record_progress(item_id, written)

    def _download_subtitles(
        self, api: Any, item: JsonDict, source: JsonDict, directory: str, rel_path: str
    ) -> None:
        """External subtitle sidecars beside the media file (plan W1.6).

        An original download does not contain the external subtitles the
        streaming play route attaches, so a repointed item would silently
        lose them. Kodi auto-loads sidecars by name. Failures are logged and
        non-fatal — a missing subtitle must never fail the download.
        """
        base = os.path.basename(rel_path).rsplit(".", 1)[0]
        for stream_info in source.get("MediaStreams") or []:
            if stream_info.get("Type") != "Subtitle" or not stream_info.get(
                "IsExternal"
            ):
                continue
            codec = str(stream_info.get("Codec") or "").lower()
            extension = SUBTITLE_EXTENSIONS.get(codec, codec)
            if not extension:
                continue
            language = str(stream_info.get("Language") or "und")
            url = api.subtitle_stream_url(
                str(item.get("Id")),
                str(source.get("Id")),
                int(stream_info.get("Index") or 0),
                extension,
            )
            target = os.path.join(
                directory, "%s.%s.%s" % (base, files.sanitize(language), extension)
            )
            try:
                payload = api.download(url)
                with open(target, "wb") as handle:
                    handle.write(payload)
            except (JellyfinError, OSError) as error:
                LOG.warning("subtitle sidecar failed for %s: %s", url, error)

    # -- failure and cleanup ---------------------------------------------------

    def _retry_or_fail(self, row: "store.Download", error: str) -> None:
        item_id = row.jellyfin_id
        if state.is_offline():
            # The server went away mid-transfer. Same reasoning as a
            # shutdown: leave the row recoverable rather than spending its
            # attempts against an unreachable server.
            LOG.info("download interrupted by an outage: %s", item_id)
            return
        if self._should_stop():
            # Not a failure: the service is going away mid-transfer, and the
            # abort that ended the chunk loop is our own. The row stays
            # ``active`` so ``recover_interrupted`` re-queues it at the next
            # start, resuming from the .part with a Range — settling it as
            # failed instead both mislabels a shutdown and loses the
            # automatic resume (found live, G6b: a Kodi quit left the row
            # failed and nothing picked it up again).
            LOG.info("download interrupted by shutdown: %s", item_id)
            return
        attempts = self._attempts.get(item_id, 0) + 1
        self._attempts[item_id] = attempts
        store.fail(item_id, error)
        if attempts < MAX_ATTEMPTS and not self._should_stop():
            LOG.warning(
                "download attempt %d/%d failed for %s: %s",
                attempts,
                MAX_ATTEMPTS,
                item_id,
                error,
            )
            # fail -> queue re-queues in place, keeping bytes_done for the
            # Range resume; only this worker sits out the backoff.
            store.queue(
                store.Download(
                    jellyfin_id=item_id, origin=row.origin, quality=row.quality
                )
            )
            self._stopping_aware_sleep(BACKOFF_SECONDS * attempts)
            return
        self._attempts.pop(item_id, None)
        LOG.error("download failed for %s: %s", item_id, error)
        self._toast(30713, item_id)

    def _stopping_aware_sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self._should_stop():
            time.sleep(0.2)

    def _delete_part(self, row: "store.Download") -> None:
        """Drop a partial transfer and the directories it created.

        The prune matters here as much as in ``_delete_media``: a cancel
        before the first byte lands still made the season folder on its way
        in, and without this an empty ``Season 03/`` survived every cancel
        (found live, G6a).
        """
        if not row.rel_path:
            return
        root = downloads_root()
        absolute = os.path.join(root, row.rel_path)
        _remove_quietly(absolute + ".part")
        _prune_empty_dirs(os.path.dirname(absolute), root)

    def _delete_media(self, row: "store.Download") -> None:
        """The media file, its .part and its sidecars, then any empty dirs."""
        if not row.rel_path:
            return
        root = downloads_root()
        absolute = os.path.join(root, row.rel_path)
        directory = os.path.dirname(absolute)
        base = os.path.basename(row.rel_path).rsplit(".", 1)[0]
        _remove_quietly(absolute + ".part")
        _remove_quietly(absolute)
        try:
            for name in os.listdir(directory):
                if name.startswith(base + "."):
                    _remove_quietly(os.path.join(directory, name))
        except OSError:
            pass
        _prune_empty_dirs(directory, root)

    def _explain_write_error(self, error: OSError) -> str:
        if getattr(error, "errno", None) == 27:  # EFBIG
            return "file exceeds the filesystem's size limit (FAT32's 4 GiB?)"
        if getattr(error, "errno", None) == 28:  # ENOSPC
            return "disk full"
        return "write failed: %s" % error

    # -- reconcile -------------------------------------------------------------

    def _run_reconcile(self) -> None:
        """Walk the done rows once at start: a missing file restores the
        library row and flags the download failed (self-healing toward
        streaming, never a broken local play), and a present file re-asserts
        the repoint — which is what heals a library repair, whose rebuilt
        rows are all in writer shape."""
        try:
            root = downloads_root()
            touched = False
            for row in store.rows(store.DONE):
                if self._should_stop():
                    return
                absolute = os.path.join(root, row.rel_path)
                if not row.rel_path or not os.path.exists(absolute):
                    LOG.warning(
                        "downloaded file missing for %s; restoring the library row",
                        row.jellyfin_id,
                    )
                    repoint.restore(row, root)
                    store.fail(row.jellyfin_id, "file missing")
                    touched = True
                    continue
                if repoint.repoint(row, root):
                    repoint.stamp_tag(row)  # idempotent; a repair wiped links
                    touched = True
            if touched:
                self._refresh_quietly()
        except Exception:  # pragma: no cover - never break service start
            LOG.exception("downloads reconcile failed")

    # -- plumbing --------------------------------------------------------------

    def _refresh_quietly(self) -> None:
        try:
            self._refresh()
        except Exception:  # pragma: no cover - refresh is best-effort
            LOG.exception("downloads refresh failed")

    def _toast(self, string_id: int, name: Any) -> None:
        try:
            toast.show(settings.localized(string_id) % name, time_ms=4000)
        except Exception:  # pragma: no cover - uncached string etc.
            LOG.debug("download toast failed for %s", string_id)


def _expected_total(stream: StreamedResponse, start: int, fallback: int) -> int:
    """What the finished file must weigh, from the response's own headers.

    A 206 states the whole in Content-Range ("bytes a-b/total") — including
    the 416 already-complete answer's "bytes */total". A 200's Content-Length
    is the whole outright. A transcode (phase 3) states neither, which is
    what the fallback (and clean-EOF completion) is for.
    """
    content_range = stream.header("Content-Range")
    if "/" in content_range:
        total = content_range.rsplit("/", 1)[1].strip()
        if total.isdigit():
            return int(total)
    length = stream.header("Content-Length")
    if length.isdigit():
        return start + int(length) if stream.status == 206 else int(length)
    return int(fallback)


def _userdata_json(userdata: Any) -> str:
    import json

    if not isinstance(userdata, dict):
        return ""
    try:
        return json.dumps(userdata)
    except (TypeError, ValueError):
        return ""


def _dir_taken_by_other(owner_id: str) -> Callable[[str], bool]:
    """Is this directory already claimed by a *different* owner? A row's
    owner is its series (episodes) or itself (films), matching how
    files.item_dirs assigns directories."""

    def taken(directory: str) -> bool:
        prefix = directory + "/"
        for row in store.rows():
            if not row.rel_path.startswith(prefix):
                continue
            if (row.series_id or row.jellyfin_id) != owner_id:
                return True
        return False

    return taken


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _prune_empty_dirs(directory: str, root: str) -> None:
    """Remove now-empty directories up to (never including) the root."""
    current = os.path.abspath(directory)
    stop = os.path.abspath(root)
    while current.startswith(stop) and current != stop:
        try:
            os.rmdir(current)
        except OSError:
            return
        current = os.path.dirname(current)
