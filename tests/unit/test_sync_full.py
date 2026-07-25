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


def test_item_gone_server_side_is_skipped_not_fatal(fullsync):
    """A show deleted after it was paged 404s on the writer's /Seasons fetch.
    Live phase 5: that aborted the whole library and re-fired a sync-failed
    toast on every service start, forever."""

    def apply(obj, item):
        raise HttpError(404, "GET /Shows/%s/Seasons -> 404" % item["Id"])

    assert fullsync.apply_or_skip(apply, None, {"Id": "gone-show"}, "Series") is False


def test_item_other_http_error_still_aborts_the_pass(fullsync):
    """The guard is for dead ids only — a 500 is a real failure and must not
    be downgraded into a silently incomplete library."""

    def apply(obj, item):
        raise HttpError(500, "GET /Shows/%s/Seasons -> 500" % item["Id"])

    with pytest.raises(HttpError):
        fullsync.apply_or_skip(apply, None, {"Id": "flaky-show"}, "Series")


def test_item_applied_normally_reports_success(fullsync):
    written = []
    assert (
        fullsync.apply_or_skip(
            lambda obj, item: written.append(item["Id"]),
            None,
            {"Id": "live-show"},
            "Series",
        )
        is True
    )
    assert written == ["live-show"]


def test_synced_library_still_whitelisted(fullsync, monkeypatch):
    fullsync.server = FakeServer({"lib1": 200})
    fullsync.sync["Libraries"] = ["lib1"]
    monkeypatch.setattr(fullsync, "movies", lambda library: None)
    failures = []

    fullsync.process_libraries(["lib1"], failures)

    assert failures == []
    assert fullsync.sync["Libraries"] == []
    assert fullsync.sync["Whitelist"] == ["lib1"]


class RecordingLibrary:
    """The Library slice start() touches once the passes are done."""

    def __init__(self):
        self.refreshed = []

    def stamp_watermark_if_empty(self):
        pass

    def refresh_libraries(self, databases):
        self.refreshed.append(set(databases))


def run_start(fullsync, monkeypatch, update):
    toasts = []
    monkeypatch.setattr(
        "kofin.sync.full_sync.notification",
        lambda *a, **kw: toasts.append(a[0] if a else kw),
    )
    monkeypatch.setattr("kofin.sync.full_sync.localized", lambda code: str(code))
    monkeypatch.setattr(fullsync, "_media_type", lambda library_id: "music")
    monkeypatch.setattr(fullsync, "process_libraries", lambda libraries, failures: None)

    fullsync.library = RecordingLibrary()
    fullsync.update_library = update
    fullsync.sync["Libraries"] = ["lib2"]
    fullsync.start()

    return toasts


def test_update_mode_does_not_announce_a_completed_sync(fullsync, monkeypatch):
    """Update mode only plans: prune diffs the library and hands the work to
    the incremental pipeline, which reports its own progress. The completion
    toast fired seconds after queueing a 22k-item backlog, claiming it was
    already written."""
    assert run_start(fullsync, monkeypatch, update=True) == []


def test_full_sync_still_announces_completion(fullsync, monkeypatch):
    toasts = run_start(fullsync, monkeypatch, update=False)

    assert len(toasts) == 1
    assert "30409" in toasts[0]


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
