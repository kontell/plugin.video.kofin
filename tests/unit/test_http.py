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


def test_500_raises_http_error_with_status(monkeypatch):
    transport, _ = make_http(monkeypatch, [FakeResponse(503)])
    with pytest.raises(http.HttpError) as exc:
        transport.request("GET", "http://s/x")
    assert exc.value.status == 503
