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


def test_targets_never_exceed_the_source_rates():
    """Measured live (G11): a codec-forced transcode of a 0.9 Mbit/s file
    under a 3 Mbit/s cap came back 2.9 Mbit/s — 3.5x the bytes for
    nothing. The URL's targets are spliced down to the source's own rates;
    equality keeps a planned stream copy legal (the copy gates on >=)."""
    source = {
        "MediaStreams": [
            {"Type": "Video", "BitRate": 760_000},
            {"Type": "Audio", "BitRate": 96_000},
        ]
    }
    url = "/v/stream.mp4?deviceId=d&VideoBitrate=2712000&AudioBitrate=288000&x=1"
    capped = quality.cap_bitrates_to_source(url, source)
    assert "VideoBitrate=760000" in capped
    assert "AudioBitrate=96000" in capped
    assert capped.endswith("&x=1") and "deviceId=d" in capped

    # Already below the source: left exactly alone (a bitrate-forced
    # transcode is *meant* to be smaller).
    low = "/v/stream.mp4?VideoBitrate=500000&AudioBitrate=64000"
    assert quality.cap_bitrates_to_source(low, source) == low

    # No bitrate params, unknown source rates: nothing to do.
    bare = "/v/stream.mp4?deviceId=d"
    assert quality.cap_bitrates_to_source(bare, source) == bare
    assert quality.cap_bitrates_to_source(url, {"MediaStreams": []}) == url


def test_decide_caps_the_transcode_url():
    FakeAddon.store = {"downloadsTranscode": "true"}
    api = FakeApi(
        {
            "PlaySessionId": "ps1",
            "MediaSources": [
                {
                    "SupportsDirectPlay": False,
                    "TranscodingUrl": "/v/stream.mp4?VideoBitrate=2712000",
                    "TranscodingContainer": "mp4",
                    "MediaStreams": [{"Type": "Video", "BitRate": 760_000}],
                }
            ],
        }
    )
    assert "VideoBitrate=760000" in quality.decide(api, MOVIE).url


def test_estimated_bytes_from_the_url_targets():
    url = "/v/stream.mp4?VideoBitrate=760000&AudioBitrate=96000&x=1"
    ticks = 100 * 10_000_000  # 100 s
    assert quality.estimated_bytes(url, ticks) == (760_000 + 96_000) * 100 // 8
    assert quality.estimated_bytes("/v/stream.mp4?x=1", ticks) == 0  # no targets
    assert quality.estimated_bytes(url, 0) == 0  # no runtime
