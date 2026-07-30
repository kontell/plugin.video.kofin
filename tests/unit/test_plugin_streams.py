"""The in-playback stream menu: what it offers, and what a pick actually does.

A text subtitle switches in place; audio and a burned-in image subtitle cost a
restart, because a Jellyfin transcode carries one audio track and no subtitles
(docs/transcode-stream-selection-plan.md §2.1).
"""

import pytest

from kofin.core import state
from kofin.plugin import streams as menu
from tests.unit.fakes import FakeAddon, FakeWindow

AUDIO_1 = {"Index": 1, "Type": "Audio", "DisplayTitle": "English - AC3"}
AUDIO_2 = {"Index": 2, "Type": "Audio", "DisplayTitle": "Commentary - AAC"}
SUB_3 = {
    "Index": 3,
    "Type": "Subtitle",
    "DisplayTitle": "English - SUBRIP",
    "IsTextSubtitleStream": True,
}
SUB_4 = {
    "Index": 4,
    "Type": "Subtitle",
    "DisplayTitle": "English SDH - PGSSUB",
    "IsTextSubtitleStream": False,
}


class Recorder:
    """Stands in for every Kodi surface the menu touches."""

    def __init__(self):
        self.selected = []
        self.context = []
        self.builtins = []
        self.subtitle_stream = []
        self.shown = []
        self.toasts = []
        self.answers = []
        self.context_answers = []

    def select(self, heading, options):
        self.selected.append((heading, list(options)))
        return self.answers.pop(0) if self.answers else -1

    def contextmenu(self, options):
        self.context.append(list(options))
        return self.context_answers.pop(0) if self.context_answers else -1


@pytest.fixture
def env(monkeypatch):
    FakeWindow.store = {}
    FakeAddon.store = {}
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    rec = Recorder()

    class Dialog:
        def select(self, heading, options):
            return rec.select(heading, options)

        def contextmenu(self, options):
            return rec.contextmenu(options)

    class Player:
        def getTime(self):
            return 300.0

        def getPlayingFile(self):
            return "plugin://plugin.video.kofin/?mode=play&id=m1&dbid=99"

        def setSubtitleStream(self, index):
            rec.subtitle_stream.append(index)

        def showSubtitles(self, show):
            rec.shown.append(show)

    monkeypatch.setattr(menu.xbmcgui, "Dialog", Dialog)
    monkeypatch.setattr(menu.xbmc, "Player", Player)
    monkeypatch.setattr(menu.xbmc, "getLocalizedString", lambda i: "K%d" % i)
    monkeypatch.setattr(
        menu.xbmc, "executebuiltin", lambda cmd: rec.builtins.append(cmd)
    )
    monkeypatch.setattr(menu.toast, "show", lambda *a, **k: rec.toasts.append(a))
    monkeypatch.setattr(menu.settings, "localized", lambda i: "S%d" % i)
    monkeypatch.setattr(menu.kodirpc, "current_subtitle", lambda: None)
    return rec


def publish(method="Transcode", attached=(3,), media_streams=None, request=None):
    payload = {
        "Id": "m1",
        "MediaSourceId": "src1",
        "PlayMethod": method,
        "AudioStreamIndex": 1,
        "MediaStreams": media_streams or [AUDIO_1, AUDIO_2, SUB_3, SUB_4],
        "Attached": list(attached),
        "Request": dict(request or {}),
    }
    state.publish_playing_streams(payload, "both")


def restart_params(rec):
    from urllib.parse import parse_qsl, urlparse

    assert rec.builtins, "no restart was started"
    url = rec.builtins[-1][len("PlayMedia(") : -1]
    return dict(parse_qsl(urlparse(url).query))


# -- what gets offered ---------------------------------------------------------


def test_nothing_published_does_nothing(env):
    menu.context_menu()
    assert env.selected == [] and env.builtins == []


def test_both_on_offer_asks_which_kind_first(env):
    publish()
    env.context_answers = [1]  # subtitles
    env.answers = [0]  # "None"
    menu.context_menu()
    assert env.context == [["K460", "K287"]]
    assert env.shown == [False]


def test_only_subtitles_on_offer_skips_the_kind_question(env):
    publish(media_streams=[AUDIO_1, SUB_3])
    env.answers = [0]
    menu.context_menu()
    assert env.context == []  # straight to the subtitle list
    assert env.selected[0][0] == "K287"


# -- subtitles -----------------------------------------------------------------


def test_a_text_subtitle_switches_in_place(env):
    publish(method="Transcode", attached=[3])
    env.context_answers = [1]
    env.answers = [1]  # row 0 is "None", row 1 is the text sub
    menu.context_menu()
    # Attached first and only on a transcode, so Kodi index 0. No restart.
    assert env.subtitle_stream == [0]
    assert env.shown == [True]
    assert env.builtins == []


def test_an_image_subtitle_on_a_transcode_restarts_and_burns_in(env):
    publish(method="Transcode", attached=[3])
    env.context_answers = [1]
    env.answers = [2]  # None, text, image
    menu.context_menu()
    params = restart_params(env)
    assert params["subtitleindex"] == "4"
    assert params["burnsubs"] == "1"
    assert params["startticks"] == str(int(300.0 * 10_000_000))


def test_an_image_subtitle_on_direct_play_just_switches(env):
    publish(method="DirectStream", attached=[])
    env.context_answers = [1]
    env.answers = [2]
    menu.context_menu()
    # Embedded subtitles 3 and 4 are in the container: Kodi 0 and 1.
    assert env.subtitle_stream == [1]
    assert env.builtins == []


def test_the_burn_in_row_says_so(env):
    publish(method="Transcode", attached=[3])
    env.context_answers = [1]
    env.answers = [-1]
    menu.context_menu()
    _, options = env.selected[0]
    # Nothing on screen, so "None" is the marked row.
    assert options[0] == "K231 K461"
    assert options[1] == "English - SUBRIP"
    assert options[2] == "English SDH - PGSSUB (S30617)"


def test_subtitles_off(env):
    publish()
    env.context_answers = [1]
    env.answers = [0]
    menu.context_menu()
    assert env.shown == [False]
    assert env.subtitle_stream == []


def test_the_active_subtitle_is_marked(env, monkeypatch):
    publish(method="Transcode", attached=[3])
    monkeypatch.setattr(menu.kodirpc, "current_subtitle", lambda: 0)
    env.context_answers = [1]
    env.answers = [-1]
    menu.context_menu()
    _, options = env.selected[0]
    assert options[1] == "English - SUBRIP K461"


# -- audio ---------------------------------------------------------------------


def test_choosing_another_audio_track_restarts_at_the_position(env):
    publish(method="Transcode", request={"transcode": "1", "bitrate": "3"})
    env.context_answers = [0]
    env.answers = [1]
    menu.context_menu()
    params = restart_params(env)
    assert params["audioindex"] == "2"
    assert params["id"] == "m1"
    assert params["startticks"] == str(int(300.0 * 10_000_000))
    # The forced transcode and its bitrate are reproduced; the settings alone
    # would resolve this straight back to direct play.
    assert params["transcode"] == "1" and params["bitrate"] == "3"
    assert params["mediasourceid"] == "src1"
    assert env.toasts  # the five-second gap is announced


def test_choosing_the_playing_audio_track_changes_nothing(env):
    publish()
    env.context_answers = [0]
    env.answers = [0]  # already the active one
    menu.context_menu()
    assert env.builtins == []


def test_a_restart_recovers_the_dbid_from_the_playing_path(env):
    # Play Next builds its own URL with no dbid, so the published Request has
    # none — Kodi's playing path still does.
    publish(request={})
    env.context_answers = [0]
    env.answers = [1]
    menu.context_menu()
    assert restart_params(env)["dbid"] == "99"


def test_dbid_from_path_ignores_anything_that_is_not_one():
    assert menu.dbid_from_path("plugin://x/?dbid=12") == "12"
    assert menu.dbid_from_path("plugin://x/?dbid=abc") == ""
    assert menu.dbid_from_path("plugin://x/?id=m1") == ""
    assert menu.dbid_from_path("") == ""
