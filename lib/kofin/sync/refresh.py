"""The widget-refresh policy (P2.3): what a write has to do so Kodi shows it.

Kodi raises no library-change event for direct SQLite writes, so a list or
a home widget showing the affected library does not pick them up on its
own, and ``Library.HasContent`` stays cached. ``Refresher`` owns the four
answers kofin has to that -- the fingerprint gate that suppresses a refresh
nothing would notice, the settle window that folds a burst of drains into
one refresh, the cheap scan/probe that fires the library event, and the
skin reload for the empty→populated transition (with its during-playback
hold) -- and nothing else. The Library asks it: ``refresh`` when something
was written, ``arm`` when a drain completed, ``settled`` on every tick, and
``flush_pending_reload`` for the held reload.

Measured facts these rest on are in docs/widget-refresh-plan.md; the
inline notes below keep the ones that are easy to undo.
"""

from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

import xbmc

from kofin.core.log import Logger
from kofin.sync import widgetstate
from kofin.sync.clock import Deferred
from kofin.sync.db import Database

LOG = Logger(__name__)

# A directory that does not exist: scanning it gets the music library event
# without the walk (see refresh_music).
MUSIC_REFRESH_PROBE = "special://temp/kofin-music-refresh-probe/"

VIDEO_CONTENT_FLAGS = (
    "Library.HasContent(Movies)",
    "Library.HasContent(TVShows)",
    "Library.HasContent(MusicVideos)",
)
MUSIC_CONTENT_FLAGS = ("Library.HasContent(Music)",)
CONTENT_FLAG_TIMEOUT_SECONDS = 10.0
CONTENT_FLAG_POLL_SECONDS = 0.25

# Drain-completion refreshes wait out this settle, so the mini-cycles one
# user action fans out into (a music track change is two userdata echoes,
# stop-of-A and start-of-B, seconds apart) fold into a single refresh
# instead of re-rendering every widget per echo (widget-refresh-plan F3/D3).
# Two service ticks: long enough to catch the trailing echo, short enough
# that a lone change is visible almost as fast as before.
REFRESH_SETTLE_SECONDS = 4
# ...but never wait longer than this from the first deferred cycle: a steady
# event stream re-arms the settle forever, and bounded staleness beats none.
REFRESH_MAX_HOLD_SECONDS = 15


class Refresher:
    def __init__(
        self,
        owner,
        required_kinds: Callable[[], Set[str]],
        cycle_active: Callable[[], bool],
        now=None,
    ) -> None:
        # The player and the monitor are read off the owner at use time:
        # the Library builds this in its constructor, and tests replace
        # both on the Library afterwards.
        self.owner = owner
        self.required_kinds = required_kinds
        self.cycle_active = cycle_active
        # Per-database widget fingerprints from the last refresh decision.
        self.widget_fingerprints: Dict[str, Dict[str, str]] = {}
        # Databases owed a settled refresh, and the settle clock over them.
        self.pending: Set[str] = set()
        self.settle = Deferred(REFRESH_SETTLE_SECONDS, **({"now": now} if now else {}))
        # A first-content skin reload held back because video was playing.
        self.pending_skin_reload = False

    @property
    def player(self):
        return self.owner.player

    @property
    def monitor(self):
        return self.owner.monitor

    # -- the refresh itself ---------------------------------------------------

    def refresh(self, databases: Iterable[str], force_reload: bool = False) -> None:
        """Make writes made straight to Kodi's databases visible.

        ``force_reload`` rebuilds the skin whenever anything moved, instead
        of asking the first-content probes whether it is needed. The end of
        a full sync passes it: the probes key on ``Library.HasContent``, and
        that bool can flip true *mid-sync* -- it is a tri-state cache in
        Kodi's ``CLibraryGUIInfo``, re-queried whenever a scan resets it --
        so by the end it reads "Kodi knows about this content" when the
        question that matters is "has Home been rebuilt since the content
        appeared" (measured on a Pi 3B: a movies reload rebuilt Home while
        music was empty, music synced for 27 minutes, and the end-of-sync
        music probe then self-disarmed and left the row blank).

        Stays suppressed when the fingerprints say nothing moved, so a
        resumed sync that changed nothing still reloads nothing.

        ``UpdateLibrary(video)`` is the reset for the cached bools, and it
        is cheap *by construction*: every path the video writers create
        carries ``noUpdate=1``, so the scan walks nothing and still fires
        the scan-finished event. ``UpdateLibrary(music)`` is a different
        animal and is never called: MyMusic's path table has no noUpdate,
        so a music scan probes every song's remote path (~21k requests) and
        overlapping scans have crashed Kodi on Android. Music gets the
        probe scan (refresh_music) and the container refresh only.
        """
        databases = set(databases)

        if not databases:
            return

        self.pending -= databases

        if not self.pending:
            self.settle.disarm()

        moved = self.moved(databases)

        if not moved:
            LOG.info(
                "--[ widgets unchanged: %s; refresh suppressed ]",
                "+".join(sorted(databases)),
            )
            return

        reload_flags: List[str] = []

        if "video" in moved:
            if force_reload or self.video_content_hidden():
                reload_flags.extend(VIDEO_CONTENT_FLAGS)

            xbmc.executebuiltin("UpdateLibrary(video)")

        if "music" in moved:
            if force_reload or self.music_content_hidden():
                reload_flags.extend(MUSIC_CONTENT_FLAGS)

            self.refresh_music()

        if reload_flags:
            self.reload_for_content(tuple(reload_flags))

        if xbmc.getCondVisibility("Window.IsMedia") and (
            widgetstate.container_wants_refresh(
                xbmc.getInfoLabel("Container.FolderPath"), moved
            )
        ):
            xbmc.executebuiltin("Container.Refresh")

    def moved(self, databases: Set[str]) -> Set[str]:
        """The candidate databases whose widget fingerprint moved since the
        last refresh; unknown fingerprints count as moved (pvr.kofin's
        first-poll rule -- a service restart refreshes once).

        Computed only here, at refresh decision time, for the candidates
        only: behind the settle that is at most one fingerprint pass per
        user action. No process lock is taken -- the connections run WAL, so
        the read never blocks a mid-drain writer and must not block the
        service tick; a mid-drain snapshot at worst costs one extra refresh
        when that drain completes and re-arms. An unreadable fingerprint
        refreshes: firing for nothing is recoverable, suppressing a real
        change is not.
        """
        moved = set()

        for db_file in sorted(databases):
            if db_file == "music" and "music" not in self.required_kinds():
                moved.add(db_file)
                continue

            try:
                current = widgetstate.fingerprint(db_file)
            except Exception as error:
                LOG.warning(
                    "widget fingerprint failed for %s (%s); refreshing",
                    db_file,
                    error,
                )
                self.widget_fingerprints.pop(db_file, None)
                moved.add(db_file)
                continue

            stored = self.widget_fingerprints.get(db_file)
            changed = widgetstate.moved_sections(stored or {}, current)

            if changed:
                LOG.info(
                    "--[ widgets moved: %s/%s ]", db_file, "+".join(sorted(changed))
                )
                self.widget_fingerprints[db_file] = current
                moved.add(db_file)

        return moved

    # -- the settle ----------------------------------------------------------

    def arm(self, databases: Iterable[str]) -> None:
        """Defer a drain-completion refresh behind the settle window.

        Each drain pushes the due clock out by REFRESH_SETTLE_SECONDS; the
        hold clock is stamped once, by the first deferred drain, and caps
        how long re-arming can postpone the refresh.
        """
        databases = set(databases)

        if not databases:
            return

        self.pending |= databases
        self.settle.settle(REFRESH_SETTLE_SECONDS, REFRESH_MAX_HOLD_SECONDS)

    def settled(self) -> Optional[Set[str]]:
        """The databases whose deferred refresh is due now, taking them; None
        while nothing is owed or the settle must still hold.

        Waits for quiet -- never before the settle is out, and an active
        cycle holds it (that cycle's completion folds its own databases in
        and re-arms) -- but never past the hold cap, so a steady stream of
        server events cannot postpone visibility indefinitely.
        """
        if not self.pending:
            return None

        if not self.settle.capped():
            if self.settle.waiting():
                return None

            if self.cycle_active():
                return None

        databases, self.pending = self.pending, set()
        self.settle.disarm()
        return databases

    # -- the skin reload -----------------------------------------------------

    def reload_for_content(self, conditions: Tuple[str, ...]) -> None:
        """Rebuild the skin for the empty -> populated transition, once the
        scan cycle has flipped a matching ``Library.HasContent`` bool.

        A reload is the only mechanism that works here: the skin's widget
        sections are gated on ``Library.HasContent`` conditions that bake at
        window load, and a widget container whose last fetch was empty is
        deaf to library announcements. Polling for the flag replaces the old
        fixed 2 s wait; on timeout the reload still fires, because a reload
        against stale bools at least becomes right on the next one, while
        not reloading leaves the section invisible until Kodi restarts.

        Held while video plays -- a skin reload rebuilds the OSD under the
        viewer -- and fired by the service tick once playback ends.
        """
        for _ in range(int(CONTENT_FLAG_TIMEOUT_SECONDS / CONTENT_FLAG_POLL_SECONDS)):
            if any(xbmc.getCondVisibility(flag) for flag in conditions):
                break

            if self.monitor.waitForAbort(CONTENT_FLAG_POLL_SECONDS):
                return
        else:
            LOG.warning(
                "Library.HasContent did not flip within %ss; reloading anyway",
                CONTENT_FLAG_TIMEOUT_SECONDS,
            )

        if self.player.isPlayingVideo():
            LOG.info("holding the first-content skin reload until playback ends")
            self.pending_skin_reload = True
            return

        self.fire_skin_reload()

    def flush_pending_reload(self) -> bool:
        """Fire a held first-content reload once video playback has ended."""
        if not self.pending_skin_reload or self.player.isPlayingVideo():
            return False

        self.pending_skin_reload = False
        self.fire_skin_reload()
        return True

    def fire_skin_reload(self) -> None:
        LOG.info("first content synced; reloading skin for home widgets")
        xbmc.executebuiltin("ReloadSkin()")

    def reload_after_repair(self, kinds: Iterable[str]) -> None:
        """Rebuild the skin once a repair has re-added its libraries.

        A repair empties whole Kodi tables and refills them over minutes,
        and any home widget that re-fetches inside that hollow gets zero
        items -- a DirectoryProvider whose last fetch was empty is deaf to
        every later library announcement, so the end-of-sync refresh cannot
        reach it (observed live: a 27-minute music repair left the Music row
        blank with the data underneath fully healed). The first-content
        probes cannot cover this: they key on Library.HasContent, whose
        cached bool only re-samples after a scan cycle, and a repair's scan
        cycles all run while the tables hold rows. So the repair owns an
        unconditional reload, routed through the first-content machinery
        for its HasContent poll and its during-playback hold.
        """
        kinds = set(kinds)
        flags: List[str] = []

        if "video" in kinds:
            flags.extend(VIDEO_CONTENT_FLAGS)

        if "music" in kinds:
            flags.extend(MUSIC_CONTENT_FLAGS)

        self.reload_for_content(
            tuple(flags) or VIDEO_CONTENT_FLAGS + MUSIC_CONTENT_FLAGS
        )

    # -- the probes ----------------------------------------------------------

    def music_content_hidden(self) -> bool:
        """MyMusic holds rows while Kodi still believes there is no music
        library. Guarded on the whitelist actually requiring music so this
        never puts the music schema gate in front of users who never asked
        kofin to touch their music. Self-limiting like the video probe: once
        the reload has happened the cache is right and this is False."""
        if xbmc.getCondVisibility("Library.HasContent(Music)"):
            return False

        if "music" not in self.required_kinds():
            return False

        try:
            with Database("music") as musicdb:
                for table in ("album", "song"):
                    musicdb.cursor.execute("SELECT 1 FROM %s LIMIT 1" % table)
                    if musicdb.cursor.fetchone():
                        return True
        except Exception:
            LOG.exception("could not determine music library content state")

        return False

    def refresh_music(self) -> None:
        """Give music the library event that direct SQLite writes never fire.

        Scanning a directory that **does not exist** gets the event without
        the walk: Kodi logs "does not exist - skipping scan", finishes in 0 s
        having probed nothing, and still completes the scan cycle that
        invalidates the cached containers. Verified on both generations.
        """
        if xbmc.getCondVisibility("Library.IsScanningMusic"):
            return

        xbmc.executebuiltin("UpdateLibrary(music,%s)" % MUSIC_REFRESH_PROBE)

    def video_content_hidden(self) -> bool:
        """Whether Kodi still believes the video library is empty while rows
        exist -- the state where Home reads "Your library is currently empty".

        Per kind, not "does the video library have anything": Kodi's cached
        bool is per kind and so is the widget row it gates, and with
        libraries published as they finish a movies-only reload rebuilds Home
        while ``tvshow`` is still empty. Testing the stale state itself keeps
        this self-limiting: once every populated kind's bool is right it is
        False forever after, and it stays False on a profile whose library was
        populated at startup -- the reload only ever costs the first sync.
        """
        try:
            with Database("video") as videodb:
                for flag, table in (
                    ("Library.HasContent(Movies)", "movie"),
                    ("Library.HasContent(TVShows)", "tvshow"),
                    ("Library.HasContent(MusicVideos)", "musicvideo"),
                ):
                    if xbmc.getCondVisibility(flag):
                        continue

                    videodb.cursor.execute("SELECT 1 FROM %s LIMIT 1" % table)
                    if videodb.cursor.fetchone():
                        return True
        except Exception:
            LOG.exception("could not determine video library content state")

        return False
