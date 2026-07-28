import pytest

from kofin.core import http as http_module
from kofin.core import lyrics
from kofin.core import state
from kofin.core.api import Api
from kofin.core.http import Http, HttpError
from kofin.service.player import Player, playing_jellyfin_id
from tests.unit.fakes import FakeAddon, FakeWindow

# One second is 10_000_000 ticks; these are the values a real server returns.
SYNCED = {
    "Metadata": {},
    "Lyrics": [
        {"Text": "Tonight", "Start": 5800000, "Cues": []},
        {"Text": "I just want to take you higher", "Start": 946000000, "Cues": []},
    ],
}
PLAIN = {
    "Metadata": {},
    "Lyrics": [{"Text": "Sunrise, wrong side of another day"}, {"Text": "Sky high"}],
}


# -- rendering ---------------------------------------------------------------


def test_synced_payload_renders_as_lrc():
    assert lyrics.is_synced(SYNCED)
    assert lyrics.to_text(SYNCED) == (
        "[00:00.58]Tonight\n[01:34.60]I just want to take you higher"
    )


def test_plain_payload_renders_without_stamps():
    assert not lyrics.is_synced(PLAIN)
    assert lyrics.to_text(PLAIN) == "Sunrise, wrong side of another day\nSky high"


def test_empty_payloads_render_to_nothing():
    assert lyrics.to_text(None) is None
    assert lyrics.to_text({}) is None
    assert lyrics.to_text({"Lyrics": []}) is None
    # A payload of blank lines is no more use than no payload at all.
    assert lyrics.to_text({"Lyrics": [{"Text": ""}, {"Text": "   "}]}) is None


def test_metadata_is_synced_is_ignored():
    """Jellyfin core never sets it, so only the line stamps may be trusted."""
    lying = {"Metadata": {"IsSynced": True}, "Lyrics": [{"Text": "no stamp here"}]}
    assert not lyrics.is_synced(lying)
    assert lyrics.to_text(lying) == "no stamp here"


def test_untimed_line_inside_a_timed_payload_is_emitted_bare():
    mixed = {
        "Lyrics": [
            {"Text": "first", "Start": 0},
            {"Text": "[ar: Someone]"},
            {"Text": "third", "Start": 610000000},
        ]
    }
    assert lyrics.to_text(mixed) == "[00:00.00]first\n[ar: Someone]\n[01:01.00]third"


@pytest.mark.parametrize(
    "ticks,stamp",
    [
        (0, "[00:00.00]"),
        (5800000, "[00:00.58]"),
        (600000000, "[01:00.00]"),
        # Rounds up to a whole minute: must not render as "[00:60.00]", which
        # no LRC reader would accept.
        (599999999, "[01:00.00]"),
        (36000000000, "[60:00.00]"),
        (-1, "[00:00.00]"),
    ],
)
def test_timestamp_formatting(ticks, stamp):
    assert lyrics.to_text({"Lyrics": [{"Text": "x", "Start": ticks}]}) == stamp + "x"


def test_rendered_stamps_match_what_lrclyrics_looks_for():
    """script.cu.lrclyrics decides a body is timed with this exact pattern; if
    it does not match, our lyrics lose to every online scraper."""
    import re

    text = lyrics.to_text(SYNCED)
    assert re.search(r"\[(\d+):(\d\d)(\.\d+|)\]", text)


# -- api ---------------------------------------------------------------------


class StubHttp(Http):
    def __init__(self, payload=None, error=None):
        super().__init__()
        self.payload = payload if payload is not None else {}
        self.error = error
        self.calls = []

    def request(self, method, url, headers=None, params=None, json_body=None, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        if self.error is not None:
            raise self.error
        payload = self.payload

        class Response:
            content = b"{}"

            def json(self):
                return payload

        return Response()


def make_api(transport):
    return Api(transport, "http://s:8096", "Kodi", "dev1", "0.1.0", "tok", "uid")


def test_lyrics_endpoint_and_payload():
    transport = StubHttp(SYNCED)
    assert make_api(transport).lyrics("abc123") == SYNCED
    call = transport.calls[0]
    assert call["url"] == "http://s:8096/Audio/abc123/Lyrics"
    assert call["method"] == "GET"


def test_lyrics_forfeits_rather_than_stalls():
    """A late answer is worth no more than none, so the fetch gets a short
    budget and no retries."""
    transport = StubHttp(SYNCED)
    make_api(transport).lyrics("abc123")
    kwargs = transport.calls[0]["kwargs"]
    assert kwargs["retries"] == 0
    assert max(kwargs["timeout"]) < max(http_module.DEFAULT_TIMEOUT)
    assert max(kwargs["timeout"]) <= 5.0


def test_missing_lyrics_is_not_an_error():
    transport = StubHttp(error=HttpError(404, "nope"))
    assert make_api(transport).lyrics("abc123") == {}


def test_other_http_errors_still_raise():
    transport = StubHttp(error=HttpError(500, "boom"))
    with pytest.raises(HttpError):
        make_api(transport).lyrics("abc123")


# -- identifying the playing song --------------------------------------------


class FakeMusicTag:
    def __init__(self, dbid=0):
        self._dbid = dbid

    def getDbId(self):
        return self._dbid


class FakeListItem:
    def __init__(self, dbid=0):
        self._tag = FakeMusicTag(dbid)
        self.info = {}
        self.props = {}

    def getMusicInfoTag(self):
        return self._tag

    def setInfo(self, kind, values):
        self.info.setdefault(kind, {}).update(values)

    def setProperty(self, key, value):
        self.props[key] = value


DIRECT = "http://s:8096/Audio/641f2c2a8c00a47efac033996582d550/stream.flac?static=true"
PLUGIN = (
    "plugin://plugin.video.kofin/lib1/641f2c2a8c00a47efac033996582d550/"
    "stream.flac?mode=play&id=641f2c2a8c00a47efac033996582d550&dbid=10851"
)
JID = "641f2c2a8c00a47efac033996582d550"


def test_database_id_is_preferred(monkeypatch):
    monkeypatch.setattr(
        "kofin.service.player.mapped_jellyfin_id", lambda kodi_id, media: "from-db"
    )
    assert playing_jellyfin_id(FakeListItem(dbid=10851), DIRECT) == "from-db"


@pytest.mark.parametrize("path", [DIRECT, PLUGIN])
def test_id_falls_back_to_the_path(monkeypatch, path):
    """Songs played from kofin's browse listing have no library row."""
    monkeypatch.setattr(
        "kofin.service.player.mapped_jellyfin_id", lambda kodi_id, media: None
    )
    assert playing_jellyfin_id(FakeListItem(dbid=0), path) == JID


def test_foreign_playback_is_not_claimed(monkeypatch):
    monkeypatch.setattr(
        "kofin.service.player.mapped_jellyfin_id", lambda kodi_id, media: None
    )
    assert playing_jellyfin_id(FakeListItem(dbid=0), "/home/me/song.mp3") is None


# -- structured lines, for the skin overlay ----------------------------------


def test_timed_payload_becomes_seconds_and_text():
    assert lyrics.to_lines(SYNCED) == [
        (0.58, "Tonight"),
        (94.6, "I just want to take you higher"),
    ]


def test_untimed_payload_has_no_starts():
    assert lyrics.to_lines(PLAIN) == [
        (None, "Sunrise, wrong side of another day"),
        (None, "Sky high"),
    ]
    assert lyrics.to_lines({}) == []


@pytest.mark.parametrize(
    "position,expected",
    [
        (0.0, None),  # before the first stamp: nothing is current yet
        (0.57, None),
        (0.58, 0),  # exactly on a stamp is that line
        (50.0, 0),
        (94.6, 1),
        (9999.0, 1),  # past the last line it stays on the last line
    ],
)
def test_active_index_follows_the_clock(position, expected):
    assert lyrics.active_index(lyrics.to_lines(SYNCED), position) == expected


def test_untimed_lyrics_have_no_active_line():
    assert lyrics.active_index(lyrics.to_lines(PLAIN), 30.0) is None
    assert lyrics.active_index([], 30.0) is None


def test_repeated_stamps_resolve_to_the_last_line():
    """A stacked '[00:12.00]' pair is one moment with two lines; the later one
    is what should be lit."""
    lines = [(0.0, "a"), (12.0, "b"), (12.0, "c"), (20.0, "d")]
    assert lyrics.active_index(lines, 12.0) == 2
    assert lyrics.active_index(lines, 19.9) == 2


# -- driving it from the player ----------------------------------------------


class LyricsApi:
    def __init__(self, payload=None, error=None):
        self.payload = payload if payload is not None else SYNCED
        self.error = error
        self.asked = []

    def lyrics(self, item_id):
        self.asked.append(item_id)
        if self.error is not None:
            raise self.error
        return self.payload


@pytest.fixture(autouse=True)
def kodi_fakes(monkeypatch):
    FakeWindow.store = {}
    # Default the suite to publishing; the hand-off tests opt in.
    FakeAddon.store = {"musicLyricsMode": "1"}
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)


def make_player(monkeypatch, api, path=DIRECT, audio=True, landed=True):
    player = Player(api)  # type: ignore[arg-type]
    pushed = []

    monkeypatch.setattr(player, "isPlayingAudio", lambda: audio)
    monkeypatch.setattr(player, "getPlayingFile", lambda: path)
    monkeypatch.setattr(player, "getPlayingItem", lambda: FakeListItem(dbid=10851))
    monkeypatch.setattr(player, "updateInfoTag", lambda item: pushed.append(item))
    monkeypatch.setattr(
        "kofin.service.player.mapped_jellyfin_id", lambda kodi_id, media: JID
    )
    monkeypatch.setattr(
        "xbmc.getInfoLabel",
        lambda label: (
            lyrics.to_text(SYNCED)
            if landed and pushed and label.endswith("Lyrics")
            else ""
        ),
    )
    return player, pushed


def test_publishes_timed_lines(monkeypatch):
    api = LyricsApi()
    player, pushed = make_player(monkeypatch, api)

    player.start_lyrics()

    assert api.asked == [JID]
    assert FakeWindow.store.get(state.PROP_LYRIC_HAS) == "true"
    # Timings ride along: deciding which line is current belongs to whatever
    # renders them, not to kofin.
    assert state.lyric_lines() == [
        [0.58, "Tonight"],
        [94.6, "I just want to take you higher"],
    ]
    assert state.lyric_texts() == ["Tonight", "I just want to take you higher"]
    # The path carries the song id, so it differs per song and a skin's list
    # re-reads it instead of showing the previous song's lines.
    assert FakeWindow.store[state.PROP_LYRIC_PATH].endswith("id=" + JID)
    assert pushed == []  # publishing must not also drive a lyrics addon


def test_untimed_lines_publish_with_no_starts(monkeypatch):
    api = LyricsApi(payload=PLAIN)
    player, _ = make_player(monkeypatch, api)

    player.start_lyrics()

    assert state.lyric_lines() == [
        [None, "Sunrise, wrong side of another day"],
        [None, "Sky high"],
    ]


def test_finalize_releases_the_lyrics(monkeypatch):
    api = LyricsApi()
    player, _ = make_player(monkeypatch, api)
    player.start_lyrics()
    assert FakeWindow.store.get(state.PROP_LYRIC_HAS) == "true"

    player.finalize()

    # Lyrics must not outlive the playback that fetched them.
    assert state.PROP_LYRIC_HAS not in FakeWindow.store
    assert state.lyric_lines() == []


def test_addon_mode_sets_lyrics_and_source(monkeypatch):
    FakeAddon.store = {"musicLyricsMode": "2"}
    api = LyricsApi()
    player, pushed = make_player(monkeypatch, api)

    player.start_lyrics()

    assert len(pushed) == 1
    # setInfo, not the info tag setter: only setInfo marks the tag loaded, and
    # an unloaded tag is re-read from the music database, which clears it.
    assert pushed[0].info["music"]["lyrics"] == lyrics.to_text(SYNCED)
    assert pushed[0].props["culrc.source"] == "Jellyfin"
    # The hand-off must not also publish for a renderer.
    assert state.PROP_LYRIC_HAS not in FakeWindow.store


def test_addon_mode_retries_until_kodi_accepts_it(monkeypatch):
    FakeAddon.store = {"musicLyricsMode": "2"}
    api = LyricsApi()
    player, pushed = make_player(monkeypatch, api, landed=False)
    monkeypatch.setattr("xbmc.sleep", lambda ms: None)

    player.start_lyrics()

    assert len(pushed) == 4  # retried, then gave up rather than looping


def test_off_asks_the_server_for_nothing(monkeypatch):
    FakeAddon.store = {"musicLyricsMode": "0"}
    api = LyricsApi()
    player, pushed = make_player(monkeypatch, api)

    player.start_lyrics()

    assert api.asked == []
    assert pushed == []
    assert state.PROP_LYRIC_HAS not in FakeWindow.store


def test_video_playback_is_left_alone(monkeypatch):
    api = LyricsApi()
    player, pushed = make_player(monkeypatch, api, audio=False)

    player.start_lyrics()

    assert api.asked == []
    assert pushed == []
    assert state.PROP_LYRIC_HAS not in FakeWindow.store


def test_song_without_lyrics_shows_nothing(monkeypatch):
    api = LyricsApi(payload={})
    player, pushed = make_player(monkeypatch, api)

    player.start_lyrics()

    assert api.asked == [JID]
    assert pushed == []
    assert state.PROP_LYRIC_HAS not in FakeWindow.store


def test_a_failing_server_never_breaks_playback(monkeypatch):
    from kofin.core.http import ServerUnreachable

    api = LyricsApi(error=ServerUnreachable("down"))
    player, pushed = make_player(monkeypatch, api)

    player.start_lyrics()  # must not raise

    assert pushed == []
    assert state.PROP_LYRIC_HAS not in FakeWindow.store


def test_unexpected_errors_never_break_playback(monkeypatch):
    api = LyricsApi(error=ValueError("bug"))
    player, pushed = make_player(monkeypatch, api)

    player.start_lyrics()  # must not raise

    assert pushed == []
