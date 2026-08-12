"""L1: the segment engine — boundary-crossing timing, the Play Next decision
matrix, overlay lifetime, and the near-end prompt (phase-3 plan §5 steps 2-3).

The engine state is driven directly through ``segment_tick`` with an injected
clock — no checker thread, no real dialogs.
"""

import threading

import pytest

from kofin.service import player as player_mod
from kofin.service.player import (
    MIN_PROMPT_SECONDS,
    SEEK_RETRIES,
    SEEK_SETTLE_TICKS,
    Player,
    crossed_into,
    format_span,
    near_end_prompt_at,
    next_episode_label,
    plan_for_crossing,
    prompt_hide_at,
    safe_seek_end,
    segments_entered_at,
)
from kofin.service.segments import deconflict, parse_segments
from tests.unit.fakes import FakeAddon, FakeWindow

SETTINGS_ON = {
    "mediaSegmentsEnabled": "true",
    "skipIntroductionMode": "1",
    "skipCreditsMode": "2",
    "skipRecapMode": "2",
    "skipPreviewMode": "0",
    "skipCommercialMode": "1",
    "playNextEnabled": "true",
    "playNextLeadTime": "30",
    # 100% keeps a prompt up for its whole segment, which is what the overlay
    # lifetime cases below are about; the shipped 50% default and its floor get
    # their own tests rather than perturbing every other assertion.
    "skipPromptHidePercent": "100",
    "playNextAutoplay": "false",
}

NEXT_EPISODE = {
    "Id": "ep2",
    "Name": "The Next One",
    "ParentIndexNumber": 2,
    "IndexNumber": 5,
}


def seg(segment_type, start, end):
    return {"Type": segment_type, "Start": float(start), "End": float(end)}


class SegmentsApi:
    """The Api slice the segment engine touches."""

    def __init__(self):
        self.segments_response = {"Items": []}
        self.adjacent_response = {"Items": []}
        self.fail_segments = 0
        self.fail_adjacent = False
        self.calls = []

    def media_segments(self, item_id):
        self.calls.append(("media_segments", item_id))
        if self.fail_segments > 0:
            self.fail_segments -= 1
            raise RuntimeError("segments down")
        return self.segments_response

    def adjacent_episodes(self, series_id, item_id):
        self.calls.append(("adjacent", series_id, item_id))
        if self.fail_adjacent:
            raise RuntimeError("adjacent down")
        return self.adjacent_response

    def session_playing(self, data):
        pass

    def session_progress(self, data):
        pass

    def session_stopped(self, data):
        pass

    def close_transcode(self, device_id, play_session_id):
        pass


class FakeOverlay:
    def __init__(
        self, skip_label, skip_duration, next_label, next_info, on_skip, on_play_next
    ):
        self.skip_label = skip_label
        self.skip_duration = skip_duration
        self.next_label = next_label
        self.next_info = next_info
        self.on_skip = on_skip
        self.on_play_next = on_play_next
        self.closed = False
        self.countdowns = []

    def set_countdown(self, seconds):
        self.countdowns.append(seconds)

    def close(self):
        self.closed = True


class Engine:
    """A Player wired to fakes, driven by an explicit clock."""

    def __init__(self, monkeypatch):
        self.api = SegmentsApi()
        self.player = Player(self.api)
        self.now = 0.0
        self.total = 1500.0
        self.seeks = []
        self.overlays = []
        self.builtins = []

        monkeypatch.setattr(self.player, "getTime", lambda: self.now)
        monkeypatch.setattr(self.player, "getTotalTime", lambda: self.total)
        monkeypatch.setattr(self.player, "seekTime", self.seeks.append)
        monkeypatch.setattr("xbmc.executebuiltin", self.builtins.append)

        def fake_open_overlay(
            skip_label, skip_duration, next_label, next_info, on_skip, on_play_next
        ):
            overlay = FakeOverlay(
                skip_label, skip_duration, next_label, next_info, on_skip, on_play_next
            )
            self.overlays.append(overlay)
            return overlay

        import kofin.plugin.skip as skip_mod

        monkeypatch.setattr(skip_mod, "open_overlay", fake_open_overlay)

    def arm(self, segments, item_type="Episode", next_episode=None, runtime=None):
        if runtime is not None:
            self.total = runtime
        self.player._item = {
            "Id": "ep1",
            "Type": item_type,
            "SeriesId": "show1",
            "Runtime": int(self.total * 10_000_000),
            "CurrentPosition": 0.0,
            "MediaSourceId": "src1",
            "PlaySessionId": "ps1",
            "PlayMethod": "DirectStream",
        }
        self.player._segments = sorted(segments, key=lambda s: s["Start"])
        self.player._segments_loaded = True
        self.player._next_episode = next_episode

    def tick(self, at):
        self.now = at
        self.player.segment_tick()

    @property
    def overlay(self):
        return self.overlays[-1] if self.overlays else None


class SegmentsFakeAddon(FakeAddon):
    """FakeAddon with real formatting for the strings the engine %-formats."""

    FORMATTED = {30488: "Skipped %s", 30489: "Up next: %s"}

    def getLocalizedString(self, string_id: int) -> str:
        return self.FORMATTED.get(string_id, "string-%d" % string_id)


@pytest.fixture(autouse=True)
def kodi_env(monkeypatch):
    FakeWindow.store = {}
    FakeAddon.store = dict(SETTINGS_ON)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    monkeypatch.setattr("xbmcaddon.Addon", SegmentsFakeAddon)


@pytest.fixture
def engine(monkeypatch):
    return Engine(monkeypatch)


# --- pure helpers ------------------------------------------------------------


def test_parse_segments_maps_sorts_and_drops():
    response = {
        "Items": [
            {"Type": "Outro", "StartTicks": 14_000_000_000, "EndTicks": 14_700_000_000},
            {"Type": "Intro", "StartTicks": 100_000_000, "EndTicks": 400_000_000},
            {"Type": "Chapter", "StartTicks": 0, "EndTicks": 10_000_000},
            {"Type": "Recap", "StartTicks": 50_000_000, "EndTicks": 50_000_000},
        ]
    }
    segments = parse_segments(response)
    assert [s["Type"] for s in segments] == ["Introduction", "Credits"]
    assert segments[0]["Start"] == 10.0
    assert segments[0]["End"] == 40.0
    assert parse_segments(None) == []
    assert parse_segments({}) == []


def test_parse_segments_deconflicts_a_second_provider():
    # Intro Skipper and the Chapter Segments plugin both analysed this episode
    # and the server returned the union: same type, overlapping. First by start
    # wins its run; the later opinion is dropped rather than merged.
    response = {
        "Items": [
            {"Type": "Intro", "StartTicks": 900_000_000, "EndTicks": 1_800_000_000},
            {"Type": "Intro", "StartTicks": 950_000_000, "EndTicks": 1_850_000_000},
        ]
    }
    assert parse_segments(response) == [
        {"Type": "Introduction", "Start": 90.0, "End": 180.0}
    ]


def test_deconflict_keeps_genuinely_separate_breaks():
    # Three commercial breaks are three segments, not one provider disagreeing
    # with another.
    breaks = [seg("Commercial", 100, 220), seg("Commercial", 500, 620)]
    breaks.append(seg("Commercial", 900, 1020))
    assert deconflict(sorted(breaks, key=lambda s: s["Start"])) == breaks


def test_deconflict_leaves_abutting_segments_of_different_types():
    # A recap ending exactly where the intro begins is not a conflict, and
    # cross-type overlap is never collapsed either — only the type is shared.
    segments = [seg("Recap", 0, 60), seg("Introduction", 60, 150)]
    assert deconflict(segments) == segments
    overlapping_types = [seg("Recap", 0, 60), seg("Introduction", 30, 150)]
    assert deconflict(overlapping_types) == overlapping_types


def test_deconflict_chain_keeps_the_ends_that_do_not_overlap():
    # A spans into B, B into C, but C clears A: keeping first-by-start leaves A
    # and C, which do not overlap each other.
    a, b, c = seg("Preview", 0, 60), seg("Preview", 50, 120), seg("Preview", 110, 200)
    assert deconflict([a, b, c]) == [a, c]


def test_deconflict_tolerates_a_second_of_overlap():
    # Under the tolerance is rounding between two views of the same boundary,
    # not a second provider's segment.
    segments = [seg("Commercial", 0, 60), seg("Commercial", 59.5, 120)]
    assert deconflict(segments) == segments


def test_format_span():
    assert format_span(90) == "1:30"
    assert format_span(45) == "0:45"
    assert format_span(0) == "0:00"
    assert format_span(-5) == "0:00"
    assert format_span(3600) == "60:00"


def test_prompt_hide_at_takes_a_share_of_the_segment():
    assert prompt_hide_at(100.0, 200.0, 50.0, 100.0) == 150.0
    # 100% is the old behaviour: up for the whole segment.
    assert prompt_hide_at(100.0, 200.0, 100.0, 100.0) == 200.0


def test_prompt_hide_at_keeps_a_short_prompt_readable():
    # 50% of a 12s intro is 6s, under the floor, so the floor wins...
    assert prompt_hide_at(0.0, 12.0, 50.0, 0.0) == MIN_PROMPT_SECONDS
    # ...but never past the segment end.
    assert prompt_hide_at(0.0, 5.0, 50.0, 0.0) == 5.0


def test_prompt_hide_at_measures_the_floor_from_now():
    # Entered late (a fetch that landed after the boundary): the prompt still
    # gets its full dwell rather than opening already expired.
    assert prompt_hide_at(0.0, 200.0, 50.0, 150.0) == 158.0
    # Not before the share, though, when the share is further out.
    assert prompt_hide_at(0.0, 200.0, 50.0, 20.0) == 100.0


def test_crossed_into_truth_table():
    # Inside the window fires, with or without a previous position.
    assert crossed_into(None, 15.0, 10.0, 40.0)
    assert crossed_into(9.9, 10.0, 10.0, 40.0)
    assert crossed_into(9.9, 40.0, 10.0, 40.0)
    # A stepped-over boundary fires even when the tick landed past the end.
    assert crossed_into(9.9, 40.5, 10.0, 40.0)
    # Ahead of the window: nothing.
    assert not crossed_into(None, 9.9, 10.0, 40.0)
    assert not crossed_into(5.0, 9.9, 10.0, 40.0)
    # Past the window without having crossed it this tick: nothing.
    assert not crossed_into(None, 41.0, 10.0, 40.0)
    assert not crossed_into(40.5, 41.0, 10.0, 40.0)


def test_safe_seek_end_eof_clamp():
    # A segment ending past runtime never seeks past it (margin 1s).
    assert safe_seek_end(1499.5, 1500.0, 1400.0) == 1499.0
    assert safe_seek_end(2000.0, 1500.0, 1400.0) == 1499.0
    # Normal mid-file target passes through.
    assert safe_seek_end(40.0, 1500.0, 12.0) == 40.0
    # Backwards or no-op seeks are refused.
    assert safe_seek_end(40.0, 1500.0, 40.0) is None
    assert safe_seek_end(40.0, 1500.0, 55.0) is None
    # Unknown runtime: target passes through unclamped.
    assert safe_seek_end(40.0, 0.0, 12.0) == 40.0
    # Garbage end values are refused.
    assert safe_seek_end(None, 1500.0, 12.0) is None
    assert safe_seek_end("x", 1500.0, 12.0) is None


def test_near_end_prompt_at_clamps_short_items():
    assert near_end_prompt_at(1500.0, 30.0) == 1470.0
    # Shorter than the lead: clamp so the prompt still appears.
    assert near_end_prompt_at(40.0, 30.0) == 20.0
    assert near_end_prompt_at(1500.0, 0.0) == 1500.0


@pytest.mark.parametrize(
    "segment_type,mode,offer_next,expected",
    [
        # Non-credits types never carry Play Next.
        ("Introduction", 0, True, (False, ())),
        ("Introduction", 1, True, (True, ())),
        ("Introduction", 2, True, (False, ("skip", "close"))),
        ("Recap", 2, False, (False, ("skip", "close"))),
        ("Preview", 1, True, (True, ())),
        ("Commercial", 0, False, (False, ())),
        # Credits: the unified dialog decision.
        ("Credits", 2, True, (False, ("skip", "playnext", "close"))),
        ("Credits", 2, False, (False, ("skip", "close"))),
        ("Credits", 1, True, (True, ("playnext", "close"))),
        ("Credits", 1, False, (True, ())),
        ("Credits", 0, True, (False, ("playnext", "close"))),
        ("Credits", 0, False, (False, ())),
    ],
)
def test_plan_for_crossing_matrix(segment_type, mode, offer_next, expected):
    assert plan_for_crossing(segment_type, mode, offer_next) == expected


def test_next_episode_label():
    assert next_episode_label(NEXT_EPISODE) == "S02E05. The Next One"
    assert next_episode_label({"Name": "Special"}) == "Special"
    assert next_episode_label({"ParentIndexNumber": 1, "IndexNumber": 2}) == "S01E02"


# --- auto-skip timing --------------------------------------------------------


def test_auto_skip_fires_on_boundary_crossing(engine):
    engine.arm([seg("Introduction", 10, 40)])
    engine.tick(9.0)
    engine.tick(9.3)
    assert engine.seeks == []
    engine.tick(10.1)
    assert engine.seeks == [40.0]


def test_auto_skip_settles_against_laggy_position(engine):
    engine.arm([seg("Introduction", 10, 40)])
    engine.tick(9.9)
    engine.tick(10.2)
    assert engine.seeks == [40.0]
    # getTime keeps reporting pre-seek positions: no re-fire.
    engine.tick(10.4)
    engine.tick(10.7)
    assert engine.seeks == [40.0]
    # The seek lands; the engine resyncs and moves on.
    engine.tick(40.1)
    engine.tick(40.4)
    assert engine.seeks == [40.0]
    assert engine.player._armed_index == 1


def test_auto_skip_retries_dropped_seek_and_defers_toast(engine, monkeypatch):
    # An Intro at t~=0 whose first seek the player drops (not yet seekable):
    # the position stays short through the settle window, so the engine must
    # re-issue the seek, and the "Skipped" toast must wait until it lands.
    notifies = []
    monkeypatch.setattr(engine.player, "_notify", notifies.append)
    engine.arm([seg("Introduction", 0, 88)])
    engine.tick(0.0)  # crossing at t=0 -> first (dropped) seek
    assert engine.seeks == [88.0]
    assert notifies == []  # no toast before the skip actually takes
    # Seek dropped: getTime stays short for the whole settle window.
    for i in range(SEEK_SETTLE_TICKS + 1):
        engine.tick(0.5 + i * 0.1)
    assert engine.seeks == [88.0, 88.0]  # window expired short -> re-issued
    assert notifies == []
    engine.tick(88.1)  # the retry lands
    assert notifies == ["Skipped Introduction"]
    assert engine.player._armed_index == 1


def test_auto_skip_gives_up_after_retries_without_false_toast(engine, monkeypatch):
    # A seek that never lands must not report a skip that did not happen: after
    # SEEK_RETRIES re-issues, the engine gives up silently (no toast).
    notifies = []
    monkeypatch.setattr(engine.player, "_notify", notifies.append)
    engine.arm([seg("Introduction", 0, 88)])
    engine.tick(0.0)
    now = 0.5
    for _ in range((SEEK_SETTLE_TICKS + 1) * (SEEK_RETRIES + 2)):
        engine.tick(now)
        now += 0.05  # held well short of the target for every window
    assert notifies == []  # never toasted
    assert engine.seeks == [88.0] * (SEEK_RETRIES + 1)  # initial + retries


def test_fresh_start_ignores_stale_transition_position(engine):
    # Play Next A->B (started from the beginning): B's engine briefly sees A's
    # near-its-end position (1341), which lands inside B's own credits. Because
    # it is far from B's intended start (0), it must not fire a phantom Skip
    # Outro; once playback reaches B the intro arms normally.
    engine.arm([seg("Introduction", 0, 80), seg("Credits", 1300, 1380)])
    engine.player._item["CurrentPosition"] = 0.0  # fromstart
    engine.player._fresh_start = True
    engine.tick(1341.0)  # stale (A's position), inside B's credits
    assert engine.overlay is None and engine.seeks == []  # ignored
    engine.tick(0.4)  # B's real position near its start -> arm; intro auto-skips
    assert engine.seeks == [80.0]
    assert engine.overlay is None  # the stale credits never fired


def test_fresh_start_waits_for_a_resume_seek_to_land(engine):
    """The live failure: Kodi reports 0 while it seeks to the resume point, and
    an intro beginning at 0.0 fired against that phantom zero -- the flash of
    Skip Intro on every resume. The engine has to wait for the real position.
    """
    FakeAddon.store["skipIntroductionMode"] = "2"
    engine.arm([seg("Introduction", 0, 88.4)])
    engine.player._item["CurrentPosition"] = 180.3  # resolved resume position
    engine.player._fresh_start = True
    engine.tick(0.0)  # pre-seek phantom
    engine.tick(0.0)
    assert engine.overlays == []
    engine.tick(178.9)  # the seek landed
    assert engine.overlays == []  # the intro is long behind us
    assert engine.player._fresh_start is False


def test_fresh_start_gives_up_waiting_rather_than_disarming_the_item(engine):
    # A seek that never lands must not cost the whole item its segments.
    FakeAddon.store["skipIntroductionMode"] = "2"
    engine.arm([seg("Introduction", 100, 130)])
    engine.player._item["CurrentPosition"] = 900.0  # never reached
    engine.player._fresh_start = True
    for _ in range(player_mod.FRESH_START_MAX_TICKS):
        engine.tick(0.0)
    assert engine.player._fresh_start is False
    engine.tick(101.0)
    assert engine.overlay is not None  # armed again, offering the skip


def test_resume_into_a_segment_offers_no_skip(engine):
    # Resuming mid-credits is the viewer asking to pick up exactly there. The
    # prompt used to open and auto-close a moment later, which is all they saw.
    engine.arm([seg("Credits", 1300, 1380)], next_episode=None)
    engine.player._item["CurrentPosition"] = 1341.0  # resumed here
    engine.player._fresh_start = True
    engine.tick(1341.0)
    assert engine.overlay is None
    assert engine.seeks == []


def test_resume_into_a_segment_still_auto_skips(engine):
    # Auto mode is "never watch an intro": a resume that lands inside one must
    # not make the setting lapse for that playback.
    FakeAddon.store["skipIntroductionMode"] = "1"
    engine.arm([seg("Introduction", 100, 130)])
    engine.player._item["CurrentPosition"] = 115.0
    engine.player._fresh_start = True
    engine.tick(115.0)
    assert engine.seeks == [130.0]
    assert engine.overlay is None


def test_resume_into_credits_still_offers_play_next(engine):
    # Only the skip button is withheld; the Play Next offer is the whole point
    # of reaching the credits and stands either way.
    engine.arm([seg("Credits", 1300, 1380)], next_episode=NEXT_EPISODE)
    engine.player._item["CurrentPosition"] = 1341.0
    engine.player._fresh_start = True
    engine.tick(1341.0)
    overlay = engine.overlay
    assert overlay is not None
    assert overlay.skip_label == ""
    assert overlay.next_label == "string-30486"


def test_resume_suppression_lifts_once_the_segment_is_left(engine):
    # Seeking back into the segment later is a fresh request for the offer.
    FakeAddon.store["skipIntroductionMode"] = "2"
    engine.arm([seg("Introduction", 100, 130)])
    engine.player._item["CurrentPosition"] = 115.0
    engine.player._fresh_start = True
    engine.tick(115.0)
    assert engine.overlays == []
    engine.tick(131.0)  # leave it
    engine.player.note_seek(105.0)
    engine.tick(105.0)  # and seek back in
    assert len(engine.overlays) == 1
    assert engine.overlays[0].skip_label == "string-30481"


def test_segments_entered_at_excludes_a_position_on_the_start():
    # A playback starting exactly where a segment starts has not entered it --
    # it is about to watch it, which is when the skip offer is worth having.
    segments = [seg("Introduction", 0, 40), seg("Credits", 100, 130)]
    assert segments_entered_at(segments, 0.0) == set()
    assert segments_entered_at(segments, 0.1) == {(0.0, 40.0)}
    assert segments_entered_at(segments, 40.0) == {(0.0, 40.0)}
    assert segments_entered_at(segments, 40.1) == set()
    assert segments_entered_at(segments, 100.0) == set()
    assert segments_entered_at(segments, 130.0) == {(100.0, 130.0)}


def test_playing_from_the_start_still_offers_to_skip_an_intro_at_zero(engine):
    # The suppression must not swallow the ordinary case: intros routinely
    # begin at 0.0, and every from-the-start playback begins there too.
    FakeAddon.store["skipIntroductionMode"] = "2"
    engine.arm([seg("Introduction", 0, 88.4)])
    engine.player._item["CurrentPosition"] = 0.0
    engine.player._fresh_start = True
    engine.tick(0.1)
    overlay = engine.overlay
    assert overlay is not None and overlay.skip_label == "string-30481"


def test_short_segment_stepped_over_still_fires(engine):
    # A credits segment shorter than the tick interval: no tick ever lands
    # inside it, but the crossing (prev < start <= now) still fires. The skip
    # moment has passed — no backwards seek, no skip button — yet the Play
    # Next offer stands.
    engine.arm([seg("Credits", 1400, 1400.2)], next_episode=NEXT_EPISODE)
    engine.tick(1399.9)
    engine.tick(1400.5)
    overlay = engine.overlay
    assert overlay is not None
    assert overlay.next_label == "string-30486"
    assert overlay.skip_label == ""
    assert engine.seeks == []


def test_mode_off_skips_nothing(engine):
    FakeAddon.store["skipIntroductionMode"] = "0"
    engine.arm([seg("Introduction", 10, 40)])
    engine.tick(12.0)
    engine.tick(20.0)
    assert engine.seeks == []
    assert engine.overlays == []


def test_segments_disabled_master_toggle(engine):
    FakeAddon.store["mediaSegmentsEnabled"] = "false"
    engine.arm([seg("Introduction", 10, 40)])
    engine.tick(12.0)
    assert engine.seeks == []
    assert engine.overlays == []


# --- the ask overlay ---------------------------------------------------------


def test_ask_overlay_opens_and_skip_seeks(engine):
    FakeAddon.store["skipIntroductionMode"] = "2"
    engine.arm([seg("Introduction", 10, 40)])
    engine.tick(10.1)
    overlay = engine.overlay
    assert overlay is not None
    assert overlay.skip_label == "string-30481"
    assert overlay.next_label == ""
    overlay.on_skip()
    assert engine.seeks == [40.0]


def test_ask_overlay_autocloses_past_segment_end(engine):
    FakeAddon.store["skipIntroductionMode"] = "2"
    engine.arm([seg("Introduction", 10, 40)])
    engine.tick(10.1)
    overlay = engine.overlay
    engine.tick(39.0)
    assert not overlay.closed
    engine.tick(40.3)
    assert overlay.closed
    assert engine.player._overlay is None


def test_skip_button_carries_the_span_it_would_save(engine):
    FakeAddon.store["skipIntroductionMode"] = "2"
    engine.arm([seg("Introduction", 10, 100)])
    engine.tick(10.1)
    assert engine.overlay.skip_duration == "1:30"


def test_skip_button_span_is_what_is_left_when_entered_late(engine):
    # A segment whose fetch landed after the boundary fires from inside it; the
    # button must promise what pressing it actually saves, not the full span.
    FakeAddon.store["skipIntroductionMode"] = "2"
    engine.arm([seg("Introduction", 10, 100)])
    engine.tick(70.0)
    assert engine.overlay.skip_duration == "0:30"


def test_skip_prompt_hides_part_way_through_the_segment(engine):
    FakeAddon.store["skipIntroductionMode"] = "2"
    FakeAddon.store["skipPromptHidePercent"] = "50"
    engine.arm([seg("Introduction", 100, 200)])
    engine.tick(100.1)
    overlay = engine.overlay
    assert engine.player._overlay_hide_at == 150.0
    engine.tick(149.0)
    assert not overlay.closed
    engine.tick(150.5)
    assert overlay.closed


def test_a_hidden_prompt_does_not_come_back_inside_its_segment(engine):
    # The dedup key is the real segment, not the shortened prompt window, so
    # the rest of the intro plays without the prompt flickering back.
    FakeAddon.store["skipIntroductionMode"] = "2"
    FakeAddon.store["skipPromptHidePercent"] = "50"
    engine.arm([seg("Introduction", 100, 200)])
    engine.tick(100.1)
    engine.tick(150.5)
    engine.tick(160.0)
    engine.tick(190.0)
    assert len(engine.overlays) == 1


def test_a_seek_back_before_the_start_still_reoffers_after_a_hide(engine):
    FakeAddon.store["skipIntroductionMode"] = "2"
    FakeAddon.store["skipPromptHidePercent"] = "50"
    engine.arm([seg("Introduction", 100, 200)])
    engine.tick(100.1)
    engine.tick(150.5)  # hidden, but still inside the segment
    assert len(engine.overlays) == 1
    engine.player.note_seek(90.0)
    engine.tick(90.0)
    engine.tick(100.5)
    assert len(engine.overlays) == 2


def test_a_short_segment_keeps_its_prompt_readable(engine):
    # 50% of a 12s intro is under the floor, so the floor governs.
    FakeAddon.store["skipIntroductionMode"] = "2"
    FakeAddon.store["skipPromptHidePercent"] = "50"
    engine.arm([seg("Introduction", 100, 112)])
    engine.tick(100.0)
    assert engine.player._overlay_hide_at == 100.0 + MIN_PROMPT_SECONDS


def test_a_segment_too_short_to_read_offers_nothing(engine):
    FakeAddon.store["skipIntroductionMode"] = "2"
    engine.arm([seg("Introduction", 100, 102)])
    engine.tick(100.1)
    assert engine.overlays == []


def test_a_segment_too_short_to_seek_is_not_auto_skipped(engine):
    FakeAddon.store["skipIntroductionMode"] = "1"
    engine.arm([seg("Introduction", 100, 100.5)])
    engine.tick(100.1)
    assert engine.seeks == []


def test_recoverable_dedup_reoffers_after_seek_back(engine):
    FakeAddon.store["skipIntroductionMode"] = "2"
    engine.arm([seg("Introduction", 100, 130)])
    engine.player.note_seek(110.0)
    engine.tick(110.0)
    assert len(engine.overlays) == 1
    engine.tick(110.3)
    assert len(engine.overlays) == 1  # no re-nag while inside
    engine.tick(131.0)  # leave the segment: dedup re-arms
    engine.player.note_seek(105.0)
    engine.tick(105.0)
    assert len(engine.overlays) == 2


def test_never_two_overlays_at_once(engine):
    FakeAddon.store["skipIntroductionMode"] = "2"
    engine.arm(
        [seg("Introduction", 10, 40), seg("Credits", 1400, 1470)],
        next_episode=NEXT_EPISODE,
    )
    engine.tick(10.1)
    first = engine.overlays[0]
    engine.player.note_seek(1401.0)
    engine.tick(1401.0)
    assert len(engine.overlays) == 2
    assert first.closed


def test_seek_away_closes_stale_overlay(engine):
    # Seeking out of the credits closes the Play Next overlay instead of
    # leaving it up for the rest of the episode; seeking back in re-offers.
    engine.arm([seg("Credits", 1400, 1470)], next_episode=NEXT_EPISODE)
    engine.tick(1400.2)
    overlay = engine.overlay
    assert overlay is not None
    engine.player.note_seek(100.0)
    engine.tick(100.0)
    assert overlay.closed
    engine.player.note_seek(1401.0)
    engine.tick(1401.0)
    assert len(engine.overlays) == 2


def test_user_seek_echo_of_our_own_skip_is_ignored(engine):
    engine.arm([seg("Introduction", 10, 40)])
    engine.tick(10.1)
    assert engine.player._settle_target == 40.0
    engine.player.note_seek(40.0)  # Kodi's onPlayBackSeek echo of our seek
    assert engine.player._settle_target == 40.0
    assert not engine.player._pending_jump
    engine.player.note_seek(300.0)  # a real user seek re-arms
    assert engine.player._settle_target is None
    assert engine.player._pending_jump


# --- the unified Play Next dialog (S3.3 decision surface) --------------------


def test_credits_ask_with_next_episode_offers_all_three(engine):
    engine.arm([seg("Credits", 1400, 1470)], next_episode=NEXT_EPISODE)
    engine.tick(1400.2)
    overlay = engine.overlay
    assert overlay.skip_label == "string-30482"
    assert overlay.next_label == "string-30486"
    assert overlay.next_info == "Up next: S02E05. The Next One"
    # A Play Next offer stands to the end of the video, not the segment end —
    # this is the autoplay deadline, and it must stay put now that a separate
    # field decides when the window closes.
    assert engine.player._overlay_end == 1500.0
    overlay.on_play_next()
    # Play Next starts the next episode from the beginning, never a resume point.
    assert any(
        "mode=play" in b and "id=ep2" in b and "fromstart=1" in b
        for b in engine.builtins
    )


def test_the_credits_overlay_hides_on_the_deadline_too(engine):
    # The hide deadline applies to the whole overlay, Play Next included.
    FakeAddon.store["skipPromptHidePercent"] = "50"
    engine.arm([seg("Credits", 1400, 1470)], next_episode=NEXT_EPISODE)
    engine.tick(1400.2)
    overlay = engine.overlay
    assert engine.player._overlay_hide_at == 1435.0
    assert engine.player._overlay_end == 1500.0  # autoplay deadline untouched
    engine.tick(1434.0)
    assert not overlay.closed
    engine.tick(1435.5)
    assert overlay.closed


def test_an_armed_autoplay_overlay_is_exempt_from_the_hide(engine):
    # The countdown is the warning; hiding it while still handing over would
    # take the warning away and act anyway.
    FakeAddon.store["skipPromptHidePercent"] = "50"
    FakeAddon.store["playNextAutoplay"] = "true"
    engine.arm([seg("Credits", 1400, 1470)], next_episode=NEXT_EPISODE)
    engine.tick(1400.2)
    overlay = engine.overlay
    assert engine.player._overlay_hide_at == 1500.0
    engine.tick(1436.0)
    assert not overlay.closed
    assert overlay.countdowns[-1] == 64


def test_credits_finale_offers_skip_and_close_only(engine):
    engine.arm([seg("Credits", 1400, 1470)], next_episode=None)
    engine.tick(1400.2)
    overlay = engine.overlay
    assert overlay.skip_label == "string-30482"
    assert overlay.next_label == ""
    assert engine.player._overlay_end == 1470.0


def test_credits_auto_seeks_and_still_offers_play_next(engine):
    FakeAddon.store["skipCreditsMode"] = "1"
    engine.arm([seg("Credits", 1400, 1470)], next_episode=NEXT_EPISODE)
    engine.tick(1400.2)
    assert engine.seeks == [1470.0]
    overlay = engine.overlay
    assert overlay is not None
    assert overlay.skip_label == ""  # already skipped; only Play Next remains
    assert overlay.next_label == "string-30486"


def test_credits_auto_finale_shows_nothing(engine):
    FakeAddon.store["skipCreditsMode"] = "1"
    engine.arm([seg("Credits", 1400, 1470)], next_episode=None)
    engine.tick(1400.2)
    assert engine.seeks == [1470.0]
    assert engine.overlays == []


def test_syncplay_group_withholds_play_next(engine):
    engine.player.syncplay_group_active = True
    engine.arm([seg("Credits", 1400, 1470)], next_episode=NEXT_EPISODE)
    engine.tick(1400.2)
    overlay = engine.overlay
    assert overlay.skip_label == "string-30482"
    assert overlay.next_label == ""


def test_movie_never_offers_play_next(engine):
    engine.arm([seg("Credits", 1400, 1470)], item_type="Movie")
    engine.tick(1400.2)
    overlay = engine.overlay
    assert overlay.skip_label == "string-30482"
    assert overlay.next_label == ""


# --- the near-end prompt (no segment data) -----------------------------------


def test_near_end_prompt_without_segments(engine):
    engine.arm([], next_episode=NEXT_EPISODE)
    engine.tick(1469.5)
    assert engine.overlays == []
    engine.tick(1470.2)
    overlay = engine.overlay
    assert overlay is not None
    assert overlay.skip_label == ""
    assert overlay.next_label == "string-30486"
    engine.tick(1499.0)
    assert not overlay.closed
    engine.tick(1500.1)
    assert overlay.closed


def test_near_end_prompt_absent_when_credits_segment_exists(engine):
    engine.arm([seg("Credits", 1400, 1470)], next_episode=NEXT_EPISODE)
    engine.tick(100.0)
    assert engine.player._near_end_at is None


def test_near_end_prompt_clamped_on_short_items(engine):
    engine.arm([], next_episode=NEXT_EPISODE, runtime=40.0)
    engine.tick(19.0)
    assert engine.overlays == []
    engine.tick(20.3)
    assert engine.overlay is not None


def test_near_end_prompt_needs_next_episode(engine):
    engine.arm([], next_episode=None)
    engine.tick(1470.2)
    assert engine.overlays == []
    assert engine.player._near_end_at is None


def test_near_end_prompt_disabled_by_setting(engine):
    FakeAddon.store["playNextEnabled"] = "false"
    engine.arm([], next_episode=NEXT_EPISODE)
    engine.tick(1470.2)
    assert engine.overlays == []


def test_autoplay_counts_down_and_starts_next(engine):
    FakeAddon.store["playNextAutoplay"] = "true"
    engine.arm([], next_episode=NEXT_EPISODE)
    engine.tick(1470.2)
    overlay = engine.overlay
    engine.tick(1495.0)
    assert overlay.countdowns[-1] == 5
    engine.tick(1499.2)
    assert overlay.closed
    assert any("id=ep2" in b for b in engine.builtins)


# --- preparation (warm fetch, adjacency) -------------------------------------


def test_prepare_falls_back_to_service_fetch(engine):
    engine.player._item = {"Id": "ep1", "Type": "Episode", "SeriesId": "show1"}
    engine.api.segments_response = {
        "Items": [{"Type": "Intro", "StartTicks": 0, "EndTicks": 300_000_000}]
    }
    engine.player.prepare_segment_state(threading.Event())
    assert engine.player._segments_loaded
    assert engine.player._segments[0]["Type"] == "Introduction"


def test_prepare_retries_once_then_degrades(engine):
    engine.player._item = {"Id": "ep1", "Type": "Episode", "SeriesId": "show1"}
    engine.api.fail_segments = 1
    engine.api.segments_response = {
        "Items": [{"Type": "Intro", "StartTicks": 0, "EndTicks": 300_000_000}]
    }
    engine.player.prepare_segment_state(threading.Event())
    assert engine.player._segments_loaded
    assert len(engine.player._segments) == 1

    engine.player._segment_reset()
    engine.player._item = {"Id": "ep1", "Type": "Episode", "SeriesId": "show1"}
    engine.api.fail_segments = 2
    engine.player.prepare_segment_state(threading.Event())
    assert engine.player._segments_loaded
    assert engine.player._segments == []


def test_prepare_resolves_next_episode(engine):
    engine.player._item = {"Id": "ep1", "Type": "Episode", "SeriesId": "show1"}
    engine.player._segments_loaded = True
    engine.api.adjacent_response = {"Items": [{"Id": "ep1"}, {"Id": "ep2"}]}
    engine.player.prepare_segment_state(threading.Event())
    assert engine.player._next_episode == {"Id": "ep2"}


def test_prepare_finale_has_no_next(engine):
    engine.player._item = {"Id": "ep1", "Type": "Episode", "SeriesId": "show1"}
    engine.player._segments_loaded = True
    engine.api.adjacent_response = {"Items": [{"Id": "ep0"}, {"Id": "ep1"}]}
    engine.player.prepare_segment_state(threading.Event())
    assert engine.player._next_episode is None


def test_prepare_survives_adjacency_failure(engine):
    engine.player._item = {"Id": "ep1", "Type": "Episode", "SeriesId": "show1"}
    engine.player._segments_loaded = True
    engine.api.fail_adjacent = True
    engine.player.prepare_segment_state(threading.Event())
    assert engine.player._next_episode is None


def test_prepare_discards_result_for_superseded_playback(engine):
    # A slow fetch returning after another playback claimed the player must
    # not land its segments on the new item.
    old_item = {"Id": "ep1", "Type": "Episode", "SeriesId": "show1"}
    new_item = {"Id": "ep9", "Type": "Episode", "SeriesId": "show1"}
    engine.player._item = old_item
    engine.api.segments_response = {
        "Items": [{"Type": "Intro", "StartTicks": 0, "EndTicks": 300_000_000}]
    }

    original = engine.api.media_segments

    def swap_playback_mid_fetch(item_id):
        engine.player._item = new_item
        return original(item_id)

    engine.api.media_segments = swap_playback_mid_fetch
    engine.player.prepare_segment_state(threading.Event())
    assert engine.player._segments == []
    assert not engine.player._segments_loaded
    assert engine.player._next_episode is None


# --- engine lifecycle --------------------------------------------------------


class DummyChecker:
    started = 0

    def __init__(self, player):
        self.player = player

    def start(self):
        DummyChecker.started += 1

    def stop(self):
        pass


def test_start_segment_engine_uses_prefetched_segments(engine, monkeypatch):
    monkeypatch.setattr(player_mod, "SegmentChecker", DummyChecker)
    DummyChecker.started = 0
    item = {
        "Id": "ep1",
        "Type": "Episode",
        "SeriesId": "show1",
        "Segments": [seg("Credits", 1400, 1470), seg("Introduction", 10, 40)],
    }
    engine.player._item = item
    engine.player._start_segment_engine(item)
    assert DummyChecker.started == 1
    assert engine.player._segments_loaded
    assert [s["Type"] for s in engine.player._segments] == [
        "Introduction",
        "Credits",
    ]


def test_start_segment_engine_skips_non_video_and_disabled(engine, monkeypatch):
    monkeypatch.setattr(player_mod, "SegmentChecker", DummyChecker)
    DummyChecker.started = 0
    engine.player._start_segment_engine({"Id": "a1", "Type": "Audio"})
    assert DummyChecker.started == 0
    FakeAddon.store["mediaSegmentsEnabled"] = "false"
    FakeAddon.store["playNextEnabled"] = "false"
    engine.player._start_segment_engine({"Id": "m1", "Type": "Movie"})
    assert DummyChecker.started == 0


def test_play_next_disabled_engine_still_runs_for_segments(engine, monkeypatch):
    monkeypatch.setattr(player_mod, "SegmentChecker", DummyChecker)
    DummyChecker.started = 0
    FakeAddon.store["playNextEnabled"] = "false"
    engine.player._start_segment_engine(
        {"Id": "m1", "Type": "Movie", "Segments": [seg("Introduction", 10, 40)]}
    )
    assert DummyChecker.started == 1


def test_finalize_tears_down_overlay_and_state(engine):
    FakeAddon.store["skipIntroductionMode"] = "2"
    engine.arm([seg("Introduction", 10, 40)])
    engine.tick(10.1)
    overlay = engine.overlay
    engine.player.finalize()
    assert overlay.closed
    assert engine.player._segments == []
    assert not engine.player._segments_loaded
    assert engine.player._next_episode is None
