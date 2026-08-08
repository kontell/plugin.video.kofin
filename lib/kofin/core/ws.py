"""Jellyfin websocket client: auth-header connect, keepalive, reconnect.

Transport only — received messages are handed to a callback; what they mean
is the service's business.
"""

import json
import sys
import threading
import time
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
# The half-open verdict: a healthy socket never goes this long without an
# inbound frame, because the server echoes every KeepAlive _KeepAlive sends
# (and pushes ForceKeepAlive on its own). Two missed echoes plus grace.
STALE_SECONDS = 75
# How often the keepalive thread wakes to check liveness between sends.
LIVENESS_TICK = 15
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
        # Whether the socket is currently up: run_forever returns on every
        # failure and the run loop retries, so the flag is what turns that
        # stream into one edge per actual transition.
        self._connected = False
        # Monotonic time of the last inbound frame — the liveness signal
        # half_open() polices (see its docstring).
        self._last_inbound = 0.0
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
            # No ping_timeout and no reconnect=, both deliberately, both
            # measured. ping_timeout tears healthy connections down ~120 s in:
            # the server's own 2-minute keepalive ping corrupts
            # websocket-client's pong bookkeeping, and every connection died
            # on an exact 130 s cycle on both test boxes. reconnect= swallows
            # the edges — its internal retry invokes neither on_error nor
            # on_close, so the disconnect callback was dead code. This loop
            # owns reconnection instead, which makes the close edge honest,
            # and half-open detection is app-level: half_open() recycles the
            # socket when the server's KeepAlive echoes stop arriving.
            self._app.run_forever(ping_interval=10)
            if self._stop or monitor.waitForAbort(RECONNECT_SECONDS):
                break
        LOG.debug("websocket thread exit")

    def stop(self) -> None:
        self._stop = True
        if self._keepalive is not None:
            self._keepalive.stop()
            self._keepalive = None
        app = self._app
        # The raw-socket handle is taken before close(): WebSocketApp.close()
        # drops its reference on the way through, and the escalation below
        # needs something left to sever.
        sock = getattr(app, "sock", None)
        if app is not None:
            try:
                app.close()
            except Exception as error:  # pragma: no cover - defensive
                LOG.debug("websocket close failed: %s", error)
        if not self.is_alive():
            return
        self.join(timeout=5)
        if not self.is_alive():
            return
        # The graceful close did not unblock the receive loop. That is what a
        # close handshake that raised looks like: the descriptor stays open,
        # recv() keeps delivering, and the thread outlives every deadline —
        # which blocks Kodi's script finalisation outright ("waiting on
        # thread"), because Kodi will not finalise a script while a thread it
        # started is alive. Observed on a quit: the socket kept receiving for
        # over a minute after the service exited, and Kodi needed SIGKILL
        # (2026-08-07). WebSocket.shutdown() closes the raw socket with no
        # handshake, which fails the pending recv immediately.
        try:
            if sock is not None:
                sock.shutdown()
        except Exception as error:  # pragma: no cover - defensive
            LOG.debug("websocket shutdown failed: %s", error)
        self.join(timeout=5)
        if self.is_alive():  # pragma: no cover - watchdog logging only
            LOG.warning("websocket thread did not stop within deadline")

    def _handle_open(self, app: "websocket.WebSocketApp") -> None:
        if self._stop:
            # A connect completing while stop() tears the client down must
            # not restart the machinery: the keepalive it would spawn has no
            # stop() left to run it down, and the connected callback would
            # fire into a service that is mid-teardown.
            return
        LOG.info("websocket connected")
        self._connected = True
        self._last_inbound = time.monotonic()
        if self._keepalive is not None:
            self._keepalive.stop()
        self._keepalive = _KeepAlive(app, self)
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

    def half_open(self) -> bool:
        """True — after closing the socket — when the connection has gone
        silent for STALE_SECONDS.

        The liveness contract: the server echoes every application-level
        KeepAlive sent at it, so a healthy socket receives a frame at least
        every KEEPALIVE_SECONDS. A half-open socket (NAT drop, sleeping AP)
        receives nothing while sends keep succeeding into the void — the
        one failure shape nothing else detects. Closing makes run_forever
        return; the run loop reconnects, and on_open fires the catch-up.
        """
        if not self._connected:
            return False
        if time.monotonic() - self._last_inbound <= STALE_SECONDS:
            return False
        LOG.info("websocket half-open: no traffic for %ss; recycling", STALE_SECONDS)
        try:
            if self._app is not None:
                self._app.close()
        except Exception as error:  # pragma: no cover - defensive
            LOG.debug("half-open close failed: %s", error)
        return True

    def _handle_message(self, app: "websocket.WebSocketApp", raw: str) -> None:
        if self._stop:
            # A stopped client hands its owner nothing: the service is
            # mid-teardown and the objects the callback reaches — the
            # Library, the kofin database — may already be gone. Observed
            # before this gate: a LibraryChanged dispatched a minute after
            # the service exited, applied against a torn-down Library while
            # Kodi sat in "waiting on thread" (2026-08-07 quit wedge).
            return
        # Every inbound frame is liveness, including the KeepAlive echoes the
        # event filter below discards and frames that fail to parse.
        self._last_inbound = time.monotonic()
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
    """Sends the Jellyfin application-level KeepAlive and polices its echo.

    One thread for both halves because they are one contract: every KeepAlive
    sent is echoed by the server, so the sender is exactly the thread that
    knows silence is abnormal (WSClient.half_open).
    """

    def __init__(self, app: "websocket.WebSocketApp", client: "WSClient") -> None:
        super().__init__(name="kofin-ws-keepalive")
        self._app = app
        self._client = client
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=5)

    def run(self) -> None:
        since_send = 0
        while not self._halt.wait(LIVENESS_TICK):
            if self._client.half_open():
                return
            since_send += LIVENESS_TICK
            if since_send < KEEPALIVE_SECONDS:
                continue
            since_send = 0
            try:
                self._app.send(
                    json.dumps({"MessageType": "KeepAlive", "Data": KEEPALIVE_SECONDS})
                )
            except Exception as error:
                LOG.debug("keepalive send failed: %s", error)
                return
