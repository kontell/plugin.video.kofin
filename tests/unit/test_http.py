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
