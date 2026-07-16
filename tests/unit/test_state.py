import pytest

from kofin.core import state
from tests.unit.fakes import FakeWindow


@pytest.fixture(autouse=True)
def fake_window(monkeypatch):
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    return FakeWindow


def test_online_round_trip():
    assert state.is_online() is False
    state.set_online(True)
    assert state.is_online() is True
    state.set_online(False)
    assert state.is_online() is False


def test_claim_by_path_match():
    state.push_play_item({"Path": "http://a", "Id": "1"})
    state.push_play_item({"Path": "http://b", "Id": "2"})
    claimed = state.claim_play_item("http://b")
    assert claimed is not None and claimed["Id"] == "2"
    remaining = state.claim_play_item("http://a")
    assert remaining is not None and remaining["Id"] == "1"
    assert state.claim_play_item("http://a") is None


def test_claim_falls_back_to_oldest():
    state.push_play_item({"Path": "http://a", "Id": "1"})
    state.push_play_item({"Path": "http://b", "Id": "2"})
    claimed = state.claim_play_item("http://other")
    assert claimed is not None and claimed["Id"] == "1"


def test_claim_on_empty_and_garbage():
    assert state.claim_play_item("x") is None
    FakeWindow.store[state.PROP_PLAY_QUEUE] = "not-json"
    assert state.claim_play_item("x") is None


def test_clear_all():
    state.set_online(True)
    state.set_playing_id("42")
    state.push_play_item({"Path": "p"})
    state.clear_all()
    assert state.is_online() is False
    assert state.get_playing_id() == ""
    assert state.claim_play_item("p") is None
