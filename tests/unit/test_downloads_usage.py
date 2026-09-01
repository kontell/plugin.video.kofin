"""L1 units for the download size report (D1)."""

import os

from kofin.downloads import usage


def _file(path, size):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(str(path), "wb") as handle:
        handle.write(b"x" * size)


def _stub_free(monkeypatch, value):
    monkeypatch.setattr(usage.files, "free_bytes", lambda root: value)


def test_scan_totals_each_category_from_disk(tmp_path, monkeypatch):
    """Every file counts, not just the media: the whole reason the report
    walks the folder instead of summing the store's size_actual."""
    _stub_free(monkeypatch, 1000)
    root = tmp_path / "dl"
    _file(root / "Movies" / "The Movie (2019)" / "The Movie (2019).mkv", 300)
    _file(root / "Movies" / "The Movie (2019)" / "poster.jpg", 20)
    _file(root / "Movies" / "The Movie (2019)" / "The Movie (2019).eng.srt", 5)
    _file(root / "Shows" / "The Show" / "Season 01" / "S01E01.mkv", 100)
    _file(root / "Music" / "The Band" / "The Album" / "01 Track.flac", 50)
    _file(root / "Music" / "The Band" / "The Album" / "folder.jpg", 10)

    report = usage.scan(str(root))

    sizes = {bucket.key: (bucket.size, bucket.file_count) for bucket in report.buckets}
    assert sizes["Movies"] == (325, 3)  # media + poster + subtitle
    assert sizes["Shows"] == (100, 1)
    assert sizes["Music"] == (60, 2)
    assert report.total == 485
    assert report.free == 1000
    assert report.capped is False
    assert report.empty is False


def test_a_half_finished_transfer_counts(tmp_path, monkeypatch):
    """A .part is space the folder is using, whatever the store thinks."""
    _stub_free(monkeypatch, 0)
    root = tmp_path / "dl"
    _file(root / "Movies" / "The Movie (2019)" / "The Movie (2019).mkv.part", 700)

    report = usage.scan(str(root))

    assert report.total == 700
    assert report.free == 0  # a real answer: the disk is full


def test_foreign_files_get_their_own_bucket(tmp_path, monkeypatch):
    """The root may be shared with other media. Those files are listed
    rather than left out, or the total would not match the user's own file
    manager — but they are never counted as kofin's."""
    _stub_free(monkeypatch, 1)
    root = tmp_path / "media"
    _file(root / "Movies" / "Ours (2019)" / "Ours.mkv", 10)
    _file(root / "holiday.mp4", 40)
    _file(root / "Home videos" / "wedding.mkv", 50)

    report = usage.scan(str(root))

    buckets = {bucket.key: bucket for bucket in report.buckets}
    assert buckets["Movies"].size == 10
    other = buckets[""]
    assert other.label_id == usage.OTHER_LABEL
    assert (other.size, other.file_count) == (90, 2)
    assert report.total == 100


def test_no_foreign_bucket_when_there_is_nothing_foreign(tmp_path, monkeypatch):
    _stub_free(monkeypatch, 1)
    root = tmp_path / "dl"
    _file(root / "Movies" / "Ours (2019)" / "Ours.mkv", 10)

    report = usage.scan(str(root))

    assert [bucket.key for bucket in report.buckets] == ["Movies", "Shows", "Music"]


def test_a_root_that_does_not_exist_is_empty_not_an_error(tmp_path, monkeypatch):
    """Normal before the first download, and the caller says so in words
    rather than showing three zeroes."""
    _stub_free(monkeypatch, -1)

    report = usage.scan(str(tmp_path / "never-made"))

    assert report.total == 0
    assert report.empty is True
    assert report.free == -1  # "could not tell", never rendered as zero
    assert [bucket.file_count for bucket in report.buckets] == [0, 0, 0]


def test_the_walk_is_bounded_and_says_when_it_stopped(tmp_path, monkeypatch):
    """A total that quietly stopped counting is worse than no total."""
    _stub_free(monkeypatch, 1)
    monkeypatch.setattr(usage, "MAX_ENTRIES", 3)
    root = tmp_path / "dl"
    for index in range(10):
        _file(root / "Movies" / ("m%d" % index) / "film.mkv", 1)

    report = usage.scan(str(root))

    assert report.capped is True
    assert report.total < 10


def test_symlinked_directories_are_not_followed(tmp_path, monkeypatch):
    """A link into the media library would count somebody else's files as
    kofin's, and a link loop would never end."""
    _stub_free(monkeypatch, 1)
    root = tmp_path / "dl"
    elsewhere = tmp_path / "elsewhere"
    _file(elsewhere / "big.mkv", 999)
    _file(root / "Movies" / "Ours (2019)" / "Ours.mkv", 10)
    os.symlink(str(elsewhere), str(root / "Movies" / "linked"))

    report = usage.scan(str(root))

    buckets = {bucket.key: bucket for bucket in report.buckets}
    assert buckets["Movies"].size < 999
    assert report.total < 999
