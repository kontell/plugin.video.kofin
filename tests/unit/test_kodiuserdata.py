"""Kodi-side watched / resume changes pushed back to Jellyfin.

The two announcement shapes are the ones Kodi sends: the nested one from
"Mark as watched", the flat one from "Reset resume position".
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


@pytest.fixture(autouse=True)
def online(monkeypatch, tmp_path):
    """A connected service, with kofin.db in the test's directory."""
    from tests.unit.fakes import FakeAddon, FakeWindow

    FakeAddon.store = {}
    FakeWindow.store = {"kofin.online": "true"}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    monkeypatch.setattr("xbmcvfs.exists", lambda p: True)
    monkeypatch.setattr("xbmcvfs.translatePath", lambda p: str(tmp_path))
    from kofin.sync import private as sync_db

    sync_db.reset_overrides()
    sync_db.set_path_override("kofin", str(tmp_path / "kofin.db"))
    yield
    sync_db.reset_overrides()


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


def test_no_worker_thread_without_work():
    watcher = KodiUserData(RecordingApi())
    watcher.submit({"item": {"id": 1, "type": "tvshow"}, "playcount": 1})
    assert watcher._worker is None
    watcher.stop()  # a no-op, not a crash


def test_resume_query_covers_every_watched_media_type():
    from kofin.core import kodirpc

    assert set(kodirpc.RESUME_QUERY) == set(kodiuserdata.WATCHED_MEDIA)


# --- the edits go to the catalogue's outbox, never to the server directly ----


class FakeStore:
    def __init__(self):
        self.edits = []
        self.echoes = set()

    def is_echo(self, item_id, field, value):
        return (item_id, field) in self.echoes

    def local(self, item_id, fields):
        self.edits.append((item_id, fields))


@pytest.fixture
def outbox(monkeypatch):
    """Row 5910 is jf-ep-1 in the catalogue; everything else is not kofin's."""
    store = FakeStore()
    monkeypatch.setattr(
        "kofin.sync.backends.api.identity.mapped_item",
        lambda kodi_id, media: "jf-ep-1" if kodi_id == 5910 else None,
    )
    monkeypatch.setattr("kofin.sync.backends.api.identity.current_store", lambda: store)
    return store


def test_a_watched_mark_is_a_local_edit_for_the_pass(outbox):
    api = RecordingApi()
    assert drain(api, [MARK_WATCHED, MARK_UNWATCHED]) == []
    assert outbox.edits == [
        ("jf-ep-1", {"playcount": 1}),
        ("jf-ep-1", {"playcount": 0}),
    ]


def test_the_echo_of_the_passs_own_write_is_not_an_edit(outbox):
    outbox.echoes.add(("jf-ep-1", "playcount"))
    drain(RecordingApi(), [MARK_WATCHED])
    assert outbox.edits == []


def test_a_reset_resume_records_kodis_own_bookmark(outbox, kodi_resume):
    kodi_resume["position"] = 0.0
    drain(RecordingApi(), [RESET_RESUME])
    assert outbox.edits == [("jf-ep-1", {"position": 0.0})]


def test_an_unreadable_kodi_resume_writes_nothing(outbox, monkeypatch):
    monkeypatch.setattr("xbmc.executeJSONRPC", lambda query: "not json")
    drain(RecordingApi(), [RESET_RESUME])
    assert outbox.edits == []


def test_rows_kofin_did_not_sync_are_left_alone(outbox):
    foreign = {"item": {"id": 4242, "type": "movie"}, "playcount": 1}
    drain(RecordingApi(), [foreign])
    assert outbox.edits == []
