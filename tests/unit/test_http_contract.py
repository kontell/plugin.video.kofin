"""The two transports against a real loopback server.

``stdhttp.py`` promises "the same error taxonomy" as the requests transport;
the library-level fakes in ``test_http.py``/``test_stdhttp.py`` cannot check
that promise where it is most likely to break — on what the wire actually
carries. Every test here runs once per transport through ``Api`` and asserts
one answer for both.
"""

import http.server
import shutil
import ssl
import subprocess
import threading
from urllib.parse import unquote_plus

import pytest

from kofin.core import http as http_module
from kofin.core import stdhttp
from kofin.core.api import Api
from kofin.core.http import HttpError, ServerUnreachable

from tests.unit.transportserver import ScriptedServer


@pytest.fixture
def server():
    with ScriptedServer() as scripted:
        yield scripted


@pytest.fixture(params=["requests", "stdlib"])
def client(request, server, monkeypatch):
    # The ladder's backoff is real time; the contract is about answers.
    monkeypatch.setattr(http_module.time, "sleep", lambda seconds: None)
    if request.param == "requests":
        transport = http_module.Http()
    else:
        transport = stdhttp.StdlibHttp()
    api = Api(
        transport, server.url, "Kodi", "dev1", "0.1.0", token="tok", user_id="uid"
    )
    yield api, request.param
    transport.close()


def test_a_json_200_is_the_body(server, client):
    api, _ = client
    server.answer("/Items", 200, json_body={"Items": [1, 2], "TotalRecordCount": 2})
    assert api.get("/Items") == {"Items": [1, 2], "TotalRecordCount": 2}


def test_a_redirect_is_refused_on_both_and_names_the_location(server, client):
    """A redirect is an HttpError that carries the address the user should
    have entered; its empty body read as JSON would list an empty library."""
    api, _ = client
    server.answer("/Old", 302, headers={"Location": server.url + "/Items"})
    server.answer("/Items", 200, json_body={"Items": [1]})

    with pytest.raises(HttpError) as raised:
        api.get("/Old")

    assert raised.value.status == 302
    assert server.url + "/Items" in str(raised.value)
    assert [r[1] for r in server.requests] == ["/Old"]  # never followed


def test_a_non_json_200_is_an_http_error_on_both(server, client):
    """A proxy that is up while Jellyfin is down answers HTML with a 200,
    which must raise inside the ``JellyfinError`` taxonomy, not a
    ValueError past it."""
    api, _ = client
    server.answer(
        "/Items",
        200,
        body=b"<html>Bad gateway</html>",
        headers={"Content-Type": "text/html"},
    )

    with pytest.raises(HttpError) as raised:
        api.get("/Items")

    assert raised.value.status == 200
    assert "not JSON" in str(raised.value)


def test_a_device_name_in_any_script_is_sent_on_both(server, client):
    """http.client encodes header values as Latin-1; the device name rides in
    the auth header, so it has to arrive encoded and decode to what was set."""
    _, transport_name = client
    transport = (
        http_module.Http() if transport_name == "requests" else stdhttp.StdlibHttp()
    )
    api = Api(transport, server.url, "Гостиная", "dev1", "0.1.0", token="tok")
    server.answer("/Items", 200, json_body={"Items": []})

    try:
        assert api.get("/Items") == {"Items": []}
    finally:
        transport.close()

    sent = server.requests[0][2]["Authorization"]
    assert unquote_plus(sent.split('Device="', 1)[1].split('"', 1)[0]) == "Гостиная"


def test_a_non_json_probe_is_an_http_error_on_both(server, client):
    """The service's connection probe reads the same answer the same way: a
    ValueError out of it would end the service."""
    api, _ = client
    server.answer(
        "/System/Info/Public",
        200,
        body=b"<html>Sign in to the guest network</html>",
        headers={"Content-Type": "text/html"},
    )

    with pytest.raises(HttpError) as raised:
        api.probe_info()

    assert raised.value.status == 200
    assert "not JSON" in str(raised.value)


def test_a_probe_answer_that_is_not_an_object_is_an_http_error_on_both(server, client):
    api, _ = client
    server.answer("/System/Info/Public", 200, json_body=["not", "an", "object"])

    with pytest.raises(HttpError) as raised:
        api.probe_info()

    assert "not an object" in str(raised.value)


def test_a_body_cut_short_is_unreachable_on_both(server, client):
    """A connection that closes before the body it announced is a transport
    failure, replayed on the GET budget and then ServerUnreachable."""
    api, _ = client
    server.answer("/Items", 200, json_body={"Items": [1, 2, 3]}, repeat=True, cut=True)

    with pytest.raises(ServerUnreachable):
        api.get("/Items")

    assert [r[1] for r in server.requests] == ["/Items"] * 4


def test_a_transient_status_rides_the_get_budget(server, client):
    """A 503 or 502 from a proxy waiting for Jellyfin spends the GET budget
    like a transport error."""
    api, _ = client
    server.answer("/Items", 503)
    server.answer("/Items", 502)
    server.answer("/Items", 200, json_body={"Items": [1]})

    assert api.get("/Items") == {"Items": [1]}
    assert [r[1] for r in server.requests] == ["/Items"] * 3


def test_a_transient_status_that_never_clears_is_the_last_answer(server, client):
    api, _ = client
    server.answer("/Items", 503, repeat=True)

    with pytest.raises(HttpError) as raised:
        api.get("/Items")

    assert raised.value.status == 503
    assert len(server.requests) == 4  # the GET budget: one try + three replays


def test_a_500_is_terminal_for_every_method(server, client):
    """Jellyfin answers deterministic 500s for broken items; replaying those
    would only slow a walk."""
    api, _ = client
    server.answer("/Items", 500, repeat=True)
    server.answer("/Sessions/Playing", 500, repeat=True)

    with pytest.raises(HttpError) as raised:
        api.get("/Items")
    assert raised.value.status == 500

    with pytest.raises(HttpError):
        api.post("/Sessions/Playing", {"x": 1})

    assert [r[0] for r in server.requests] == ["GET", "POST"]


def test_a_post_answered_503_is_never_replayed(server, client):
    """POST carries no retry budget by design (a replay double-applies), and
    a 5xx is exactly the case where "never arrived" and "answered and lost"
    are indistinguishable."""
    api, _ = client
    server.answer("/Sessions/Playing", 503, repeat=True)

    with pytest.raises(HttpError) as raised:
        api.post("/Sessions/Playing", {"x": 1})

    assert raised.value.status == 503
    assert len(server.requests) == 1


def test_401_is_unauthorized_on_both(server, client):
    from kofin.core.http import Unauthorized

    api, _ = client
    server.answer("/Items", 401)

    with pytest.raises(Unauthorized):
        api.get("/Items")


UA = "Kodi/23.0 (X11; Linux x86_64) Version/23.0"


@pytest.mark.parametrize("transport_name", ["requests", "stdlib"])
def test_the_user_agent_given_is_the_one_sent_on_both(server, transport_name):
    """Left to themselves, http.client sends no User-Agent and requests sends
    python-requests/<version>."""
    transport = (
        http_module.Http(user_agent=UA)
        if transport_name == "requests"
        else stdhttp.StdlibHttp(user_agent=UA)
    )
    api = Api(transport, server.url, "Kodi", "dev1", "0.1.0", token="tok")
    server.answer("/Items", 200, json_body={"Items": []}, repeat=True)

    try:
        api.get("/Items")
        api.post("/Items", {"x": 1})
    finally:
        transport.close()

    assert [(m, h.get("User-Agent")) for m, _, h, _ in server.requests] == [
        ("GET", UA),
        ("POST", UA),
    ]


def test_the_plugin_client_sends_kodis_user_agent(server, monkeypatch):
    from kofin.core.settings import Credentials

    monkeypatch.setattr("xbmc.getUserAgent", lambda: UA)
    server.answer("/Items", 200, json_body={"Items": []})
    api = Api.for_plugin(
        Credentials(server_address=server.url, token="tok", device_id="dev1")
    )

    try:
        api.get("/Items")
    finally:
        api.close()

    assert server.requests[0][2].get("User-Agent") == UA


@pytest.fixture(scope="module")
def private_ca(tmp_path_factory):
    """A CA of the user's own, and a loopback server whose certificate it
    signed: ``(ca file, https url)``."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("needs the openssl command")
    work = tmp_path_factory.mktemp("private-ca")
    ca, ca_key = work / "ca.pem", work / "ca.key"
    cert, key, csr = work / "server.pem", work / "server.key", work / "server.csr"
    extensions = work / "server.cnf"
    extensions.write_text(
        "subjectAltName=IP:127.0.0.1\n"
        "authorityKeyIdentifier=keyid,issuer\n"
        "basicConstraints=CA:FALSE\n"
        "keyUsage=digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
    )
    for command in (
        ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2"]
        + ["-subj", "/CN=Kofin test CA"]
        + ["-addext", "keyUsage=critical,keyCertSign,cRLSign"]
        + ["-keyout", str(ca_key), "-out", str(ca)],
        ["req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=127.0.0.1"]
        + ["-keyout", str(key), "-out", str(csr)],
        ["x509", "-req", "-in", str(csr), "-CA", str(ca), "-CAkey", str(ca_key)]
        + ["-CAcreateserial", "-days", "2", "-extfile", str(extensions)]
        + ["-out", str(cert)],
    ):
        subprocess.run([openssl, *command], check=True, capture_output=True)

    class Answer(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Answer)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield str(ca), "https://127.0.0.1:%d" % server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("transport_name", ["requests", "stdlib"])
def test_a_ca_given_to_kodi_is_trusted_by_both(private_ca, transport_name, monkeypatch):
    """Kodi exports advancedsettings' catrustfile as SSL_CERT_FILE. A server
    that CA signed is reached by both transports with it and refused by both
    without it; requests on its own would trust certifi's bundle alone."""
    ca, url = private_ca
    monkeypatch.setattr(http_module.time, "sleep", lambda seconds: None)
    for name in (
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    ):
        monkeypatch.delenv(name, raising=False)

    def get():
        transport = (
            http_module.Http() if transport_name == "requests" else stdhttp.StdlibHttp()
        )
        api = Api(transport, url, "Kodi", "dev1", "0.1.0", token="tok")
        try:
            return api.get("/System/Info/Public")
        finally:
            transport.close()

    with pytest.raises(ServerUnreachable):
        get()

    monkeypatch.setenv("SSL_CERT_FILE", ca)
    assert get() == {}
