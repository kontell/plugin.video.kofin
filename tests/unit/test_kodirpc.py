"""L1: reads of Kodi's own library state.

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


def test_drop_cached_texture_removes_every_match(monkeypatch):
    rpc = TextureRpc(
        [
            {"textureid": 7, "url": "image://…%2fplugin.video.kofin%2f…fanart.png/"},
            {"textureid": 9, "url": "image://…%2fkofin%2ffanart.png/transform?x=1"},
        ]
    )
    monkeypatch.setattr("xbmc.executeJSONRPC", rpc)

    assert kodirpc.drop_cached_texture("plugin.video.kofin", require="fanart.png") == 2

    lookup = rpc.calls[0]
    # Filtered server-side: a real install's cache runs to thousands of rows.
    assert lookup["params"]["filter"] == {
        "field": "url",
        "operator": "contains",
        "value": "plugin.video.kofin",
    }
    # Without this Kodi answers with bare ids and `require` has nothing to test.
    assert lookup["params"]["properties"] == ["url"]
    assert [c["params"]["textureid"] for c in rpc.calls[1:]] == [7, 9]


def test_drop_cached_texture_spares_another_addons_file_of_the_same_name():
    """Live finding: plugin.video.jellyfin ships its own resources/fanart.png,
    so a filename-only match would evict a texture that is not ours."""
    rpc = TextureRpc(
        [
            {"textureid": 7, "url": "image://…%2fplugin.video.kofin%2ffanart.png/"},
            {"textureid": 8, "url": "image://…%2fplugin.video.kofin%2ficon.png/"},
        ]
    )
    import xbmc

    original, xbmc.executeJSONRPC = xbmc.executeJSONRPC, rpc
    try:
        assert (
            kodirpc.drop_cached_texture("plugin.video.kofin", require="fanart.png") == 1
        )
    finally:
        xbmc.executeJSONRPC = original
    assert [c["params"]["textureid"] for c in rpc.calls[1:]] == [7]


def test_drop_cached_texture_survives_a_failed_lookup(monkeypatch):
    monkeypatch.setattr("xbmc.executeJSONRPC", lambda query: "not json")
    assert kodirpc.drop_cached_texture("plugin.video.kofin") == 0


def test_drop_cached_texture_reports_nothing_to_do(monkeypatch):
    rpc = TextureRpc([])
    monkeypatch.setattr("xbmc.executeJSONRPC", rpc)
    assert kodirpc.drop_cached_texture("plugin.video.kofin") == 0


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
