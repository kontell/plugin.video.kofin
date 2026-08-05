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
        self.preselected = []
        self.context = []
        self.builtins = []
        self.subtitle_stream = []
        self.audio_stream = []
        self.shown = []
        self.toasts = []
        self.answers = []
        self.context_answers = []

    def select(self, heading, options, preselect=-1):
        self.selected.append((heading, list(options)))
        self.preselected.append(preselect)
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
        def select(self, heading, options, preselect=-1):
            return rec.select(heading, options, preselect)

        def contextmenu(self, options):
            return rec.contextmenu(options)

    class Player:
        def getTime(self):
            return 300.0

        def getPlayingFile(self):
            return "plugin://plugin.video.kofin/?mode=play&id=m1&dbid=99"

        def setSubtitleStream(self, index):
            rec.subtitle_stream.append(index)

        def setAudioStream(self, index):
            rec.audio_stream.append(index)

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
    monkeypatch.setattr(menu.kodirpc, "current_audio", lambda: None)
    return rec


def publish(
    method="Transcode",
    attached=(3,),
    media_streams=None,
    request=None,
    subtitle_index=None,
    audio_index=1,
):
    payload = {
        "Id": "m1",
        "MediaSourceId": "src1",
        "PlayMethod": method,
        "AudioStreamIndex": audio_index,
        "SubtitleStreamIndex": subtitle_index,
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


# -- a burned-in subtitle ------------------------------------------------------

# What the server answers once the profile has withdrawn the image formats:
# the chosen image track comes back Encode, i.e. in the picture rather than in
# a track of its own.
SUB_4_BURNED = dict(SUB_4, DeliveryMethod="Encode")


def burned(**kwargs):
    publish(
        method="Transcode",
        attached=[3],
        media_streams=[AUDIO_1, AUDIO_2, SUB_3, SUB_4_BURNED],
        subtitle_index=4,
        **kwargs,
    )


def test_a_burned_in_subtitle_is_the_marked_row(env):
    """It is pixels in the video, so the player reports no subtitle at all --
    which used to mark "None" as the current one and leave the subtitle
    actually on screen looking unselected."""
    burned()
    env.context_answers = [1]
    env.answers = [-1]
    menu.context_menu()

    _, options = env.selected[0]
    assert options[0] == "K231"  # "None", unmarked
    assert options[2] == "English SDH - PGSSUB K461"
    # …and the cursor opens on it, the way Kodi's own stream dialogs do.
    assert env.preselected == [2]


def test_the_burn_in_warning_is_dropped_once_it_is_burned_in(env):
    burned()
    env.context_answers = [1]
    env.answers = [-1]
    menu.context_menu()
    _, options = env.selected[0]
    assert "S30617" not in options[2]  # nothing left to warn about


def test_choosing_the_burned_in_subtitle_again_does_nothing(env):
    """Re-picking it used to re-resolve the stream: a five-second gap to
    arrive exactly where we already were."""
    burned()
    env.context_answers = [1]
    env.answers = [2]
    menu.context_menu()
    assert env.builtins == [] and env.subtitle_stream == []


def test_turning_off_a_burned_in_subtitle_needs_a_new_stream(env):
    """showSubtitles(False) cannot touch it -- there is no track to hide."""
    burned()
    env.context_answers = [1]
    env.answers = [0]  # "None"
    menu.context_menu()

    assert env.shown == []  # not something the player can answer
    params = restart_params(env)
    assert params["subtitleindex"] == "-1"  # explicit: not "server, you choose"
    assert "burnsubs" not in params
    assert params["audioindex"] == "1"  # the track being heard survives it


def test_an_audio_switch_keeps_the_burned_in_subtitle(env):
    """The server resolves what it is not told from the user's profile, so an
    audio restart that named only the audio came back without the burn-in."""
    burned()
    env.context_answers = [0]
    env.answers = [1]
    menu.context_menu()

    params = restart_params(env)
    assert params["audioindex"] == "2"
    assert params["subtitleindex"] == "4"
    assert params["burnsubs"] == "1"


def test_a_burn_in_keeps_the_audio_track(env):
    """The other direction: burning a subtitle in used to drop back to the
    profile's audio track."""
    publish(method="Transcode", attached=[3], audio_index=2)
    env.context_answers = [1]
    env.answers = [2]  # the image sub
    menu.context_menu()

    params = restart_params(env)
    assert params["subtitleindex"] == "4" and params["burnsubs"] == "1"
    assert params["audioindex"] == "2"


def test_a_restart_states_that_no_subtitle_is_wanted(env):
    """Nothing showing has to be said out loud, or the server picks the
    profile's own and a subtitle the viewer turned off comes back."""
    publish(method="Transcode", attached=[3])
    env.context_answers = [0]
    env.answers = [1]
    menu.context_menu()
    assert restart_params(env)["subtitleindex"] == "-1"


# -- audio on a direct play ----------------------------------------------------


def test_audio_on_a_direct_play_switches_in_place(env, monkeypatch):
    """Kodi holds every track; restarting to reach one would cost five seconds
    to arrive at what setAudioStream does immediately."""
    publish(method="DirectStream", attached=[])
    monkeypatch.setattr(menu.kodirpc, "current_audio", lambda: 0)
    env.context_answers = [0]
    env.answers = [1]
    menu.context_menu()

    assert env.audio_stream == [1]  # Kodi ordinal for Jellyfin index 2
    assert env.builtins == []  # no restart, no toast about a gap
    assert env.toasts == []


def test_the_marked_audio_row_is_the_one_being_heard(env, monkeypatch):
    """Kodi's own audio menu moves the track without kofin hearing of it, so
    the resolved index is not necessarily what is playing."""
    publish(method="DirectStream", attached=[])
    monkeypatch.setattr(menu.kodirpc, "current_audio", lambda: 1)  # not the 1st
    env.context_answers = [0]
    env.answers = [-1]
    menu.context_menu()

    _, options = env.selected[0]
    assert options[0] == "English - AC3"
    assert options[1] == "Commentary - AAC K461"
    assert env.preselected == [1]
