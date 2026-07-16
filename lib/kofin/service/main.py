"""Service lifecycle: build, run, and rebuild on soft restart.

The outer loop owns restarts — a restart tears the Service object down and
builds a fresh one. Nothing may survive a cycle at module level; all state
lives on the objects rebuilt each pass.
"""

import time
from typing import Any, Dict, Optional

import xbmc

from kofin.core import auth, ipc, settings, state
from kofin.core.api import Api
from kofin.core.http import Http, JellyfinError
from kofin.core.log import Logger
from kofin.core.settings import Credentials, addon_version
from kofin.core.ws import WSClient
from kofin.service.player import Player

LOG = Logger(__name__)

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
        self._online = False
        self._backoff = Backoff()
        self._device_name = settings.get_str("deviceName")
        self._ssl_verify = settings.get_bool("sslVerify")

    # -- lifecycle -----------------------------------------------------------

    def run(self) -> bool:
        """Run until abort or restart; returns True when a rebuild is wanted."""
        LOG.info("--->>> kofin service %s", addon_version())
        LOG.info("kodi %s", xbmc.getInfoLabel("System.BuildVersion"))
        try:
            while not self.abortRequested():
                if self._restart_requested:
                    break
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
        self._start_websocket()

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

    def _on_ws_event(self, message_type: str, data: Dict[str, Any]) -> None:
        # Remote control dispatch lands in step 11; log for now.
        LOG.debug("ws event %s", message_type)

    # -- kodi callbacks --------------------------------------------------------

    def onNotification(self, sender: str, method: str, data: str) -> None:
        if sender != ipc.SENDER:
            return
        name = ipc.method_name(method)
        if name == ipc.RESTART:
            LOG.info("restart requested")
            self._restart_requested = True
        elif name == ipc.AUTH_CHANGED:
            LOG.info("auth changed; restarting service cycle")
            self._restart_requested = True

    def onSettingsChanged(self) -> None:
        device_name = settings.get_str("deviceName")
        if device_name != self._device_name:
            self._device_name = device_name
            if self._online:
                LOG.info("device name changed; re-registering capabilities")
                self._on_ws_connected()
        ssl_verify = settings.get_bool("sslVerify")
        if ssl_verify != self._ssl_verify:
            LOG.info("sslVerify changed; restarting service cycle")
            self._restart_requested = True

    # -- teardown ---------------------------------------------------------------

    def _shutdown(self) -> None:
        self.player.stop_threads()
        if self.ws is not None:
            self.ws.stop()
            self.ws = None
        self.http.close()
        state.clear_all()


def run_forever() -> None:
    while True:
        service = Service()
        if not service.run():
            break
        del service
