"""L1 units for the websocket client's connection edges.

Only the callback bookkeeping is exercised — the socket itself is never
opened. What matters here is that one real drop produces exactly one
disconnect callback, because ``run_forever(reconnect=...)`` retries on its
own and calls back repeatedly while a server stays down.
"""

import pytest

from kofin.core.ws import WSClient, socket_url, sslopt


@pytest.fixture
def client():
    events = {"connected": 0, "disconnected": 0}

    ws = WSClient(
        "http://server:8096",
        "auth",
        on_event=lambda message_type, data: None,
        on_connected=lambda: events.__setitem__("connected", events["connected"] + 1),
        on_disconnected=lambda: events.__setitem__(
            "disconnected", events["disconnected"] + 1
        ),
    )
    # _handle_open starts a keepalive thread against a real app object; the
    # flag bookkeeping is the part under test.
    ws._handle_open = lambda app: setattr(ws, "_connected", True) or ws._on_connected()
    return ws, events


def test_importing_the_client_does_not_poison_numpy():
    """The module used to set sys.modules['numpy'] = None at import — a
    workaround for a websocket-client code path that no longer exists (zero
    numpy references in the pinned 1.6.4), and a module-level mutation of
    shared interpreter state in a tree whose rule is that such state argues
    its way in (audit M3). ``import numpy`` must not be made to fail."""
    import importlib
    import sys

    sys.modules.pop("numpy", None)
    importlib.reload(sys.modules["kofin.core.ws"])

    assert sys.modules.get("numpy", "absent") is not None


def test_socket_url_follows_the_scheme():
    assert socket_url("http://server:8096") == "ws://server:8096/socket"
    assert socket_url("https://server:8096") == "wss://server:8096/socket"


def test_sslopt_matches_the_verify_flag():
    import ssl

    required = sslopt(True)
    assert required["cert_reqs"] == ssl.CERT_REQUIRED
    assert required["check_hostname"] is True
    none = sslopt(False)
    assert none["cert_reqs"] == ssl.CERT_NONE
    assert none["check_hostname"] is False


def test_open_then_close_is_one_edge_each(client):
    ws, events = client

    ws._handle_open(None)
    ws._handle_close(None, 1006, "gone")

    assert events == {"connected": 1, "disconnected": 1}


def test_repeated_close_reports_once(client):
    """A server that stays down keeps failing to reconnect. Only the drop
    from an actually-open socket is a disconnection."""
    ws, events = client

    ws._handle_open(None)
    ws._handle_close(None, 1006, "gone")
    ws._handle_close(None, 1006, "still gone")
    ws._handle_close(None, 1006, "still gone")

    assert events["disconnected"] == 1


def test_close_without_a_prior_open_is_silent(client):
    ws, events = client

    ws._handle_close(None, 1006, "never connected")

    assert events["disconnected"] == 0


def test_reconnect_reports_both_edges_again(client):
    ws, events = client

    ws._handle_open(None)
    ws._handle_close(None, 1006, "gone")
    ws._handle_open(None)
    ws._handle_close(None, 1006, "gone again")

    assert events == {"connected": 2, "disconnected": 2}


def test_deliberate_stop_is_not_a_drop(client):
    """Shutting the service down must not toast "lost connection" at the user
    on the way out."""
    ws, events = client

    ws._handle_open(None)
    ws._stop = True
    ws._handle_close(None, 1000, "bye")

    assert events["disconnected"] == 0


def test_disconnect_callback_is_optional():
    ws = WSClient(
        "http://server:8096",
        "auth",
        on_event=lambda message_type, data: None,
        on_connected=lambda: None,
    )
    ws._connected = True

    ws._handle_close(None, 1006, "gone")  # must not raise


def test_run_forever_keeps_pings_but_never_a_pong_deadline(monkeypatch):
    """Two settings are poison here, both measured live (perf plan W2.5 rev):
    ping_timeout tears healthy connections down ~120 s in — the server's own
    2-minute keepalive ping corrupts websocket-client's pong bookkeeping, and
    both test boxes flapped on an exact 130 s cycle — and reconnect= swallows
    the close/error callbacks, so drops were invisible. The run loop owns
    reconnection; half-open detection is app-level (half_open)."""
    from kofin.core import ws as ws_module

    captured = {}
    timeouts = []

    class FakeApp:
        def __init__(self, *args, **kwargs):
            pass

        def run_forever(self, **kwargs):
            captured.update(kwargs)

        def close(self):
            pass

    class StopMonitor:
        def waitForAbort(self, seconds):
            return True

    monkeypatch.setattr(ws_module.websocket, "WebSocketApp", FakeApp)
    monkeypatch.setattr(ws_module.websocket, "setdefaulttimeout", timeouts.append)
    monkeypatch.setattr(ws_module.xbmc, "Monitor", StopMonitor)

    client = WSClient(
        "http://s:8096",
        "auth",
        on_event=lambda message_type, data: None,
        on_connected=lambda: None,
    )
    client.run()

    assert captured == {"ping_interval": 10}
    assert timeouts == [10]


def test_wss_honours_ssl_verify(monkeypatch):
    """HTTP honours sslVerify; WSS used to inherit websocket-client's
    CERT_REQUIRED even when the setting was off."""
    import ssl

    from kofin.core import ws as ws_module

    captured = {}

    class FakeApp:
        def __init__(self, *args, **kwargs):
            pass

        def run_forever(self, **kwargs):
            captured.update(kwargs)

        def close(self):
            pass

    class StopMonitor:
        def waitForAbort(self, seconds):
            return True

    monkeypatch.setattr(ws_module.websocket, "WebSocketApp", FakeApp)
    monkeypatch.setattr(ws_module.websocket, "setdefaulttimeout", lambda *_a: None)
    monkeypatch.setattr(ws_module.xbmc, "Monitor", StopMonitor)

    client = WSClient(
        "https://s:8096",
        "auth",
        on_event=lambda message_type, data: None,
        on_connected=lambda: None,
        verify_ssl=False,
    )
    client.run()

    assert captured["ping_interval"] == 10
    assert captured["sslopt"]["cert_reqs"] == ssl.CERT_NONE
    assert captured["sslopt"]["check_hostname"] is False


def _liveness_client(events=None):
    return WSClient(
        "http://s:8096",
        "auth",
        on_event=(
            (lambda t, d: events.append((t, d)))
            if events is not None
            else (lambda t, d: None)
        ),
        on_connected=lambda: None,
    )


def test_half_open_recycles_only_a_silent_live_connection():
    """Silence past STALE_SECONDS on a connected socket closes it — the run
    loop then reconnects and on_open fires the catch-up. A fresh connection
    and a disconnected client are both left alone (the keepalive thread of a
    dead connection must not thrash the next one)."""
    import time as time_module

    from kofin.core import ws as ws_module

    closes = []

    class ClosableApp:
        def close(self):
            closes.append(1)

    client = _liveness_client()
    client._app = ClosableApp()

    client._connected = True
    client._last_inbound = time_module.monotonic()
    assert client.half_open() is False

    client._last_inbound = time_module.monotonic() - (ws_module.STALE_SECONDS + 5)
    assert client.half_open() is True
    assert closes == [1]

    client._connected = False
    assert client.half_open() is False
    assert closes == [1]


def test_a_stopped_client_dispatches_nothing():
    """Events arriving after stop() must be dropped, not handed over: the
    owner is mid-teardown, and the objects the callback reaches — the
    Library, the kofin database — may already be gone. Observed before the
    gate existed: a LibraryChanged dispatched a minute after the service
    exited was applied against a torn-down Library (2026-08-07 quit wedge)."""
    events = []
    client = _liveness_client(events)

    client._stop = True
    client._handle_message(
        None, '{"MessageType": "LibraryChanged", "Data": {"ItemsAdded": ["x"]}}'
    )

    assert events == []


def test_a_connect_landing_after_stop_spawns_nothing():
    """A connect that completes while stop() runs must not restart the
    machinery — the keepalive it would spawn has no stop() left to run it
    down, and the thread it starts blocks Kodi's script finalisation."""
    connected = []
    client = WSClient(
        "http://s:8096",
        "auth",
        on_event=lambda t, d: None,
        on_connected=lambda: connected.append(1),
    )

    client._stop = True
    client._handle_open(None)

    assert connected == []
    assert client._keepalive is None
    assert client._connected is False


class _EscalationHarness:
    """A stop() harness whose thread only dies once the descriptor is severed
    (or immediately, when ``dies_on_close``): is_alive/join are replaced so
    no real thread is needed."""

    def __init__(self, dies_on_close: bool):
        self.trace = []
        harness = self

        class Sock:
            def shutdown(self):
                harness.trace.append("shutdown")

        class App:
            sock = Sock()

            def close(self):
                harness.trace.append("close")
                if not dies_on_close:
                    raise RuntimeError("close handshake died")

        self.client = _liveness_client()
        self.client._app = App()
        self._alive = True

        def is_alive():
            return self._alive

        def join(timeout=None):
            if dies_on_close and "close" in self.trace:
                self._alive = False
            if "shutdown" in self.trace:
                self._alive = False

        self.client.is_alive = is_alive  # type: ignore[method-assign]
        self.client.join = join  # type: ignore[method-assign]


def test_stop_severs_the_socket_when_the_graceful_close_fails():
    """A close handshake that raises leaves the descriptor open, and a thread
    still in recv() then outlives every deadline and blocks Kodi's script
    finalisation ("waiting on thread") — the observed quit wedge needed
    SIGKILL. stop() must escalate to a raw-socket shutdown()."""
    harness = _EscalationHarness(dies_on_close=False)

    harness.client.stop()

    assert harness.trace == ["close", "shutdown"]
    assert not harness.client.is_alive()


def test_stop_prefers_the_graceful_close():
    """When the graceful close unblocks the thread, no escalation happens —
    the close handshake is the polite exit and the server sees a clean
    disconnect."""
    harness = _EscalationHarness(dies_on_close=True)

    harness.client.stop()

    assert harness.trace == ["close"]
    assert not harness.client.is_alive()


def test_every_inbound_frame_feeds_liveness_even_when_ignored():
    """The KeepAlive echoes the event filter discards are exactly the frames
    the liveness check lives on; an unparseable frame still proves the pipe."""
    events = []
    client = _liveness_client(events)
    client._last_inbound = 0.0

    client._handle_message(None, '{"MessageType": "KeepAlive"}')
    assert client._last_inbound > 0.0
    assert events == []  # still ignored as an event

    client._last_inbound = 0.0
    client._handle_message(None, "not-json")
    assert client._last_inbound > 0.0
    assert events == []


def test_each_attempt_builds_a_fresh_app(monkeypatch):
    """``WebSocketApp.teardown()`` is one-shot per instance (websocket-client
    1.6.4: ``has_done_teardown`` is set in __init__ and never reset by
    run_forever). So a reused app that saw a *failed connect* — setSock()
    assigns ``sock`` before connect() raises — keeps the dead socket, and the
    next run_forever raises "socket is already opened" straight out of this
    thread. Live consequence: one connect failure after a healthy session
    killed the websocket for the life of the Kodi process.
    """
    from kofin.core import ws as ws_mod

    built = []

    class FakeApp:
        def __init__(self, *args, **kwargs):
            built.append(self)
            self.sock = None

        def run_forever(self, **kwargs):
            raise ws_mod.websocket.WebSocketException("socket is already opened")

        def close(self):
            pass

    monkeypatch.setattr(ws_mod.websocket, "WebSocketApp", FakeApp)
    monkeypatch.setattr("xbmc.Monitor.waitForAbort", lambda self, seconds=0: False)

    client = WSClient(
        "http://server:8096",
        "auth",
        on_event=lambda message_type, data: None,
        on_connected=lambda: None,
    )
    real_build = client._build_app

    def build_then_stop():
        app = real_build()
        if len(built) >= 2:
            client._stop = True  # two attempts is enough to prove the point
        return app

    client._build_app = build_then_stop

    client.run()

    # The raise cost one attempt, not the thread, and the second attempt got
    # its own app rather than the poisoned one.
    assert len(built) == 2
    assert built[0] is not built[1]
