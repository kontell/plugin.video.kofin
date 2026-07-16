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
