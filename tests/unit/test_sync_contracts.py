"""Commit, ownership and restart contracts shared by both distributions."""

from queue import Queue
from types import SimpleNamespace

import pytest

from kofin.sync import private
from kofin.sync.backend import ApplyResult
from kofin.sync.catalogue import Catalogue, BackendMismatch, claim_backend
from kofin.sync.hooks import announce
from kofin.sync.model import MediaItem, Metadata
from kofin.sync.policy import plan_update


def media(title="Movie"):
    return MediaItem.from_dto({"Id": "m1", "Type": "Movie", "Name": title})


@pytest.fixture
def store(tmp_path):
    private.reset_overrides()
    private.set_path_override("kofin", str(tmp_path / "kofin.db"))
    yield Catalogue("server-1/user-1")
    private.reset_overrides()


def test_desired_is_durable_and_only_current_readback_advances_applied(store):
    first = store.stage(media())
    assert first == store.stage(media())
    assert store.state("m1").applied == 0
    reopened = Catalogue("server-1/user-1")
    assert reopened.pending() == [(media(), first, "upsert")]
    reopened.failed("m1", first, "timeout")
    second = reopened.stage(media("Changed"))
    assert second > first
    assert not reopened.confirm("m1", first)
    assert reopened.state("m1").applied == 0
    assert reopened.confirm("m1", second)
    assert reopened.state("m1").applied == second
    assert reopened.pending() == []
    removed = reopened.stage(media("Changed"), "remove")
    assert reopened.state("m1").applied == second
    assert reopened.pending()[0][1:] == (removed, "remove")
    assert Catalogue("server-2/user-1").pending() == []


def test_sql_and_api_mappings_are_never_adopted_across_backends(store):
    with private.Database() as db:
        claim_backend(db.cursor, "sql")
        db.cursor.execute(
            "INSERT INTO view VALUES ('shared', 'Still readable', 'movies')"
        )
    with pytest.raises(BackendMismatch):
        store.stage(media())
    with private.Database() as db:
        assert (
            db.cursor.execute("SELECT view_name FROM view").fetchone()[0]
            == "Still readable"
        )


def test_unmarked_legacy_mappings_require_fresh_state(store):
    with private.Database() as db:
        db.cursor.execute(
            "INSERT INTO jellyfin(jellyfin_id, kodi_id) VALUES ('old', 42)"
        )
    with pytest.raises(BackendMismatch):
        store.stage(media())


@pytest.mark.parametrize("status", ["prepared", "staged", "skipped", "unsupported"])
def test_unconfirmed_content_is_never_announced(status):
    output = Queue()
    announce(ApplyResult(media(), status, new=True), output)
    assert output.empty()


def test_sql_commit_confirms_only_after_native_and_private_commits():
    from kofin.sync.backends.sql.backend import SQLBatch

    events = []
    batch = SQLBatch.__new__(SQLBatch)
    batch._native = SimpleNamespace(
        conn=SimpleNamespace(commit=lambda: events.append("native"))
    )
    batch._mapping = SimpleNamespace(
        conn=SimpleNamespace(commit=lambda: events.append("private"))
    )
    batch._pending = [ApplyResult(media(), "prepared", new=True)]
    results = batch.commit()
    assert events == ["native", "private"]
    assert results[0].confirmed
    assert batch.commit() == []


def test_failed_native_commit_never_commits_mapping_or_announces():
    from kofin.sync.backends.sql.backend import SQLBatch
    from kofin.sync.workers import UpdateWorker
    import threading

    def fail():
        raise OSError("disk full")

    batch = SQLBatch.__new__(SQLBatch)
    batch._native = SimpleNamespace(conn=SimpleNamespace(commit=fail))
    batch._mapping = SimpleNamespace(
        conn=SimpleNamespace(commit=lambda: pytest.fail("mapping committed"))
    )
    batch._pending = [ApplyResult(media(), "prepared", new=True)]
    output, failures = Queue(), []
    worker = UpdateWorker(
        Queue(),
        output,
        threading.Lock(),
        "video",
        notify_enabled=True,
        unapplied=lambda *args: failures.append(args),
    )
    with pytest.raises(OSError):
        worker._commit(batch)
    assert output.empty()
    assert failures and "batch commit failed" in failures[0][1]


def test_metadata_policy_keeps_unverifiable_and_forced_items_writable():
    assert not plan_update({}, None, exists=True).skip_metadata
    assert not plan_update(
        {"Etag": "x"}, "x|plugin", exists=True, force=True
    ).skip_metadata
    assert not plan_update({"Etag": "x"}, "x|plugin", exists=False).skip_metadata
    unchanged = plan_update(
        {"Etag": "x", "_userdata_changed": False}, "x|plugin", exists=True
    )
    assert unchanged.skip_metadata and not unchanged.apply_userdata
    assert plan_update({"Etag": "x"}, "x|plugin", exists=True).apply_userdata
    assert Metadata({"RunTimeTicks": 15_000_000}).get_runtime() == 1.5
