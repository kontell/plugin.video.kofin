"""Unit tests for text external subtitle eligibility, naming, and cache."""

import os
import time

import pytest

from kofin.core import subtitles


@pytest.fixture
def subs_root(tmp_path, monkeypatch):
    root = str(tmp_path / "subs")
    os.makedirs(root, exist_ok=True)
    subtitles.set_subs_root_override(root)
    yield root
    subtitles.set_subs_root_override(None)


def test_subtitle_filename_language_and_codec():
    assert (
        subtitles.subtitle_filename({"Index": 3, "Language": "eng", "Codec": "srt"})
        == "03.eng.srt"
    )
    assert (
        subtitles.subtitle_filename({"Index": 5, "Language": "swe", "Codec": "ass"})
        == "05.swe.ass"
    )


def test_subrip_codec_is_eligible_as_srt():
    """Jellyfin reports Codec=subrip for sidecar SRT files (not 'srt')."""
    stream = {
        "Type": "Subtitle",
        "Index": 0,
        "Codec": "subrip",
        "Language": "eng",
        "IsExternal": True,
        "IsTextSubtitleStream": True,
        "DeliveryMethod": "External",
        "DeliveryUrl": "/Videos/id/id/Subtitles/0/0/Stream.srt?ApiKey=x",
        "SupportsExternalStream": True,
    }
    assert subtitles.normalize_codec(stream) == "srt"
    assert subtitles.is_text_subtitle_eligible(stream)
    assert subtitles.subtitle_filename(stream) == "00.eng.srt"


def test_text_flag_rescues_unknown_codec():
    stream = {
        "Type": "Subtitle",
        "Index": 1,
        "Codec": "weirdtext",
        "IsTextSubtitleStream": True,
        "DeliveryMethod": "External",
        "DeliveryUrl": "/subs/1.bin",
    }
    assert subtitles.is_text_subtitle_eligible(stream)
    assert subtitles.subtitle_filename(stream) == "01.und.srt"


def test_subtitle_filename_und_and_webvtt_normalise():
    assert (
        subtitles.subtitle_filename(
            {"Index": 1, "Language": "English", "Codec": "webvtt"}
        )
        == "01.und.vtt"
    )
    assert (
        subtitles.subtitle_filename({"Index": 2, "DeliveryUrl": "/x.srt"})
        == "02.und.srt"
    )


def test_eligible_requires_external_text_codec():
    source = {
        "MediaStreams": [
            {
                "Type": "Subtitle",
                "Index": 1,
                "Codec": "srt",
                "DeliveryMethod": "External",
                "DeliveryUrl": "/subs/1.srt",
                "IsExternal": False,  # extractable text — now eligible
            },
            {
                "Type": "Subtitle",
                "Index": 2,
                "Codec": "pgs",
                "DeliveryMethod": "External",
                "DeliveryUrl": "/subs/2.sup",
            },
            {
                "Type": "Subtitle",
                "Index": 3,
                "Codec": "srt",
                "IsExternal": True,
                "DeliveryUrl": "/subs/3.srt",
                # no DeliveryMethod
            },
            {
                "Type": "Audio",
                "Index": 0,
                "DeliveryUrl": "/nope",
            },
        ]
    }
    eligible = subtitles.eligible_text_subtitles(source)
    assert [s["Index"] for s in eligible] == [1]


def test_supports_external_stream_false_skips():
    stream = {
        "Type": "Subtitle",
        "Index": 1,
        "Codec": "srt",
        "DeliveryMethod": "External",
        "DeliveryUrl": "/s.srt",
        "SupportsExternalStream": False,
    }
    assert not subtitles.is_text_subtitle_eligible(stream)


def test_external_subtitle_urls():
    source = {
        "MediaStreams": [
            {
                "Type": "Subtitle",
                "Index": 1,
                "Codec": "srt",
                "DeliveryMethod": "External",
                "DeliveryUrl": "/subs/1.srt",
            }
        ]
    }
    assert subtitles.external_subtitle_urls("http://s:8096", source) == [
        "http://s:8096/subs/1.srt"
    ]


def test_materialize_writes_labelled_files(subs_root):
    source = {
        "MediaStreams": [
            {
                "Type": "Subtitle",
                "Index": 3,
                "Language": "eng",
                "Codec": "srt",
                "DeliveryMethod": "External",
                "DeliveryUrl": "/subs/1.srt",
            },
            {
                "Type": "Subtitle",
                "Index": 5,
                "Language": "swe",
                "Codec": "ass",
                "DeliveryMethod": "External",
                "DeliveryUrl": "/subs/2.ass",
            },
        ]
    }

    def download(url):
        if url.endswith("1.srt"):
            return b"1\n00:00:01,000 --> 00:00:02,000\nHi\n"
        if url.endswith("2.ass"):
            return b"[Script Info]\n"
        return None

    paths, order, local = subtitles.materialize_text_subs(
        "http://s:8096", source, "ps-abc", download
    )
    assert order == [3, 5]
    assert len(paths) == 2
    assert paths == local
    assert all(os.path.isfile(p) for p in paths)
    assert paths[0].endswith("03.eng.srt")
    assert paths[1].endswith("05.swe.ass")
    with open(paths[0], "rb") as handle:
        assert b"Hi" in handle.read()


def test_materialize_falls_back_to_url_on_download_failure(subs_root):
    source = {
        "MediaStreams": [
            {
                "Type": "Subtitle",
                "Index": 1,
                "Language": "eng",
                "Codec": "srt",
                "DeliveryMethod": "External",
                "DeliveryUrl": "/subs/1.srt",
            }
        ]
    }
    paths, order, local = subtitles.materialize_text_subs(
        "http://s:8096", source, "ps1", lambda url: None
    )
    assert order == [1]
    assert paths == ["http://s:8096/subs/1.srt"]
    assert local == []


def test_cleanup_and_reap_and_wipe(subs_root):
    session = os.path.join(subs_root, "ps-old")
    os.makedirs(session)
    marker = os.path.join(session, "01.eng.srt")
    with open(marker, "w") as handle:
        handle.write("x")
    # Age the dir so reaper removes it.
    old = time.time() - 48 * 3600
    os.utime(session, (old, old))

    assert subtitles.reap_old_subs(max_age_seconds=24 * 3600) == 1
    assert not os.path.isdir(session)

    fresh = os.path.join(subs_root, "ps-new")
    os.makedirs(fresh)
    subtitles.cleanup_session_subs("ps-new")
    assert not os.path.isdir(fresh)

    leftover = os.path.join(subs_root, "ps-left")
    os.makedirs(leftover)
    subtitles.wipe_subs_cache()
    assert not os.path.isdir(subs_root)


def test_play_state_subtitle_fields_provisional():
    fields = subtitles.play_state_subtitle_fields([3, 5], ["/tmp/03.eng.srt"])
    assert fields["SubsAttachOrder"] == [3, 5]
    assert fields["SubsPaths"] == ["/tmp/03.eng.srt"]
    assert fields["SubsMapping"] == {}
    assert fields["SubsMappingReady"] is False
