"""Jellyfin stream index <-> Kodi ordinal, and the stream-menu model.

The layout under test throughout is the one measured live (plan §2.8): one
video, two audio, three subtitles of which the last is image-based, plus a
sidecar subtitle file the server found beside the media.
"""

import pytest

from kofin.core import streams

SERVER = "http://s:8096"


def source(*media_streams, selected=None):
    """A MediaSource. ``selected`` is PlaybackInfo's
    DefaultSubtitleStreamIndex — the track this playback was resolved to
    show, and on a transcode the only embedded one that gets attached."""
    layout = {"MediaStreams": list(media_streams)}
    if selected is not None:
        layout["DefaultSubtitleStreamIndex"] = selected
    return layout


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
    selected=3,  # what the server resolved this playback to show
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


def test_transcode_attaches_the_resolved_track_and_the_sidecar_only():
    """A transcode carries no subtitles, so the resolved one has to arrive as
    a file — but only that one.

    Kodi opens every attached subtitle while building the demuxer, not when
    one is picked, and each embedded track is extracted on demand by the
    server. Attaching all of them made a film with several stall for
    20 seconds per track the server could not produce (measured live: one
    ground for 48 s and then answered 400). Track 4 stays reachable through
    the menu, which restarts into it; the image one was never attachable.
    """
    assert [
        (item.stream_index, item.url)
        for item in streams.attached_subtitles(SERVER, LAYOUT, "Transcode")
    ] == [
        (3, SERVER + "/subs/3.srt"),
        (6, SERVER + "/subs/6.srt"),
    ]


def test_the_fallback_prefers_a_forced_track():
    """The server often selects nothing, and a film with twenty tracks and no
    default is exactly the case that used to attach all twenty. Forced first,
    because that is what forced means."""
    layout = source(text_sub(3), text_sub(4, IsForced=True), text_sub(5))
    assert streams.preferred_embedded(layout["MediaStreams"], None, "eng") == 4


def test_the_fallback_then_takes_the_viewers_language():
    layout = source(
        text_sub(3, Language="fre"), text_sub(4, Language="eng"), text_sub(5)
    )
    assert streams.preferred_embedded(layout["MediaStreams"], None, "eng") == 4
    # Case is the server's business, not the viewer's.
    assert streams.preferred_embedded(layout["MediaStreams"], None, "ENG") == 4


def test_the_fallback_guesses_nothing_when_neither_applies():
    """Attaching an arbitrary track would be a guess, and the menu is there."""
    layout = source(text_sub(3, Language="fre"), text_sub(4, Language="ger"))
    assert streams.preferred_embedded(layout["MediaStreams"], None, "eng") is None
    assert streams.preferred_embedded(layout["MediaStreams"], None, "") is None


def test_the_servers_own_choice_beats_the_fallback():
    layout = source(text_sub(3, Language="eng"), text_sub(4, IsForced=True))
    assert streams.preferred_embedded(layout["MediaStreams"], 3, "eng") == 3


def test_the_fallback_never_picks_an_image_or_a_sidecar():
    """An image track cannot be attached at all — Kodi will not render a
    standalone PGS and the server hands over a raw dump. A sidecar is already
    attached by its own rule."""
    layout = source(
        image_sub(3, IsForced=True),
        text_sub(4, IsExternal=True, Language="eng"),
        text_sub(5, Language="ger"),
    )
    assert streams.preferred_embedded(layout["MediaStreams"], None, "eng") is None


def test_the_fallback_reaches_attachment(monkeypatch):
    """End to end: no server choice, a forced track, one attachment."""
    layout = source(text_sub(3), text_sub(4, IsForced=True), image_sub(5))
    attached = streams.attached_subtitles(SERVER, layout, "Transcode", "eng")
    assert [item.stream_index for item in attached] == [4]


def test_transcode_attaches_no_embedded_track_when_nothing_fits():
    """Nothing chosen, nothing forced, nothing in the viewer's language: no
    extraction at all. The sidecar still rides, and the menu still offers
    every track."""
    layout = source(
        text_sub(3, Language="fre"),
        text_sub(4, Language="ger"),
        text_sub(6, IsExternal=True),
    )
    assert [
        item.stream_index
        for item in streams.attached_subtitles(SERVER, layout, "Transcode", "eng")
    ] == [6]


def test_direct_play_attaches_only_the_sidecar():
    # The container already holds 3, 4 and 5. Attaching them again would list
    # every track twice.
    for method in ("DirectPlay", "DirectStream"):
        assert [
            (item.stream_index, item.url, item.sidecar)
            for item in streams.attached_subtitles(SERVER, LAYOUT, method)
        ] == [(6, SERVER + "/subs/6.srt", True)]


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


def test_subtitle_ordinal_on_direct_play_puts_attached_before_embedded():
    # Kodi registers a ListItem's subtitle files in OpenInputStream, before
    # OpenDemuxStream adds the container's tracks, so the sidecar leads and
    # every embedded track starts one along.
    attached = [6]
    assert streams.subtitle_ordinal(SUMMARY, 6, attached, "DirectStream") == 0
    assert streams.subtitle_ordinal(SUMMARY, 3, attached, "DirectStream") == 1
    assert streams.subtitle_ordinal(SUMMARY, 4, attached, "DirectStream") == 2
    assert streams.subtitle_ordinal(SUMMARY, 5, attached, "DirectStream") == 3


def test_subtitle_ordinal_counts_embedded_in_container_order_not_jellyfin():
    # The sidecar's own Jellyfin index is lower than both embedded ones here,
    # but it is not in the container: the embedded pair keeps its container
    # order behind the attached sidecar rather than being renumbered by index.
    layout = streams.summarize(
        source(text_sub(3, IsExternal=True), text_sub(4), text_sub(5))
    )
    assert streams.subtitle_ordinal(layout, 3, [3], "DirectStream") == 0
    assert streams.subtitle_ordinal(layout, 4, [3], "DirectStream") == 1
    assert streams.subtitle_ordinal(layout, 5, [3], "DirectStream") == 2


def test_subtitle_ordinal_direct_play_with_no_sidecar_is_the_container_order():
    layout = streams.summarize(source(text_sub(3), text_sub(4), text_sub(5)))
    assert streams.subtitle_ordinal(layout, 3, [], "DirectStream") == 0
    assert streams.subtitle_ordinal(layout, 4, [], "DirectStream") == 1
    assert streams.subtitle_ordinal(layout, 5, [], "DirectStream") == 2


def test_subtitle_ordinal_regression_12_angry_men():
    """The live case: one sidecar plus 20 embedded PGS tracks.

    Measured on Piers via Player.GetProperties — Kodi listed the sidecar at 0,
    Korean at 12 and Norwegian at 13. Asking for Jellyfin 17 (Norwegian) used
    to return 12, so the viewer got Korean.
    """
    langs = "eng zho zho dan nld fin fra deu isl ita jpn kor nor pol por por spa spa swe jpn"
    layout = streams.summarize(
        source(
            text_sub(0, IsExternal=True, Language="eng"),
            {"Index": 1, "Type": "Video", "Codec": "h265"},
            audio(2),
            audio(3),
            audio(4),
            *(
                image_sub(5 + offset, Language=language)
                for offset, language in enumerate(langs.split())
            ),
        )
    )
    assert streams.subtitle_ordinal(layout, 0, [0], "DirectStream") == 0
    assert streams.subtitle_ordinal(layout, 16, [0], "DirectStream") == 12  # kor
    assert streams.subtitle_ordinal(layout, 17, [0], "DirectStream") == 13  # nor


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


def test_selectable_on_a_transcode_is_every_track():
    """Only one embedded track is attached now, so the others are reached by
    restarting into them — an answer, and one the menu labels. Hiding them
    would leave the viewer with no way to that subtitle at all."""
    selectable = streams.selectable_subtitles(SUMMARY, [3, 6], "Transcode")
    assert [stream["Index"] for stream in selectable] == [3, 4, 5, 6]


def test_a_transcode_text_track_that_is_not_attached_costs_a_restart():
    # 3 is attached and switches in place; 4 is not and needs a new stream;
    # 5 is an image track, which can only be burned in.
    assert not streams.needs_restart(text_sub(3), "Transcode", [3, 6])
    assert streams.needs_restart(text_sub(4), "Transcode", [3, 6])
    assert streams.needs_restart(image_sub(5), "Transcode", [3, 6])
    # Direct play holds every track in the container: never a restart.
    assert not streams.needs_restart(text_sub(4), "DirectStream", [6])


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
        # A text subtitle that is not attached is still offered: picking it
        # restarts into a stream that has it, which is a real answer. Before
        # only one embedded track was attached, an unattached one could do
        # nothing and was hidden.
        (
            streams.summarize(source(audio(1), text_sub(3))),
            [],
            "Transcode",
            streams.OFFER_SUBTITLE,
        ),
        # Nothing to offer means no subtitle streams at all.
        (
            streams.summarize(source(audio(1), audio(2), text_sub(3))),
            [],
            "DirectStream",
            streams.OFFER_BOTH,
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


def test_an_attachment_carries_what_it_takes_to_name_it():
    """Kodi reads a subtitle's language off its filename, and Jellyfin's route
    cannot carry one -- so the naming fields ride along with the URL."""
    layout = source(
        text_sub(3, IsExternal=True, Language="ger", Title="Kommentar", IsForced=True)
    )
    (item,) = streams.attached_subtitles(SERVER, layout, "DirectStream")
    assert item.sidecar is True
    assert (item.language, item.title, item.forced) == ("ger", "Kommentar", True)


def test_an_embedded_attachment_is_not_a_sidecar():
    embedded, sidecar = streams.attached_subtitles(
        SERVER,
        source(text_sub(3), text_sub(6, IsExternal=True), selected=3),
        "Transcode",
    )
    assert embedded.sidecar is False and sidecar.sidecar is True
