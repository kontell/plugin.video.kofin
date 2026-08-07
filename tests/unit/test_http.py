import subprocess
import sys
from pathlib import Path

import pytest
import requests

from kofin.core import http


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.content = b"x"

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda seconds: None)


def make_http(monkeypatch, outcomes):
    transport = http.Http()
    session = FakeSession(outcomes)
    monkeypatch.setattr(transport, "session", lambda: session)
    return transport, session


def test_retries_then_succeeds(monkeypatch):
    transport, session = make_http(
        monkeypatch, [requests.ConnectionError("boom"), FakeResponse(200, {"ok": 1})]
    )
    response = transport.request("GET", "http://s/x")
    assert response.json() == {"ok": 1}
    assert len(session.calls) == 2


def test_exhausted_retries_raise_unreachable(monkeypatch):
    transport, _ = make_http(monkeypatch, [requests.ConnectionError("boom")] * 3)
    with pytest.raises(http.ServerUnreachable):
        transport.request("GET", "http://s/x", retries=2)


def test_401_raises_unauthorized_without_retry(monkeypatch):
    transport, session = make_http(monkeypatch, [FakeResponse(401)])
    with pytest.raises(http.Unauthorized):
        transport.request("GET", "http://s/x")
    assert len(session.calls) == 1


def test_every_request_is_logged_for_counting(monkeypatch):
    """The scenario gates assert request counts ("zero per-show /Episodes
    calls", "3067 fetches to 0"); those are ungreppable unless successes are
    logged too, not only failures."""
    lines = []
    monkeypatch.setattr(http.LOG, "debug", lambda msg, *a: lines.append(msg % a))

    response = FakeResponse(200, {"ok": 1})
    response.request = type("Sent", (), {"url": "http://s/Items?Ids=abc"})()
    transport, _ = make_http(monkeypatch, [response])
    transport.request("GET", "http://s/Items")

    assert any("Items?Ids=abc" in line and "-> 200" in line for line in lines)


def test_request_log_survives_a_response_without_a_request(monkeypatch):
    """Not every response object carries .request; fall back to the url."""
    lines = []
    monkeypatch.setattr(http.LOG, "debug", lambda msg, *a: lines.append(msg % a))

    transport, _ = make_http(monkeypatch, [FakeResponse(200, {"ok": 1})])
    transport.request("GET", "http://s/plain")

    assert any("http://s/plain" in line for line in lines)


def test_request_log_is_masked_like_every_other_line():
    """The log chokepoint must redact a token even if one reaches a URL."""
    from kofin.core import log as log_module

    log_module.register_secret("tok-secret-value")
    assert "tok-secret-value" not in log_module.mask(
        "http GET http://s/Items?api_key=tok-secret-value -> 200"
    )


def test_500_raises_http_error_with_status(monkeypatch):
    transport, _ = make_http(monkeypatch, [FakeResponse(503)])
    with pytest.raises(http.HttpError) as exc:
        transport.request("GET", "http://s/x")
    assert exc.value.status == 503


def test_post_is_never_retried_by_default(monkeypatch):
    """A replayed POST double-applies whenever the response was lost after the
    server acted: a second SyncPlay group, a queue item added twice, a second
    AutoOpenLiveStream transcode session nothing closes. Fail fast instead."""
    transport, session = make_http(monkeypatch, [requests.Timeout("boom")])
    with pytest.raises(http.ServerUnreachable):
        transport.request("POST", "http://s/Sessions/Playing")
    assert len(session.calls) == 1


def test_delete_gets_one_replay_by_default(monkeypatch):
    """A DELETE states an absolute fact, so one replay is safe and keeps
    unfavorite/mark-unplayed resilient to a dropped keep-alive socket."""
    transport, session = make_http(
        monkeypatch, [requests.ConnectionError("boom"), FakeResponse(204)]
    )
    transport.request("DELETE", "http://s/UserFavoriteItems/x")
    assert len(session.calls) == 2


def test_an_explicit_retries_still_wins_for_post(monkeypatch):
    """Callers that know their POST is an absolute-state write can opt back
    in; the per-method default only fills the blank."""
    transport, session = make_http(
        monkeypatch,
        [requests.ConnectionError("boom"), FakeResponse(200, {"ok": 1})],
    )
    response = transport.request("POST", "http://s/x", retries=2)
    assert response.json() == {"ok": 1}
    assert len(session.calls) == 2


def test_importing_the_transport_does_not_import_requests():
    """requests costs ~1 s inside Kodi's Python (no bytecode cache), and
    routes that never talk to the server still import this module through the
    Api plumbing — the import must stay deferred to first use (perf plan
    W1.2). Checked in a subprocess so the suite's own imports cannot mask a
    regression."""
    lib = str(Path(__file__).resolve().parents[2] / "lib")
    code = (
        "import sys; sys.path.insert(0, %r); "
        "import kofin.core.http; "
        "assert 'requests' not in sys.modules, 'requests imported at module load'" % lib
    )
    subprocess.check_call([sys.executable, "-c", code])


# --- the retry ladder has to know when the service is stopping ---------------


def test_a_stopping_service_abandons_the_ladder_instead_of_replaying(monkeypatch):
    """Measured on Omega: a black-holed GET rides 4 attempts x (6s connect +
    30s read) plus backoff — about 147s — and Kodi will not finalise a script
    while a thread it started is alive, so it blocks on "waiting on thread"
    for all of it. Nothing in the ladder consulted the stop flag."""
    stopping = {"yes": False}
    transport = http.Http(abort=lambda: stopping["yes"])
    session = FakeSession([requests.ConnectionError("boom")] * 4)
    monkeypatch.setattr(transport, "session", lambda: session)

    stopping["yes"] = True
    with pytest.raises(http.ServerUnreachable) as raised:
        transport.request("GET", "http://s/x")

    assert len(session.calls) == 1  # the one in flight, and no replay
    assert "stopping" in str(raised.value)


def test_the_request_already_in_flight_is_still_allowed_to_answer(monkeypatch):
    """The abort bounds the replays; it must not cancel a call that is about
    to succeed, or a teardown would drop the last write of every session."""
    transport = http.Http(abort=lambda: True)
    session = FakeSession([FakeResponse(200, {"ok": 1})])
    monkeypatch.setattr(transport, "session", lambda: session)

    assert transport.request("GET", "http://s/x").json() == {"ok": 1}


def test_without_an_abort_the_ladder_is_unchanged(monkeypatch):
    """The plugin process has no teardown to serve and passes none."""
    transport, session = make_http(
        monkeypatch,
        [requests.ConnectionError("boom")] * 3 + [FakeResponse(200, {"ok": 1})],
    )
    assert transport.request("GET", "http://s/x").json() == {"ok": 1}
    assert len(session.calls) == 4
