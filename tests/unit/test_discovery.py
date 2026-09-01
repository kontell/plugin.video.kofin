"""L1: Jellyfin's UDP auto-discovery, against a scripted socket.

No real socket is bound. ``discovery.scan`` takes its socket and its clock as
arguments for exactly this — the shape of the window (how many probes, how
long) is the thing worth pinning, and it is not observable from a live scan.
"""

import socket

import pytest

from kofin.core import discovery

PAYLOAD = (
    b'{"Address":"http://192.168.1.167:8096",'
    b'"Id":"2606bcf8bf2f455485b967be07508adc",'
    b'"Name":"minipie","EndpointAddress":null}'
)
HOST = "192.168.1.167"


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeSocket:
    """Answers a script of ``(delay, payload, host)`` and drives the clock.

    A ``None`` payload, or running off the end of the script, burns the whole
    read timeout and raises ``socket.timeout`` — which is what a quiet network
    does.
    """

    def __init__(self, clock, script=(), send_error=None):
        self.clock = clock
        self.script = list(script)
        self.send_error = send_error
        self.sent = []
        self.timeout = None
        self.closed = False

    def sendto(self, data, addr):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((data, addr, self.clock.now))

    def settimeout(self, value):
        self.timeout = value

    def recvfrom(self, _size):
        if self.script:
            delay, payload, host = self.script.pop(0)
            if payload is not None and delay < self.timeout:
                self.clock.advance(delay)
                return payload, (host, discovery.DISCOVERY_PORT)
        self.clock.advance(self.timeout)
        raise socket.timeout()

    def close(self):
        self.closed = True


def _scan(script=(), send_error=None, on_found=None):
    clock = FakeClock()
    sock = FakeSocket(clock, script, send_error)
    found = discovery.scan(on_found, sock_factory=lambda: sock, clock=clock)
    return found, sock, clock


# -- the window ---------------------------------------------------------------


def test_quiet_network_sends_three_probes_and_stops_at_the_window():
    """The budget buys repeats, not patience: a lost broadcast is the only
    thing a longer wait would recover, and another probe recovers it sooner."""
    found, sock, clock = _scan()

    assert found == []
    assert len(sock.sent) == 3
    assert [round(when, 3) for _d, _a, when in sock.sent] == [0.0, 1.0, 2.0]
    assert clock.now == pytest.approx(discovery.SCAN_SECONDS)


def test_the_probe_is_the_broadcast_every_other_client_sends():
    _found, sock, _clock = _scan()

    data, addr, _when = sock.sent[0]
    assert data == b"who is JellyfinServer?"
    assert addr == ("255.255.255.255", 7359)


def test_the_socket_is_closed_even_when_nothing_answers():
    _found, sock, _clock = _scan()

    assert sock.closed


def test_a_reply_does_not_shorten_the_window():
    """Two servers on one network must both get their chance to answer."""
    _found, sock, clock = _scan([(0.003, PAYLOAD, HOST)])

    assert len(sock.sent) == 3
    assert clock.now == pytest.approx(discovery.SCAN_SECONDS)


# -- parsing ------------------------------------------------------------------


def test_a_reply_is_parsed_into_a_found():
    found, _sock, _clock = _scan([(0.003, PAYLOAD, HOST)])

    assert found == [
        discovery.Found(
            server_id="2606bcf8bf2f455485b967be07508adc",
            name="minipie",
            address="http://192.168.1.167:8096",
            source_host=HOST,
        )
    ]


def test_on_found_fires_as_the_datagram_lands():
    """The caller verifies each hit over HTTP while the window is still open,
    which is what keeps verification free in the ordinary case."""
    seen = []
    _found, _sock, _clock = _scan([(0.003, PAYLOAD, HOST)], on_found=seen.append)

    assert [entry.name for entry in seen] == ["minipie"]


def test_the_advertised_address_is_normalised():
    payload = b'{"Address":"http://box:8096/","Id":"a","Name":"box"}'
    found, _sock, _clock = _scan([(0.01, payload, "10.0.0.9")])

    assert found[0].address == "http://box:8096"


def test_a_nameless_server_falls_back_to_its_address():
    payload = b'{"Address":"http://10.0.0.9:8096","Id":"a","Name":""}'
    found, _sock, _clock = _scan([(0.01, payload, "10.0.0.9")])

    assert found[0].name == "10.0.0.9"


def test_a_payload_without_an_address_is_dropped():
    payload = b'{"Id":"a","Name":"box"}'
    found, _sock, _clock = _scan([(0.01, payload, "10.0.0.9")])

    assert found == []


# -- deduplication ------------------------------------------------------------


def test_a_repeated_datagram_is_counted_once():
    found, _sock, _clock = _scan(
        [(0.003, PAYLOAD, HOST), (0.004, PAYLOAD, HOST)],
    )

    assert len(found) == 1


def test_two_servers_sharing_an_id_are_both_kept():
    """Restoring one data directory onto a second box yields two real servers
    with one Id. Upstream dedupes on the id alone and silently hides the
    second, which reads as discovery being broken (jellyfin-android#1510)."""
    one = b'{"Address":"http://10.0.0.1:8096","Id":"same","Name":"attic"}'
    two = b'{"Address":"http://10.0.0.2:8096","Id":"same","Name":"attic"}'
    found, _sock, _clock = _scan([(0.01, one, "10.0.0.1"), (0.02, two, "10.0.0.2")])

    assert [entry.address for entry in found] == [
        "http://10.0.0.1:8096",
        "http://10.0.0.2:8096",
    ]


# -- surviving the network ----------------------------------------------------


def test_a_datagram_that_is_not_json_does_not_end_the_scan():
    """Something else may be sitting on 7359; the window belongs to whatever
    else answers."""
    found, sock, _clock = _scan(
        [(0.01, b"\xff\xfe not json", "10.0.0.9"), (0.02, PAYLOAD, HOST)],
    )

    assert [entry.name for entry in found] == ["minipie"]
    assert len(sock.sent) == 3


def test_an_unsendable_probe_is_not_fatal():
    """An interface that is down should answer "nothing found", not raise into
    the settings dialog."""
    found, _sock, clock = _scan(send_error=OSError("Network is unreachable"))

    assert found == []
    assert clock.now == pytest.approx(discovery.SCAN_SECONDS)


# -- the endpoint fallback ----------------------------------------------------


def test_fallback_rehosts_the_published_url_on_the_source_address():
    found = discovery.Found("a", "attic", "https://attic.example.com:8920", "10.0.0.5")

    assert discovery.fallback_address(found) == "https://10.0.0.5:8920"


def test_fallback_keeps_a_base_path():
    found = discovery.Found("a", "attic", "http://attic:8096/jellyfin", "10.0.0.5")

    assert discovery.fallback_address(found) == "http://10.0.0.5:8096/jellyfin"


def test_fallback_is_the_same_address_when_the_server_named_its_own_ip():
    found = discovery.Found("a", "minipie", "http://192.168.1.167:8096", HOST)

    assert discovery.fallback_address(found) == found.address


# -- row text -----------------------------------------------------------------


def test_label_without_a_probe_is_name_over_address():
    assert discovery.label_for("minipie", "http://10.0.0.1:8096") == (
        "minipie",
        "http://10.0.0.1:8096",
    )


def test_label_prefers_the_verified_name_and_shows_the_version():
    label, detail = discovery.label_for(
        "minipie",
        "http://10.0.0.1:8096",
        {"ServerName": "attic", "Version": "10.11.0"},
    )

    assert (label, detail) == ("attic", "http://10.0.0.1:8096 · 10.11.0")


def test_label_falls_back_to_the_datagram_name_when_the_probe_gave_none():
    label, detail = discovery.label_for(
        "minipie", "http://10.0.0.1:8096", {"ServerName": ""}
    )

    assert (label, detail) == ("minipie", "http://10.0.0.1:8096")
