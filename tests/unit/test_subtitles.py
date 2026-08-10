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


def test_everything_attached_is_fetched_to_a_named_file(tmp_path, monkeypatch):
    """Both kinds are fetched now. The list is short — sidecars plus at most
    the one embedded track a transcode resolved — and a local file is what
    gives Kodi a language to read off the name."""
    monkeypatch.setattr(
        subtitles.xbmc, "convertLanguage", lambda code, fmt: "English", raising=False
    )
    http = FakeHttp()
    attached = [
        attachment(stream_index=3, sidecar=False),
        attachment(stream_index=6, sidecar=True, language="ger"),
    ]

    localized = subtitles.localize(http, attached, directory=str(tmp_path))

    assert [item.stream_index for item, _path in localized.files] == [3, 6]
    assert all(path.startswith(str(tmp_path)) for _item, path in localized.files)
    assert localized.deferred == []
    # Fetched over the converted URL, with no retries.
    assert len(http.requests) == 2
    assert all(request[1].endswith("Stream.srt?ApiKey=k") for request in http.requests)
    assert all(request[3] == 0 for request in http.requests)


def test_a_failed_sidecar_falls_back_to_its_url(tmp_path):
    """Worse-labelled, never missing. Safe for a sidecar: the server already
    has the file, so Kodi opening the URL itself costs nothing."""
    attached = [attachment(stream_index=6, sidecar=True)]
    localized = subtitles.localize(
        FakeHttp(fail=True), attached, directory=str(tmp_path)
    )
    assert localized.files == [(attached[0], attached[0].url)]
    assert localized.deferred == []


def test_a_failed_embedded_track_is_deferred_not_dropped(tmp_path):
    """The opposite call: an embedded track is extracted on demand, and its
    URL is exactly what made Kodi stall for 20 seconds while building the
    demuxer — so it is never attached unfetched. It is handed back instead of
    dropped, because the extraction it just started is still running and the
    service chases it onto the running playback (service/latesubs.py)."""
    attached = [attachment(stream_index=3, sidecar=False)]
    localized = subtitles.localize(
        FakeHttp(fail=True), attached, directory=str(tmp_path)
    )
    assert localized.files == []
    assert localized.deferred == attached


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
    localized = subtitles.localize(FakeHttp(), attached, directory=str(tmp_path))
    assert [item.stream_index for item, _path in localized.files] == [3, 6, 7]
    assert localized.files[1][1].endswith("English.ger.srt")


def test_only_so_many_are_worth_the_first_frame(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subtitles.xbmc, "convertLanguage", lambda code, fmt: "English", raising=False
    )
    http = FakeHttp()
    attached = [attachment(stream_index=n, language="l%02d" % n) for n in range(12)]

    localized = subtitles.localize(http, attached, directory=str(tmp_path))

    assert len(http.requests) == subtitles.MAX_FILES
    assert sum(
        1 for _item, path in localized.files if path.startswith(str(tmp_path))
    ) == (subtitles.MAX_FILES)
    # The rest are sidecars, so they keep their URL rather than dropping.
    assert localized.files[-1][1] == attached[-1].url


def test_each_play_sweeps_the_one_before_it(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subtitles.xbmc, "convertLanguage", lambda code, fmt: "English", raising=False
    )
    (tmp_path / "Deutsch.ger.srt").write_bytes(b"from the last playback")

    subtitles.localize(FakeHttp(), [attachment()], directory=str(tmp_path))

    assert sorted(os.listdir(str(tmp_path))) == ["English.eng.srt"]


def test_an_embedded_track_waits_less_than_a_sidecar(tmp_path):
    """They are on different clocks. A sidecar is a file the server already
    has; an embedded track is an ffmpeg extraction measured at 28-146 s, after
    which it is cached and answers in ~25 ms. So no budget between those two
    outcomes buys anything, and every second spent on a cold one is a second
    of black screen — the single 8 s budget missed every first play and
    charged 8 s to do it."""
    http = FakeHttp()
    subtitles.localize(
        http,
        [
            attachment(stream_index=3, sidecar=False),
            attachment(stream_index=6, url="http://s:8096/side.srt"),
        ],
        directory=str(tmp_path),
    )
    budgets = {request[1]: request[2] for request in http.requests}
    assert len(budgets) == 2
    embedded = [url for url in budgets if "/Subtitles/" in url][0]
    sidecar = [url for url in budgets if url != embedded][0]
    assert budgets[embedded] == subtitles.EMBEDDED_TIMEOUT
    assert budgets[sidecar] == subtitles.TIMEOUT
    assert subtitles.EMBEDDED_TIMEOUT[1] < subtitles.TIMEOUT[1]


def test_the_service_can_ask_for_a_budget_of_its_own(tmp_path):
    """The late chase reuses this naming and file handling, so it needs a way
    in that is not the play route's hurry (service/latesubs.py)."""
    http = FakeHttp()
    path = subtitles.fetch_to(
        http, attachment(stream_index=3, sidecar=False), str(tmp_path), (3.0, 60.0)
    )
    assert path.endswith(".srt") and os.path.exists(path)
    assert http.requests[0][2] == (3.0, 60.0)


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
    assert subtitles.localize(http, [], directory=str(tmp_path)) == ([], [])
    assert http.requests == []


def test_a_deferred_track_leaves_the_others_in_position(tmp_path, monkeypatch):
    """The ordinal mapping reads the attached list in order, so a track that
    did not land must close the gap rather than leave a hole."""
    monkeypatch.setattr(
        subtitles.xbmc, "convertLanguage", lambda code, fmt: "English", raising=False
    )

    class OneBadHttp(FakeHttp):
        def request(self, method, url, timeout=None, retries=0, **kwargs):
            self.requests.append((method, url, timeout, retries))
            if "/3/" in url:
                raise OSError("the server could not produce this track")
            return type("Response", (), {"content": self.body})()

    attached = [
        attachment(stream_index=3, sidecar=False, url="http://s/x/3/0/Stream.subrip"),
        attachment(stream_index=6, sidecar=True, language="ger"),
    ]
    localized = subtitles.localize(OneBadHttp(), attached, directory=str(tmp_path))

    assert [item.stream_index for item, _path in localized.files] == [6]
    assert [item.stream_index for item in localized.deferred] == [3]


def test_sidecars_share_the_wait_instead_of_queuing(tmp_path, monkeypatch):
    """Sequential fetches held the first frame for the sum of their round
    trips; they must run concurrently, order-of-results still the order in
    (perf plan W2.6)."""
    import threading
    import time

    monkeypatch.setattr(
        subtitles.xbmc, "convertLanguage", lambda code, fmt: "English", raising=False
    )

    class SlowHttp(FakeHttp):
        def request(self, method, url, timeout=None, retries=0, **kwargs):
            time.sleep(0.05)
            self.requests.append(threading.get_ident())
            return type("Response", (), {"content": self.body})()

    http = SlowHttp()
    attached = [
        attachment(stream_index=index, language="l%02d" % index) for index in range(4)
    ]
    started = time.monotonic()
    localized = subtitles.localize(http, attached, directory=str(tmp_path))

    assert len(localized.files) == 4
    assert all(path.startswith(str(tmp_path)) for _item, path in localized.files)
    # Four fetches on more than one thread, in far less than 4 x 50 ms.
    assert len(set(http.requests)) > 1
    assert time.monotonic() - started < 0.15


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
