"""The Library speaks the host port itself (review of #192: the SyncHost
wrapper was an identity hop): a FullSync built by the service sees the same
claim, locks and queues the ticks do."""

from tests.unit.test_sync_library import make_library, sync_env  # noqa: F401


def test_the_claim_is_one_at_a_time_and_dies_with_the_manager():
    manager, _api = make_library()

    assert manager.claim() is True
    assert manager.claim() is False
    manager.release()
    assert manager.claim() is True

    rebuilt, _api = make_library()  # a fresh manager: a fresh claim
    assert rebuilt.claim() is True


def test_the_plan_lands_in_the_managers_queues():
    manager, _api = make_library()

    manager.added(["a1", "a2"])
    manager.updated(["u1"])
    manager.removed(["r1"])

    assert list(manager.added_queue.queue)[0] == ["a1", "a2"]
    assert list(manager.updated_queue.queue)[0] == ["u1"]
    assert list(manager.removed_queue.queue) == ["r1"]


def test_every_member_of_the_port_is_there():
    """What FullSync, prune.plan and removal reach for, by name."""
    manager, _api = make_library()
    for name in (
        "database_lock",
        "music_database_lock",
        "claim",
        "release",
        "added",
        "updated",
        "removed",
        "refresh_libraries",
        "stamp_watermark_if_empty",
        "defer_playlist_poll",
        "sync_failure_toasted",
    ):
        assert hasattr(manager, name), name
