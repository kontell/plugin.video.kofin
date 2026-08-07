"""Service lifecycle: build, run, and rebuild on soft restart.

The outer loop owns restarts — a restart tears the Service object down and
builds a fresh one. Nothing may survive a cycle at module level; all state
lives on the objects rebuilt each pass.
"""

import json
import threading
import time
from typing import Any, Dict, Optional

import xbmc

from kofin.core import auth, ipc, log, settings, state, toast
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

# Seconds the service ignores settings changes after start, covering Kodi's
# startup settings-load transients. A user cannot open the settings dialog and
# edit within this window; a real change always lands well after it.
SETTINGS_READY_DELAY = 5.0

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
        self.credentials = Credentials.load()
        self.http = Http(settings.get_bool("sslVerify"))
        self.api = Api.from_credentials(self.http, self.credentials)
        self.ws: Optional[WSClient] = None
        self.player = Player(self.api)
        self.remote = RemoteHandler()
        self.kodi_userdata = KodiUserData(self.api)
        self.library: Optional[Any] = None  # kofin.sync.library.Library
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
        self._backoff = Backoff()
        self.settings_apply = SettingsApplier(self)

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
        if (
            self.credentials.is_logged_in
            and not self._online
            and self._backoff.due(time.time())
        ):
            self._connect()

    def _connect(self) -> None:
        try:
            info = self.api.public_info()
        except JellyfinError as error:
            delay = self._backoff.failed(time.time())
            LOG.warning("server not reachable (%s); retry in %.0fs", error, delay)
            return

        LOG.info("connected to %s (%s)", info.get("ServerName"), info.get("Version"))
        self._backoff.succeeded()
        self._online = True
        state.set_online(True)
        self._start_syncplay()  # before the websocket: messages route into it
        self._start_websocket()
        self._start_library()
        self._start_backdrop()
        # Cheap to start unconditionally: the worker sleeps until the setting
        # is on *and* the box is idle, so a user who never enables it pays a
        # parked thread and nothing else.
        self.artcache.start()

    def _start_library(self) -> None:
        """Start the sync manager once online, when there is anything to sync
        or resume. Import and failures are contained: playback and remote
        control must survive a broken sync stack (degrade, don't die)."""
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

    def _new_api(self) -> Api:
        """A fresh Api with its own HTTP session (one per sync worker)."""
        return Api.from_credentials(
            Http(settings.get_bool("sslVerify")), Credentials.load()
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
        elif name in LIBRARY_COMMANDS:
            self._start_library()
            if self.library is None:
                LOG.warning("library command %s ignored: manager not running", name)
                return
            self.library.enqueue_command(name, ipc.decode(data))

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
        state.set_should_stop(True)
        self._stop_syncplay()
        if self.library is not None:
            self.library.stop_client()
            self.library.join(timeout=15)
            if self.library.is_alive():  # pragma: no cover - watchdog only
                LOG.warning("library thread did not stop within deadline")
            self.library = None
        self.player.stop_threads()
        self.artcache.stop()
        self.kodi_userdata.stop()
        if self.ws is not None:
            self.ws.stop()
            self.ws = None
        self.http.close()
        state.clear_all()


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
        service = Service()
        if not service.run():
            break
        del service
