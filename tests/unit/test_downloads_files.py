"""L1 units for download file layout and naming (plan W1.4)."""

import os

import pytest

from kofin.downloads import files


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Plain Title", "Plain Title"),
        ("Mission: Impossible", "Mission Impossible"),
        ('A "quoted" name', "A quoted name"),
        ("AC/DC - Live", "ACDC - Live"),
        ("What?*", "What"),
        ("Pipe|Star*Lt<Gt>", "PipeStarLtGt"),
        ("  spaced   out  ", "spaced out"),
        ("Best. Movie. Ever.", "Best. Movie. Ever"),  # only trailing dots go
        ("Amélie", "Amélie"),  # unicode survives
        ("千と千尋の神隠し", "千と千尋の神隠し"),
        ("ctrl\x01char", "ctrlchar"),
        ('<>:"/\\|?*', "untitled"),  # nothing survives -> placeholder
        ("", "untitled"),
    ],
)
def test_sanitize_vectors(raw, expected):
    assert files.sanitize(raw) == expected


def test_filename_from_disposition_plain_and_extended():
    plain = 'attachment; filename="Any Number Can Win (1963) Bluray-720p x264 AAC.mp4"'
    assert (
        files.filename_from_disposition(plain, "fb.mkv")
        == "Any Number Can Win (1963) Bluray-720p x264 AAC.mp4"
    )

    extended = "attachment; filename*=UTF-8''Am%C3%A9lie%20%282001%29.mkv"
    assert files.filename_from_disposition(extended, "fb.mkv") == "Amélie (2001).mkv"


def test_filename_from_disposition_falls_back():
    assert files.filename_from_disposition("", "fb.mkv") == "fb.mkv"
    assert files.filename_from_disposition("attachment", "fb.mkv") == "fb.mkv"


def test_filename_from_disposition_cannot_escape_the_tree():
    """The header is server input; a traversal in it must die here, not at
    open()."""
    sneaky = 'attachment; filename="../../etc/passwd"'
    assert files.filename_from_disposition(sneaky, "fb.mkv") == "passwd"

    windows = 'attachment; filename="..\\..\\boot.ini"'
    assert files.filename_from_disposition(windows, "fb.mkv") == "boot.ini"


def test_movie_dirs_with_and_without_year():
    assert files.item_dirs(
        {"Type": "Movie", "Name": "Heat", "ProductionYear": 1995}
    ) == ("Movies/Heat (1995)", None)
    assert files.item_dirs({"Type": "Movie", "Name": "Heat"}) == ("Movies/Heat", None)


def test_episode_dirs_split_show_from_season():
    """The show directory is the owner, the season a leaf inside it — the
    split is what lets siblings share a season folder while a name clash
    between two *shows* still separates them (see unique_dir)."""
    episode = {"Type": "Episode", "SeriesName": "UFC PPV Events"}
    assert files.item_dirs({**episode, "ParentIndexNumber": 1}) == (
        "Shows/UFC PPV Events",
        "Season 01",
    )
    assert files.item_dirs({**episode, "ParentIndexNumber": 0}) == (
        "Shows/UFC PPV Events",
        "Specials",
    )
    assert files.item_dirs(episode) == ("Shows/UFC PPV Events", None)


def test_unknown_type_raises_instead_of_inventing_a_home():
    with pytest.raises(ValueError):
        files.item_dirs({"Type": "Photo", "Name": "Holiday"})


def test_unique_dir_suffixes_only_on_collision():
    assert files.unique_dir("Movies/Crash (2004)", "abcdef1234", lambda d: False) == (
        "Movies/Crash (2004)"
    )
    assert files.unique_dir("Movies/Crash (2004)", "abcdef1234", lambda d: True) == (
        "Movies/Crash (2004) [abcdef12]"
    )


class FakeStatvfs:
    def __init__(self, free_bytes):
        self.f_bavail = free_bytes // 4096
        self.f_frsize = 4096


def test_free_space_needs_download_plus_reserve(monkeypatch):
    monkeypatch.setattr(
        os, "statvfs", lambda root: FakeStatvfs(files.FREE_SPACE_RESERVE + 8192)
    )
    assert files.free_space_ok("/dl", 8192) is True
    assert files.free_space_ok("/dl", 8193) is False


def test_free_space_probe_failure_allows_rather_than_refuses(monkeypatch):
    def broken(root):
        raise OSError("no statvfs here")

    monkeypatch.setattr(os, "statvfs", broken)
    assert files.free_space_ok("/dl", 10**12) is True


# -- the music layout and the fallback names (plan W3.2) -----------------------


def test_audio_dirs_are_artist_album_owned_by_the_album():
    song = {
        "Type": "Audio",
        "AlbumArtist": "The Band",
        "Album": "Greatest Hits",
        "Name": "Opening Track",
    }
    assert files.item_dirs(song) == ("Music/The Band/Greatest Hits", None)


def test_audio_dirs_fall_back_through_the_artist_fields():
    from_items = {
        "Type": "Audio",
        "AlbumArtists": [{"Name": "Solo Artist", "Id": "a1"}],
        "Album": "Album",
    }
    assert files.item_dirs(from_items)[0] == "Music/Solo Artist/Album"
    from_artists = {"Type": "Audio", "Artists": ["Credited"], "Album": "Album"}
    assert files.item_dirs(from_artists)[0] == "Music/Credited/Album"
    bare = {"Type": "Audio"}
    assert files.item_dirs(bare) == ("Music/Unknown artist/Unknown album", None)


def test_default_filename_orders_directories():
    episode = {
        "Type": "Episode",
        "Name": "The One",
        "ParentIndexNumber": 2,
        "IndexNumber": 9,
    }
    assert files.default_filename(episode, "mp4") == "S02E09 The One.mp4"
    song = {"Type": "Audio", "Name": "Opening Track", "IndexNumber": 1}
    assert files.default_filename(song, "opus") == "01 Opening Track.opus"
    movie = {"Type": "Movie", "Name": "The Movie"}
    assert files.default_filename(movie, "mp4") == "The Movie.mp4"
    assert files.default_filename({"Type": "Movie", "Id": "x"}, "") == "x.bin"


def test_sanitize_degrades_to_ascii_only_when_the_filesystem_cannot(monkeypatch):
    """Kodi's embedded Python can run with an ASCII filesystem encoding
    (measured live, G12: '’' in an album name killed the download in
    os.remove). Accents fold, unmappable glyphs drop — but only there; a
    healthy UTF-8 box keeps the real name."""
    fancy = "Black Betty (Rough ’n’ Ready remix)"
    accented = "Café Tacvba"

    monkeypatch.setattr(files.sys, "getfilesystemencoding", lambda: "utf-8")
    assert files.sanitize(fancy) == fancy
    assert files.sanitize(accented) == accented

    monkeypatch.setattr(files.sys, "getfilesystemencoding", lambda: "ascii")
    assert files.sanitize(fancy) == "Black Betty (Rough n Ready remix)"
    assert files.sanitize(accented) == "Cafe Tacvba"
    assert files.sanitize("’’’") == "untitled"
