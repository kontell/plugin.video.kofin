"""The two transports against a real loopback server (audit fixes plan H0).

``stdhttp.py`` promises "the same error taxonomy" as the requests transport;
the library-level fakes in ``test_http.py``/``test_stdhttp.py`` cannot check
that promise where it is most likely to break — on what the wire actually
carries. Every test here runs once per transport through ``Api`` and asserts
one answer for both.

Written on the before build with today's behaviour pinned, so the H4 and H5
commits flip named assertions instead of adding tests nobody watched fail.
"""

import pytest

from kofin.core import http as http_module
from kofin.core import stdhttp
from kofin.core.api import Api
from kofin.core.http import HttpError

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
    """Before H4 (audit F4) requests followed the redirect while the plugin
    transport read the empty 302 body as an empty library — a working
    service beside listings of nothing. One answer now: an HttpError that
    carries the address the user should have entered."""
    api, _ = client
    server.answer("/Old", 302, headers={"Location": server.url + "/Items"})
    server.answer("/Items", 200, json_body={"Items": [1]})

    with pytest.raises(HttpError) as raised:
        api.get("/Old")

    assert raised.value.status == 302
    assert server.url + "/Items" in str(raised.value)
    assert [r[1] for r in server.requests] == ["/Old"]  # never followed


def test_a_non_json_200_is_an_http_error_on_both(server, client):
    """A proxy that is up while Jellyfin is down answers HTML with a 200.
    That used to raise a ValueError past every ``except JellyfinError``
    and end in a Kodi error dialog (audit F4)."""
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


def test_a_transient_status_rides_the_get_budget(server, client):
    """Before H5 the ladder replayed transport errors only (audit F7): a
    503 from a proxy waiting for Jellyfin spent none of the GET budget and
    the sync healed at the 60 s resume ladder instead of the half-second
    backoff already in this function."""
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
    would only slow a walk. Stays terminal after H5."""
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
    are indistinguishable. Unchanged by H5."""
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
