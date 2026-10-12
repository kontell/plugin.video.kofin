"""Per-kind desired state, scanner rows and payload compaction."""

import copy

import pytest

from kofin.sync.backends.api import metadata
from tests.unit.apikodi import SERVER
from tests.unit.apifixtures import (  # noqa: F401
    LIB,
    LIB2,
    SHOW,
    SHOW2,
    backend,
    boxset,
    episode,
    kodi,
    methods,
    movie,
    musicvideo,
    season,
    series,
    show_bundle,
    store,
)


@pytest.mark.parametrize(
    "values, expected",
    [
        ({"ParentIndexNumber": 1, "IndexNumber": 3}, (1, 3)),
        ({"ParentIndexNumber": 0, "IndexNumber": 2}, (0, 2)),
        ({"ParentIndexNumber": 2, "IndexNumber": 0}, (2, 0)),
        ({"ParentIndexNumber": 2, "IndexNumber": None}, (2, 0)),
        ({"ParentIndexNumber": 0, "IndexNumber": 0}, None),
        ({"ParentIndexNumber": None, "IndexNumber": None}, None),
        (
            {"ParentIndexNumber": None, "IndexNumber": 4, "AbsoluteEpisodeNumber": 9},
            (1, 9),
        ),
    ],
)
def test_episode_numbering_follows_the_scanner_rules(values, expected):
    numbers = metadata.episode_numbers(episode("e", **values))
    if expected is None:
        assert numbers is None
    else:
        assert (numbers["season"], numbers["episode"]) == expected


def test_specials_sort_before_or_after_their_season():
    before = metadata.episode_numbers(
        episode(
            "e",
            season_number=0,
            number=1,
            AirsBeforeSeasonNumber=2,
            AirsBeforeEpisodeNumber=5,
        )
    )
    assert (before["sortseason"], before["sortepisode"]) == (2, 5)
    after = metadata.episode_numbers(
        episode("e", season_number=0, number=1, AirsAfterSeasonNumber=1)
    )
    assert (after["sortseason"], after["sortepisode"]) == (1, 4096)


def test_default_season_names_are_left_to_kodi():
    assert metadata.season_title(season("s", number=3)) == ""
    assert metadata.season_title(season("s", number=0, Name="Specials")) == ""
    assert metadata.season_title(season("s", number=1, Name="Book One")) == "Book One"


def test_show_hash_moves_for_membership_and_numbering_only():
    episodes = [episode("e1"), episode("e2", number=2)]
    base = metadata.show_hash(episodes)
    assert metadata.show_hash([dict(e, Overview="edited") for e in episodes]) == base
    assert metadata.show_hash(episodes[:1]) != base
    assert metadata.show_hash([episodes[0], dict(episodes[1], IndexNumber=3)]) != base
    assert metadata.hash_date(base).endswith("T00:00:00Z")


def test_compact_drops_blur_hashes_and_keeps_one_stream_list():
    item = movie(
        ImageBlurHashes={"Primary": {"x": "y"}},
        People=[{"Name": "A", "Type": "Actor", "ImageBlurHashes": {"Primary": {}}}],
        MediaSources=[
            {"Id": "s", "MediaStreams": [{"Type": "Video", "Codec": "hevc"}]}
        ],
    )
    del item["MediaStreams"]
    compact = metadata.compact(item)
    assert "ImageBlurHashes" not in compact
    assert "ImageBlurHashes" not in compact["People"][0]
    assert compact["MediaStreams"] == [{"Type": "Video", "Codec": "hevc"}]
    assert "MediaStreams" not in compact["MediaSources"][0]
    assert metadata.refresh_token(compact) == metadata.refresh_token(
        metadata.compact(copy.deepcopy(item))
    )


def test_native_metadata_normalizes_tags_studios_and_release_year():
    item = movie(
        Name=" Fixture title ",
        SortName=" Fixture sort \t",
        Tags=[" Tag ", "Tag", " "],
        Studios=[{"Name": " Studio one / Studio two "}],
        ProductionYear=1999,
        PremiereDate="2000-01-02T00:00:00Z",
    )
    result = metadata.details(item, "", "key", LIB)
    assert result["title"] == "Fixture title"
    assert result["sorttitle"] == "Fixture sort"
    assert result["tag"] == ["Tag", "kofin.library." + LIB]
    assert result["studio"] == ["Studio one", "Studio two"]
    assert result["year"] == 2000
    result = metadata.details(item, "", "key", LIB, separator=" | ")
    assert result["studio"] == ["Studio one / Studio two"]


def test_show_and_episode_details_speak_their_setters():
    show = metadata.details(
        series(), SERVER, "key", LIB, seasons=[season("s", number=1, Overview="arc")]
    )
    assert show["status"] == "Ended" and show["premiered"] == "2001-02-03"
    assert "Favorite tvshows" in show["tag"]
    assert "set" not in show and "playcount" not in show
    # tvshowcounts derives a show's date added from its episode files.
    assert "dateadded" not in show
    changed = metadata.details(
        series(),
        SERVER,
        "key",
        LIB,
        seasons=[season("s", number=1, Overview="other arc")],
    )
    assert changed["uniqueid"]["kofinrefresh"] != show["uniqueid"]["kofinrefresh"]
    ep = metadata.details(episode("e1"), SERVER, "key", LIB)
    assert (ep["season"], ep["episode"], ep["firstaired"]) == (1, 1, "2001-02-10")
    assert "tag" not in ep and ep["resume"]["position"] == 0
    clip = metadata.details(musicvideo(), SERVER, "key", LIB)
    assert clip["artist"] == ["Band"] and clip["album"] == "Album"
    assert metadata.details(boxset("b", " Set ", ["a"]), SERVER, "key", LIB) == {
        "title": "Set",
        "plot": "",
        "art": {},
    }


def test_collections_pick_one_set_per_movie_stably():
    sets = [boxset("b2", "Zeta", ["a", "b"]), boxset("b1", "alpha", ["a"])]
    assert metadata.collections_of(sets) == {"a": "alpha", "b": "Zeta"}


def _recorded_streams(monkeypatch, item):
    from unittest.mock import Mock

    tag = Mock()
    li = Mock()
    li.getVideoInfoTag.return_value = tag
    monkeypatch.setattr(metadata.xbmcgui, "ListItem", lambda *a, **k: li)
    for name in ("VideoStreamDetail", "AudioStreamDetail", "SubtitleStreamDetail"):
        monkeypatch.setattr(metadata.xbmc, name, lambda **kw: kw)
    metadata.listitem(item, SERVER, "key", LIB)
    return (
        [c.args[0] for c in tag.addVideoStream.call_args_list],
        [c.args[0] for c in tag.addAudioStream.call_args_list],
        [c.args[0] for c in tag.addSubtitleStream.call_args_list],
    )


STREAMS = [
    {"Type": "Video", "Codec": "hevc", "Width": 3840, "Height": 2160},
    {"Type": "Audio", "Codec": "eac3", "Channels": 6, "Language": "eng"},
    {"Type": "Audio", "Codec": "aac", "Channels": 2, "Language": "fra"},
    {"Type": "Subtitle", "Codec": "subrip", "Language": "eng"},
]


def test_a_single_source_row_is_filed_with_its_streams(monkeypatch):
    """compact keeps a single source's streams on the item alone and the
    shared reader looked in the first source, so every such movie and
    episode was filed with a stub video stream, no codec, and no audio or
    subtitle stream (1,787 movies and 4,316 episodes on one profile)."""
    for build in (movie, lambda **values: episode("e1", **values)):
        item = metadata.compact(
            build(
                MediaSources=[
                    {"Id": "src", "Container": "mkv", "MediaStreams": STREAMS}
                ],
                MediaStreams=STREAMS,
            )
        )
        assert "MediaStreams" not in item["MediaSources"][0]
        video, audio, subtitle = _recorded_streams(monkeypatch, item)
        assert [(v["codec"], v["width"], v["height"]) for v in video] == [
            ("hevc", 3840, 2160)
        ]
        assert [(a["codec"], a["channels"], a["language"]) for a in audio] == [
            ("eac3", 6, "eng"),
            ("aac", 2, "fra"),
        ]
        assert [s["language"] for s in subtitle] == ["eng"]


def test_a_version_row_is_filed_with_its_own_source_streams(monkeypatch):
    """A movie with several sources keeps each source's streams, and the
    row of one version must not take the item's."""
    other = [
        {"Type": "Video", "Codec": "h264", "Width": 1920, "Height": 1080},
        {"Type": "Audio", "Codec": "ac3", "Channels": 6, "Language": "eng"},
    ]
    item = metadata.compact(
        movie(
            MediaSources=[
                {"Id": "a", "Container": "mkv", "MediaStreams": STREAMS},
                {"Id": "b", "Container": "mkv", "MediaStreams": other},
            ],
            MediaStreams=STREAMS,
        )
    )
    video, audio, _ = _recorded_streams(monkeypatch, item)
    assert [v["codec"] for v in video] == ["hevc"]
    shown = dict(item, MediaSources=[item["MediaSources"][1]], MediaStreams=other)
    video, audio, subtitle = _recorded_streams(monkeypatch, shown)
    assert [v["codec"] for v in video] == ["h264"]
    assert [a["codec"] for a in audio] == ["ac3"]
    assert subtitle == []


def test_scanner_listitem_never_stamps_a_zero_resume_point(monkeypatch):
    from unittest.mock import Mock

    tag = Mock()
    li = Mock()
    li.getVideoInfoTag.return_value = tag
    monkeypatch.setattr(metadata.xbmcgui, "ListItem", lambda *a, **k: li)
    metadata.listitem(movie(UserData={}), SERVER, "key", LIB)
    assert not tag.setResumePoint.called
    metadata.listitem(movie(), SERVER, "key", LIB, set_name="Trilogy")
    tag.setResumePoint.assert_called_once_with(12.0, 120.0)
    tag.setSet.assert_called_once_with("Trilogy")
    tag.reset_mock()
    metadata.listitem(
        series(),
        SERVER,
        "key",
        LIB,
        seasons=[season("s", number=2, Name="Book Two", Overview="arc")],
        episodes=[episode("e1")],
    )
    tag.addSeason.assert_called_once_with(2, "Book Two", "arc")
    assert not tag.addAvailableArtwork.called
    # A season's own art is offered to the scanner under its number.
    tag.reset_mock()
    pictured = season(
        "s", number=2, ImageTags={"Primary": "p1", "Banner": "b1"}, SeriesId="show"
    )
    pictured["SeriesPrimaryImageTag"] = "sp"
    metadata.listitem(
        series(), SERVER, "key", LIB, seasons=[pictured], episodes=[episode("e1")]
    )
    offered = {
        (call.args[1], call.kwargs["season"]): call.args[0]
        for call in tag.addAvailableArtwork.call_args_list
    }
    assert set(offered) == {("banner", 2), ("poster", 2), ("thumb", 2)}
    assert offered == {
        (kind, 2): url
        for kind, url in metadata.details(pictured, SERVER, "key", LIB)["art"].items()
    }
    li.setProperty.assert_called_with("hash", metadata.show_hash([episode("e1")]))
    assert not tag.setResumePoint.called


def test_timestamps_are_written_in_kodi_local_time():
    from kofin.sync.shims import convert_to_local

    item = movie(
        DateCreated="2026-10-08T18:33:36.5788691Z",
        UserData={
            "Played": True,
            "PlayCount": 1,
            "LastPlayedDate": "2026-10-08T18:36:22.5959355Z",
        },
        PremiereDate="2001-02-03T00:00:00.0000000Z",
    )
    data = metadata.details(item, SERVER, "key", LIB)
    assert data["dateadded"] == convert_to_local("2026-10-08T18:33:36Z")[:19].replace(
        "T", " "
    )
    assert data["lastplayed"] == convert_to_local("2026-10-08T18:36:22Z")[:19].replace(
        "T", " "
    )
    assert len(data["dateadded"]) == 19 and " " in data["dateadded"]
    # Calendar dates are not shifted by the zone.
    assert data["premiered"] == "2001-02-03"
    assert metadata.userdata(movie(UserData={"Played": False}))["lastplayed"] == ""


def test_no_tag_is_taken_from_a_temporary_listitem():
    """An InfoTag is a pointer into its ListItem (InfoTagVideo(tag, offscreen),
    owned=false; CFileItem's destructor deletes the tag). Taken from a temporary
    the item is freed at once and the first setter writes freed memory; it
    segfaulted a 32-bit ARM Kodi in setGenres."""
    import re
    from pathlib import Path

    pattern = re.compile(r"ListItem\([^)]*\)\.get\w*InfoTag\(\)")
    root = Path(__file__).resolve().parents[2] / "lib"
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if pattern.search(path.read_text())
    ]
    assert offenders == []


def test_show_hash_is_the_same_from_payloads_and_their_rows():
    """The root listing reduces each episode payload to its row as it reads
    it, so a library's episodes are never held together; the hash it hands
    the scanner must not move for that."""
    from kofin.sync.backends.api import metadata

    episodes = [
        {"Id": "e1", "ParentIndexNumber": 1, "IndexNumber": 2, "Overview": "x"},
        {"Id": "e2", "ParentIndexNumber": 0, "IndexNumber": None},  # unfiled
        {"Id": "e3", "AbsoluteEpisodeNumber": 7},
    ]
    rows = [metadata.episode_row(e) for e in episodes]
    assert rows[1] is None
    assert metadata.show_hash(episodes) == metadata.show_hash(r for r in rows if r)
    assert metadata.show_hash(episodes) != metadata.show_hash(episodes[:1])


def test_details_have_no_opinion_on_a_date_the_server_lacks():
    """Kodi fills a missing premiere or aired date itself and its setters
    ignore an empty one, so an empty desired date never matches the row:
    187 episodes and a show stayed pending on the LibreELEC box, re-patched
    by every pass. The key is absent instead, as for dateadded."""
    from kofin.sync.backends.api import metadata
    from tests.unit.apifixtures import LIB, SERVER, episode, movie, series

    show = series(PremiereDate=None, ProductionYear=1993)
    data = metadata.details(show, SERVER, "ns", LIB)
    assert "premiered" not in data
    data = metadata.details(episode("e1", PremiereDate=None), SERVER, "ns", LIB)
    assert "firstaired" not in data
    dated = metadata.details(
        movie(PremiereDate="2009-04-22T00:00:00Z"), SERVER, "ns", LIB
    )
    assert dated["premiered"] == "2009-04-22" and dated["year"] == 2009


def test_episode_details_leave_inherited_art_to_the_show():
    """tvshow.* and season.* art is read off the show and season rows; an
    episode setter cannot make it true, so it never belongs in the desired
    state (two episodes looped on the LibreELEC box after the art cap moved
    every URL but the show's, acknowledged earlier)."""
    from kofin.sync.backends.api import metadata
    from tests.unit.apifixtures import LIB, SERVER, episode

    item = episode("e1", SeriesPrimaryImageTag="t1", ImageTags={"Primary": "p1"})
    data = metadata.details(item, SERVER, "ns", LIB)
    assert data["art"] and all("." not in key for key in data["art"])
    assert "thumb" in data["art"]


def test_merge_never_clears_inherited_art_an_old_acknowledgement_owned():
    """A mapping acknowledged before tvshow.* left the desired state lists
    it as owned; clearing it is impossible for an episode row, so the
    compare must leave it alone."""
    from kofin.sync.backends.api.patch import merge

    desired = {"art": {"thumb": "http://s/ep.jpg"}}
    row = {
        "art": {
            "thumb": "image://http%3a%2f%2fs%2fep.jpg/",
            "tvshow.poster": "image://x/",
        }
    }
    compare = merge("Episode", desired, row, {"art": ["thumb", "tvshow.poster"]})
    assert "tvshow.poster" not in compare["art"]
    assert "tvshow.poster" not in desired["art"]


def test_song_tags_are_total_for_numbers_that_do_not_parse():
    """song_tags is the listing and the hash: a value that does not parse is
    the missing-field zero, so one song cannot stall its library's scans."""
    from tests.unit.apifixtures import album, song

    bad = song(
        "t1",
        IndexNumber="x",
        ParentIndexNumber=None,
        RunTimeTicks="n/a",
        ProductionYear="abc",
        MediaSources=[{"Size": "big", "Container": "flac"}],
    )
    tags = metadata.song_tags(bad, album())
    assert (tags["track"], tags["disc"], tags["duration"], tags["year"]) == (0, 0, 0, 0)
    assert tags["size"] == 0 and tags["releasedate"] == ""
    absent = song("t1", MediaSources=[{"Container": "flac"}])
    for key in ("IndexNumber", "ParentIndexNumber", "RunTimeTicks", "ProductionYear"):
        absent.pop(key)
    assert metadata.tag_hash(bad, album()) == metadata.tag_hash(absent, album())


def test_a_played_song_without_a_server_date_is_last_played_when_it_was_added():
    """Kodi's UpdateSong stamps the current time on a played song with no
    date, which put songs marked played years ago at the top of a tablet's
    recently played albums on import day."""
    from tests.unit.apifixtures import song

    played = song("t1", UserData={"Played": True, "PlayCount": 2})
    assert metadata.userdata(played)["lastplayed"] == metadata._timestamp(
        "2023-11-15T19:18:34.08Z"
    )
    dated = song(
        "t1",
        UserData={
            "Played": True,
            "PlayCount": 2,
            "LastPlayedDate": "2026-01-02T03:04:05Z",
        },
    )
    assert metadata.userdata(dated)["lastplayed"] == metadata._timestamp(
        "2026-01-02T03:04:05Z"
    )
    assert metadata.userdata(song("t1"))["lastplayed"] == ""
    # The video setters leave a missing date alone: unchanged.
    assert "lastplayed" not in metadata.userdata(movie(UserData={"Played": True}))


def test_inputs_token_moves_with_the_art_query_and_the_zone(monkeypatch):
    """Everything details reads besides the payload is in the token, or the
    short-circuit freezes the state the next change should have moved."""
    from kofin.plugin import listitems

    base = metadata.inputs_token("http://s", "k", LIB, " / ")
    monkeypatch.setattr(listitems, "art_query", lambda: "&MaxHeight=720&Quality=90")
    capped = metadata.inputs_token("http://s", "k", LIB, " / ")
    assert capped != base
    monkeypatch.setattr(metadata.time, "timezone", 12345)
    assert metadata.inputs_token("http://s", "k", LIB, " / ") != capped


def test_a_refresh_can_say_which_part_of_its_token_moved():
    """The token says only that something no setter carries has changed. A
    viewer watching a show being scanned again wants to know what."""
    from kofin.sync.backends.api.patch import moved_parts

    cast = [{"Name": "A", "Type": "Actor"}]
    first = metadata.refresh_parts(movie(People=cast, MediaStreams=STREAMS))
    assert set(first) == {"cast", "streams"}
    recast = metadata.refresh_parts(
        movie(People=cast + [{"Name": "B", "Type": "Actor"}], MediaStreams=STREAMS)
    )
    assert moved_parts(first, recast) == "its cast moved"
    remuxed = metadata.refresh_parts(movie(People=cast, MediaStreams=STREAMS[:2]))
    assert moved_parts(first, remuxed) == "its streams moved"
    assert moved_parts(first, first) == "nothing on record moved"
    # A row acknowledged by an earlier build has no parts on record.
    assert moved_parts(None, first) == "what moved is not on record"
    assert moved_parts({}, first) == "what moved is not on record"
    # A show's parts are its cast and its seasons' plots; an episode's
    # numbering is its own.
    show = metadata.refresh_parts(
        series(People=cast), [season("s", number=1, Overview="arc")]
    )
    assert set(show) == {"cast", "seasons"}
    replotted = metadata.refresh_parts(
        series(People=cast), [season("s", number=1, Overview="another arc")]
    )
    assert moved_parts(show, replotted) == "its seasons moved"
    numbered = metadata.refresh_parts(episode("e1", People=cast, IndexNumberEnd=2))
    assert "numbering" in numbered
    # The parts never change the token a row was filed under.
    item = movie(People=cast, MediaStreams=STREAMS)
    assert metadata.refresh_token(item) == metadata.refresh_token(dict(item))


def test_the_parts_are_the_tokens_own_fields():
    """The log's reason is built from the dict the token hashes: a field in
    the token and not in the parts would refresh a row and say nothing
    moved."""
    samples = [
        movie(),
        episode("e1"),
        series(),
        dict(movie(), Type="MusicVideo"),
    ]
    for item in samples:
        fields = metadata.refresh_fields(item, [season("s", number=1)])
        assert metadata.refresh_token(item, [season("s", number=1)]) == (
            metadata._token(fields)
        )
        # Every field has a word a viewer would use for it.
        assert set(fields) <= set(metadata.REFRESH_PARTS)
        parts = metadata.refresh_parts(item, [season("s", number=1)])
        assert set(parts) == {metadata.REFRESH_PARTS[key] for key in fields}
    # A field that moves from nothing to something moves its part.
    bare = metadata.refresh_parts(episode("e1"))
    ended = metadata.refresh_parts(episode("e1", IndexNumberEnd=2))
    assert bare["numbering"] != ended["numbering"] and bare["cast"] == ended["cast"]
