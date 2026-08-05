"""Sidecar subtitles fetched to locally-named files.

Every expectation here is pinned to what Kodi was measured doing with a
filename on Omega 21.3 (see the module docstring): a name, a language, and
``forced`` -- and nothing else, because anything Kodi does not parse is
rendered in the label instead.
"""

import os

import pytest

from kofin.core.streams import Attachment
from kofin.plugin import subtitles


def attachment(**kwargs):
    base = {
        "stream_index": 3,
        "url": "http://s:8096/Videos/m1/src1/Subtitles/3/0/Stream.subrip?ApiKey=k",
        "sidecar": True,
        "language": "eng",
        "title": "",
        "forced": False,
    }
    base.update(kwargs)
    return Attachment(**base)


class FakeHttp:
    def __init__(self, body=b"1\n00:00:01,000 --> 00:00:02,000\nhi\n", fail=False):
        self.body = body
        self.fail = fail
        self.requests = []

    def request(self, method, url, timeout=None, retries=0, **kwargs):
        self.requests.append((method, url, timeout, retries))
        if self.fail:
            raise OSError("server said no")
        return type("Response", (), {"content": self.body})()


# -- the URL -------------------------------------------------------------------


def test_a_codec_extension_is_asked_for_as_srt():
    """Jellyfin names the file after the codec, and Kodi has no parser for
    ".subrip"; the same route answers .srt with the same cues."""
    assert subtitles.delivery_url(attachment().url) == (
        "http://s:8096/Videos/m1/src1/Subtitles/3/0/Stream.srt?ApiKey=k"
    )


def test_an_extension_kodi_knows_is_left_alone():
    """.ass keeps its styling and .vtt its own parser -- converting either to
    SRT would throw that away."""
    for extension in ("ass", "ssa", "vtt", "srt"):
        url = "http://s/Subtitles/3/0/Stream.%s?ApiKey=k" % extension
        assert subtitles.delivery_url(url) == url


def test_a_url_with_no_query_still_converts():
    assert subtitles.delivery_url("http://s/Stream.subrip") == "http://s/Stream.srt"


# -- the filename --------------------------------------------------------------


def test_the_language_is_spelled_out_when_there_is_no_title(monkeypatch):
    monkeypatch.setattr(
        subtitles.xbmc, "convertLanguage", lambda code, fmt: "English", raising=False
    )
    assert subtitles.filename_for(attachment()) == "English.eng.srt"


def test_a_title_the_server_gave_wins(monkeypatch):
    monkeypatch.setattr(
        subtitles.xbmc, "convertLanguage", lambda code, fmt: "Japanese", raising=False
    )
    named = attachment(title="Signs & Songs", language="jpn")
    assert subtitles.filename_for(named) == "Signs & Songs.jpn.srt"


def test_forced_is_a_token_kodi_reads_as_a_flag(monkeypatch):
    monkeypatch.setattr(
        subtitles.xbmc, "convertLanguage", lambda code, fmt: "English", raising=False
    )
    assert subtitles.filename_for(attachment(forced=True)) == "English.eng.forced.srt"


def test_the_language_code_stands_in_when_kodi_cannot_spell_it(monkeypatch):
    monkeypatch.setattr(
        subtitles.xbmc, "convertLanguage", lambda code, fmt: "", raising=False
    )
    assert subtitles.filename_for(attachment(language="qaa")) == "qaa.qaa.srt"


def test_a_nameless_track_still_gets_a_filename(monkeypatch):
    monkeypatch.setattr(
        subtitles.xbmc, "convertLanguage", lambda code, fmt: "", raising=False
    )
    assert subtitles.filename_for(attachment(language="")) == "Subtitle.srt"


def test_dots_and_separators_are_scrubbed_out_of_the_name(monkeypatch):
    """Kodi tokenises the filename on dots, so one inside the name would read
    as another token -- and a slash would not be a filename at all."""
    monkeypatch.setattr(
        subtitles.xbmc, "convertLanguage", lambda code, fmt: "English", raising=False
    )
    named = attachment(title="S.D.H. / full")
    assert subtitles.filename_for(named) == "S_D_H_ _ full.eng.srt"


# -- localize ------------------------------------------------------------------


def test_a_sidecar_is_fetched_and_the_rest_keep_their_url(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subtitles.xbmc, "convertLanguage", lambda code, fmt: "English", raising=False
    )
    http = FakeHttp()
    attached = [
        attachment(stream_index=3, sidecar=False),
        attachment(stream_index=6, sidecar=True),
    ]

    paths = subtitles.localize(http, attached, directory=str(tmp_path))

    assert paths[0] == attached[0].url  # embedded: fifty of these is a startup cost
    assert paths[1] == str(tmp_path / "English.eng.srt")
    assert (tmp_path / "English.eng.srt").read_bytes() == http.body
    # Fetched over the converted URL, once, with no retries.
    assert len(http.requests) == 1
    assert http.requests[0][1].endswith("Stream.srt?ApiKey=k")
    assert http.requests[0][3] == 0


def test_a_failed_fetch_falls_back_to_the_url(tmp_path):
    """Worse-labelled, never missing: the track has to stay in position or the
    Jellyfin index no longer maps to a Kodi ordinal."""
    attached = [attachment(stream_index=6)]
    paths = subtitles.localize(FakeHttp(fail=True), attached, directory=str(tmp_path))
    assert paths == [attached[0].url]


def test_the_order_in_is_the_order_out(tmp_path, monkeypatch):
    """It is what makes a Jellyfin index translatable to a Kodi subtitle
    number at all (streams.subtitle_ordinal)."""
    monkeypatch.setattr(
        subtitles.xbmc, "convertLanguage", lambda code, fmt: "English", raising=False
    )
    attached = [
        attachment(stream_index=3, sidecar=False),
        attachment(stream_index=6, sidecar=True, language="ger"),
        attachment(stream_index=7, sidecar=False),
    ]
    paths = subtitles.localize(FakeHttp(), attached, directory=str(tmp_path))
    assert len(paths) == 3
    assert paths[0] == attached[0].url
    assert paths[1].endswith("English.ger.srt")
    assert paths[2] == attached[2].url


def test_only_so_many_are_worth_the_first_frame(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subtitles.xbmc, "convertLanguage", lambda code, fmt: "English", raising=False
    )
    http = FakeHttp()
    attached = [attachment(stream_index=n, language="l%02d" % n) for n in range(12)]

    paths = subtitles.localize(http, attached, directory=str(tmp_path))

    assert len(http.requests) == subtitles.MAX_FILES
    assert sum(1 for path in paths if path.startswith(str(tmp_path))) == (
        subtitles.MAX_FILES
    )
    # The rest are still attached, just by URL.
    assert paths[-1] == attached[-1].url


def test_each_play_sweeps_the_one_before_it(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subtitles.xbmc, "convertLanguage", lambda code, fmt: "English", raising=False
    )
    (tmp_path / "Deutsch.ger.srt").write_bytes(b"from the last playback")

    subtitles.localize(FakeHttp(), [attachment()], directory=str(tmp_path))

    assert sorted(os.listdir(str(tmp_path))) == ["English.eng.srt"]


def test_a_sweep_that_cannot_delete_is_not_fatal(tmp_path, monkeypatch):
    """A file the outgoing playback still holds open is a failed unlink on some
    platforms, and a stale file is not worth failing a playback over."""

    def refuse(path):
        raise OSError("in use")

    (tmp_path / "held.srt").write_bytes(b"x")
    monkeypatch.setattr(subtitles.os, "remove", refuse)
    assert subtitles.sweep(str(tmp_path)) == 0


def test_nothing_attached_costs_nothing(tmp_path):
    http = FakeHttp()
    assert subtitles.localize(http, [], directory=str(tmp_path)) == []
    assert http.requests == []


@pytest.mark.parametrize(
    "url,extension",
    [
        ("http://s/Stream.subrip?ApiKey=k", "subrip"),
        ("http://s/Stream.SRT", "srt"),
        # A dot is common in a host name and rare in a subtitle route.
        ("http://s.co/Stream", ""),
        ("http://s/Stream", ""),
    ],
)
def test_extension_of_reads_the_filename_only(url, extension):
    assert subtitles.extension_of(url) == extension


def test_a_dot_in_the_host_is_not_an_extension():
    """Rewriting on the last dot in the whole URL turned http://s.co/Stream
    into http://s.srt -- a different server."""
    assert subtitles.delivery_url("http://s.co/Stream") == "http://s.co/Stream.srt"
    assert subtitles.delivery_url("http://s.co/Stream.subrip?k=1") == (
        "http://s.co/Stream.srt?k=1"
    )
