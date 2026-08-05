"""Jellyfin stream index <-> Kodi ordinal, and the stream-menu model.

The layout under test throughout is the one measured live (plan §2.8): one
video, two audio, three subtitles of which the last is image-based, plus a
sidecar subtitle file the server found beside the media.
"""

import pytest

from kofin.core import streams

SERVER = "http://s:8096"


def source(*media_streams):
    return {"MediaStreams": list(media_streams)}


def audio(index, **extra):
    return dict({"Index": index, "Type": "Audio", "Codec": "ac3"}, **extra)


def text_sub(index, **extra):
    return dict(
        {
            "Index": index,
            "Type": "Subtitle",
            "Codec": "subrip",
            "IsTextSubtitleStream": True,
            "IsExternal": False,
            "DeliveryMethod": "External",
            "DeliveryUrl": "/subs/%d.srt" % index,
        },
        **extra,
    )


def image_sub(index, **extra):
    return dict(
        {
            "Index": index,
            "Type": "Subtitle",
            "Codec": "PGSSUB",
            "IsTextSubtitleStream": False,
            "IsExternal": False,
            "DeliveryMethod": "External",
            "DeliveryUrl": "/subs/%d.pgssub" % index,
        },
        **extra,
    )


LAYOUT = source(
    audio(1, IsDefault=True),
    audio(2),
    text_sub(3),
    text_sub(4),
    image_sub(5),
    text_sub(6, IsExternal=True),  # sidecar file beside the media
)
SUMMARY = streams.summarize(LAYOUT)


# -- summarize ---------------------------------------------------------------


def test_summarize_drops_video_and_keeps_only_menu_fields():
    summary = streams.summarize(
        source({"Index": 0, "Type": "Video", "Codec": "h264"}, audio(1))
    )
    assert [stream["Type"] for stream in summary] == ["Audio"]
    assert set(summary[0]) == {
        "Index",
        "Type",
        "Codec",
        "Language",
        "DisplayTitle",
        "IsDefault",
        "IsForced",
        "IsExternal",
        "IsTextSubtitleStream",
        "DeliveryMethod",
    }


# -- attached_subtitles ------------------------------------------------------


def test_transcode_attaches_embedded_text_and_the_sidecar_not_the_image():
    # A transcode carries no subtitles at all, so the embedded text ones have
    # to arrive as files; the image one cannot (a raw .sup Kodi will not draw).
    assert streams.attached_subtitles(SERVER, LAYOUT, "Transcode") == [
        (3, SERVER + "/subs/3.srt"),
        (4, SERVER + "/subs/4.srt"),
        (6, SERVER + "/subs/6.srt"),
    ]


def test_direct_play_attaches_only_the_sidecar():
    # The container already holds 3, 4 and 5. Attaching them again would list
    # every track twice.
    for method in ("DirectPlay", "DirectStream"):
        assert streams.attached_subtitles(SERVER, LAYOUT, method) == [
            (6, SERVER + "/subs/6.srt")
        ]


def test_attach_skips_streams_the_server_will_not_deliver():
    layout = source(
        text_sub(3, DeliveryMethod="Encode"),
        text_sub(4, DeliveryUrl=None),
        {"Type": "Audio", "Index": 1, "DeliveryUrl": "/nope"},
    )
    assert streams.attached_subtitles(SERVER, layout, "Transcode") == []


# -- ordinals ----------------------------------------------------------------


def test_audio_ordinal_is_the_position_within_its_kind():
    assert streams.audio_ordinal(SUMMARY, 1) == 0
    assert streams.audio_ordinal(SUMMARY, 2) == 1
    assert streams.audio_ordinal(SUMMARY, 99) is None
    assert streams.audio_ordinal(SUMMARY, None) is None


def test_subtitle_ordinal_on_a_transcode_is_the_attached_order():
    attached = [3, 4, 6]
    assert streams.subtitle_ordinal(SUMMARY, 3, attached, "Transcode") == 0
    assert streams.subtitle_ordinal(SUMMARY, 4, attached, "Transcode") == 1
    assert streams.subtitle_ordinal(SUMMARY, 6, attached, "Transcode") == 2
    # Burned in rather than attached: no Kodi track of its own.
    assert streams.subtitle_ordinal(SUMMARY, 5, attached, "Transcode") is None


def test_subtitle_ordinal_on_direct_play_puts_attached_after_embedded():
    attached = [6]
    assert streams.subtitle_ordinal(SUMMARY, 3, attached, "DirectStream") == 0
    assert streams.subtitle_ordinal(SUMMARY, 4, attached, "DirectStream") == 1
    assert streams.subtitle_ordinal(SUMMARY, 5, attached, "DirectStream") == 2
    assert streams.subtitle_ordinal(SUMMARY, 6, attached, "DirectStream") == 3


def test_subtitle_ordinal_excludes_sidecars_from_the_embedded_count():
    # A sidecar occupies a Jellyfin index but is not in the container, so it
    # must not push the embedded tracks along.
    layout = streams.summarize(
        source(text_sub(3, IsExternal=True), text_sub(4), text_sub(5))
    )
    assert streams.subtitle_ordinal(layout, 4, [3], "DirectStream") == 0
    assert streams.subtitle_ordinal(layout, 5, [3], "DirectStream") == 1
    assert streams.subtitle_ordinal(layout, 3, [3], "DirectStream") == 2


def test_subtitle_ordinal_of_nothing():
    assert streams.subtitle_ordinal(SUMMARY, None, [3], "Transcode") is None


# -- image subtitles and restarts --------------------------------------------


def test_is_image_subtitle_prefers_the_flag_and_falls_back_to_the_codec():
    assert streams.is_image_subtitle({"IsTextSubtitleStream": False})
    assert not streams.is_image_subtitle({"IsTextSubtitleStream": True})
    # Older payloads omit the flag entirely.
    assert streams.is_image_subtitle({"Codec": "PGSSUB"})
    assert streams.is_image_subtitle({"Codec": "dvdsub"})
    assert not streams.is_image_subtitle({"Codec": "subrip"})


def test_only_an_image_subtitle_on_a_transcode_needs_a_restart():
    assert streams.needs_restart(image_sub(5), "Transcode")
    assert not streams.needs_restart(text_sub(3), "Transcode")
    # On direct play it is in the container; Kodi just draws it.
    assert not streams.needs_restart(image_sub(5), "DirectStream")


# -- selectable / offer ------------------------------------------------------


def test_selectable_on_direct_play_is_everything():
    assert len(streams.selectable_subtitles(SUMMARY, [6], "DirectStream")) == 4


def test_selectable_on_a_transcode_is_attached_plus_burnable():
    selectable = streams.selectable_subtitles(SUMMARY, [3, 4, 6], "Transcode")
    assert [stream["Index"] for stream in selectable] == [3, 4, 5, 6]


def test_selectable_drops_a_transcode_text_track_that_did_not_attach():
    # Nothing would happen if it were picked, so it is not offered.
    selectable = streams.selectable_subtitles(SUMMARY, [3], "Transcode")
    assert [stream["Index"] for stream in selectable] == [3, 5]


@pytest.mark.parametrize(
    "media_streams,attached,method,expected",
    [
        (SUMMARY, [3, 4, 6], "Transcode", streams.OFFER_BOTH),
        # One audio track is not a choice.
        (
            streams.summarize(source(audio(1), text_sub(3))),
            [3],
            "Transcode",
            streams.OFFER_SUBTITLE,
        ),
        (
            streams.summarize(source(audio(1), audio(2))),
            [],
            "Transcode",
            streams.OFFER_AUDIO,
        ),
        (streams.summarize(source(audio(1))), [], "Transcode", streams.OFFER_NONE),
        # Text subtitles that never attached leave nothing to show.
        (
            streams.summarize(source(audio(1), text_sub(3))),
            [],
            "Transcode",
            streams.OFFER_NONE,
        ),
    ],
)
def test_menu_offer_matrix(media_streams, attached, method, expected):
    assert streams.menu_offer(media_streams, attached, method) == expected


# -- labels ------------------------------------------------------------------


def test_label_prefers_the_servers_own_wording(monkeypatch):
    import xbmc

    monkeypatch.setattr(xbmc, "getLocalizedString", lambda i: "[active]")
    assert (
        streams.label_for({"DisplayTitle": "English - AC3"}, False) == "English - AC3"
    )
    assert (
        streams.label_for({"DisplayTitle": "English - AC3"}, True)
        == "English - AC3 [active]"
    )


def test_label_falls_back_through_language_to_codec(monkeypatch):
    import xbmc

    monkeypatch.setattr(xbmc, "getLocalizedString", lambda i: "[active]")
    assert streams.label_for({"Language": "eng", "Codec": "ac3"}, False) == "eng"
    assert streams.label_for({"Codec": "ac3"}, False) == "ac3"
    assert streams.label_for({"Type": "Audio"}, False) == "Audio"


# -- what is actually playing ------------------------------------------------


def test_audio_index_at_inverts_the_ordinal():
    assert streams.audio_index_at(SUMMARY, 0) == 1
    assert streams.audio_index_at(SUMMARY, 1) == 2
    assert streams.audio_index_at(SUMMARY, 2) is None  # past the end
    assert streams.audio_index_at(SUMMARY, -1) is None
    assert streams.audio_index_at(SUMMARY, None) is None


def test_a_burned_in_subtitle_is_the_selected_encode_one():
    """A burn profile flips *every* image subtitle to Encode, not only the one
    asked for (measured against 10.11) — so Encode alone is a set of
    candidates, and the index the playback was resolved with is what says which
    of them is on screen."""
    burned = streams.summarize(
        source(
            audio(1),
            text_sub(3),
            image_sub(5, DeliveryMethod="Encode"),
            image_sub(7, DeliveryMethod="Encode"),
        )
    )
    assert streams.burned_subtitle(burned, 5, "Transcode") is True
    # The text one is still delivered as a file, Encode neighbours or not.
    assert streams.burned_subtitle(burned, 3, "Transcode") is False
    # 7 is a candidate and would answer True if it were the selection; only
    # one index can be, which is the whole point of asking with it.
    assert streams.burned_subtitle(burned, 7, "Transcode") is True


def test_nothing_is_burned_in_on_a_direct_play():
    """The stream is the file; the server encoded nothing for it."""
    assert streams.burned_subtitle(SUMMARY, 5, "DirectPlay") is False
    assert streams.burned_subtitle(SUMMARY, 5, "DirectStream") is False


def test_burned_subtitle_of_nothing():
    assert streams.burned_subtitle(SUMMARY, None, "Transcode") is False
    assert streams.burned_subtitle(SUMMARY, 99, "Transcode") is False
