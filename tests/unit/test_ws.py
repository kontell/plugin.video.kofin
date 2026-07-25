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
