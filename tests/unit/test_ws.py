"""L1 units for the websocket client's connection edges.

Only the callback bookkeeping is exercised — the socket itself is never
opened. What matters here is that one real drop produces exactly one
disconnect callback, because ``run_forever(reconnect=...)`` retries on its
own and calls back repeatedly while a server stays down.
"""

import pytest

from kofin.core.ws import WSClient, socket_url


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


def test_socket_url_follows_the_scheme():
    assert socket_url("http://server:8096") == "ws://server:8096/socket"
    assert socket_url("https://server:8096") == "wss://server:8096/socket"


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
