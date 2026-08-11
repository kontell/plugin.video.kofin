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
from datetime import datetime
from queue import Empty, Queue
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import xbmc

from kofin.core import settings, state, toast
from kofin.core.http import JellyfinError, StreamedResponse, Unauthorized
from kofin.core.log import Logger
from kofin.sync.db import Database
from kofin.downloads import (
    downloads_root,
    export,
    files,
    notify_allowed,
    probe,
    progress,
    quality,
    repoint,
    store,
)

LOG = Logger(__name__)

JsonDict = Dict[str, Any]

MEDIA_TYPE_BY_DTO = {"Movie": "movie", "Episode": "episode", "Audio": "song"}

# What each worker pool claims. "" is the unknown kind — a row queued by a
# release that did not carry the type, or by a sender that had no DTO — and
# it belongs to the video pool, whose pacing already assumes an item might
# be a two-hour film.
VIDEO_KINDS = ("movie", "episode", "")
MUSIC_KINDS = ("song",)

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

# Same again, while playback holds the pool back. Short because it is the
# floor on how long the queue stays idle after the credits: the service also
# wakes the pool on Player.OnStop, and this is the cover for the stop it
# never sees (a foreign player, a crash, a claim that never happened).
PLAYBACK_POLL_SECONDS = 5.0

# One join per worker at stop: a chunk-loop abort plus one full read timeout,
# with a little grace (core.http.DEFAULT_TIMEOUT read budget is 30 s).
JOIN_SECONDS = 35.0

# How often the maintenance thread sweeps watched auto-origin downloads
# (plan W4.2). Kodi marks played at ~90%, so "promptly after the credits"
# needs no better than minutes.
RETENTION_SWEEP_SECONDS = 300.0

# The stale sweep's unit and its fallback (plan W4.8): the setting is in
# days, and an unset one behaves as the slider's own default so a profile
# that predates it reads the same as one that has never touched it.
SECONDS_PER_DAY = 86400.0
STALE_DAYS_DEFAULT = 30

# Store progress roughly every 8 MiB rather than every chunk.
PROGRESS_EVERY_CHUNKS = 32

# The longest a finished download waits to become visible while the pool is
# still busy. Short enough that a long album shows its first tracks well
# before the last one lands; long enough that a burst of tracks refreshes
# once rather than per track.
REFRESH_MAX_DEFER_SECONDS = 20.0

# ffmpeg-style codec -> sidecar extension, where they differ. Anything not
# listed uses the codec name itself, which is right for ass/ssa/vtt/sup.
SUBTITLE_EXTENSIONS = {"subrip": "srt", "webvtt": "vtt"}


class _Cancelled(Exception):
    """The item was cancelled mid-transfer (never an error)."""


def worker_count() -> int:
    configured = settings.get_int("downloadsMaxParallel")
    return max(1, min(4, configured or 2))


def music_worker_count() -> int:
    configured = settings.get_int("downloadsMusicParallel")
    return max(1, min(10, configured or 5))


class DownloadManager:
    def __init__(
        self,
        api_factory: Callable[[], Any],
        refresh: Callable[[List[str]], None],
        stopping: "threading.Event",
    ) -> None:
        self._api_factory = api_factory
        self._refresh = refresh
        # The service generation's own event (never state.should_stop —
        # the successor lowers that on its way up; see service/main.py).
        self._stopping = stopping
        self._stop = threading.Event()
        # One wake Event per worker, not one shared by the pool.
        #
        # Shared, it was a claim-latency bug rather than a race in the usual
        # sense: `submit` sets the Event once, every worker wakes, and the
        # first to notice clears it for everybody. When that first worker is
        # from the *music* pool and the row is an episode, it drains the op,
        # finds nothing it can claim, sees an empty ops queue and goes back to
        # sleep — while the video workers, which could have taken the row, are
        # already back in a 30 s wait with the Event clear. Measured on the
        # Omega box: three consecutive user-initiated downloads sat queued for
        # 31, 32 and 32 seconds with the machine otherwise idle.
        #
        # Per-worker Events fix it outright. Nobody can consume anybody else's,
        # and a set that lands while a worker is between claim and wait is
        # remembered rather than lost — which a Condition's notify_all, the
        # other obvious shape, would drop.
        self._wakes: List[threading.Event] = []
        self._wakes_lock = threading.Lock()
        self._ops: "Queue[Tuple[str, str, str, str]]" = Queue()
        self._cancels: set = set()
        self._cancels_lock = threading.Lock()
        self._attempts: Dict[str, int] = {}
        self._workers: List[threading.Thread] = []
        self._reconciler: Optional[threading.Thread] = None
        # Which Kodi databases a finished download has made stale, and since
        # when. Coalesced rather than refreshed per item — see _mark_dirty.
        self._dirty: Set[str] = set()
        self._dirty_since = 0.0
        self._dirty_lock = threading.Lock()
        # The Downloaded-music view is written once per generation, not once
        # per track (see _ensure_music_view).
        self._music_view_written = False
        # Albums whose completion has already been announced, so a burst of
        # tracks is one notification (see _announce_complete).
        self._announced: Set[str] = set()
        self._announce_lock = threading.Lock()
        # One *video* transcode pull at a time (plan W3.1): the encoder runs
        # flat-out with throttling force-disabled (V2), so two parallel pulls
        # would saturate the server unbidden. Originals keep the full pool.
        #
        # Audio gets its own counter, and it is the whole music pool. An
        # Opus encode is seconds of one core, nothing like an h264 pass, and
        # sharing the video slot was what made music downloads crawl: every
        # track in an album queued behind every other track's encode, which
        # measured out at roughly a tenth of Finamp's throughput. Sizing it
        # to the pool means the semaphore never actually blocks a music
        # worker — it stays a semaphore only so the two paths keep one shape.
        self._transcode_slot = threading.Semaphore(1)
        self._music_transcode_slot = threading.Semaphore(music_worker_count())
        # The aggregate bar on Kodi's library-update surface (W3.4).
        self._progress = progress.Reporter(self._should_stop)

    # -- lifecycle -----------------------------------------------------------

    def _new_wake(self) -> "threading.Event":
        """A worker's own wake Event, registered so _wake_all reaches it."""
        event = threading.Event()
        with self._wakes_lock:
            self._wakes.append(event)
        return event

    def _wake_all(self, skip: Optional["threading.Event"] = None) -> None:
        with self._wakes_lock:
            for event in self._wakes:
                if event is not skip:
                    event.set()

    def start(self) -> None:
        # No wake needed for what recover_interrupted requeued: every worker
        # runs the loop body — drain, then claim — before it ever waits.
        store.recover_interrupted()
        self._reconciler = threading.Thread(
            target=self._run_maintenance,
            name="kofin-downloads-maintenance",
            daemon=True,
        )
        self._reconciler.start()
        # Two pools, one queue. The kinds a pool claims are what keep an
        # album from sitting behind a film — and the video pool takes the
        # unknown kind, because a row queued before the type travelled with
        # the id could be anything (store.claim).
        for index in range(worker_count()):
            self._spawn_worker("kofin-downloads-%d" % index, VIDEO_KINDS)
        for index in range(music_worker_count()):
            self._spawn_worker("kofin-downloads-music-%d" % index, MUSIC_KINDS)
        LOG.info("download manager started (%d worker(s))", len(self._workers))

    def _spawn_worker(self, name: str, kinds: Tuple[str, ...]) -> None:
        worker = threading.Thread(
            target=self._run_worker,
            args=(kinds, self._new_wake()),
            name=name,
            daemon=True,
        )
        worker.start()
        self._workers.append(worker)

    def stop(self) -> None:
        self._stop.set()
        self._wake_all()
        for worker in [self._reconciler] + self._workers:
            if worker is None or not worker.is_alive():
                continue
            worker.join(timeout=JOIN_SECONDS)
            if worker.is_alive():  # pragma: no cover - watchdog logging only
                LOG.warning("%s did not stop within its deadline", worker.name)
        # After the joins, so no worker races the close; unconditional, so
        # no bar ghosts in the corner past the manager that drew it.
        self._progress.close()
        LOG.info("download manager stopped")

    def _should_stop(self) -> bool:
        return self._stop.is_set() or self._stopping.is_set()

    # -- the IPC surface (notification thread: enqueue only) -------------------

    def submit(
        self,
        item_ids: List[str],
        origin: str = store.ORIGIN_USER,
        media_types: Optional[List[str]] = None,
    ) -> None:
        """Queue ids, optionally with each one's Jellyfin DTO type.

        The type is what lets a row be claimed by the right worker pool
        before anything has been fetched for it. Unknown is fine and stays
        unknown until ``record_details`` learns it the slow way.
        """
        kinds = media_types or []
        for index, item_id in enumerate(item_ids):
            if not item_id:
                continue
            kind = MEDIA_TYPE_BY_DTO.get(kinds[index] if index < len(kinds) else "", "")
            self._ops.put(("add", str(item_id), origin, kind))
        self._wake_all()

    def cancel(self, item_id: str) -> None:
        with self._cancels_lock:
            self._cancels.add(item_id)
        self._ops.put(("cancel", item_id, "", ""))
        self._wake_all()

    def remove(self, item_id: str) -> None:
        self._ops.put(("remove", item_id, "", ""))
        self._wake_all()

    def remove_all(self) -> None:
        """The settings button: every download, finished or not.

        One op rather than one per row — the store is read on the worker
        side, where the walk is already serialized, and a NotifyAll per row
        would put a whole library's worth of messages through Kodi's
        notification bus for a single button press.
        """
        self._ops.put(("removeall", "", "", ""))
        self._wake_all()

    def _cancelled(self, item_id: str) -> bool:
        with self._cancels_lock:
            return item_id in self._cancels

    def _clear_cancel(self, item_id: str) -> None:
        with self._cancels_lock:
            self._cancels.discard(item_id)

    # -- workers ---------------------------------------------------------------

    def _run_worker(
        self,
        kinds: Tuple[str, ...] = VIDEO_KINDS,
        wake: Optional["threading.Event"] = None,
    ) -> None:
        # Own Event, never the pool's (see _wakes). Defaulted so a test can
        # drive one pass of this loop without standing a worker up.
        wake = self._new_wake() if wake is None else wake
        api = self._api_factory()
        try:
            while not self._should_stop():
                self._drain_ops(own=wake)
                if state.is_offline():
                    # Hold the queue rather than burn it: every claim while
                    # the server is gone would fail its three attempts and
                    # settle, so a user who queued a season offline came back
                    # to a list of failures instead of a list of downloads
                    # (the gap phase 1's gates exposed). Ops still drain
                    # above, so a cancel or a remove works offline.
                    self._progress.idle()
                    if wake.wait(timeout=OFFLINE_POLL_SECONDS):
                        wake.clear()
                    continue
                if self._paused_for_playback():
                    # The gate is on *claiming*, never inside _transfer: a
                    # file already coming down runs to completion, which is
                    # both what the setting promises and the only behaviour
                    # that cannot strand a half-written .part for the length
                    # of a film. Ops drain above, so cancelling and removing
                    # still work while playback holds the pool.
                    self._progress.idle()
                    if wake.wait(timeout=PLAYBACK_POLL_SECONDS):
                        wake.clear()
                    continue
                row = store.claim(kinds)
                if row is None:
                    if not self._ops.empty():
                        # Ops arrived while this worker was claiming: never
                        # sleep on a non-empty queue. This predates the
                        # per-worker Events and is no longer the only thing
                        # standing between a queued row and a 30 s wait, but
                        # it still saves a needless round trip through the
                        # wait when work is visibly pending.
                        continue
                    # Nothing left for this pool: the moment to pay for the
                    # refreshes the finished items deferred.
                    self._flush_refresh(force=True)
                    self._progress.idle()
                    if wake.wait(timeout=IDLE_POLL_SECONDS):
                        wake.clear()
                    continue
                try:
                    self._process(api, row)
                except Exception:
                    # Contain per item: a store hiccup must not kill the
                    # worker for the rest of the generation.
                    LOG.exception("download processing died for %s", row.jellyfin_id)
                    store.fail(row.jellyfin_id, "internal error")
                self._flush_refresh()
        except Exception:  # pragma: no cover - the backstop, never expected
            LOG.exception("download worker died")
        finally:
            api.close()

    def _drain_ops(self, own: Optional["threading.Event"] = None) -> None:
        """Apply everything waiting on the ops queue.

        Re-notifies the other workers when an add actually created a row, and
        that is the whole point of the method's second half. ``submit`` wakes
        the pool when it *enqueues* an op, but the row does not exist until
        some worker gets here and ``store.queue`` returns — and the worker
        that can claim it has usually spent its wake by then:

            submit sets every wake  ->  video worker wakes, drains nothing
            (a music worker got the op first), claims nothing (no row yet),
            finds the queue empty, and sleeps for the full idle poll
            ->  the music worker finishes store.queue, cannot claim an
                episode, and sleeps too  ->  nobody is left to notice

        Measured on the Omega box: the row was queued 1.6 s after the request
        and claimed 31 s after that, five trials running. The re-notify here
        closes the window; ``own`` is skipped because the draining worker is
        about to try to claim anyway.
        """
        queued = False
        while True:
            try:
                op, item_id, origin, media_type = self._ops.get_nowait()
            except Empty:
                break
            try:
                if op == "add":
                    queued = self._apply_add(item_id, origin, media_type) or queued
                elif op == "cancel":
                    self._apply_cancel(item_id)
                elif op == "remove":
                    self._apply_remove(item_id)
                elif op == "removeall":
                    self._apply_remove_all()
            except Exception:
                LOG.exception("download op %s failed for %s", op, item_id)
        if queued:
            self._wake_all(skip=own)

    def _apply_add(self, item_id: str, origin: str, media_type: str = "") -> bool:
        """Queue a row; True when one was actually created (see _drain_ops)."""
        if store.queue(
            store.Download(
                jellyfin_id=item_id, origin=origin, media_type=media_type or ""
            )
        ):
            self._attempts.pop(item_id, None)
            # New work re-arms every album announcement: downloading an
            # album, removing it and downloading it again should say so
            # both times (_announce_complete).
            with self._announce_lock:
                self._announced.clear()
            LOG.info("download queued: %s", item_id)
            return True
        return False

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

    def _apply_remove(self, item_id: str, flush: bool = True) -> None:
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
        repoint.clear_badge(row)
        # A removal answers something the user just asked for, so it does
        # not wait out the completion path's defer window — but "immediate"
        # has to mean once per *request*, not once per row. Removing an
        # album is one menu press and seventeen rows, and refreshing per row
        # meant seventeen widget passes for one answer.
        self._mark_dirty(row.media_type)
        if flush and self._ops.empty():
            self._flush_refresh(force=True)
        LOG.info("download removed: %s", item_id)

    def _apply_remove_all(self) -> None:
        """Every download goes: finished ones through the full remove path,
        unfinished ones cancelled.

        The refresh is suppressed per row and forced once at the end:
        ``_apply_remove`` normally refreshes as soon as the ops queue runs
        dry, and here it is dry from the first row on, which would make a
        whole library's worth of widget passes out of one button press.

        Unfinished rows are marked cancelled before being applied, not put
        back on the ops queue: a row being transferred right now aborts at
        its next chunk off that flag, and re-queueing would mean one op per
        row for work this loop is already doing.
        """
        rows = store.rows()

        if not rows:
            return

        for row in rows:
            if self._should_stop():
                break
            if row.state == store.DONE:
                self._apply_remove(row.jellyfin_id, flush=False)
            else:
                with self._cancels_lock:
                    self._cancels.add(row.jellyfin_id)
                self._apply_cancel(row.jellyfin_id)

        LOG.info("removed all %d download(s)", len(rows))
        self._flush_refresh(force=True)

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
        finally:
            # Every exit leaves the active set — done, failed, cancelled or
            # requeued — and only a completed item advances the bar.
            self._progress.finish(item_id, store.is_done(item_id))

    def _transfer(self, api: Any, row: "store.Download", item: JsonDict) -> None:
        item_id = row.jellyfin_id
        media_type = MEDIA_TYPE_BY_DTO.get(str(item.get("Type")))
        if media_type is None:
            store.fail(item_id, "unsupported type %r" % item.get("Type"))
            return
        source = (item.get("MediaSources") or [{}])[0]

        decision = quality.decide(api, item)  # JellyfinError -> retry ladder
        original = decision.kind == quality.ORIGINAL
        if row.rel_path and row.quality != decision.kind:
            # The target was frozen for the other kind (the settings moved
            # between attempts): the extension and the resume semantics are
            # both wrong for it, so unfreeze and let this attempt re-decide.
            self._delete_part(row)
            store.record_target(item_id, "", "")
            row.rel_path = ""

        container = (
            str(source.get("Container") or "").split(",")[0]
            if original
            else decision.container
        )
        # A transcode's finished size is unknowable up front; 0 keeps the
        # size verification and the free-space precheck honest (the reserve
        # still applies).
        size_expected = int(source.get("Size") or 0) if original else 0
        # The grouping id (the series_id column): what the item's directory
        # belongs to — a show for episodes, an album for songs.
        group_id = str(item.get("SeriesId") or item.get("AlbumId") or "")
        store.record_details(
            item_id,
            media_type,
            group_id,
            size_expected,
            _userdata_json(item.get("UserData")),
            decision.kind,
        )

        root = downloads_root()
        if not row.rel_path and not files.free_space_ok(root, size_expected):
            store.fail(item_id, "not enough free space")
            self._toast(30715, item.get("Name", item_id))
            return

        owner_id = group_id or item_id
        if original:
            rel_path, actual = self._pull_original(
                api, row, item, owner_id, container, size_expected, root
            )
        else:
            rel_path, actual = self._pull_transcode(
                api, row, item, source, owner_id, decision, root
            )

        absolute = os.path.join(root, rel_path)
        part = absolute + ".part"
        self._download_subtitles(
            api,
            item,
            source,
            os.path.dirname(absolute),
            rel_path,
            include_embedded=not original,
        )
        os.replace(part, absolute)
        store.finish(item_id, rel_path, container, actual)
        self._attempts.pop(item_id, None)
        finished = store.get(item_id)
        if finished is not None:
            repoint.repoint(finished, root)
            repoint.stamp_tag(finished)
            repoint.stamp_badge(finished)
        if media_type in ("movie", "episode"):
            self._capture_segments(api, item_id)
        if settings.get_bool("downloadsExportMetadata"):
            export.export_item(api, item, absolute)  # best-effort by contract
        if media_type == "song":
            self._ensure_music_view()
        # Deferred, not fired here. A refresh costs a widget fingerprint pass
        # and an UpdateLibrary scan, which is nothing beside a film and
        # everything beside a three-minute track — a 12-track album paid it
        # twelve times over. The flusher does it once the pool goes quiet,
        # for the databases that actually moved.
        self._mark_dirty(media_type)
        self._announce_complete(media_type, group_id, item)
        LOG.info("download complete: %s (%d bytes) at %s", item_id, actual, rel_path)

    def _capture_segments(self, api: Any, item_id: str) -> None:
        """The offline segment cache (W4.7): the raw /MediaSegments body,
        taken at completion and parsed at claim time, where the parser
        lives. A failed fetch leaves the column empty — unknown, which the
        online claim path covers with its own fallback fetch — while a
        successful empty answer is stored as known-empty so nobody asks
        again. Best-effort: segments must never fail a download."""
        import json

        try:
            payload = api.media_segments(item_id)
            store.set_segments(item_id, json.dumps(payload))
        except Exception as error:
            # Broad on purpose: this runs after the download already
            # finished, and any escape here would mislabel a completed
            # download as an internal error.
            LOG.debug("segment capture skipped for %s: %s", item_id, error)

    def _ensure_music_view(self, force: bool = False) -> None:
        """The Downloaded-music smart playlist and node exist from the first
        song on (plan W3.3); idempotent, and never worth failing a download
        over.

        Once per manager generation, not once per track: the document does
        not depend on which songs exist, so rewriting it for every track of
        an album was twelve identical writes. ``force`` is the reconcile,
        which runs precisely to heal a file somebody deleted by hand.
        """
        if self._music_view_written and not force:
            return
        try:
            from kofin.sync import playlists, views

            playlists.refresh_downloaded_music()
            views.write_music_nodes()
            self._music_view_written = True
        except Exception:  # pragma: no cover - the view is best-effort
            LOG.exception("downloaded-music view refresh failed")

    def _pull_original(
        self,
        api: Any,
        row: "store.Download",
        item: JsonDict,
        owner_id: str,
        container: str,
        size_expected: int,
        root: str,
    ) -> Tuple[str, int]:
        """The phase-1 path: the original bytes, Range-resumable."""
        item_id = row.jellyfin_id
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
            self._progress.begin(
                item_id, str(item.get("Name") or item_id), expected_total
            )
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
        return rel_path, actual

    def _pull_transcode(
        self,
        api: Any,
        row: "store.Download",
        item: JsonDict,
        source: JsonDict,
        owner_id: str,
        decision: "quality.Decision",
        root: str,
    ) -> Tuple[str, int]:
        """The W3.1 path: a progressive fMP4 (or music) transcode.

        Never resumed — a re-encode is not byte-stable, so every attempt
        starts from a clean ``.part`` — and verified by the duration probe,
        because a dead encoder ends the response cleanly and leaves no
        Content-Length to miss.
        """
        item_id = row.jellyfin_id
        slot = self._slot_for(MEDIA_TYPE_BY_DTO.get(str(item.get("Type")), ""))
        if not self._acquire_transcode_slot(item_id, slot):
            raise JellyfinError("interrupted waiting for the transcode slot")
        try:
            runtime_ticks = int(
                source.get("RunTimeTicks") or item.get("RunTimeTicks") or 0
            )
            self._progress.begin(
                item_id,
                str(item.get("Name") or item_id),
                quality.estimated_bytes(decision.url, runtime_ticks),
            )
            rel_path = row.rel_path
            stream = api.transcode_stream(decision.url)
            try:
                if not rel_path:
                    rel_path = self._decide_target(
                        item,
                        item_id,
                        owner_id,
                        decision.container,
                        stream.header("Content-Disposition"),
                    )
                absolute = os.path.join(root, rel_path)
                part = absolute + ".part"
                os.makedirs(os.path.dirname(absolute), exist_ok=True)
                _remove_quietly(part)
                self._write_body(stream, part, 0, item_id)
            finally:
                stream.close()

            actual = os.path.getsize(part) if os.path.exists(part) else 0
            self._verify_transcode(item, source, part, decision.container, actual)
            return rel_path, actual
        finally:
            self._close_transcode(api, decision.play_session_id)
            slot.release()

    def _slot_for(self, media_type: str) -> "threading.Semaphore":
        """Which transcode counter this item queues on — see the two
        semaphores in ``__init__``. Video is one at a time; music is as wide
        as its pool."""
        return (
            self._music_transcode_slot if media_type == "song" else self._transcode_slot
        )

    def _acquire_transcode_slot(
        self, item_id: str, slot: "threading.Semaphore"
    ) -> bool:
        """Wait for a transcode slot; False when stop or an outage ends the
        wait (the caller raises into the interruption paths). A worker parks
        here while another transcodes — accepted: the rest of the pool keeps
        draining originals, and the wait honors stop, outage and this item's
        own cancel within half a second."""
        while not self._should_stop() and not state.is_offline():
            if self._cancelled(item_id):
                raise _Cancelled()
            if slot.acquire(timeout=0.5):
                return True
        return False

    def _verify_transcode(
        self,
        item: JsonDict,
        source: JsonDict,
        part: str,
        container: str,
        actual: int,
    ) -> None:
        if actual <= 0:
            raise JellyfinError("transcode produced no bytes")
        runtime_ticks = int(source.get("RunTimeTicks") or item.get("RunTimeTicks") or 0)
        expected_seconds = runtime_ticks / 10_000_000
        if expected_seconds <= 0:
            return  # nothing to hold it to
        probed = probe.duration_seconds(part, container)
        if probed is None:
            return  # container this probe cannot read; clean EOF stands alone
        if probed < expected_seconds * 0.9 - 5:
            raise JellyfinError(
                "transcode truncated: %.0fs of %.0fs on disk"
                % (probed, expected_seconds)
            )

    def _close_transcode(self, api: Any, play_session_id: str) -> None:
        """End the server-side job by name. Also fired on success — closing a
        finished job is a no-op there, and saying it is free; on failure and
        cancel it is what stops an encoder working for nobody (the closed
        connection kills it too, eventually — V2 — but not promptly)."""
        if not play_session_id:
            return
        try:
            api.close_transcode(api.device_id, play_session_id)
        except JellyfinError as error:
            LOG.debug("closing the transcode job failed: %s", error)

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
        fallback = files.default_filename(item, container)
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
                        self._progress.tick(item_id, written)
        finally:
            # Also on the way out through an abort or a dead socket: the
            # resume reads the .part's real size, but a watermark left at
            # whatever the last 8 MiB tick happened to catch (or at 0)
            # misreports an interrupted download to everything that renders
            # one.
            store.record_progress(item_id, written)

    def _download_subtitles(
        self,
        api: Any,
        item: JsonDict,
        source: JsonDict,
        directory: str,
        rel_path: str,
        include_embedded: bool = False,
    ) -> None:
        """Subtitle sidecars beside the media file (plan W1.6, W3.1).

        An original download does not contain the external subtitles the
        streaming play route attaches, so a repointed item would silently
        lose them. A *transcoded* download loses the embedded text tracks
        too — the fMP4 output carries no subtitles — so those are extracted
        as sidecars as well (``include_embedded``); the endpoint serves both
        kinds, converting embedded text to the asked-for srt. Embedded image
        tracks (PGS/DVDSUB) stay lost: Kodi cannot render a standalone one,
        and burning-in is a quality decision nobody made. Kodi auto-loads
        sidecars by name. Failures are logged and non-fatal — a missing
        subtitle must never fail the download.
        """
        base = os.path.basename(rel_path).rsplit(".", 1)[0]
        taken: set = set()
        for stream_info in source.get("MediaStreams") or []:
            if stream_info.get("Type") != "Subtitle":
                continue
            external = bool(stream_info.get("IsExternal"))
            if external:
                codec = str(stream_info.get("Codec") or "").lower()
                extension = SUBTITLE_EXTENSIONS.get(codec, codec)
            elif include_embedded and stream_info.get("IsTextSubtitleStream"):
                extension = "srt"
            else:
                continue
            if not extension:
                continue
            language = str(stream_info.get("Language") or "und")
            url = api.subtitle_stream_url(
                str(item.get("Id")),
                str(source.get("Id")),
                int(stream_info.get("Index") or 0),
                extension,
            )
            # Two tracks of one language must not overwrite each other; the
            # second takes a numbered name (Kodi lists both).
            stem = "%s.%s" % (base, files.sanitize(language))
            candidate = "%s.%s" % (stem, extension)
            ordinal = 2
            while candidate in taken:
                candidate = "%s.%d.%s" % (stem, ordinal, extension)
                ordinal += 1
            taken.add(candidate)
            target = os.path.join(directory, candidate)
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
            # The server went away mid-transfer: back to queued without
            # spending an attempt, so the offline hold in the worker loop
            # picks it up again on reconnect. Left active it would sit stuck
            # until the next service start — recover_interrupted runs only
            # there (a gap phase 3 closed; shutdown below is different
            # because the manager *is* about to restart).
            LOG.info("download interrupted by an outage: %s", item_id)
            store.release(item_id)
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

    # -- maintenance: reconcile once, then the retention sweep ------------------

    def _run_maintenance(self) -> None:
        """Reconcile at start, then sweep on a slow beat (plan W4.2). The
        first sweep runs right after the reconcile — it covers items watched
        while the service was down."""
        self._reconcile_once()
        self._backfill_segment_caches()
        self._retention_sweep()
        self._stale_sweep()
        while not self._stop.wait(timeout=RETENTION_SWEEP_SECONDS):
            if self._should_stop():
                return
            # Before the retention sweep: a file somebody deleted by hand is
            # not a download any more, and the sweep should not spend a
            # watched-check on it.
            self._sweep_vanished()
            self._retention_sweep()
            self._stale_sweep()

    def _backfill_segment_caches(self) -> None:
        """Downloads without a segment cache get one at the next start
        (W4.7): pre-cache downloads, and any whose capture failed. Online
        only, never-fetched rows only — a known-empty answer is kept — so
        the pass is one bounded fetch per healed row, then permanently
        quiet."""
        if state.is_offline():
            return
        wanting = [
            row
            for row in store.rows(store.DONE)
            if row.media_type in ("movie", "episode") and not row.segments_json
        ]
        if not wanting:
            return
        api = self._api_factory()
        try:
            for row in wanting:
                if self._should_stop():
                    return
                self._capture_segments(api, row.jellyfin_id)
        finally:
            api.close()

    def _reconcile_once(self) -> None:
        """Walk the done rows once at start: a missing file is cleaned up
        after (:meth:`_handle_vanished`), and a present file re-asserts the
        repoint — which is what heals a library repair, whose rebuilt rows
        are all in writer shape."""
        try:
            root = downloads_root()
            touched = False
            songs = False
            for row in store.rows(store.DONE):
                if self._should_stop():
                    return
                songs = songs or row.media_type == "song"
                absolute = os.path.join(root, row.rel_path)
                if not row.rel_path or not os.path.exists(absolute):
                    self._handle_vanished(row, root)
                    touched = True
                    continue
                if repoint.repoint(row, root):
                    repoint.stamp_tag(row)  # idempotent; a repair wiped links
                    repoint.stamp_badge(row)
                    touched = True
            if songs:
                self._ensure_music_view(force=True)  # heals a hand-deleted .xsp too
            if touched:
                self._refresh_quietly(["music", "video"] if songs else ["video"])
        except Exception:  # pragma: no cover - never break service start
            LOG.exception("downloads reconcile failed")

    def _handle_vanished(self, row: "store.Download", root: str) -> None:
        """A downloaded file deleted behind Kodi's back — a file manager, a
        card pulled and cleaned up on a computer, another app.

        This used to restore the library row and leave a ``failed`` store row
        behind it, which meant the leftovers stayed: the sidecars, the empty
        season directory, the Downloads tag and the badge. The item went on
        advertising itself as downloaded in the Downloaded nodes with nothing
        behind it.

        So it goes through the removal path proper, and the item is marked
        watched. Deleting a file by hand is how people finish with something,
        and the alternative reading — that they wanted it re-downloaded — is
        the one the automatic arms would act on.
        """
        LOG.warning(
            "downloaded file missing for %s; cleaning up and marking it watched",
            row.jellyfin_id,
        )
        repoint.restore(row, root)
        self._delete_media(row)
        # Before the tag check, exactly as in _apply_remove: an episode's
        # unstamp asks whether a sibling still holds the show in the node.
        store.remove(row.jellyfin_id)
        repoint.unstamp_tag(row)
        repoint.clear_badge(row)
        self._mark_watched(row)
        self._mark_dirty(row.media_type)

    def _mark_watched(self, row: "store.Download") -> None:
        """Mark a vanished download watched, here and on the server.

        The two halves are independent and neither gates the other: a local
        row kofin cannot find (an item removed from the library since) does
        not make the server's copy wrong, and a server that cannot be
        reached does not make Kodi's wrong.

        Songs are left alone, the same exclusion the retention sweep makes:
        a played track is not a finished one, and "watched" is not a thing a
        song is.
        """
        if row.media_type not in ("movie", "episode"):
            return
        self._mark_local_watched(row)
        self._push_played(row)

    def _mark_local_watched(self, row: "store.Download") -> None:
        """Kodi's own playcount, written straight through SQLite.

        Never JSON-RPC: an announcer-visible library write feeds the
        userdata echo cycle (report → server echoes UserDataChanged → kofin
        writes it back), which terminates only because direct writes raise
        no Kodi announcement — see service/kodiuserdata.py.
        """
        try:
            with Database("kofin") as kofin_db, Database("video") as video:
                mapping = repoint.mapping_for_on(kofin_db.cursor, row.jellyfin_id)
                if mapping is None:
                    return
                video.cursor.execute(
                    "UPDATE files SET playCount = 1, lastPlayed = ? WHERE idFile = ?",
                    (
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        mapping.kodi_fileid,
                    ),
                )
        except Exception:
            LOG.exception("could not mark %s watched locally", row.jellyfin_id)

    def _push_played(self, row: "store.Download") -> None:
        """Tell the server, or park it for the next connect.

        Parked rather than dropped when offline: deleting a file is exactly
        the thing someone does on a plane, and the replay path already exists
        for watching one there (downloads/pending.py).
        """
        from kofin.downloads import pending

        if state.is_offline():
            pending.enqueue(row.jellyfin_id, row.media_type, played=True)
            return
        api = self._api_factory()
        try:
            api.mark_played(row.jellyfin_id)
        except Exception as error:
            LOG.info(
                "played push failed for %s (%s); parking it for replay",
                row.jellyfin_id,
                error,
            )
            pending.enqueue(row.jellyfin_id, row.media_type, played=True)
        finally:
            api.close()

    def _sweep_vanished(self) -> None:
        """The presence check on the maintenance beat, not just at start.

        ``_reconcile_once`` runs once per service generation, so a file
        deleted mid-session went unnoticed until the next restart — the item
        stayed in the Downloaded nodes, and playing it failed in Kodi rather
        than falling back to the server. An ``os.path.exists`` per done row
        every few minutes costs nothing.
        """
        try:
            root = downloads_root()
            touched = False
            songs = False
            for row in store.rows(store.DONE):
                if self._should_stop():
                    return
                if row.rel_path and os.path.exists(os.path.join(root, row.rel_path)):
                    continue
                if self._playing_now(row):
                    # Not gone, just unreadable for a moment (a network share
                    # blinking); tearing the row down under the player would
                    # turn a stutter into a lost download.
                    continue
                songs = songs or row.media_type == "song"
                self._handle_vanished(row, root)
                touched = True
            if touched:
                self._refresh_quietly(["music", "video"] if songs else ["video"])
        except Exception:  # pragma: no cover - never kill the maintenance loop
            LOG.exception("vanished-download sweep failed")

    def _retention_sweep(self) -> None:
        """W4.2: remove watched downloads, through the full remove path —
        never a bare unlink.

        The backstop for what the end-of-playback offer cannot see: an item
        watched while the service was down, or through something other than
        kofin's player. It runs only in the *silent* mode — with the confirm
        mode chosen, removal is a question, and this sweep has no one to ask
        (the thing it noticed may have finished an hour ago).

        Two exclusions are load-bearing: songs are never swept (a played
        track is not a finished one), and the item currently playing is
        skipped — Kodi marks played at ~90%, and a sweep firing in a binge
        episode's last minutes would delete the file under the player.
        Watched-ness is Kodi's own playcount — local truth, works offline.
        """
        try:
            if not settings.get_bool("downloadsDeleteAfterWatching"):
                return
            if not settings.get_bool("downloadsDeleteAutomatically"):
                return
            for row in store.rows(store.DONE):
                if self._should_stop():
                    return
                if row.media_type not in ("movie", "episode"):
                    continue
                if not _watched_locally(row):
                    continue
                if self._playing_now(row):
                    continue
                LOG.info(
                    "retention: removing watched %s (%s)",
                    row.jellyfin_id,
                    row.origin,
                )
                self._apply_remove(row.jellyfin_id)
        except Exception:  # pragma: no cover - never kill the maintenance loop
            LOG.exception("retention sweep failed")

    def _stale_sweep(self) -> None:
        """W4.8: remove downloads nobody has touched in ``downloadsStaleDays``
        days, through the full remove path like every other removal here.

        The companion to the watched sweep, and deliberately not nested under
        it: that pair answers "you finished it", this one answers "you never
        got to it", which is the case a watched-ness test can never reach.
        Watched items past the cutoff are collected too — a download watched
        a month ago is stale by any reading, and this is the only thing that
        clears them with ``downloadsDeleteAfterWatching`` off.

        Age is the *last touch*: the later of the download finishing and
        Kodi's own ``files.lastPlayed``, so re-watching something resets its
        clock. Both facts are local, so the sweep works offline exactly like
        the watched one.

        The exclusions, in order of how badly each would be missed:

        * an item with a **resume point** is never stale, however long it has
          sat. A part-watched film is in progress, not abandoned, and it is
          precisely what a pure age clock reads wrong. The exemption needs no
          bookkeeping to clear: Kodi drops the resume bookmark when playback
          reaches the end, so finishing something puts it back in the pool.
          It inherits from the server too — the writers write playstate from
          Jellyfin's UserData, so starting an episode on a phone protects the
          copy downloaded here.
        * the file currently playing, for the reason the watched sweep skips
          it.
        * songs, the same exclusion the rest of the lifecycle makes: staleness
          per track would delete an album one track at a time.
        * a row whose age cannot be established — no ``done_at``, never
          played, or no library mapping to read. An unknown age is not an old
          age.
        """
        try:
            if not settings.get_bool("downloadsDeleteStale"):
                return
            days = max(1, settings.get_int("downloadsStaleDays") or STALE_DAYS_DEFAULT)
            cutoff = time.time() - days * SECONDS_PER_DAY
            for row in store.rows(store.DONE):
                if self._should_stop():
                    return
                if row.media_type not in ("movie", "episode"):
                    continue
                if not row.done_at or row.done_at > cutoff:
                    # The cheap half, off the store row alone: a library
                    # inside its window never opens Kodi's database here.
                    continue
                touched = _last_touch(row)
                if touched is None:
                    continue
                last_played, resuming = touched
                if resuming or last_played > cutoff:
                    continue
                if self._playing_now(row):
                    continue
                LOG.info(
                    "stale: removing %s, untouched for %d day(s)",
                    row.jellyfin_id,
                    int(
                        (time.time() - max(row.done_at, last_played)) / SECONDS_PER_DAY
                    ),
                )
                self._apply_remove(row.jellyfin_id)
        except Exception:  # pragma: no cover - never kill the maintenance loop
            LOG.exception("stale-download sweep failed")

    def _paused_for_playback(self) -> bool:
        """Whether the pool should stop claiming while something plays.

        ``isPlaying`` rather than ``state.get_playing_id``: the property only
        covers playbacks kofin claimed, and a download competing for
        bandwidth does not care who started the video. Audio counts too — a
        transcode pull saturates the same link either way.
        """
        if not settings.get_bool("downloadsPauseDuringPlayback"):
            return False
        try:
            return bool(xbmc.Player().isPlaying())
        except RuntimeError:  # pragma: no cover - no player, so nothing plays
            return False

    def wake(self) -> None:
        """Let the pool re-check its gates now rather than at the next poll —
        the service calls this when playback stops."""
        self._wake_all()

    def _playing_now(self, row: "store.Download") -> bool:
        try:
            playing = xbmc.Player().getPlayingFile()
        except RuntimeError:
            return False
        if not playing or not row.rel_path:
            return False
        absolute = os.path.join(downloads_root(), row.rel_path)
        return os.path.abspath(playing) == os.path.abspath(absolute)

    # -- plumbing --------------------------------------------------------------

    # -- making writes visible --------------------------------------------------

    def _mark_dirty(self, media_type: str) -> None:
        """Note that a database needs a refresh, without doing it yet.

        Songs move Kodi's music database, everything else the video one, and
        asking for the wrong database is not free: the refresh runs a widget
        fingerprint pass over whatever it is handed. Before this split every
        finished track fired ``UpdateLibrary(video)``.
        """
        with self._dirty_lock:
            self._dirty.add("music" if media_type == "song" else "video")
            if not self._dirty_since:
                self._dirty_since = time.monotonic()

    def _flush_refresh(self, force: bool = False) -> None:
        """Refresh the dirty databases — on ``force`` (the pool went quiet),
        or once the oldest mark has waited out the defer window."""
        with self._dirty_lock:
            if not self._dirty:
                return
            waited = time.monotonic() - self._dirty_since
            if not force and waited < REFRESH_MAX_DEFER_SECONDS:
                return
            databases = sorted(self._dirty)
            self._dirty.clear()
            self._dirty_since = 0.0
        self._refresh_quietly(databases)

    def _refresh_quietly(self, databases: Optional[List[str]] = None) -> None:
        try:
            self._refresh(databases or ["video"])
        except Exception:  # pragma: no cover - refresh is best-effort
            LOG.exception("downloads refresh failed")

    def _announce_complete(
        self, media_type: str, group_id: str, item: JsonDict
    ) -> None:
        """One "Download complete" per album, not one per track.

        A track is seconds of work and an album lands as a burst, so the
        per-item toast stacked a dozen notifications naming songs nobody
        had asked for individually — the album is the thing they asked for.
        Video keeps its per-item toast: a film or an episode *is* the unit
        somebody chose, and they finish minutes apart.

        The album is announced by whichever worker finishes the last of it,
        claimed under the lock so two tracks landing together cannot both
        be last. ``_apply_add`` re-arms every album when anything new is
        queued, so downloading the same one again announces again.
        """
        if media_type != "song" or not group_id:
            self._toast(30712, item.get("Name") or item.get("Id", ""))
            return
        with self._announce_lock:
            if group_id in self._announced:
                return
            if store.container_counts(group_id)["pending"]:
                return  # more of this album is still coming
            self._announced.add(group_id)
        self._toast(30712, item.get("Album") or item.get("Name") or group_id)

    def _toast(self, string_id: int, name: Any) -> None:
        if not notify_allowed(string_id):
            return
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


def _watched_locally(row: "store.Download") -> bool:
    """Kodi's own playcount for the item's file row — the local truth, which
    is what lets the sweep run offline."""
    with Database("kofin") as kofin_db, Database("video") as video:
        mapping = repoint.mapping_for_on(kofin_db.cursor, row.jellyfin_id)
        if mapping is None:
            return False
        video.cursor.execute(
            "SELECT playCount FROM files WHERE idFile = ?",
            (mapping.kodi_fileid,),
        )
        found = video.cursor.fetchone()
    return bool(found is not None and found[0])


def _last_touch(row: "store.Download") -> Optional[Tuple[float, bool]]:
    """``(lastPlayed as unix seconds, has a resume point)``, or None when the
    item has no library row to read (the stale sweep's "unknown age").

    Both facts hang off the one file row the download was repointed onto.
    The resume point is a ``type = 1`` bookmark, Kodi's own RESUME kind — the
    same row ``widgetstate`` reads for its in-progress percentages — which
    Kodi deletes at the end of playback, and which the sync writers also
    write from the server's UserData.
    """
    with Database("kofin") as kofin_db, Database("video") as video:
        mapping = repoint.mapping_for_on(kofin_db.cursor, row.jellyfin_id)
        if mapping is None:
            return None
        video.cursor.execute(
            "SELECT lastPlayed FROM files WHERE idFile = ?", (mapping.kodi_fileid,)
        )
        found = video.cursor.fetchone()
        if found is None:
            return None
        video.cursor.execute(
            "SELECT 1 AS present FROM bookmark WHERE idFile = ? AND type = 1 LIMIT 1",
            (mapping.kodi_fileid,),
        )
        resuming = video.cursor.fetchone() is not None
    return _as_epoch(found[0]), resuming


def _as_epoch(stamp: Any) -> float:
    """A Kodi timestamp column as unix seconds; 0.0 for unset or unparseable.

    Two spellings reach the column and both are local time: Kodi's own (and
    ``_mark_local_watched``'s) ``'%Y-%m-%d %H:%M:%S'``, and the ISO ``T`` form
    the sync writers hand it (``shims.date_played``). 0.0 rather than an
    exception for anything else — a column kofin did not write is not a
    reason to abandon a sweep.
    """
    if not stamp:
        return 0.0
    try:
        return datetime.strptime(
            str(stamp)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S"
        ).timestamp()
    except ValueError:
        return 0.0


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# What the metadata export leaves at directory level (W4.3). A directory
# holding only these counts as empty for pruning: the media left, and its
# escape-hatch sidecars must not keep the tree alive. The per-item
# ``<basename>.nfo`` needs no entry — the sibling sweep in ``_delete_media``
# already takes everything sharing the media file's stem.
EXPORTED_COMPANIONS = frozenset(
    {"poster.jpg", "fanart.jpg", "tvshow.nfo", "folder.jpg"}
)


def _sweep_exported(directory: str) -> None:
    try:
        names = os.listdir(directory)
    except OSError:
        return
    if not names or not set(names) <= EXPORTED_COMPANIONS:
        return
    for name in names:
        _remove_quietly(os.path.join(directory, name))


def _prune_empty_dirs(directory: str, root: str) -> None:
    """Remove now-empty directories up to (never including) the root; a
    directory down to its exported metadata counts as empty."""
    current = os.path.abspath(directory)
    stop = os.path.abspath(root)
    while current.startswith(stop) and current != stop:
        _sweep_exported(current)
        try:
            os.rmdir(current)
        except OSError:
            return
        current = os.path.dirname(current)
