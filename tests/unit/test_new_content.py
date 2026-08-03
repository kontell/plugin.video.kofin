"""L1 units for the new-content message policy: what gets announced, what
stays quiet, and what each of those adds up to as a message.

The templates come from the shipped strings.po rather than a stub, so the
assertions read as the user reads them and a template whose "%s" count does
not match its call site fails here instead of in the library thread.
"""

import os
import re

import pytest

from kofin.sync import newcontent

STRINGS_PO = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "resources",
    "language",
    "resource.language.en_gb",
    "strings.po",
)

# Every id the module can format, so a missing string is a test failure and
# not a blank toast.
MESSAGE_IDS = (
    newcontent.MOVIE_ONE,
    newcontent.MOVIE_MANY,
    newcontent.SERIES_ONE,
    newcontent.SERIES_MANY,
    newcontent.EPISODE_ONE,
    newcontent.EPISODE_MANY,
    newcontent.EPISODES_MIXED,
    newcontent.MUSICVIDEO_ONE,
    newcontent.MUSICVIDEO_MANY,
    newcontent.ARTIST_ONE,
    newcontent.ARTIST_MANY,
    newcontent.ALBUM_ONE,
    newcontent.ALBUM_MANY,
)


def load_strings():
    """{id: msgid} for the English strings, parsed the way Kodi keys them."""
    strings = {}
    string_id = None

    with open(STRINGS_PO, encoding="utf-8") as handle:
        for line in handle:
            context = re.match(r'msgctxt "#(\d+)"', line)

            if context:
                string_id = int(context.group(1))
                continue

            text = re.match(r'msgid "(.*)"', line)

            if text and string_id is not None:
                strings[string_id] = text.group(1)
                string_id = None

    return strings


@pytest.fixture(autouse=True)
def real_strings(monkeypatch):
    strings = load_strings()
    monkeypatch.setattr(
        newcontent.settings, "localized", lambda string_id: strings[string_id]
    )
    return strings


# -- item builders ------------------------------------------------------------


def movie(item_id="movie1", name="Blade Runner", played=False):
    return {
        "Type": "Movie",
        "Id": item_id,
        "Name": name,
        "UserData": {"Played": played},
    }


def series(item_id="series1", name="Severance", played=False):
    return {
        "Type": "Series",
        "Id": item_id,
        "Name": name,
        "UserData": {"Played": played},
    }


def episode(
    item_id="ep1",
    name="Good News About Hell",
    series_id="series1",
    series_name="Severance",
    played=False,
):
    return {
        "Type": "Episode",
        "Id": item_id,
        "Name": name,
        "SeriesId": series_id,
        "SeriesName": series_name,
        "UserData": {"Played": played},
    }


def musicvideo(item_id="mv1", name="Bad Guy", played=False):
    return {
        "Type": "MusicVideo",
        "Id": item_id,
        "Name": name,
        "UserData": {"Played": played},
    }


def artist(item_id="artist1", name="Kelly Lee Owens", played=False):
    return {
        "Type": "MusicArtist",
        "Id": item_id,
        "Name": name,
        "UserData": {"Played": played},
    }


def album(item_id="album1", name="Inner Song", played=False):
    return {
        "Type": "MusicAlbum",
        "Id": item_id,
        "Name": name,
        "UserData": {"Played": played},
    }


def summarize(*items):
    """The messages for these items, entries built the way a writer does."""
    entries = [newcontent.entry_for(item) for item in items]
    return newcontent.summarize([entry for entry in entries if entry is not None])


# -- one of a kind names it, several count it ---------------------------------


def test_one_movie_is_named():
    assert summarize(movie()) == ["Blade Runner movie added to library"]


def test_several_movies_are_counted():
    assert summarize(movie("movie1"), movie("movie2"), movie("movie3")) == [
        "3 movies added to library"
    ]


def test_one_show_is_named():
    assert summarize(series()) == ["Severance show added to library"]


def test_several_shows_are_counted():
    assert summarize(series("s1"), series("s2")) == ["2 new shows added to library"]


def test_one_music_video_is_named():
    assert summarize(musicvideo()) == ["Bad Guy music video added to library"]


def test_several_music_videos_are_counted():
    assert summarize(musicvideo("mv1"), musicvideo("mv2")) == [
        "2 music videos added to library"
    ]


def test_one_artist_is_named():
    assert summarize(artist()) == ["Kelly Lee Owens added to music library"]


def test_several_artists_are_counted():
    assert summarize(artist("a1"), artist("a2")) == ["2 artists added to library"]


def test_one_album_is_named():
    assert summarize(album()) == ["Inner Song added to music library"]


def test_several_albums_are_counted():
    assert summarize(album("al1"), album("al2")) == ["2 albums added to library"]


# -- episodes: counted per show, or across them -------------------------------


def test_one_episode_of_one_show():
    assert summarize(episode()) == ["1 episode of Severance added to library"]


def test_several_episodes_of_one_show():
    assert summarize(episode("ep1"), episode("ep2"), episode("ep3")) == [
        "3 episodes of Severance added to library"
    ]


def test_episodes_across_shows_lose_the_show_name():
    """Two shows and one line: naming either would be arbitrary, and naming
    both is what the count is for."""
    assert summarize(
        episode("ep1"),
        episode("ep2", series_id="series2", series_name="Andor"),
    ) == ["2 episodes added to library"]


def test_episodes_of_a_new_show_are_left_to_the_show_line():
    """The show line already says it is new; the episode line would say the
    same thing again, and there is nothing left to say once its episodes are
    the only ones."""
    assert summarize(series(), episode("ep1"), episode("ep2")) == [
        "Severance show added to library"
    ]


def test_episodes_of_other_shows_survive_a_new_show():
    assert summarize(
        series(),
        episode("ep1"),
        episode("ep2", series_id="series2", series_name="Andor"),
    ) == [
        "Severance show added to library",
        "1 episode of Andor added to library",
    ]


# -- silence ------------------------------------------------------------------


@pytest.mark.parametrize("builder", [movie, series, episode, musicvideo, artist, album])
def test_watched_items_are_never_announced(builder):
    assert newcontent.entry_for(builder(played=True)) is None
    assert summarize(builder(played=True)) == []


@pytest.mark.parametrize("item_type", ["Audio", "BoxSet", "Season", "Folder"])
def test_types_without_a_message_yield_no_entry(item_type):
    item = {
        "Type": item_type,
        "Id": "x1",
        "Name": "Something",
        "UserData": {"Played": False},
    }
    assert newcontent.entry_for(item) is None


def test_a_song_never_speaks_for_its_album():
    song = {
        "Type": "Audio",
        "Id": "song1",
        "Name": "Melt!",
        "UserData": {"Played": False},
    }
    assert summarize(album(), song, song) == ["Inner Song added to music library"]


def test_payloads_too_thin_to_name_yield_no_entry():
    assert newcontent.entry_for(dict(movie(), Name="")) is None
    assert newcontent.entry_for(dict(movie(), Name="   ")) is None
    assert newcontent.entry_for(dict(movie(), Id="")) is None
    assert newcontent.entry_for(dict(episode(), SeriesName="")) is None
    assert newcontent.entry_for(dict(episode(), SeriesId="")) is None


def test_a_missing_userdata_block_is_not_watched():
    """Absent is not "played": an item with no UserData at all is still an
    addition worth announcing."""
    bare = {"Type": "Movie", "Id": "movie1", "Name": "Blade Runner"}
    assert newcontent.entry_for(bare) == newcontent.Entry(
        "Movie", "movie1", "Blade Runner"
    )


def test_nothing_at_all_says_nothing():
    assert newcontent.summarize([]) == []


# -- the shape of a whole cycle ------------------------------------------------


def test_an_id_offered_twice_counts_once():
    """The change feed and a repair prune can both offer the same item inside
    one cycle; "2 movies added" for one movie is worse than silence."""
    assert summarize(movie("movie1"), movie("movie1")) == [
        "Blade Runner movie added to library"
    ]


def test_messages_come_in_display_order():
    assert summarize(
        album("al1"),
        artist("a1"),
        musicvideo("mv1"),
        episode("ep1"),
        series("s2", name="Andor"),
        movie("movie1"),
    ) == [
        "Blade Runner movie added to library",
        "Andor show added to library",
        "1 episode of Severance added to library",
        "Bad Guy music video added to library",
        "Kelly Lee Owens added to music library",
        "Inner Song added to music library",
    ]


def test_a_cycle_raises_at_most_one_line_per_content_type():
    messages = summarize(
        movie("m1"),
        movie("m2"),
        series("s1"),
        series("s2"),
        episode("ep1", series_id="series9", series_name="Andor"),
        musicvideo("mv1"),
        musicvideo("mv2"),
        artist("a1"),
        album("al1"),
        album("al2"),
    )
    assert len(messages) == 6


# -- the strings themselves ----------------------------------------------------


def test_every_message_id_ships_a_string(real_strings):
    missing = [string_id for string_id in MESSAGE_IDS if string_id not in real_strings]
    assert missing == []


def test_the_setting_ships_a_label_and_help(real_strings):
    assert real_strings[30622]
    assert real_strings[30623]


def test_each_template_takes_the_arguments_it_is_given(real_strings):
    """A template with one "%s" too many raises at format time, which is a
    toast lost for a reason no log would explain."""
    for string_id in MESSAGE_IDS:
        expected = (
            2 if string_id in (newcontent.EPISODE_ONE, newcontent.EPISODE_MANY) else 1
        )
        assert real_strings[string_id].count("%s") == expected
