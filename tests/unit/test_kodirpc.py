"""L1: reads of Kodi's own library state.

0.0 and None are different answers — "the row has no bookmark" versus "the row
could not be read" — and the play route branches on which it got.
"""

import json

from kofin.core import kodirpc


def responder(payload):
    return lambda query: json.dumps(payload)


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
