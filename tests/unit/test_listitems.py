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


def test_playcount_follows_played_not_the_count():
    """An item marked unwatched is unwatched, whatever its history.

    Jellyfin keeps PlayCount when Played goes false, so the count on its own
    says "has been watched before", not "is watched" -- on a real library that
    is a few percent of the items, which is what made the watched flags in
    dynamic listings look randomly wrong.
    """
    unmarked = {"UserData": {"PlayCount": 4, "Played": False}}
    assert listitems.playcount_of(unmarked) == 0
    # ...and one still in progress, which is the same shape plus a position.
    rewatching = {
        "UserData": {
            "PlayCount": 2,
            "Played": False,
            "PlaybackPositionTicks": 300 * 10_000_000,
        }
    }
    assert listitems.playcount_of(rewatching) == 0


def test_resume_of_carries_the_resume_offset(fake_addon):
    # The listing has to advertise the position playback will actually start
    # at, or Kodi's resume prompt names one time and lands on another.
    fake_addon.store["resumeJumpBack"] = "-10"
    item = {
        "RunTimeTicks": 600 * 10_000_000,
        "UserData": {"PlaybackPositionTicks": 300 * 10_000_000},
    }
    assert listitems.resume_of(item) == (290.0, 600.0)


def test_resume_of_with_a_precomputed_offset_never_reads_settings(monkeypatch):
    """Listings pass resume_offset once for the whole page: every read builds
    a fresh Addon (settings._addon), and at one per row that construction was
    most of a large listing's build time (perf plan W1.1)."""

    def forbidden() -> float:
        raise AssertionError("resume_offset must not be read when one is passed")

    monkeypatch.setattr(listitems.settings, "resume_offset", forbidden)
    item = {
        "RunTimeTicks": 600 * 10_000_000,
        "UserData": {"PlaybackPositionTicks": 300 * 10_000_000},
    }
    assert listitems.resume_of(item, offset=10.0) == (290.0, 600.0)


def test_build_threads_the_precomputed_offset_through(monkeypatch):
    """build(resume_offset=...) must reach resume_of without a settings read,
    or the hoist in browse._add_items silently stops paying."""

    def forbidden() -> float:
        raise AssertionError("build with resume_offset read the setting anyway")

    monkeypatch.setattr(listitems.settings, "resume_offset", forbidden)
    item = {
        "Id": "m1",
        "Type": "Movie",
        "Name": "M",
        "RunTimeTicks": 600 * 10_000_000,
        "UserData": {"PlaybackPositionTicks": 300 * 10_000_000},
    }
    listitems.build(item, "http://s:8096", resume_offset=0.0)


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
    """Records resume point and media type; ignores every other setter."""

    def __init__(self):
        self.resume_calls = []
        self.media_type = None

    def setResumePoint(self, time, totaltime=0.0):
        self.resume_calls.append((time, totaltime))

    def setMediaType(self, media_type):
        self.media_type = media_type

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

VIEW = {
    "Type": "CollectionFolder",
    "Name": "Movies",
    "Id": "v1",
    "CollectionType": "movies",
    "Overview": "All the movies",
    "ImageTags": {"Primary": "p1"},
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


def test_build_stamps_a_zero_point_on_a_listing_row_with_a_runtime(recorded):
    """A zero point *with a total* reads to Kodi as set-and-nothing-to-resume:
    not resumable, and -- the point of stamping it -- no fallback to the
    bookmark Kodi saved for this plugin path the last time the item was
    stopped (VideoUtils.cpp GetNonFolderItemResumeInformation)."""
    fresh = dict(EPISODE, UserData={})
    listitems.build(fresh, SERVER)
    assert recorded[-1].tag.resume_calls == [(0.0, 600.0)]


def test_build_stamps_nothing_without_a_runtime(recorded):
    """No total means no point to stamp: Kodi reads "set" off the total."""
    listitems.build({"Type": "Episode", "Name": "No runtime", "UserData": {}}, SERVER)
    assert recorded[-1].tag.resume_calls == []


def test_cast_carries_the_persons_portrait(recorded, monkeypatch):
    """A URL, fetched by Kodi only when the info dialog draws the cast; people
    without a portrait get none rather than a path to nowhere, and crew keep
    out of the cast list."""
    actors = []
    monkeypatch.setattr(
        listitems.xbmc,
        "Actor",
        lambda name, role, order, thumbnail: actors.append(
            (name, role, order, thumbnail)
        ),
    )
    item = {
        "Type": "Movie",
        "Name": "...And Justice for All",
        "People": [
            {
                "Name": "Al Pacino",
                "Role": "Arthur Kirkland",
                "Type": "Actor",
                "Id": "p1",
                "PrimaryImageTag": "t1",
            },
            {"Name": "No Portrait", "Role": "Judge", "Type": "Actor", "Id": "p2"},
            {"Name": "Norman Jewison", "Type": "Director", "Id": "p3"},
        ],
    }
    listitems.build(item, SERVER)
    assert actors == [
        ("Al Pacino", "Arthur Kirkland", 0, SERVER + "/Items/p1/Images/Primary?tag=t1"),
        ("No Portrait", "Judge", 1, ""),
    ]


def test_build_does_not_stamp_mediatype_on_library_views(recorded):
    """CollectionFolder/UserView are containers, not video media rows."""
    listitems.build(VIEW, SERVER)
    assert recorded[-1].tag.media_type is None


def test_build_stamps_mediatype_on_episodes(recorded):
    listitems.build(EPISODE, SERVER)
    assert recorded[-1].tag.media_type == "episode"


@pytest.mark.parametrize(
    "stream, expected",
    [
        # Every VideoRangeType a real library reports, sampled from the test
        # server: the DOVI* variants all carry DvProfile, which is why the
        # profile and not the range type is what decides Dolby Vision.
        ({"VideoRangeType": "DOVIWithHDR10", "DvProfile": 8}, "dolbyvision"),
        ({"VideoRangeType": "DOVIWithHDR10Plus", "DvProfile": 10}, "dolbyvision"),
        ({"VideoRangeType": "DOVIInvalid", "DvProfile": 5}, "dolbyvision"),
        ({"VideoRangeType": "DOVI", "DvProfile": 5}, "dolbyvision"),
        ({"VideoRangeType": "HDR10"}, "hdr10"),
        ({"VideoRangeType": "HDR10Plus"}, "hdr10"),
        ({"VideoRangeType": "HLG"}, "hlg"),
        ({"VideoRangeType": "DOVIWithHLG"}, "hlg"),  # no profile: HLG is left
        ({"VideoRangeType": "SDR"}, ""),
        ({}, ""),
    ],
)
def test_hdr_type_mapping(stream, expected):
    assert listitems.hdr_type(stream) == expected


def test_build_passes_hdr_type_to_the_video_stream(recorded, monkeypatch):
    """Kodistubs' VideoStreamDetail stores nothing and its getters return
    constants, so the call is what there is to assert on."""
    calls = []
    monkeypatch.setattr(
        listitems.xbmc,
        "VideoStreamDetail",
        lambda **kwargs: calls.append(kwargs),
    )
    item = dict(
        EPISODE,
        MediaStreams=[
            {
                "Type": "Video",
                "Width": 3840,
                "Height": 2160,
                "Codec": "hevc",
                "VideoRangeType": "DOVIWithHDR10",
                "DvProfile": 8,
            }
        ],
    )
    listitems.build(item, SERVER)
    assert calls == [
        {
            "width": 3840,
            "height": 2160,
            "codec": "hevc",
            "duration": 600,
            "hdrtype": "dolbyvision",
        }
    ]
