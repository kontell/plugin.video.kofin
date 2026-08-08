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


def test_movie_dir_with_and_without_year():
    assert (
        files.item_dir({"Type": "Movie", "Name": "Heat", "ProductionYear": 1995})
        == "Movies/Heat (1995)"
    )
    assert files.item_dir({"Type": "Movie", "Name": "Heat"}) == "Movies/Heat"


def test_episode_dir_season_specials_and_none():
    episode = {"Type": "Episode", "SeriesName": "UFC PPV Events"}
    assert (
        files.item_dir({**episode, "ParentIndexNumber": 1})
        == "TV/UFC PPV Events/Season 01"
    )
    assert (
        files.item_dir({**episode, "ParentIndexNumber": 0})
        == "TV/UFC PPV Events/Specials"
    )
    assert files.item_dir(episode) == "TV/UFC PPV Events"


def test_unknown_type_raises_instead_of_inventing_a_home():
    with pytest.raises(ValueError):
        files.item_dir({"Type": "Audio", "Name": "Song"})


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
