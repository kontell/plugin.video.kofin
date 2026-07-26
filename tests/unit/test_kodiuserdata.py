"""L1: Kodi-side watched / resume changes pushed back to Jellyfin.

The two announcement shapes here were captured off a running Kodi 21 — the
nested one from "Mark as watched", the flat one from "Reset resume position" —
so the parser is pinned to what Kodi actually sends, not to a guess.
"""

import json

import pytest

from kofin.service import kodiuserdata
from kofin.service.kodiuserdata import (
    UPDATE_PLAYCOUNT,
    UPDATE_RESUME,
    KodiUserData,
    parse_update,
)

MARK_WATCHED = {"item": {"id": 5910, "type": "episode"}, "playcount": 1}
MARK_UNWATCHED = {"item": {"id": 5910, "type": "episode"}, "playcount": 0}
RESET_RESUME = {"id": 5910, "type": "episode"}


class RecordingApi:
    def __init__(self):
        self.calls = []

    def mark_played(self, item_id):
        self.calls.append(("played", item_id))

    def mark_unplayed(self, item_id):
        self.calls.append(("unplayed", item_id))

    def set_resume_position(self, item_id, position_ticks):
        self.calls.append(("resume", item_id, position_ticks))


@pytest.fixture
def mapped(monkeypatch):
    """Map Kodi row 5910 to a Jellyfin id; everything else is not kofin's."""
    monkeypatch.setattr(
        "kofin.service.player.mapped_jellyfin_id",
        lambda kodi_id, media: "jf-ep-1" if kodi_id == 5910 else None,
    )


@pytest.fixture
def kodi_resume(monkeypatch):
    """Kodi's stored resume position, settable per test."""
    box = {"position": 0.0}

    def fake_rpc(query):
        request = json.loads(query)
        return json.dumps(
            {
                "result": {
                    "episodedetails": {"resume": {"position": box["position"]}},
                    "id": request["id"],
                }
            }
        )

    monkeypatch.setattr("xbmc.executeJSONRPC", fake_rpc)
    return box


def drain(api, payloads):
    watcher = KodiUserData(api)
    for payload in payloads:
        watcher.submit(payload)
    watcher.stop()  # the sentinel queues behind the work, so this drains it
    return api.calls


# --- payload parsing ---------------------------------------------------------


def test_parse_watched_shapes():
    assert parse_update(MARK_WATCHED) == (UPDATE_PLAYCOUNT, 5910, "episode", 1)
    assert parse_update(MARK_UNWATCHED) == (UPDATE_PLAYCOUNT, 5910, "episode", 0)


def test_parse_resume_shape_is_flat():
    assert parse_update(RESET_RESUME) == (UPDATE_RESUME, 5910, "episode", 0)


def test_parse_ignores_library_adds_and_foreign_media():
    # A library add carries the nested identity with no play count.
    assert parse_update({"item": {"id": 12, "type": "movie"}, "added": True}) is None
    assert parse_update({"item": {"id": 12, "type": "tvshow"}, "playcount": 1}) is None
    assert parse_update({"id": 12, "type": "season"}) is None
    assert parse_update({"item": {"type": "movie"}, "playcount": 1}) is None
    assert parse_update({}) is None


def test_parse_covers_every_mapped_media_type():
    for media in ("movie", "episode", "musicvideo"):
        assert parse_update({"item": {"id": 7, "type": media}, "playcount": 1}) == (
            UPDATE_PLAYCOUNT,
            7,
            media,
            1,
        )


# --- pushing -----------------------------------------------------------------


def test_mark_watched_reaches_the_server(mapped):
    assert drain(RecordingApi(), [MARK_WATCHED]) == [("played", "jf-ep-1")]


def test_mark_unwatched_reaches_the_server(mapped):
    assert drain(RecordingApi(), [MARK_UNWATCHED]) == [("unplayed", "jf-ep-1")]


def test_reset_resume_zeroes_the_server_position(mapped, kodi_resume):
    assert drain(RecordingApi(), [RESET_RESUME]) == [("resume", "jf-ep-1", 0)]


def test_reset_resume_needs_kodis_bookmark_to_actually_be_gone(mapped, kodi_resume):
    # The flat shape is the only signal, so confirm it before discarding a
    # resume point the viewer may never have asked to lose.
    kodi_resume["position"] = 300.0
    assert drain(RecordingApi(), [RESET_RESUME]) == []


def test_unreadable_kodi_resume_writes_nothing(mapped, monkeypatch):
    monkeypatch.setattr("xbmc.executeJSONRPC", lambda query: "not json")
    assert drain(RecordingApi(), [RESET_RESUME]) == []


def test_rows_kofin_did_not_sync_are_left_alone(mapped):
    foreign = {"item": {"id": 4242, "type": "movie"}, "playcount": 1}
    assert drain(RecordingApi(), [foreign]) == []


def test_a_failing_push_does_not_strand_the_queue(mapped):
    class HalfBrokenApi(RecordingApi):
        def mark_played(self, item_id):
            raise RuntimeError("server down")

    api = HalfBrokenApi()
    assert drain(api, [MARK_WATCHED, MARK_UNWATCHED]) == [("unplayed", "jf-ep-1")]


def test_no_worker_thread_without_work():
    watcher = KodiUserData(RecordingApi())
    watcher.submit({"item": {"id": 1, "type": "tvshow"}, "playcount": 1})
    assert watcher._worker is None
    watcher.stop()  # a no-op, not a crash


def test_resume_query_covers_every_watched_media_type():
    assert set(kodiuserdata.RESUME_QUERY) == set(kodiuserdata.WATCHED_MEDIA)
