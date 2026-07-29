import pytest

from kofin.core import settings
from tests.unit.fakes import FakeAddon


@pytest.fixture(autouse=True)
def fake_addon(monkeypatch):
    FakeAddon.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    return FakeAddon


def test_get_list_splits_and_strips():
    settings.set_str("codecs", "h264, hevc ,av1,,")
    assert settings.get_list("codecs") == ["h264", "hevc", "av1"]


def test_credentials_generate_stable_device_id():
    first = settings.Credentials.load()
    assert first.device_id
    second = settings.Credentials.load()
    assert second.device_id == first.device_id


def test_credentials_round_trip():
    creds = settings.Credentials.load()
    creds.server_address = "http://jelly:8096"
    creds.server_name = "minipie"
    creds.server_id = "srv1"
    creds.user_id = "u1"
    creds.display_user = "conor"
    creds.token = "tok1"
    creds.is_logged_in = True
    creds.save()

    loaded = settings.Credentials.load()
    assert loaded.server_address == "http://jelly:8096"
    assert loaded.server_name == "minipie"
    assert loaded.token == "tok1"
    assert loaded.is_logged_in is True


def test_clear_logs_out_but_keeps_server_address_and_device():
    creds = settings.Credentials.load()
    creds.server_address = "http://jelly:8096"
    creds.token = "tok1"
    creds.user_id = "u1"
    creds.is_logged_in = True
    creds.save()

    settings.Credentials.clear()
    loaded = settings.Credentials.load()
    assert loaded.server_address == "http://jelly:8096"
    assert loaded.device_id == creds.device_id
    assert loaded.token == ""
    assert loaded.user_id == ""
    assert loaded.is_logged_in is False


def test_clear_drops_who_is_watching_but_keeps_the_shortlist():
    """Additional-user membership is tied to this primary user; the shortlist
    is a device preference about who the picker offers and survives logout."""
    FakeAddon.store["whoIsWatching"] = "u2,u4"
    FakeAddon.store["whoIsWatchingShortlist"] = "u2,u3,u4"
    creds = settings.Credentials.load()
    creds.is_logged_in = True
    creds.save()

    settings.Credentials.clear()

    assert FakeAddon.store.get("whoIsWatching", "") == ""
    assert FakeAddon.store["whoIsWatchingShortlist"] == "u2,u3,u4"


def test_resume_offset_is_a_magnitude():
    # The slider stores a negative number ("-10s") because that reads right in
    # the settings UI; every caller wants seconds to rewind by.
    assert settings.resume_offset() == 0.0  # unset
    FakeAddon.store["resumeJumpBack"] = "-10"
    assert settings.resume_offset() == 10.0
    FakeAddon.store["resumeJumpBack"] = "0"
    assert settings.resume_offset() == 0.0


def test_adjusted_resume_rewinds_by_the_offset():
    FakeAddon.store["resumeJumpBack"] = "-10"
    assert settings.adjusted_resume(600.0) == 590.0
    assert settings.adjusted_resume(0.0) == 0.0


def test_adjusted_resume_leaves_a_position_shorter_than_the_offset():
    # Fork semantics: clamping to zero would flip a barely-started item back to
    # unwatched, losing the in-progress bookmark the viewer expects to see.
    FakeAddon.store["resumeJumpBack"] = "-60"
    assert settings.adjusted_resume(45.0) == 45.0
    assert settings.adjusted_resume(60.0) == 60.0
    assert settings.adjusted_resume(60.5) == 0.5
