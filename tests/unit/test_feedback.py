"""The plugin's one spelling of a failed request."""

from kofin.core.http import HttpError, ServerUnreachable, Unauthorized
from kofin.plugin import feedback


def test_the_message_names_what_failed(monkeypatch):
    monkeypatch.setattr(
        feedback.settings,
        "localized",
        lambda sid: "S%d %%s" % sid if sid == 30821 else "S%d" % sid,
    )

    assert feedback.message(Unauthorized("rejected")) == "S30022"
    assert feedback.message(HttpError(502, "Bad Gateway")) == "S30821 Bad Gateway"
    assert feedback.message(ServerUnreachable("refused")) == "S30018"
    assert feedback.message(RuntimeError("other")) == "S30018"


def test_a_bad_answer_carries_the_servers_words(monkeypatch):
    monkeypatch.setattr(
        feedback.settings,
        "localized",
        lambda sid: "Server error - %s" if sid == 30821 else "S%d" % sid,
    )
    assert feedback.message(HttpError(500, "boom")) == "Server error - boom"


def test_failed_toasts_the_message_as_an_error(monkeypatch):
    shown = []
    monkeypatch.setattr(feedback.settings, "localized", lambda sid: "S%d" % sid)
    monkeypatch.setattr(
        feedback.toast,
        "show",
        lambda message, level, time_ms=0: shown.append((message, level)),
    )

    feedback.failed(Unauthorized("rejected"))

    assert shown == [("S30022", feedback.toast.ERROR)]
