import pytest

from kofin.plugin import listitems
from tests.unit.fakes import FakeAddon

SERVER = "http://s:8096"


@pytest.fixture(autouse=True)
def fake_addon(monkeypatch):
    FakeAddon.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    return FakeAddon


def test_path_for_playable_and_folder():
    assert (
        listitems.path_for({"Type": "Movie", "Id": "m1"})
        == "plugin://plugin.video.kofin/?mode=play&id=m1"
    )
    series_path = listitems.path_for({"Type": "Series", "Id": "s1"})
    assert "mode=browse" in series_path and "folder=s1" in series_path
    assert "type=series" in series_path


def test_is_folder():
    assert listitems.is_folder({"Type": "Series"}) is True
    assert listitems.is_folder({"Type": "Movie"}) is False
    assert listitems.is_folder({"Type": "Video", "IsFolder": True}) is False
    assert listitems.is_folder({"Type": "Unknown", "IsFolder": True}) is True


def test_resume_and_playcount():
    item = {
        "RunTimeTicks": 60 * 10_000_000,
        "UserData": {
            "PlaybackPositionTicks": 30 * 10_000_000,
            "PlayCount": 0,
            "Played": True,
        },
    }
    position, total = listitems.resume_of(item)
    assert (position, total) == (30.0, 60.0)
    assert listitems.playcount_of(item) == 1
    assert listitems.playcount_of({"UserData": {"PlayCount": 3, "Played": True}}) == 3
    assert listitems.playcount_of({}) == 0


def test_resume_of_carries_the_resume_offset(fake_addon):
    # The listing has to advertise the position playback will actually start
    # at, or Kodi's resume prompt names one time and lands on another.
    fake_addon.store["resumeJumpBack"] = "-10"
    item = {
        "RunTimeTicks": 600 * 10_000_000,
        "UserData": {"PlaybackPositionTicks": 300 * 10_000_000},
    }
    assert listitems.resume_of(item) == (290.0, 600.0)


def test_art_primary_and_backdrop():
    art = listitems.art_for(
        {
            "Id": "m1",
            "ImageTags": {"Primary": "p1", "Logo": "l1"},
            "BackdropImageTags": ["b1"],
        },
        SERVER,
    )
    assert art["poster"] == "http://s:8096/Items/m1/Images/Primary?tag=p1"
    assert art["clearlogo"].endswith("Logo?tag=l1")
    assert art["fanart"] == "http://s:8096/Items/m1/Images/Backdrop/0?tag=b1"


def test_art_parent_fallbacks_for_episode():
    art = listitems.art_for(
        {
            "Id": "e1",
            "Type": "Episode",
            "ImageTags": {"Primary": "ep"},
            "SeriesId": "s1",
            "SeriesPrimaryImageTag": "sp",
            "ParentBackdropItemId": "s1",
            "ParentBackdropImageTags": ["sb"],
        },
        SERVER,
    )
    assert art["thumb"].endswith("e1/Images/Primary?tag=ep")
    assert art["poster"].endswith("s1/Images/Primary?tag=sp")
    assert art["tvshow.poster"].endswith("s1/Images/Primary?tag=sp")
    assert art["fanart"].endswith("s1/Images/Backdrop/0?tag=sb")


class RecordingTag:
    """Records the resume point, ignoring every other setter."""

    def __init__(self):
        self.resume_calls = []

    def setResumePoint(self, time, totaltime=0.0):
        self.resume_calls.append((time, totaltime))

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class RecordingListItem:
    def __init__(self, label="", offscreen=False):
        self.tag = RecordingTag()

    def getVideoInfoTag(self):
        return self.tag

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


EPISODE = {
    "Type": "Episode",
    "Name": "An Episode",
    "RunTimeTicks": 600 * 10_000_000,
    "UserData": {"PlaybackPositionTicks": 300 * 10_000_000},
}


@pytest.fixture
def recorded(monkeypatch):
    items = []

    def factory(label="", offscreen=False):
        item = RecordingListItem(label, offscreen)
        items.append(item)
        return item

    monkeypatch.setattr("xbmcgui.ListItem", factory)
    return items


def test_build_stamps_the_items_own_resume_point_by_default(recorded):
    listitems.build(EPISODE, SERVER)
    assert recorded[-1].tag.resume_calls == [(300.0, 600.0)]


def test_build_resume_override_states_the_start_position(recorded):
    listitems.build(EPISODE, SERVER, resume_seconds=120.0)
    assert recorded[-1].tag.resume_calls == [(120.0, 600.0)]


def test_build_with_a_zero_override_stamps_nothing(recorded):
    # A stamped resume point cannot be cleared afterwards and Kodi resumes on
    # its presence, so "start at 0" has to mean the setter is never called.
    listitems.build(EPISODE, SERVER, resume_seconds=0.0)
    assert recorded[-1].tag.resume_calls == []
