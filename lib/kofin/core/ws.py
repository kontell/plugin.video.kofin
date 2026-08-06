"""Jellyfin websocket client: auth-header connect, keepalive, reconnect.

Transport only — received messages are handed to a callback; what they mean
is the service's business.
"""

import json
import sys
import threading
from typing import Any, Callable, Dict, Optional

import xbmc

# If numpy is installed, the websocket library tries to use it, and then Kodi
# hard crashes (long-standing upstream workaround — keep before the import).
sys.modules["numpy"] = None  # type: ignore[assignment]
import websocket  # noqa: E402

from kofin.core.log import Logger  # noqa: E402

LOG = Logger(__name__)

KEEPALIVE_SECONDS = 30
RECONNECT_SECONDS = 10
IGNORED_MESSAGES = frozenset({"RefreshProgress", "KeepAlive", "ForceKeepAlive"})

EventCallback = Callable[[str, Dict[str, Any]], None]
ConnectedCallback = Callable[[], None]


def socket_url(server_address: str) -> str:
    if server_address.startswith("https://"):
        return server_address.replace("https://", "wss://", 1) + "/socket"
    return server_address.replace("http://", "ws://", 1) + "/socket"


class WSClient(threading.Thread):
    def __init__(
        self,
        server_address: str,
        auth_header: str,
        on_event: EventCallback,
        on_connected: ConnectedCallback,
        on_disconnected: Optional[ConnectedCallback] = None,
    ) -> None:
        super().__init__(name="kofin-ws")
        self._url = socket_url(server_address)
        self._header = auth_header
        self._on_event = on_event
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._stop = False
        # Whether the socket is currently up. run_forever(reconnect=) retries
        # on its own and calls back repeatedly while a server is down, so the
        # flag is what turns that stream into one edge per actual transition.
        self._connected = False
        self._app: Optional[websocket.WebSocketApp] = None
        self._keepalive: Optional[_KeepAlive] = None

    def run(self) -> None:
        monitor = xbmc.Monitor()
        LOG.info("websocket url: %s", self._url)
        # Socket-level timeout for the connect phase: without one, a
        # black-holed host blocks create_connection for the OS TCP timeout
        # (~2 min), through which stop()'s bounded join fails and the thread
        # leaks past a service restart. Module-global to websocket-client,
        # which is fine — kofin owns the only websocket in this process.
        websocket.setdefaulttimeout(10)
        self._app = websocket.WebSocketApp(
            self._url,
            header={"Authorization": self._header},
            on_open=self._handle_open,
            on_message=self._handle_message,
            on_error=self._handle_error,
            on_close=self._handle_close,
        )
        while not self._stop:
            # ping_timeout is what makes a half-open socket (NAT drop,
            # sleeping AP) detectable at all: websocket-client only raises on
            # a missed pong when one is set, and without it _handle_close
            # never fires, the reconnect catch-up never runs, and the only
            # cover is the unconditional wake-time FastSync (CLAUDE.md). Must
            # stay below ping_interval or the library refuses to run.
            self._app.run_forever(
                ping_interval=10, ping_timeout=5, reconnect=RECONNECT_SECONDS
            )
            if self._stop or monitor.waitForAbort(5):
                break
        LOG.debug("websocket thread exit")

    def stop(self) -> None:
        self._stop = True
        if self._keepalive is not None:
            self._keepalive.stop()
            self._keepalive = None
        if self._app is not None:
            try:
                self._app.close()
            except Exception as error:  # pragma: no cover - defensive
                LOG.debug("websocket close failed: %s", error)
        if self.is_alive():
            self.join(timeout=5)
            if self.is_alive():  # pragma: no cover - watchdog logging only
                LOG.warning("websocket thread did not stop within deadline")

    def _handle_open(self, app: "websocket.WebSocketApp") -> None:
        LOG.info("websocket connected")
        self._connected = True
        if self._keepalive is not None:
            self._keepalive.stop()
        self._keepalive = _KeepAlive(app)
        self._keepalive.start()
        try:
            self._on_connected()
        except Exception:
            LOG.exception("on_connected callback failed")

    def _handle_close(
        self,
        app: "websocket.WebSocketApp",
        status_code: Optional[int] = None,
        message: Optional[str] = None,
    ) -> None:
        """One callback per real drop.

        Reported only when a socket that was actually open goes down, so a
        server that stays unreachable — where run_forever keeps failing to
        connect — does not fire this on every retry. A deliberate stop() is
        not a drop either.
        """
        was_connected = self._connected
        self._connected = False

        if not was_connected or self._stop:
            return

        LOG.info("websocket disconnected (%s %s)", status_code, message)

        if self._on_disconnected is None:
            return

        try:
            self._on_disconnected()
        except Exception:
            LOG.exception("on_disconnected callback failed")

    def _handle_message(self, app: "websocket.WebSocketApp", raw: str) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            LOG.warning("undecodable websocket message")
            return
        message_type = message.get("MessageType", "")
        if message_type in IGNORED_MESSAGES:
            return
        data = message.get("Data") or {}
        if not isinstance(data, dict):
            data = {"Value": data}
        try:
            self._on_event(message_type, data)
        except Exception:
            LOG.exception("event handler failed for %s", message_type)

    def _handle_error(self, app: "websocket.WebSocketApp", error: Exception) -> None:
        LOG.debug("websocket error: %s", error)


class _KeepAlive(threading.Thread):
    def __init__(self, app: "websocket.WebSocketApp") -> None:
        super().__init__(name="kofin-ws-keepalive")
        self._app = app
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=5)

    def run(self) -> None:
        while not self._halt.wait(KEEPALIVE_SECONDS):
            try:
                self._app.send(
                    json.dumps({"MessageType": "KeepAlive", "Data": KEEPALIVE_SECONDS})
                )
            except Exception as error:
                LOG.debug("keepalive send failed: %s", error)
                return
