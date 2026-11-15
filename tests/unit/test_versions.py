import pytest

from kofin.plugin import versions
from tests.unit.fakes import FakeDialog

EPISODE = {
    "Id": "e1",
    "Type": "Episode",
    "Name": "An Episode",
    "MediaSources": [{"Id": "a", "Name": "Aired"}, {"Id": "u", "Name": "Uncensored"}],
}


@pytest.fixture
def dialog(monkeypatch):
    dialog = FakeDialog()
    monkeypatch.setattr("xbmcgui.Dialog", lambda: dialog)
    monkeypatch.setattr("xbmc.getLocalizedString", lambda string_id: "#%d" % string_id)
    return dialog


def test_the_viewer_picks_and_cancels(dialog):
    dialog.select_answers = [1, -1]
    assert versions.choose(EPISODE, False, None) == "u"
    assert versions.choose(EPISODE, False, None) is None
    assert dialog.selects[0] == ("#40000", ["Aired", "Uncensored"], 0)


def test_an_unattended_play_takes_the_default(dialog):
    assert versions.choose(EPISODE, True, None) == ""
    assert versions.choose(EPISODE, True, "Uncensored") == ""
    assert dialog.selects == []


def test_play_next_follows_by_name_or_takes_the_default(dialog):
    assert versions.choose(EPISODE, False, " uncensored ") == "u"
    assert versions.choose(EPISODE, False, "Greyscale") == ""
    assert versions.choose(EPISODE, False, "") == ""
    assert dialog.selects == []


def test_an_unnamed_version_is_labelled_by_the_item(dialog):
    item = dict(EPISODE, MediaSources=[{"Id": "a"}, {"Id": "u", "Name": "Uncensored"}])
    dialog.select_answers = [0]
    assert versions.choose(item, False, None) == "a"
    assert dialog.selects[0][1] == ["An Episode", "Uncensored"]
