"""Reads of Kodi's own library state.

0.0 and None are different answers — "the row has no bookmark" versus "the row
could not be read" — and the play route branches on which it got.
"""

import json

from kofin.core import kodirpc


def responder(payload):
    return lambda query: json.dumps(payload)


def _player_responder(properties):
    """GetActivePlayers then GetProperties, in that order."""

    def answer(query):
        method = json.loads(query)["method"]
        if method == "Player.GetActivePlayers":
            return json.dumps({"result": [{"playerid": 1}]})
        return json.dumps({"result": properties})

    return answer


def test_resume_point_keeps_the_total(monkeypatch):
    monkeypatch.setattr(
        "xbmc.executeJSONRPC",
        responder(
            {
                "result": {
                    "moviedetails": {"resume": {"position": 120.0, "total": 3600.0}}
                }
            }
        ),
    )
    assert kodirpc.resume_point(12, "movie") == (120.0, 3600.0)
    assert kodirpc.resume_seconds(12, "movie") == 120.0


def test_resume_seconds_reads_the_position(monkeypatch):
    monkeypatch.setattr(
        "xbmc.executeJSONRPC",
        responder({"result": {"episodedetails": {"resume": {"position": 895.0}}}}),
    )
    assert kodirpc.resume_seconds(8956, "episode") == 895.0


def test_resume_seconds_reports_an_absent_bookmark_as_zero(monkeypatch):
    monkeypatch.setattr(
        "xbmc.executeJSONRPC",
        responder({"result": {"moviedetails": {"resume": {"position": 0.0}}}}),
    )
    assert kodirpc.resume_seconds(12, "movie") == 0.0


def test_resume_seconds_is_none_when_the_row_cannot_be_read(monkeypatch):
    monkeypatch.setattr(
        "xbmc.executeJSONRPC",
        responder({"error": {"code": -32602, "message": "Invalid params."}}),
    )
    assert kodirpc.resume_seconds(9999, "episode") is None

    monkeypatch.setattr("xbmc.executeJSONRPC", lambda query: "not json")
    assert kodirpc.resume_seconds(8956, "episode") is None


def test_resume_seconds_is_none_for_a_media_type_without_bookmarks(monkeypatch):
    def explode(query):  # pragma: no cover - must not be reached
        raise AssertionError("no query for a type Kodi does not resume")

    monkeypatch.setattr("xbmc.executeJSONRPC", explode)
    assert kodirpc.resume_seconds(1, "song") is None
    assert kodirpc.resume_seconds(1, "tvshow") is None


def test_each_query_asks_the_matching_method(monkeypatch):
    for media, (method, id_field, result_field) in kodirpc.RESUME_QUERY.items():
        seen = {}

        def capture(query, seen=seen, result_field=result_field):
            seen.update(json.loads(query))
            return json.dumps({"result": {result_field: {"resume": {"position": 7.0}}}})

        monkeypatch.setattr("xbmc.executeJSONRPC", capture)
        assert kodirpc.resume_seconds(42, media) == 7.0
        assert seen["method"] == method
        assert seen["params"][id_field] == 42


# --- texture invalidation ----------------------------------------------------


class TextureRpc:
    """Records the calls a texture drop makes; answers the lookup with rows."""

    def __init__(self, textures):
        self.textures = textures
        self.calls = []

    def __call__(self, query):
        request = json.loads(query)
        self.calls.append(request)
        if request["method"] == "Textures.GetTextures":
            return json.dumps({"result": {"textures": self.textures}})
        return json.dumps({"result": "OK"})


# -- what the player is currently playing -------------------------------------


def test_current_audio_reads_the_stream_index(monkeypatch):
    monkeypatch.setattr(
        "xbmc.executeJSONRPC",
        _player_responder({"currentaudiostream": {"index": 2, "name": "AC3"}}),
    )
    assert kodirpc.current_audio() == 2


def test_current_audio_without_a_player(monkeypatch):
    monkeypatch.setattr("xbmc.executeJSONRPC", responder({"result": []}))
    assert kodirpc.current_audio() is None


def test_current_audio_survives_a_junk_answer(monkeypatch):
    monkeypatch.setattr("xbmc.executeJSONRPC", lambda query: "not json")
    assert kodirpc.current_audio() is None


def test_current_subtitle_is_none_when_subtitles_are_off(monkeypatch):
    monkeypatch.setattr(
        "xbmc.executeJSONRPC",
        _player_responder({"currentsubtitle": {"index": 1}, "subtitleenabled": False}),
    )
    assert kodirpc.current_subtitle() is None


def test_current_subtitle_reads_the_index(monkeypatch):
    monkeypatch.setattr(
        "xbmc.executeJSONRPC",
        _player_responder({"currentsubtitle": {"index": 1}, "subtitleenabled": True}),
    )
    assert kodirpc.current_subtitle() == 1


def _stop_recorder(active, after_stop=None):
    """Records every query; answers GetActivePlayers with ``active``, then with
    ``after_stop`` once a stop has been issued."""
    sent = []
    state = {"stopped": False}

    def answer(query):
        payload = json.loads(query)
        sent.append(payload)
        method = payload["method"]
        if method == "Player.GetActivePlayers":
            if state["stopped"] and after_stop is not None:
                return json.dumps({"result": after_stop})
            return json.dumps({"result": active})
        if method == "Player.Stop":
            state["stopped"] = True
            return json.dumps({"result": "OK"})
        raise AssertionError("unexpected method %s" % method)

    return sent, answer


def test_stop_player_does_nothing_when_nothing_plays(monkeypatch):
    sent, answer = _stop_recorder([])
    monkeypatch.setattr("xbmc.executeJSONRPC", answer)

    assert kodirpc.stop_player() is False
    assert [q["method"] for q in sent] == ["Player.GetActivePlayers"]


def test_stop_player_stops_each_active_player_by_its_own_id(monkeypatch):
    # playerid 0 is music: SyncPlay drives audio too, and Player.Stop answers
    # FailedToExecute for a playerid that is not the one playing.
    sent, answer = _stop_recorder([{"playerid": 0, "type": "audio"}])
    monkeypatch.setattr("xbmc.executeJSONRPC", answer)

    assert kodirpc.stop_player() is True
    stops = [q for q in sent if q["method"] == "Player.Stop"]
    assert [q["params"]["playerid"] for q in stops] == [0]


def test_stop_player_never_calls_the_gil_holding_binding(monkeypatch):
    """``xbmc.Player.stop()`` blocks on Kodi's app thread holding the GIL,
    which can deadlock; the helper stops through JSON-RPC."""

    def explode(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("xbmc.Player.stop() holds the GIL; use JSON-RPC")

    monkeypatch.setattr("xbmc.Player.stop", explode)
    _sent, answer = _stop_recorder([{"playerid": 1, "type": "video"}])
    monkeypatch.setattr("xbmc.executeJSONRPC", answer)

    assert kodirpc.stop_player() is True


def test_stop_player_waits_for_the_player_to_go(monkeypatch):
    sent, answer = _stop_recorder([{"playerid": 1, "type": "video"}], after_stop=[])
    monkeypatch.setattr("xbmc.executeJSONRPC", answer)
    monkeypatch.setattr("xbmc.Monitor.waitForAbort", lambda self, timeout=-1: False)

    assert kodirpc.stop_player(wait_seconds=1.0) is True
    # One read to find the player, the stop, then one poll that finds it gone.
    assert [q["method"] for q in sent] == [
        "Player.GetActivePlayers",
        "Player.Stop",
        "Player.GetActivePlayers",
    ]


def test_stop_player_stops_waiting_when_kodi_is_shutting_down(monkeypatch):
    sent, answer = _stop_recorder([{"playerid": 1, "type": "video"}])
    monkeypatch.setattr("xbmc.executeJSONRPC", answer)
    monkeypatch.setattr("xbmc.Monitor.waitForAbort", lambda self, timeout=-1: True)

    assert kodirpc.stop_player(wait_seconds=30.0) is True
    assert [q["method"] for q in sent] == ["Player.GetActivePlayers", "Player.Stop"]


def test_stop_player_survives_an_unreadable_player_list(monkeypatch):
    monkeypatch.setattr("xbmc.executeJSONRPC", lambda query: "not json")
    assert kodirpc.stop_player() is False


# --- clearing the bookmark Kodi keeps for a plugin path ----------------------


def test_clear_resume_bookmark_asks_kodi_for_a_zero_position(monkeypatch):
    """Files.SetFileDetails is the one JSON-RPC write that reaches a plugin
    path, and a zero position is what makes it clear the bookmark rather than
    write one (VideoLibrary.cpp UpdateResumePoint)."""
    sent = []

    def rpc(query):
        sent.append(json.loads(query))
        return json.dumps({"result": "OK"})

    monkeypatch.setattr("xbmc.executeJSONRPC", rpc)

    path = "plugin://plugin.video.kofin/?mode=play&id=jf1"
    assert kodirpc.clear_resume_bookmark(path) is True
    assert sent[0]["method"] == "Files.SetFileDetails"
    assert sent[0]["params"] == {
        "file": path,
        "media": "video",
        "resume": {"position": 0},
    }


def test_clear_resume_bookmark_reports_a_refusal(monkeypatch):
    monkeypatch.setattr(
        "xbmc.executeJSONRPC",
        responder({"error": {"code": -32602, "message": "Invalid params."}}),
    )
    assert kodirpc.clear_resume_bookmark("plugin://x") is False

    monkeypatch.setattr("xbmc.executeJSONRPC", lambda query: "not json")
    assert kodirpc.clear_resume_bookmark("plugin://x") is False


# --- kodi_setting: absent vs unreachable -------------------------------------


def test_kodi_setting_reads_the_value(monkeypatch):
    monkeypatch.setattr("xbmc.executeJSONRPC", responder({"result": {"value": 50}}))
    assert kodirpc.kodi_setting("musicplayer.crossfade") == 50


def test_kodi_setting_is_none_only_for_a_genuinely_absent_setting(monkeypatch):
    """Kodi answered, refusing: this version has no such setting."""
    monkeypatch.setattr(
        "xbmc.executeJSONRPC",
        responder({"error": {"code": -32602, "message": "Invalid params."}}),
    )
    assert kodirpc.kodi_setting("musicplayer.crossfade") is None


def test_kodi_setting_reports_a_blip_as_failed_not_absent(monkeypatch):
    """A transient failure does not read as "this Kodi lacks the setting",
    an answer a caller may branch the Kodi version on."""

    def boom(query):
        raise RuntimeError("no answer")

    monkeypatch.setattr("xbmc.executeJSONRPC", boom)
    value = kodirpc.kodi_setting("musicplayer.crossfade")
    assert value is kodirpc.FAILED
    assert value is not None

    monkeypatch.setattr("xbmc.executeJSONRPC", lambda query: "not json")
    assert kodirpc.kodi_setting("musicplayer.crossfade") is kodirpc.FAILED
