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

from kofin.core import auth, ipc, log, settings, state
from kofin.core.api import Api
from kofin.core.http import Http, JellyfinError
from kofin.core.log import Logger
from kofin.core.settings import Credentials, addon_version
from kofin.core.ws import WSClient
from kofin.service.player import Player, backfill_library_claim
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
        self.library: Optional[Any] = None  # kofin.sync.library.Library
        self.syncplay: Optional[Any] = None  # kofin.syncplay.SyncPlayManager
        self._syncplay_menu: Optional[threading.Thread] = None
        self._online = False
        self._backoff = Backoff()
        self.settings_apply = SettingsApplier(self)

    # -- lifecycle -----------------------------------------------------------

    def run(self) -> bool:
        """Run until abort or restart; returns True when a rebuild is wanted."""
        LOG.info("--->>> kofin service %s", addon_version())
        LOG.info("kodi %s", xbmc.getInfoLabel("System.BuildVersion"))
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
            import xbmcgui

            xbmcgui.Dialog().notification(
                "SyncPlay",
                settings.localized(30574),
                xbmcgui.NOTIFICATION_INFO,
                4000,
                False,
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

    def _start_websocket(self) -> None:
        header = auth.build_auth_header(
            settings.get_str("deviceName") or "Kodi",
            self.credentials.device_id,
            addon_version(),
            self.credentials.token,
        )
        self.ws = WSClient(
            self.credentials.server_address,
            header,
            on_event=self._on_ws_event,
            on_connected=self._on_ws_connected,
        )
        self.ws.start()

    def _on_ws_connected(self) -> None:
        # The server registers the socket's session asynchronously; give it a
        # beat before attaching capabilities to that session.
        xbmc.Monitor().waitForAbort(2)
        try:
            self.api.post_capabilities(CAPABILITIES)
            LOG.info("capabilities registered")
        except JellyfinError as error:
            LOG.warning("capabilities registration failed: %s", error)
        if self.syncplay is not None:
            # Reconnect contract (plan §2): after any WS drop assume kicked;
            # the manager probes /SyncPlay/List and rejoins on its own thread.
            self.syncplay.on_notification("WebSocketConnected", {})

    def _on_ws_event(self, message_type: str, data: Dict[str, Any]) -> None:
        if self.remote.handle(message_type, data):
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
                LOG.info("[ UserDataChanged ] %s", log.mask(str(data)))
                library.userdata(data.get("UserDataList") or [])
                return

        LOG.debug("ws event %s (unhandled)", message_type)

    # -- kodi callbacks --------------------------------------------------------

    def onNotification(self, sender: str, method: str, data: str) -> None:
        if sender == "xbmc":
            if (
                method == "GUI.OnScreensaverDeactivated"
                and settings.get_bool("dbSyncScreensaver")
                and self.library is not None
            ):
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
        elif name in LIBRARY_COMMANDS:
            self._start_library()
            if self.library is None:
                LOG.warning("library command %s ignored: manager not running", name)
                return
            self.library.enqueue_command(name, ipc.decode(data))

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
        try:
            backfill_library_claim(data, self.api)
        except Exception:
            LOG.exception("library claim back-fill failed")

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
        if self.ws is not None:
            self.ws.stop()
            self.ws = None
        self.http.close()
        state.clear_all()


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
