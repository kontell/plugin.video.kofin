"""FullSync queue behavior: a library deleted server-side must not wedge
the sync (it 404s forever otherwise — the queue only drops entries that
complete), and a crash-resumed queue must not carry duplicates."""

import pytest

from kofin.core.http import HttpError
from kofin.sync.full_sync import FullSync


class FakeServer:
    """server.item() by canned status: 200 -> payload, else HttpError."""

    def __init__(self, status_by_id):
        self.status_by_id = status_by_id

    def item(self, item_id):
        status = self.status_by_id[item_id]
        if status != 200:
            raise HttpError(status, "GET /Items/%s -> %d" % (item_id, status))
        return {"Id": item_id, "CollectionType": "movies"}


@pytest.fixture
def fullsync(monkeypatch):
    monkeypatch.setattr("kofin.sync.full_sync.save_sync", lambda sync: None)
    monkeypatch.setattr("kofin.sync.full_sync.notification", lambda *a, **kw: None)
    FullSync._shared_state.clear()
    sync = FullSync(library=None, server=None)
    sync.sync = {"Libraries": [], "Whitelist": [], "RestorePoints": {}}
    yield sync
    FullSync._shared_state.clear()


def test_deleted_library_dropped_not_whitelisted(fullsync):
    fullsync.server = FakeServer({"gone1": 404})
    fullsync.sync["Libraries"] = ["gone1"]
    failures = []

    fullsync.process_libraries(["gone1"], failures)

    assert failures == []
    assert fullsync.sync["Libraries"] == []
    assert fullsync.sync["Whitelist"] == []


def test_other_http_errors_still_fail_and_keep_the_entry(fullsync):
    fullsync.server = FakeServer({"flaky1": 500})
    fullsync.sync["Libraries"] = ["flaky1"]
    failures = []

    fullsync.process_libraries(["flaky1"], failures)

    assert len(failures) == 1
    assert isinstance(failures[0], HttpError)
    assert fullsync.sync["Libraries"] == ["flaky1"]
    assert fullsync.sync["Whitelist"] == []


def test_synced_library_still_whitelisted(fullsync, monkeypatch):
    fullsync.server = FakeServer({"lib1": 200})
    fullsync.sync["Libraries"] = ["lib1"]
    monkeypatch.setattr(fullsync, "movies", lambda library: None)
    failures = []

    fullsync.process_libraries(["lib1"], failures)

    assert failures == []
    assert fullsync.sync["Libraries"] == []
    assert fullsync.sync["Whitelist"] == ["lib1"]


def test_resumed_queue_is_deduplicated(fullsync, monkeypatch):
    monkeypatch.setattr(
        "kofin.sync.full_sync.get_sync",
        lambda: {
            "Libraries": ["a", "Boxsets:x", "a", "b", "Boxsets:x", "a"],
            "Whitelist": [],
            "RestorePoints": {},
        },
    )
    started = []
    monkeypatch.setattr(fullsync, "start", lambda: started.append(True))

    fullsync.libraries()

    assert fullsync.sync["Libraries"] == ["a", "Boxsets:x", "b"]
    assert started == [True]
