"""The stdlib transport, held to the same contract as the requests one.

The whole point of the split is that the plugin process must behave
identically while skipping a ~1 s import (see the module docstring), so these
tests mirror ``test_http.py``'s expectations — status taxonomy, per-method
retry budget, timeouts — and add the two things only this implementation has
to get right: the connect/read timeout split, and reusing one connection
without inheriting a dead one.
"""

import http.client

import pytest

from kofin.core import http as http_module
from kofin.core import stdhttp


class FakeRaw:
    """What http.client hands back."""

    def __init__(self, status=200, payload=b"{}", will_close=False):
        self.status = status
        self._payload = payload
        self.will_close = will_close

    def read(self):
        return self._payload


class FakeConnection:
    """Records what was sent and plays a scripted sequence of outcomes."""

    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.sock = type("Sock", (), {"settimeout": lambda self, value: None})()
        self.requests = []
        self.closed = False
        self.outcomes = []
        FakeConnection.instances.append(self)

    def connect(self):
        pass

    def request(self, method, target, body=None, headers=None):
        self.requests.append((method, target, body, headers))
        outcome = self.outcomes.pop(0) if self.outcomes else FakeRaw()
        if isinstance(outcome, Exception):
            raise outcome
        self._pending = outcome

    def getresponse(self):
        return self._pending

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def fake_connections(monkeypatch):
    FakeConnection.instances = []
    monkeypatch.setattr(stdhttp.http.client, "HTTPSConnection", FakeConnection)
    monkeypatch.setattr(stdhttp.http.client, "HTTPConnection", FakeConnection)
    monkeypatch.setattr(stdhttp.time, "sleep", lambda seconds: None)
    return FakeConnection


def test_the_request_line_carries_path_and_params():
    transport = stdhttp.StdlibHttp()
    transport.request(
        "GET",
        "https://s:8096/Items",
        headers={"Authorization": "tok"},
        params={"userId": "u1", "Fields": "a,b", "Recursive": True, "skip": None},
    )
    connection = FakeConnection.instances[0]
    method, target, body, headers = connection.requests[0]

    assert (connection.host, connection.port) == ("s", 8096)
    assert method == "GET" and body is None
    assert target.startswith("/Items?")
    # A space would be %20 and a None-valued param is dropped, matching what
    # the requests transport puts on the wire.
    assert "userId=u1" in target and "Fields=a%2Cb" in target
    assert "Recursive=True" in target and "skip" not in target
    assert headers["Authorization"] == "tok"


def test_a_json_body_is_encoded_with_its_headers():
    transport = stdhttp.StdlibHttp()
    transport.request("POST", "https://s:8096/Items/x/PlaybackInfo", json_body={"a": 1})
    _method, _target, body, headers = FakeConnection.instances[0].requests[0]

    assert body == b'{"a": 1}'
    assert headers["Content-Type"] == "application/json"
    assert headers["Content-Length"] == "8"


def _answering(monkeypatch, status, payload=b"{}"):
    """A transport whose next exchange answers with ``status``."""

    def make(host, port, timeout=None, context=None):
        connection = FakeConnection(host, port, timeout, context)
        connection.outcomes = [FakeRaw(status, payload)]
        return connection

    monkeypatch.setattr(stdhttp.http.client, "HTTPSConnection", make)
    return stdhttp.StdlibHttp()


def test_status_taxonomy_matches_the_requests_transport(monkeypatch):
    for status in (401, 403):
        with pytest.raises(http_module.Unauthorized):
            _answering(monkeypatch, status).request("GET", "https://s/x")

    for status in (404, 500, 503):
        with pytest.raises(http_module.HttpError) as caught:
            _answering(monkeypatch, status).request("GET", "https://s/x")
        assert caught.value.status == status


def test_a_success_returns_the_body_and_parses_json(monkeypatch):
    transport = _answering(monkeypatch, 200, b'{"Items": [1, 2]}')
    response = transport.request("GET", "https://s/Items")

    assert response.status_code == 200
    assert response.content == b'{"Items": [1, 2]}'
    assert response.json() == {"Items": [1, 2]}


def test_per_method_retry_budget_is_the_shared_policy(monkeypatch):
    """POST must not replay (a lost response double-applies); GET may."""
    attempts = {"count": 0}

    def make(host, port, timeout=None, context=None):
        connection = FakeConnection(host, port, timeout, context)
        connection.outcomes = [OSError("connection reset")] * 5
        attempts["count"] += 1
        return connection

    monkeypatch.setattr(stdhttp.http.client, "HTTPSConnection", make)

    transport = stdhttp.StdlibHttp()
    with pytest.raises(http_module.ServerUnreachable):
        transport.request("POST", "https://s/Sessions/Playing")
    assert attempts["count"] == 1  # one attempt, no replay

    attempts["count"] = 0
    with pytest.raises(http_module.ServerUnreachable):
        transport.request("GET", "https://s/Items")
    assert attempts["count"] == http_module.RETRIES + 1


def test_an_explicit_retry_count_still_wins(monkeypatch):
    attempts = {"count": 0}

    def make(host, port, timeout=None, context=None):
        connection = FakeConnection(host, port, timeout, context)
        connection.outcomes = [OSError("reset")] * 5
        attempts["count"] += 1
        return connection

    monkeypatch.setattr(stdhttp.http.client, "HTTPSConnection", make)
    transport = stdhttp.StdlibHttp()
    with pytest.raises(http_module.ServerUnreachable):
        transport.request("GET", "https://s/x", retries=0)
    assert attempts["count"] == 1


def test_the_connection_is_reused_across_calls():
    """The import saving would be handed straight back as TLS handshakes if
    every call opened its own connection — the play route makes three or four."""
    transport = stdhttp.StdlibHttp()
    transport.request("GET", "https://s:8096/Items/1")
    transport.request("GET", "https://s:8096/Items/2")
    transport.request("POST", "https://s:8096/Items/2/PlaybackInfo", json_body={})

    assert len(FakeConnection.instances) == 1
    assert len(FakeConnection.instances[0].requests) == 3


def test_a_dead_pooled_socket_is_retried_once_for_any_method(monkeypatch):
    """A connection can be closed by the server while it sits idle. That
    failure happens before the request reaches anyone, so replaying it is safe
    even for POST — and must not spend the (zero) POST budget."""
    made = []

    def make(host, port, timeout=None, context=None):
        connection = FakeConnection(host, port, timeout, context)
        if len(made) == 0:
            # First connection: works once, then dies like a stale keep-alive.
            connection.outcomes = [FakeRaw(200), http.client.RemoteDisconnected("bye")]
        made.append(connection)
        return connection

    monkeypatch.setattr(stdhttp.http.client, "HTTPSConnection", make)
    transport = stdhttp.StdlibHttp()

    transport.request("GET", "https://s/first")  # primes the pool
    response = transport.request("POST", "https://s/second", json_body={})

    assert response.status_code == 200
    assert len(made) == 2  # reconnected rather than failing the POST


def test_a_fresh_connection_failure_is_not_retried_for_free(monkeypatch):
    """Only the *pooled* socket gets the free replay: a brand-new connection
    that fails has no such guarantee, so the caller's budget applies (POST: 0)."""
    made = []

    def make(host, port, timeout=None, context=None):
        connection = FakeConnection(host, port, timeout, context)
        connection.outcomes = [http.client.RemoteDisconnected("bye")] * 3
        made.append(connection)
        return connection

    monkeypatch.setattr(stdhttp.http.client, "HTTPSConnection", make)
    transport = stdhttp.StdlibHttp()

    with pytest.raises(http_module.ServerUnreachable):
        transport.request("POST", "https://s/x", json_body={})
    assert len(made) == 1


def test_the_timeout_tuple_splits_connect_from_read():
    """The interactive profile depends on it: a short connect budget so an
    unreachable server fails fast, a long read budget because a big listing is
    legitimately slow."""
    settings = []

    class RecordingSock:
        def settimeout(self, value):
            settings.append(value)

    class TimingConnection(FakeConnection):
        def __init__(self, host, port, timeout=None, context=None):
            super().__init__(host, port, timeout, context)
            self.sock = RecordingSock()

    import kofin.core.stdhttp as module

    module.http.client.HTTPSConnection = TimingConnection
    try:
        transport = stdhttp.StdlibHttp()
        transport.request("GET", "https://s/x", timeout=(3.05, 30.0))
    finally:
        module.http.client.HTTPSConnection = FakeConnection

    connection = TimingConnection.instances[-1]
    assert connection.timeout == 3.05  # connect
    assert settings == [30.0]  # read


def test_a_close_flagged_response_drops_the_connection():
    """The server said it is done with the socket; offering it to the next
    call as though it were alive is how you collect mystery resets."""
    transport = stdhttp.StdlibHttp()

    def make(host, port, timeout=None, context=None):
        connection = FakeConnection(host, port, timeout, context)
        connection.outcomes = [FakeRaw(200, b"{}", will_close=True)]
        return connection

    import kofin.core.stdhttp as module

    module.http.client.HTTPSConnection = make
    try:
        transport.request("GET", "https://s/x")
    finally:
        module.http.client.HTTPSConnection = FakeConnection

    assert transport._conn is None


def test_verification_follows_the_setting():
    import ssl

    verifying = stdhttp.StdlibHttp(True)._context()
    assert verifying.verify_mode == ssl.CERT_REQUIRED and verifying.check_hostname

    unverified = stdhttp.StdlibHttp(False)._context()
    assert unverified.verify_mode == ssl.CERT_NONE
    assert unverified.check_hostname is False


def test_the_transport_imports_without_requests():
    """The reason the module exists: importing it must not drag in the tree it
    was written to avoid."""
    import subprocess
    import sys
    from pathlib import Path

    lib = str(Path(__file__).resolve().parents[2] / "lib")
    code = (
        "import sys; sys.path.insert(0, %r); "
        "import kofin.core.stdhttp; "
        "assert 'requests' not in sys.modules, 'requests imported by the stdlib "
        "transport'" % lib
    )
    subprocess.check_call([sys.executable, "-c", code])
