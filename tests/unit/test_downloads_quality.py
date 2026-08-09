"""L1 units for the conditional download-quality decision (plan W3.1/W3.2)."""

import pytest

from kofin.core.http import JellyfinError
from kofin.downloads import quality
from tests.unit.fakes import FakeAddon


@pytest.fixture(autouse=True)
def env(monkeypatch):
    FakeAddon.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)


class FakeApi:
    server = "http://s"

    def __init__(self, info=None, error=None):
        self._info = info or {}
        self._error = error
        self.calls = []

    def playback_info(self, item_id, profile, **kwargs):
        self.calls.append((item_id, profile))
        if self._error is not None:
            raise self._error
        return self._info


MOVIE = {"Id": "m1", "Type": "Movie"}
SONG = {"Id": "a1", "Type": "Audio"}


def test_toggles_off_answer_original_without_a_round_trip():
    api = FakeApi()
    assert quality.decide(api, MOVIE) == quality.Decision(quality.ORIGINAL)
    assert quality.decide(api, SONG) == quality.Decision(quality.ORIGINAL)
    assert api.calls == []


def test_wanted_is_per_medium():
    FakeAddon.store = {"downloadsTranscode": "true"}
    assert quality.wanted("Movie") and quality.wanted("Episode")
    assert not quality.wanted("Audio")  # the music toggle is its own
    FakeAddon.store = {"downloadsMusicTranscode": "true"}
    assert quality.wanted("Audio") and not quality.wanted("Movie")
    assert not quality.wanted("Series")  # containers expand before deciding


def test_within_limits_stays_original():
    FakeAddon.store = {"downloadsTranscode": "true"}
    api = FakeApi({"MediaSources": [{"SupportsDirectPlay": True}]})
    assert quality.decide(api, MOVIE).kind == quality.ORIGINAL
    # The decision consulted the server with the download profile.
    assert api.calls and api.calls[0][0] == "m1"
    assert api.calls[0][1]["TranscodingProfiles"][0]["Container"] == "mp4"


def test_direct_stream_counts_as_original():
    """All streams copied, container remuxed — but the profile states no
    container constraint and Kodi's demuxer reads anything, so a remux
    would repackage bytes that are fine as they are."""
    FakeAddon.store = {"downloadsTranscode": "true"}
    api = FakeApi(
        {"MediaSources": [{"SupportsDirectStream": True, "TranscodingUrl": "/x"}]}
    )
    assert quality.decide(api, MOVIE).kind == quality.ORIGINAL


def test_over_a_limit_transcodes_with_the_absolute_url():
    FakeAddon.store = {"downloadsTranscode": "true"}
    api = FakeApi(
        {
            "PlaySessionId": "ps1",
            "MediaSources": [
                {
                    "SupportsDirectPlay": False,
                    "TranscodingUrl": "/Videos/m1/stream.mp4?deviceId=d&api_key=k",
                    "TranscodingContainer": "mp4",
                    "TranscodeReasons": "ContainerBitrateExceedsLimit",
                }
            ],
        }
    )
    decision = quality.decide(api, MOVIE)
    assert decision.kind == quality.TRANSCODE
    assert decision.url == "http://s/Videos/m1/stream.mp4?deviceId=d&api_key=k"
    assert decision.container == "mp4"
    assert decision.play_session_id == "ps1"


def test_an_hls_answer_is_rewritten_to_the_progressive_shape():
    assert (
        quality.progressive("/Videos/m1/master.m3u8?a=1") == "/Videos/m1/stream.mp4?a=1"
    )
    assert (
        quality.progressive("/Videos/m1/main.m3u8?a=1") == "/Videos/m1/stream.mp4?a=1"
    )
    assert quality.progressive("/Videos/m1/stream.mp4?a=1") == (
        "/Videos/m1/stream.mp4?a=1"
    )


def test_music_transcode_carries_the_opus_container():
    FakeAddon.store = {
        "downloadsMusicTranscode": "true",
        "downloadsMusicCodec": "opus",
    }
    api = FakeApi(
        {
            "PlaySessionId": "ps2",
            "MediaSources": [
                {
                    "SupportsDirectPlay": False,
                    "TranscodingUrl": "/Audio/a1/stream?x=1",
                    "TranscodingContainer": "opus",
                }
            ],
        }
    )
    decision = quality.decide(api, SONG)
    assert decision.kind == quality.TRANSCODE
    assert decision.container == "opus"


def test_a_missing_container_falls_back_by_medium():
    FakeAddon.store = {
        "downloadsTranscode": "true",
        "downloadsMusicTranscode": "true",
        "downloadsMusicCodec": "opus",
    }
    answer = {
        "MediaSources": [{"SupportsDirectPlay": False, "TranscodingUrl": "/x?y=1"}]
    }
    assert quality.decide(FakeApi(answer), MOVIE).container == "mp4"
    assert quality.decide(FakeApi(answer), SONG).container == "opus"


def test_unusable_answers_raise_instead_of_downloading_the_original():
    """A failed decision must not silently download an original the caps
    ruled out — a retry is cheaper than a surprise 40 GB file."""
    FakeAddon.store = {"downloadsTranscode": "true"}
    with pytest.raises(JellyfinError):
        quality.decide(FakeApi({"MediaSources": []}), MOVIE)
    with pytest.raises(JellyfinError):
        quality.decide(
            FakeApi({"MediaSources": [{"SupportsDirectPlay": False}]}), MOVIE
        )
    with pytest.raises(JellyfinError):
        quality.decide(FakeApi(error=JellyfinError("boom")), MOVIE)
