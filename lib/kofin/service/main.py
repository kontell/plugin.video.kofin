"""Service lifecycle: build, run, and rebuild on soft restart.

The outer loop owns restarts — a restart tears the Service object down and
builds a fresh one. Nothing may survive a cycle at module level; all state
lives on the objects rebuilt each pass.
"""

import json
import threading
import time
from typing import Any, Dict, List, Optional

import xbmc

from kofin.core import auth, diag, ipc, log, settings, state, toast
from kofin.core.api import Api
from kofin.core.http import Http, JellyfinError
from kofin.core.log import Logger
from kofin.core.settings import Credentials, addon_version
from kofin.core.ws import WSClient
from kofin.service import artcache, backdrop, chapters
from kofin.service.kodiuserdata import KodiUserData
from kofin.service.player import Player
from kofin.service.remote import RemoteHandler
from kofin.service.settings_apply import SettingsApplier

LOG = Logger(__name__)

LIBRARY_COMMANDS = frozenset(
    {
        ipc.SYNC_LIBRARY,
        ipc.REMOVE_LIBRARY,
        ipc.REPAIR_LIBRARY,
        ipc.UPDATE_LIBRARY,
        ipc.REFRESH_BOXSETS,
    }
)

DOWNLOAD_COMMANDS = frozenset(
    {ipc.DOWNLOAD_ADD, ipc.DOWNLOAD_CANCEL, ipc.DOWNLOAD_REMOVE}
)

# Seconds the service ignores settings changes after start, covering Kodi's
# startup settings-load transients. A user cannot open the settings dialog and
# edit within this window; a real change always lands well after it.
SETTINGS_READY_DELAY = 5.0

# How long the teardown waits for the library thread before giving up on it.
# Long enough to cover a page fetch and a writer batch — the reason the fork's
# 15 s was too short — and bounded, which the first version of this was not:
# waiting on `abortRequested` alone never ends on an addon disable (that flag
# is Kodi shutting down, not the addon being bounced), so the service script
# never returned. Kodi gives a stopping script five seconds, then logs
# "script didn't stop"; measured after that, *every* later Python invocation
# in Kodi hung — plugin listings and plays included — until Kodi restarted.
# A thread that outlives this deadline keeps PROP_SYNC_STOP raised instead
# (see state.clear_all), so it stays paused rather than resuming into the
# replacement Library.
#
# What the deadline is actually for, measured on Omega rather than assumed: on
# an addon *bounce* it is never reached. Kodi's "let's kill it" at five seconds
# is not a kill — it raises abortRequested, the loop below sees it on its first
# tick and gives up there. This number therefore governs only a soft restart
# (kofin's own restart IPC), where Kodi is not stopping the script and nothing
# else bounds the wait.
#
# Which means the teardown returning promptly buys less than it looks like it
# does: Kodi will not finalise a script while a thread that script started is
# still alive, so it goes on to log "waiting on thread <id>" and blocks there
# regardless of how tidily the service exited. The only thing that shortens
# that is the stuck thread itself returning sooner, which is why the retry
# ladder had to learn about the stop flag (core/http.Http, ``abort``).
LIBRARY_JOIN_SECONDS = 30.0

# Server lifecycle websocket messages -> the string announcing them. These are
# the server telling us it is about to go away; the websocket drop that
# follows is then explained rather than mysterious.
SERVER_LIFECYCLE_MESSAGES = {
    "ServerRestarting": 30417,
    "ServerShuttingDown": 30418,
}

CAPABILITIES: Dict[str, Any] = {
    "PlayableMediaTypes": "Audio,Video",
    "SupportsMediaControl": True,
    "SupportedCommands": (
        "MoveUp,MoveDown,MoveLeft,MoveRight,Select,"
        "Back,ToggleContextMenu,ToggleFullscreen,ToggleOsdMenu,"
        "GoHome,PageUp,NextLetter,GoToSearch,"
        "GoToSettings,PageDown,PreviousLetter,TakeScreenshot,"
        "VolumeUp,VolumeDown,ToggleMute,SendString,DisplayMessage,"
        "SetAudioStreamIndex,SetSubtitleStreamIndex,"
        "SetRepeatMode,Mute,Unmute,SetVolume,"
        "Play,Playstate,PlayNext,PlayMediaSource"
    ),
}


class Backoff:
    """Exponential retry schedule: 5s doubling to a 120s ceiling."""

    def __init__(self, start: float = 5.0, ceiling: float = 120.0) -> None:
        self._start = start
        self._ceiling = ceiling
        self._delay = start
        self.next_attempt = 0.0

    def failed(self, now: float) -> float:
        """Record a failure; returns the delay until the next attempt."""
        delay = self._delay
        self.next_attempt = now + delay
        self._delay = min(self._delay * 2, self._ceiling)
        return delay

    def succeeded(self) -> None:
        self._delay = self._start
        self.next_attempt = 0.0

    def due(self, now: float) -> bool:
        return now >= self.next_attempt


class Service(xbmc.Monitor):
    def __init__(self) -> None:
        super().__init__()
        self._restart_requested = False
        # This generation is tearing down. Instance state, not PROP_SYNC_STOP,
        # and the difference is measurable: the next generation lowers that
        # property on the way up (see run_forever), which handed a thread
        # orphaned by *this* teardown a transport whose abort had gone quiet
        # again — it went straight back to riding the full retry ladder. An
        # Event owned by the generation that raised it cannot be un-raised by
        # its successor.
        self._stopping = threading.Event()
        self.credentials = Credentials.load()
        self.http = Http(settings.get_bool("sslVerify"), abort=self._abort_transport)
        self.api = Api.from_credentials(self.http, self.credentials)
        self.ws: Optional[WSClient] = None
        self.player = Player(self.api)
        self.remote = RemoteHandler()
        self.kodi_userdata = KodiUserData(self.api)
        self.library: Optional[Any] = None  # kofin.sync.library.Library
        self.downloads: Optional[Any] = None  # kofin.downloads.manager.DownloadManager
        self.syncplay: Optional[Any] = None  # kofin.syncplay.SyncPlayManager
        self._syncplay_menu: Optional[threading.Thread] = None
        self._who_is_watching: Optional[threading.Thread] = None
        self._chapter_sweep: Optional[threading.Thread] = None
        self._backdrop: Optional[threading.Thread] = None
        # Post-connect worker (see _on_ws_connected): the pending event is
        # how a reconnect that lands mid-run asks for one more pass.
        self._post_connect: Optional[threading.Thread] = None
        self._post_connect_pending = threading.Event()
        # Idle-time cast-image seeder, and the settings button's one-shot.
        self.artcache = artcache.ActorArtCache()
        self._precache_art: Optional[threading.Thread] = None
        self._online = False
        # Raised by the websocket's disconnect callback, consumed by the next
        # tick: a dropped socket asks for a *probe* rather than declaring the
        # server gone (see _verify_connection).
        self._verify_online = False
        self._backoff = Backoff()
        # This generation's IPC secret (see ipc.GUARDED): minted here so the
        # plugin process picks it up from the moment the service exists, and
        # invalidated by the next restart.
        self._ipc_nonce = ipc.rotate_nonce()
        self.settings_apply = SettingsApplier(self)

    def _abort_transport(self) -> bool:
        """Whether a transport retry ladder should give up rather than replay.

        Two signals, deliberately both. ``self._stopping`` is this generation
        tearing itself down — an Event owned by the generation, which its
        replacement cannot lower (see __init__). ``abortRequested()`` is Kodi
        stopping this *script* — an addon bounce, a profile switch, Kodi
        exiting — and it is the signal the Event cannot cover: Kodi raises it
        and waits, ``_shutdown`` has not run yet (the run loop is the thing
        that notices the flag), so a thread already inside a ladder read the
        Event as "carry on" and rode out the full budget. Measured on a
        profile switch with the server unreachable: the service blew Kodi's
        five-second stop grace ("script didn't stop in 5 seconds"), the
        interrupted profile login never restarted the webserver, and the
        profile came up with no kofin service at all (2026-08-08).
        """
        return self._stopping.is_set() or self.abortRequested()

    # -- lifecycle -----------------------------------------------------------

    def run(self) -> bool:
        """Run until abort or restart; returns True when a rebuild is wanted."""
        LOG.info("--->>> kofin service %s", addon_version())
        LOG.info("kodi %s", xbmc.getInfoLabel("System.BuildVersion"))
        self._start_chapter_sweep()
        started = time.time()
        try:
            while not self.abortRequested():
                if self._restart_requested:
                    break
                # Kodi's startup settings-load fires spurious onSettingsChanged
                # events with transient reads; the applier ignores changes until
                # this readiness point, then re-baselines against the settled
                # store. A real user edit only happens long after startup.
                if (
                    not self.settings_apply.ready
                    and time.time() - started >= SETTINGS_READY_DELAY
                ):
                    self.settings_apply.mark_ready()
                self._tick()
                if self.waitForAbort(1):
                    break
        finally:
            self._shutdown()
        LOG.info("---<<< kofin service")
        return self._restart_requested and not self.abortRequested()

    def _tick(self) -> None:
        if self._verify_online:
            self._verify_connection()
        if (
            self.credentials.is_logged_in
            and not self._online
            and self._backoff.due(time.time())
        ):
            self._connect()
        elif self._online:
            # A thread can also die while the server stays perfectly
            # reachable, and then nothing else ever looks: ``_connect`` is the
            # only other rebuild path and it runs only on the offline→online
            # edge. Costs two ``is_alive()`` calls a second and does nothing
            # at all until one of them answers False.
            self._recover_threads()

    def _recover_threads(self) -> None:
        """Rebuild the websocket and the sync manager if either has died.

        Silent death is the failure this answers. Both own a thread that ends
        on its own — the library on any ``LibraryException``, the websocket on
        an upstream raise — while the object stays in its slot, and every
        restart path guards on the slot rather than the thread.
        """
        if self.ws is not None and not self.ws.is_alive():
            self._reap_websocket()

            if self.ws is None:
                self._start_websocket()

        if self.library is not None and not self.library.is_alive():
            self._start_library()

    def _verify_connection(self) -> None:
        """Answer a dropped socket with a probe, not a verdict (plan W2.1).

        The online flag gates real behaviour once it is honest — most
        sharply ``sync.shims.stop``, which raises ``LibraryExitException``
        out of an in-flight writer — so lowering it on a bare disconnect
        would abandon a running sync every time a socket blinked. A
        websocket drop is therefore only a *question*; the probe answers it,
        on the same one-attempt budget the connect loop uses (~3 s on the
        service thread, and only when a drop was reported).
        """
        self._verify_online = False
        if not self._online:
            return
        try:
            self.api.probe_info()
        except JellyfinError as error:
            LOG.info("socket dropped and the server does not answer (%s)", error)
            self._go_offline()
            return
        LOG.debug("socket dropped but the server answers; staying online")

    def _go_offline(self) -> None:
        """Declare the server gone: the flag drops and the connect loop
        takes over. The websocket is left alone deliberately — it owns its
        own reconnection, and stopping it here would either block this
        thread on its join or leave two clients once ``_connect`` rebuilt
        one."""
        self._online = False
        state.set_online(False)
        self._backoff.succeeded()  # probe now, not after the old backoff

    def _connect(self) -> None:
        # The probe budget, not the transport default: this runs on the
        # service loop, whose 1 s tick is also what notices stop requests,
        # and the backoff schedule is already the retry policy (see
        # api.PROBE_TIMEOUT for the measured cost of forgetting that).
        try:
            info = self.api.probe_info()
        except JellyfinError as error:
            delay = self._backoff.failed(time.time())
            LOG.warning("server not reachable (%s); retry in %.0fs", error, delay)
            # A failed probe is knowledge, whether or not we were ever online:
            # on a cold boot away from the server nothing else ever states the
            # outage, and the flag would sit absent forever — which reads as
            # "nobody has said yet" and leaves every refusal waiting for the
            # transport instead of answering (found live, phase-2 gates).
            state.set_online(False)
            return

        LOG.info("connected to %s (%s)", info.get("ServerName"), info.get("Version"))
        self._backoff.succeeded()
        self._online = True
        state.set_online(True)
        self._start_syncplay()  # before the websocket: messages route into it
        # Only when there is none: a reconnect after a confirmed outage finds
        # the previous client still running its own retry loop, and building a
        # second one would double every event the server pushes. Liveness, not
        # presence: the slot outlives the thread, so a client whose thread has
        # ended reads as "one is already running" and the reconnect silently
        # brings back no websocket at all (observed 2026-08-09, after the
        # thread died on an upstream raise — see core/ws.WSClient.run).
        self._reap_websocket()
        if self.ws is None:
            self._start_websocket()
        self._start_library()
        self._start_downloads()
        self._start_backdrop()
        # Cheap to start unconditionally: the worker sleeps until the setting
        # is on *and* the box is idle, so a user who never enables it pays a
        # parked thread and nothing else.
        self.artcache.start()

    def _reap_websocket(self) -> None:
        """Empty the websocket slot when the thread behind it has ended.

        ``stop()`` on the way out rather than dropping the reference: the
        thread can die with the socket still open (an upstream raise out of
        ``run_forever`` does exactly that), and the descriptor is this
        object's only remaining handle on it.
        """
        client = self.ws

        if client is None or client.is_alive():
            return

        LOG.warning("websocket thread is gone; rebuilding the client")
        try:
            client.stop()
        except Exception:  # pragma: no cover - defensive
            LOG.exception("stopping the dead websocket client failed")
        self.ws = None

    def _reap_library(self) -> None:
        """Empty the library slot when the manager thread has ended.

        Held back while any worker it spawned is still running: a Library owns
        its own ``database_lock``, so rebuilding over a graph still in flight
        puts two independent locks in front of the same databases. The slot
        stays occupied and the next caller tries again — the manager thread is
        already gone, so nothing is lost by waiting for its workers.
        """
        library = self.library

        if library is None or library.is_alive():
            return

        library.stop_client()

        if library.workers_alive():
            LOG.debug("library thread gone but its workers are still running")
            return

        LOG.warning("library sync manager thread is gone; rebuilding")
        self.library = None

    def _start_library(self) -> None:
        """Start the sync manager once online, when there is anything to sync
        or resume. Import and failures are contained: playback and remote
        control must survive a broken sync stack (degrade, don't die)."""
        self._reap_library()

        if self.library is not None:
            return

        try:
            from kofin.sync import db as sync_db
            from kofin.sync import kodisetup
            from kofin.sync.library import Library

            sync_state = sync_db.get_sync()
            selection = settings.get_list("librarySelection")
            if not (selection or sync_state["Whitelist"] or sync_state["Libraries"]):
                LOG.debug("no libraries selected; sync manager not started")
                return

            kodisetup.verify_kodi_defaults()
            kodisetup.warn_incompatible_settings()

            self.library = Library(self.api, self.player, self._new_api)
            self.library.start()
            LOG.info("library sync manager started")
        except Exception:
            LOG.exception("library sync manager failed to start")
            self.library = None

    def _start_downloads(self) -> None:
        """Build the download manager when enabled. Contained like the library
        manager: playback and sync must survive a broken downloads stack
        (degrade, don't die)."""
        if self.downloads is not None:
            return
        if not settings.get_bool("downloadsEnabled"):
            LOG.debug("downloadsEnabled off; no download manager built")
            return
        try:
            from kofin.downloads.manager import DownloadManager

            self.downloads = DownloadManager(
                self._new_api, self._refresh_downloads, self._stopping
            )
            self.downloads.start()
            LOG.info("download manager started")
        except Exception:
            LOG.exception("download manager failed to start")
            self.downloads = None

    def _stop_downloads(self) -> None:
        manager = self.downloads
        if manager is None:
            return
        self.downloads = None
        try:
            manager.stop()
        except Exception:
            LOG.exception("download manager failed to stop")

    def _refresh_downloads(self, databases: List[str]) -> None:
        """The downloads manager's way onto screens: through the library
        manager's own refresh (never a bare builtin — the widget-refresh
        doctrine). No library manager means nothing native is rendering the
        rows anyway.

        The manager names the databases because it knows which it moved: a
        finished track has nothing to say to the video library, and asking
        anyway costs that library's whole widget fingerprint pass.
        """
        library = self.library
        if library is not None and databases:
            library.refresh_libraries(databases)

    def _new_api(self) -> Api:
        """A fresh Api with its own HTTP session (one per sync worker).

        ``abort`` matters most here: these are the sessions the download pool
        and the writers use, and a page fetch still riding its retry ladder is
        what keeps the library thread alive past the teardown.
        """
        return Api.from_credentials(
            Http(settings.get_bool("sslVerify"), abort=self._abort_transport),
            Credentials.load(),
        )

    # -- syncplay (phase 4) ----------------------------------------------------

    def _start_syncplay(self) -> None:
        """Build the SyncPlay manager when enabled. Contained like the
        library manager: playback and remote control must survive a broken
        SyncPlay stack (degrade, don't die)."""
        if self.syncplay is not None:
            return
        if not settings.get_bool("syncPlayEnabled"):
            LOG.debug("syncPlayEnabled off; no SyncPlay manager built")
            return
        try:
            from kofin.syncplay import SyncPlayManager

            self.syncplay = SyncPlayManager(self.api, self.player)
            self.player.syncplay = self.syncplay
            self.remote.syncplay = self.syncplay
            LOG.info("SyncPlay manager started")
        except Exception:
            LOG.exception("SyncPlay manager failed to start")
            self.syncplay = None

    def _stop_syncplay(self) -> None:
        manager = self.syncplay
        if manager is None:
            return
        self.syncplay = None
        self.player.syncplay = None
        self.remote.syncplay = None
        try:
            manager.stop()
        except Exception:
            LOG.exception("SyncPlay manager failed to stop")

    def _open_syncplay_menu(self) -> None:
        """SyncPlayMenu IPC: run the (dialog-blocking) menu on a dedicated
        worker thread — never on the notification thread."""
        manager = self.syncplay
        if manager is None:
            # The plugin gates on syncPlayEnabled, so this is the service
            # not (yet) online/built — tell the user rather than nothing.
            toast.show(
                settings.localized(30574), toast.ERROR, heading="SyncPlay", time_ms=4000
            )
            return
        if self._syncplay_menu is not None and self._syncplay_menu.is_alive():
            LOG.debug("SyncPlay menu already open")
            return
        from kofin.syncplay import show_menu

        self._syncplay_menu = threading.Thread(
            target=show_menu, args=(manager,), name="kofin-syncplay-menu"
        )
        self._syncplay_menu.daemon = True
        self._syncplay_menu.start()

    def _open_who_is_watching(self) -> None:
        """WhoIsWatching IPC: same contract as the SyncPlay menu — the picker
        blocks on a dialog, so it gets its own worker thread, never the
        notification thread."""
        if self._who_is_watching is not None and self._who_is_watching.is_alive():
            LOG.debug("who's-watching picker already open")
            return

        self._who_is_watching = threading.Thread(
            target=self._run_who_is_watching, name="kofin-whoswatching"
        )
        self._who_is_watching.daemon = True
        self._who_is_watching.start()

    def _run_who_is_watching(self) -> None:
        from kofin.plugin import adduser

        try:
            adduser.show_picker(self.api, self.credentials)
        except Exception:
            LOG.exception("who's-watching picker failed")

    def _start_chapter_sweep(self) -> None:
        """One-shot at service start: drop chapter-thumb cache rows a crashed
        playback left behind (their keys carry this install's deviceId).
        Deferred while a kofin playback is live — its entries are in use; a
        service restart mid-play leaves them to the next quiet start."""
        if state.get_playing_id():
            LOG.debug("chapter sweep deferred: playback live")
            return
        self._chapter_sweep = threading.Thread(
            target=self._run_chapter_sweep, name="kofin-chapter-sweep"
        )
        self._chapter_sweep.daemon = True
        self._chapter_sweep.start()

    def _run_chapter_sweep(self) -> None:
        try:
            chapters.sweep(self.credentials.device_id)
        except Exception:
            LOG.exception("chapter thumb sweep failed")

    def _start_backdrop(self, force: bool = False) -> None:
        """Refresh the addon fanart from the server's splashscreen.

        On its own thread because it downloads a couple of megabytes and
        nothing waits on the result — a backdrop that lands a few seconds into
        the session is indistinguishable from one that was always there.
        Restarted rather than joined: the previous run is idempotent and its
        own worst case is a redundant write.
        """
        if self._backdrop is not None and self._backdrop.is_alive():
            LOG.debug("backdrop refresh already running")
            return
        self._backdrop = threading.Thread(
            target=self._run_backdrop, name="kofin-backdrop", args=(force,)
        )
        self._backdrop.daemon = True
        self._backdrop.start()

    def _run_backdrop(self, force: bool) -> None:
        backdrop.apply(self.api if self._online else None, time.time(), force=force)

    def _start_websocket(self) -> None:
        header = auth.build_auth_header(
            settings.device_name(),
            self.credentials.device_id,
            addon_version(),
            self.credentials.token,
        )
        self.ws = WSClient(
            self.credentials.server_address,
            header,
            on_event=self._on_ws_event,
            on_connected=self._on_ws_connected,
            on_disconnected=self._on_ws_disconnected,
        )
        self.ws.start()

    def _connection_toast(
        self, string_id: int, *args: Any, level: str = toast.INFO
    ) -> None:
        """Tell the user the server came, went or is restarting.

        The websocket is the honest source for this: it is the connection the
        user perceives, and it reports its own open and close. Opt-out lives
        in the advanced sync settings for anyone who does not want the noise.

        Connecting is the only good news here; losing the connection, and a
        server restarting or shutting down, are all adverse and carry Kodi's
        warning glyph rather than kofin's icon.

        Nothing in here may raise. It is called first thing from
        ``_on_ws_connected``, which goes on to register capabilities and
        rejoin SyncPlay — losing those because a *notification* failed would
        be a poor trade. That is not hypothetical: Kodi caches addon strings
        for the process lifetime, so a newly added id renders without its
        placeholder until the next full restart, and formatting it then
        raised TypeError straight out of the callback.
        """
        if not settings.get_bool("notifyConnection"):
            return

        try:
            message = settings.localized(string_id)

            if args:
                try:
                    message = message % args
                except TypeError:
                    # No placeholder to fill (uncached id, or a language pack
                    # without this string): show the bare text.
                    pass

            toast.show(message, level, time_ms=4000)
        except Exception as error:  # pragma: no cover - defensive
            LOG.warning("connection notification failed: %s", error)

    def _register_capabilities(self) -> None:
        try:
            self.api.post_capabilities(CAPABILITIES)
            LOG.info("capabilities registered")
        except JellyfinError as error:
            LOG.warning("capabilities registration failed: %s", error)

    def _on_ws_disconnected(self) -> None:
        self._connection_toast(30416, level=toast.WARNING)
        # A question for the next tick, not a verdict (see _verify_connection).
        self._verify_online = True

    def _on_ws_connected(self) -> None:
        """Runs on the websocket's own receive loop — toast, spawn, return.

        The post-connect work blocks for seconds (a deliberate settle wait,
        capabilities registration, the who's-watching restore, the catch-up
        enqueue), and for as long as on_open runs nothing is read off the
        socket — inbound Play/SyncPlay commands and pongs stall at exactly
        the moment the server pushes state at a fresh session (audit finding
        #5). So the work moves to a worker; the pending flag makes a connect
        that lands mid-run schedule one more pass rather than pile up threads
        or be skipped — each pass re-reads current state, so the last one
        always serves the live session.
        """
        self._connection_toast(30415, self.credentials.server_name or "")
        # A live socket is the best evidence there is, and the websocket
        # reconnects itself — so this is a raising edge in its own right,
        # not only ``_connect``'s.
        self._online = True
        self._verify_online = False
        state.set_online(True)
        self._post_connect_pending.set()
        if self._post_connect is None or not self._post_connect.is_alive():
            self._post_connect = threading.Thread(
                target=self._run_post_connect, name="kofin-postconnect", daemon=True
            )
            self._post_connect.start()

    def _run_post_connect(self) -> None:
        monitor = xbmc.Monitor()
        while self._post_connect_pending.is_set() and not monitor.abortRequested():
            self._post_connect_pending.clear()
            # The server registers the socket's session asynchronously; give
            # it a beat before attaching capabilities to that session.
            monitor.waitForAbort(2)
            self._register_capabilities()
            # Capabilities attach this device's session; re-apply the Who's
            # watching? set the plugin saved, which does not survive a new
            # session (Kodi restart or a reconnect that minted a fresh one).
            self._restore_additional_users()
            # Before the catch-up, deliberately: the replay writes what this
            # device did offline, and the FastSync that follows pulls the
            # server's answer back down. The other order would apply the
            # server's stale view first and then overwrite it, so every
            # replayed item would flicker through the wrong state (plan W2.4).
            self._replay_pending_userdata()
            self._catch_up_after_reconnect()
            if self.syncplay is not None:
                # Reconnect contract (plan §2): after any WS drop assume
                # kicked; the manager probes /SyncPlay/List and rejoins on
                # its own thread.
                self.syncplay.on_notification("WebSocketConnected", {})

    def _restore_additional_users(self) -> None:
        """Re-attach saved co-watchers. Contained: must never break connect."""
        try:
            from kofin.plugin import adduser

            adduser.restore_additional_users(self.api, self.credentials.device_id)
        except Exception as error:  # pragma: no cover - defensive
            LOG.warning("who's-watching restore failed: %s", error)

    def _replay_pending_userdata(self) -> None:
        """Send what a playback produced while the server was unreachable.

        Contained and bounded: one row at a time, each failure counted so a
        row the server will never accept eventually leaves (pending.py), and
        the whole pass wrapped so a broken replay never costs the connect
        its catch-up.
        """
        try:
            from kofin.downloads import pending
        except Exception:  # pragma: no cover - defensive
            return
        try:
            rows = pending.rows()
        except Exception:
            LOG.exception("could not read the parked userdata")
            return
        if not rows:
            return
        LOG.info("replaying %d parked userdata change(s)", len(rows))
        for row in rows:
            if self._stopping.is_set() or self.abortRequested():
                return
            try:
                current = (self.api.item(row.jellyfin_id) or {}).get("UserData") or {}
                payload = pending.resolve(row, current)
                if payload:
                    self.api.update_user_data(row.jellyfin_id, payload)
                    LOG.info("replayed userdata for %s: %s", row.jellyfin_id, payload)
                pending.remove(row.jellyfin_id)
            except JellyfinError as error:
                attempts = pending.record_attempt(row.jellyfin_id)
                LOG.warning(
                    "userdata replay failed for %s (attempt %d): %s",
                    row.jellyfin_id,
                    attempts,
                    error,
                )
                return  # the server is unwell; the rest keeps for next time
            except Exception:
                LOG.exception("userdata replay failed for %s", row.jellyfin_id)
                pending.record_attempt(row.jellyfin_id)

    def _catch_up_after_reconnect(self) -> None:
        """Replay what the library missed while the socket was down.

        LibraryChanged is fire-and-forget: a message sent while the websocket
        is disconnected is gone, and the socket reconnects itself silently, so
        nothing ever noticed the hole. Observed on the Piers box — the server
        emitted LibraryChanged for a re-encoded film at 14:38:45, that client's
        socket was down and came back at 14:39:51, and the film stayed missing
        indefinitely while a second client with a live socket applied it in
        seconds.

        The incremental catch-up already covers exactly this: it asks the
        change feed for everything since the sync watermark, which is only
        advanced once a drain completes. Skipped on the first connect of a
        session, where startup() runs the same pass a moment later.
        """
        library = self.library

        if library is None or not library.startup_done:
            return

        LOG.info("websocket reconnected; catching up on missed changes")
        library.enqueue_command("FastSync")

    def _on_ws_event(self, message_type: str, data: Dict[str, Any]) -> None:
        if self.remote.handle(message_type, data):
            return

        # Announced by the server before it goes, so the drop that follows is
        # explained rather than mysterious. Handled ahead of the library
        # events: neither carries a payload the sync cares about.
        if message_type in SERVER_LIFECYCLE_MESSAGES:
            LOG.info("[ %s ]", message_type)
            self._connection_toast(
                SERVER_LIFECYCLE_MESSAGES[message_type], level=toast.WARNING
            )
            return

        library = self.library
        if library is not None and library.startup_done:
            if message_type == "LibraryChanged":
                LOG.info("[ LibraryChanged ] %s", log.mask(str(data)))
                library.added(data.get("ItemsAdded") or [])
                library.updated(data.get("ItemsUpdated") or [])
                library.removed(data.get("ItemsRemoved") or [])
                return
            if message_type == "UserDataChanged":
                if not own_userdata(data, self.api.user_id):
                    # A co-watcher's own viewing, not ours. See own_userdata.
                    LOG.debug("[ UserDataChanged ] ignored: another user's")
                    return
                LOG.info("[ UserDataChanged ] %s", log.mask(str(data)))
                library.userdata(data.get("UserDataList") or [])
                return

        LOG.debug("ws event %s (unhandled)", message_type)

    # -- kodi callbacks --------------------------------------------------------

    def onNotification(self, sender: str, method: str, data: str) -> None:
        if sender == "xbmc":
            if method == "GUI.OnScreensaverDeactivated" and self.library is not None:
                # Unconditional: the wake catch-up is the only cover for a
                # websocket that went half-open during long idle (a dead
                # socket that never reported closing delivers nothing, and
                # the reconnect catch-up only fires on *detected* drops). An
                # empty catch-up costs one request. The screensaver itself
                # never pauses sync — verified live, docs/widget-refresh-plan.md
                # F9 — so there is nothing to "resume" here, only to re-check.
                LOG.info("screensaver deactivated; catching up")
                self.library.enqueue_command("FastSync")
            # Independent of the sync kick above (plan §7): a broken manager
            # must never suppress the library catch-up, and vice versa.
            if method in ("GUI.OnScreensaverDeactivated", "System.OnWake"):
                self._syncplay_forward("on_wake")
            elif method == "System.OnSleep":
                self._syncplay_forward("on_sleep")
            elif method == "Player.OnPlay":
                decoded = _decode_kodi_data(data)
                self._syncplay_forward("on_kodi_play", decoded)
                self._backfill_library_claim(decoded)
            elif method == "VideoLibrary.OnUpdate" and self.credentials.is_logged_in:
                # Kodi's own "Mark as watched" / "Reset resume position" only
                # touch MyVideos; without this they never reach the server and
                # the next userdata sync undoes them.
                self.kodi_userdata.submit(_decode_kodi_data(data))
            return
        if sender != ipc.SENDER:
            return
        name = ipc.method_name(method)
        payload = ipc.decode(data)
        if not ipc.verify(name, payload, self._ipc_nonce):
            # Kodi passes the sender string through from whoever called
            # NotifyAll, so this is what a forged destructive command looks
            # like: our name, no secret. Logged rather than silent — if it is
            # ever a real kofin message, this line is the only trace.
            LOG.warning("dropped unauthenticated %s", name)
            return
        if name == ipc.RESTART:
            LOG.info("restart requested")
            self._restart_requested = True
        elif name == ipc.AUTH_CHANGED:
            LOG.info("auth changed; restarting service cycle")
            self._restart_requested = True
        elif name == ipc.SYNCPLAY_MENU:
            self._open_syncplay_menu()
        elif name == ipc.WHO_IS_WATCHING:
            self._open_who_is_watching()
        elif name == ipc.PRECACHE_ART:
            self._precache_art_now()
        elif name == ipc.ATTACH_SUBTITLE:
            try:
                index = int(payload["Index"])
            except (KeyError, TypeError, ValueError):
                LOG.warning("AttachSubtitle without a usable index: %s", payload)
            else:
                self.player.fetch_subtitle(index)
        elif name in DOWNLOAD_COMMANDS:
            # Notification thread: the manager's surface only enqueues onto
            # its ops queue — no database, no socket (audit finding #3).
            self._start_downloads()
            if self.downloads is None:
                LOG.warning("download command %s ignored: manager not running", name)
                return
            if name == ipc.DOWNLOAD_ADD:
                # The optional Origin marks automatic downloads for W4.2's
                # retention sweep; anything unrecognized is a user download —
                # the label that is never auto-deleted.
                from kofin.downloads import store as downloads_store

                origin = str(payload.get("Origin") or downloads_store.ORIGIN_USER)
                if origin != downloads_store.ORIGIN_USER and not (
                    downloads_store.is_auto_origin(origin)
                ):
                    origin = downloads_store.ORIGIN_USER
                # Types is optional and positional against Ids: the sender
                # holds each item's DTO type, and passing it here is what
                # lets the queue split by media kind without an item fetch
                # per row. Paired before the empty ids are dropped, so a
                # blank in the middle cannot shift the rest by one; a short
                # or absent list just leaves kinds unknown, which the video
                # pool claims.
                raw_ids = [str(one) for one in (payload.get("Ids") or [])]
                raw_types = [str(one) for one in (payload.get("Types") or [])]
                pairs = [
                    (item_id, raw_types[index] if index < len(raw_types) else "")
                    for index, item_id in enumerate(raw_ids)
                    if item_id
                ]
                self.downloads.submit(
                    [item_id for item_id, _ in pairs],
                    origin=origin,
                    media_types=[dto_type for _, dto_type in pairs],
                )
            elif name == ipc.DOWNLOAD_CANCEL:
                self.downloads.cancel(str(payload.get("Id") or ""))
            elif name == ipc.DOWNLOAD_REMOVE:
                self.downloads.remove(str(payload.get("Id") or ""))
        elif name in LIBRARY_COMMANDS:
            self._start_library()
            if self.library is None:
                LOG.warning("library command %s ignored: manager not running", name)
                return
            self.library.enqueue_command(name, payload)

    def _precache_art_now(self) -> None:
        """Settings button: seed every outstanding cast image.

        On its own thread — this runs until the work is done, which on a first
        run is thousands of downloads — and single-flight, so a second press
        joins the run already going rather than doubling the fetches.
        """
        if self._precache_art is not None and self._precache_art.is_alive():
            LOG.debug("cast-image pre-cache already running")
            toast.show(settings.localized(30672), time_ms=4000)
            return
        self._precache_art = threading.Thread(
            target=self._run_precache_art, name="kofin-artcache-now"
        )
        self._precache_art.daemon = True
        self._precache_art.start()

    def _run_precache_art(self) -> None:
        toast.show(settings.localized(30672), time_ms=4000)
        try:
            # The service's own instance, not a second one: it holds the lock
            # the idle trickle takes, so the button never races it.
            seeded = self.artcache.seed_all()
        except Exception:
            LOG.exception("cast-image pre-cache failed")
            return
        try:
            toast.show(settings.localized(30673) % seeded, time_ms=5000)
        except TypeError:  # pragma: no cover - uncached string, see _connection_toast
            pass

    def _syncplay_forward(self, name: str, *args: Any) -> None:
        manager = self.syncplay
        if manager is None:
            return
        try:
            getattr(manager, name)(*args)
        except Exception:
            LOG.exception("SyncPlay %s hook failed", name)

    def _backfill_library_claim(self, data: Dict[str, Any]) -> None:
        """Claim library playback that never passed through the play route.

        Songs live in Kodi as direct stream URLs, so playing one from the music
        library reports nothing without this. Never allowed to break playback:
        a failed back-fill just means the play stays unreported, as before.
        """
        # Queued to the player's reporter: the claim's server GET must not
        # run on Kodi's notification thread (audit finding #3); failures are
        # contained inside the job.
        self.player.submit_backfill(data)

    def onSettingsChanged(self) -> None:
        self.settings_apply.apply()

    # -- teardown ---------------------------------------------------------------

    def _shutdown(self) -> None:
        # Both, and they are not redundant: the property is what the sync
        # workers' @stop guards read across the process, the Event is what
        # this generation's HTTP transports check between retries and is the
        # only one of the two that survives the next generation starting.
        self._stopping.set()
        state.set_should_stop(True)
        self._stop_syncplay()
        # The websocket goes down before the library, not after: every event
        # it dispatches lands in the Library's queues — or, for UserDataChanged,
        # opens the kofin database — on the websocket thread, so a message
        # arriving mid-join raced the very teardown the join was waiting on
        # (observed: a LibraryChanged applied against a torn-down Library a
        # minute after the service exited, 2026-08-07). SyncPlay is stopped
        # above because it is the only other websocket consumer.
        if self.ws is not None:
            self.ws.stop()
            self.ws = None
        # Downloads before the library join: the workers write MyVideos (the
        # repoint) and would contend with the join's own drain; their chunk
        # loops hear the stop within one read timeout.
        self._stop_downloads()
        library_stuck = False
        if self.library is not None:
            self.library.stop_client()
            library_stuck = not self._join_library()
            self.library = None
        self.player.stop_threads()
        self.artcache.stop()
        self.kodi_userdata.stop()
        self._join_workers()
        self.http.close()
        # Last, and only once nothing of ours is still running: clear_all drops
        # PROP_SYNC_STOP, which is the flag every sync worker's @stop guard
        # reads. Clearing it while a thread survives un-pauses that thread
        # against a service that has already been rebuilt — two Library object
        # graphs, each with its own database locks, writing the same files. A
        # thread that outlived the join keeps the flag raised for exactly that
        # reason.
        state.clear_all(keep_stop=library_stuck)

    def _join_library(self) -> bool:
        """Wait for the library thread to die; True when it did.

        Two failures to avoid, and they pull in opposite directions. Giving up
        after 15 seconds and carrying on let the thread outlive the teardown
        while the next line cleared the stop flag it was waiting on (audit
        finding #10). Waiting on `abortRequested` alone never ends on an addon
        bounce, and a service script that does not return wedges every later
        Python invocation in Kodi (see LIBRARY_JOIN_SECONDS).

        So: a deadline generous enough for a page fetch, and a truthful answer
        when it passes — the caller keeps the flag raised rather than pretending
        the thread is gone.

        A wait that goes long also dumps every thread's stack (core/diag.py).
        The deadline makes a stuck thread survivable without saying what stuck
        it, and the event is too rare to catch by asking for it again: the
        first slow tick and the last one are both written down, so the log says
        what the thread was doing and whether it moved between the two.
        """
        library = self.library
        if library is None:
            return True
        waited = 0.0
        dumped = False
        first: Dict[int, str] = {}
        while library.is_alive() and waited < LIBRARY_JOIN_SECONDS:
            library.join(timeout=5)
            if not library.is_alive():
                return True
            waited += 5
            if not dumped:
                dumped = True
                first = diag.thread_dump("library thread still stopping after 5s")
            else:
                LOG.warning(
                    "library thread still stopping after %.0fs: %s",
                    waited,
                    diag.positions().get(library.ident, "gone"),
                )
            if self.abortRequested():
                LOG.warning("abort during teardown; library thread still alive")
                return False
        if library.is_alive():
            last = diag.thread_dump("library thread outlived the deadline")
            LOG.error(
                "library thread outlived %.0fs (%s); leaving the sync stop flag raised",
                LIBRARY_JOIN_SECONDS,
                diag.describe_movement(library.ident, first, last),
            )
            return False
        return True

    def _join_workers(self) -> None:
        """Join the named one-shot workers this service started.

        Unjoined, they outlive the rebuild and race their replacements against
        the same resources — the chapter sweep holds an open cursor on the
        texture database, and the backdrop worker rewrites a file in the addon
        directory (audit finding #13).
        """
        workers = (
            ("chapter sweep", self._chapter_sweep),
            ("backdrop", self._backdrop),
            ("post-connect", self._post_connect),
            ("cast-image pre-cache", self._precache_art),
            ("syncplay menu", self._syncplay_menu),
            ("who's watching", self._who_is_watching),
        )
        for name, worker in workers:
            if worker is None or not worker.is_alive():
                continue
            worker.join(timeout=10)
            if worker.is_alive():  # pragma: no cover - watchdog logging only
                # The dialogs are the expected case: both block on user input
                # and Kodi closes them when the addon goes.
                LOG.warning("%s worker did not stop within its deadline", name)


def _guid(value: Any) -> str:
    """Jellyfin ids compare dashless and case-insensitively."""
    return str(value or "").replace("-", "").lower()


def own_userdata(data: Dict[str, Any], user_id: str) -> bool:
    """Whether a UserDataChanged payload is about the user this client mirrors.

    The message names its subject, and on a device running "Who's watching?"
    that is regularly somebody else. Jellyfin delivers UserDataChanged to
    every session that *contains* the subject user, and a session contains
    its additional users as well as its owner (``SessionInfo.ContainsUser``),
    so attaching a co-watcher subscribes this device to everything they watch
    **anywhere** — their phone, a browser, another Kodi box. Applied
    unfiltered, that wrote a stranger's watched flag and resume point into
    this Kodi's library, while the server-side userdata of the logged-in user
    stayed correct: a purely local corruption, and a durable one, since the
    server has no reason to ever send our real value for that item again.

    Who's watching? is meant to work one way — playing *here* credits the
    co-watchers, which the server does on its own from the session. Nothing
    they do elsewhere belongs in this library.

    An unstated subject is treated as ours: the field is Jellyfin's to send,
    and dropping messages without it would silently stop userdata sync
    against any server that omits it.
    """
    subject = _guid(data.get("UserId"))
    return not subject or subject == _guid(user_id)


def _decode_kodi_data(data: str) -> Dict[str, Any]:
    """Kodi-bus notification payloads are plain JSON objects."""
    try:
        payload = json.loads(data) if data else {}
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def run_forever() -> None:
    while True:
        # A generation never inherits the previous one's stop flag. A teardown
        # that could not join its library thread leaves PROP_SYNC_STOP raised
        # on purpose (see _shutdown), and nothing else ever lowers it — so
        # without this line one stuck teardown disabled syncing until Kodi was
        # restarted, and said so in a single warning: every later library
        # thread started, ran until its first @stop guard, and exited with
        # "Should stop flag raised". Measured on Omega, and silent enough that
        # it looked like sync had simply stopped working.
        #
        # The orphan it was protecting is not left unguarded: what actually
        # ends that thread is ``Library.stop_thread``, an instance flag the
        # replacement cannot touch, which bounds it to the tick it is already
        # in. The raised property only ever covered the gap before a
        # replacement existed, and this is the moment that gap closes.
        state.set_should_stop(False)
        service = Service()
        if not service.run():
            break
        del service
