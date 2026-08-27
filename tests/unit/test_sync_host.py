"""The SyncHost port over a real Library (P2.2): every member reaches the
manager it wraps, so a FullSync built by the service sees the same claim,
locks and queues the ticks do."""

from tests.unit.test_sync_library import make_library, sync_env  # noqa: F401


def test_the_host_is_the_library_seen_through_the_port():
    manager, _api = make_library()
    host = manager.sync_host()

    assert host.database_lock is manager.database_lock
    assert host.music_database_lock is manager.music_database_lock
    assert host.failure_toasted is manager.sync_failure_toasted

    assert host.claim() is True
    assert host.claim() is False  # one sync at a time
    host.release()
    assert host.claim() is True
    host.release()


def test_the_plan_lands_in_the_managers_queues():
    manager, _api = make_library()
    host = manager.sync_host()

    host.added(["a1", "a2"])
    host.updated(["u1"])
    host.removed(["r1"])

    assert manager.added_queue.qsize() == 1  # one chunk
    assert list(manager.added_queue.queue)[0] == ["a1", "a2"]
    assert list(manager.updated_queue.queue)[0] == ["u1"]
    assert list(manager.removed_queue.queue) == ["r1"]


def test_bookkeeping_reaches_the_manager(monkeypatch):
    manager, _api = make_library()
    host = manager.sync_host()
    refreshed = []
    monkeypatch.setattr(
        manager,
        "refresh_libraries",
        lambda databases, force_reload=False: refreshed.append(
            (set(databases), force_reload)
        ),
    )

    host.refresh_libraries(["video"], force_reload=True)
    host.defer_playlist_poll()

    assert refreshed == [({"video"}, True)]
    assert manager.playlist_poll_at is not None
