"""``Library.service()`` -- the sync tick -- one call per case (P2.0c).

These pin what the tick does *between* the collaborators it calls, which
is what P2.3 restructures: reaping finished workers, the download backoff,
the playback gate, the retry ladder and the end-of-cycle bookkeeping.
"""

from datetime import timedelta

import pytest

from kofin.sync import db as sync_db
from kofin.sync.library import DOWNLOAD_BACKOFF_SECONDS
from tests.unit.fakes import FakeAddon
from tests.unit.synctick import FakeWorker, make_ticking_library, spawned_by
from tests.unit.test_sync_library import sync_env  # noqa: F401  (autouse env)


@pytest.fixture
def ticking(monkeypatch):
    return make_ticking_library(monkeypatch)


def test_finished_workers_are_released_and_dropped(ticking):
    manager, _api, _clock, _spawned = ticking
    done = FakeWorker(is_done=True, source="added", db_file="video")
    live = FakeWorker(is_done=False, source="added", db_file="video")
    manager.writer_threads["updated"] = [done, live]
    manager.writer_threads["userdata"] = [FakeWorker(is_done=True, db_file="music")]
    finished_download = FakeWorker(is_done=True, source="added")
    manager.download_threads = [finished_download]

    manager.service()

    assert manager.writer_threads["updated"] == [live]
    assert manager.writer_threads["userdata"] == []
    assert manager.download_threads == []
    assert done.server.closed == 1
    assert finished_download.server.closed == 1
    assert live.server.closed == 0


def test_an_unreachable_download_worker_backs_the_spawn_path_off(ticking):
    manager, _api, clock, spawned = ticking
    manager.download_threads = [
        FakeWorker(is_done=True, source="added", unreachable=True)
    ]
    manager.added_queue.put(["m1"])

    manager.service()

    assert manager.download_backoff.due_at == clock.datetime_now() + timedelta(
        seconds=DOWNLOAD_BACKOFF_SECONDS
    )
    assert spawned_by(spawned, source="added") == []

    clock.advance(DOWNLOAD_BACKOFF_SECONDS + 1)
    manager.service()

    assert len(spawned_by(spawned, source="added")) == 1


def test_video_playback_holds_the_workers_unless_sync_during_play(ticking):
    manager, _api, _clock, spawned = ticking
    manager.player.playing = True
    manager.added_queue.put(["m1"])

    manager.service()
    assert spawned == []

    FakeAddon.store["syncDuringPlay"] = "true"
    manager.service()
    assert len(spawned_by(spawned, source="added")) == 1
    assert spawned[0].started is True


def test_a_due_retry_runs_fast_sync_and_clears_itself(ticking):
    manager, _api, clock, _spawned = ticking
    calls = []
    manager.fast_sync = lambda: calls.append("fast") or True
    manager.retry.due_at = clock.datetime_now()

    manager.service()

    assert calls == ["fast"]
    assert manager.retry.due_at is None


def test_a_failed_retry_re_arms_with_backoff(ticking):
    manager, _api, clock, _spawned = ticking
    manager.fast_sync = lambda: False
    manager.retry.due_at = clock.datetime_now()
    before = manager.retry.delay

    manager.service()

    assert manager.retry.due_at is not None
    assert manager.retry.due_at > clock.datetime_now()
    assert manager.retry.delay > before


def test_a_retry_is_not_due_until_its_time(ticking):
    manager, _api, clock, _spawned = ticking
    calls = []
    manager.fast_sync = lambda: calls.append("fast") or True
    due = clock.datetime_now()
    clock.advance(-30)
    manager.retry.due_at = due

    manager.service()

    assert calls == [] and manager.retry.due_at == due


def test_added_work_spawns_a_download_worker_tagged_with_its_source(ticking):
    manager, api, _clock, spawned = ticking
    manager.added_queue.put(["m1", "m2"])
    manager.userdata_queue.put(["u1"])

    manager.service()

    sources = sorted(worker.source for worker in spawned)
    assert sources == ["added", "userdata"]
    assert all(worker.started for worker in spawned)
    assert manager.download_threads == spawned


def test_a_written_queue_spawns_one_writer_at_a_time(ticking):
    """One writer per category, whatever the database: video and music share
    kofin.db, and sqlite allows one write transaction per file. The music
    queue waits for the video writer to finish."""
    manager, _api, _clock, spawned = ticking
    manager.added_output["Movie"].put({"Id": "m1", "Type": "Movie"})
    manager.added_output["Audio"].put({"Id": "s1", "Type": "Audio"})

    manager.service()

    writers = spawned_by(spawned, source="added")
    assert [worker.db_file for worker in writers] == ["video"]
    assert manager.writer_threads["updated"] == writers
    assert manager.touched_databases == {"video"}
    assert manager.added_databases == {"video"}
    assert manager.pending_refresh is True

    manager.added_output["Movie"].get()  # the writer consumed its queue...
    writers[0].is_done = True  # ...and finished
    manager.service()

    assert [worker.db_file for worker in spawned_by(spawned, source="added")] == [
        "video",
        "music",
    ]
    assert manager.touched_databases == {"video", "music"}


def test_metadata_updates_wait_for_additions(ticking):
    """Strict priority: while added work is in flight, the updated queue is
    not written."""
    manager, _api, _clock, spawned = ticking
    manager.added_queue.put(["m1"])  # still to download
    manager.updated_output["Movie"].put({"Id": "m2", "Type": "Movie"})

    manager.service()

    assert spawned_by(spawned, source="updated") == []
    assert manager.writer_threads["updated"] == []


def test_a_drained_cycle_saves_the_watermark_and_arms_the_refresh(ticking, monkeypatch):
    manager, _api, _clock, _spawned = ticking
    saved = []
    armed = []
    manager.save_last_sync = lambda: saved.append(True)
    manager.refresher.arm = lambda databases: armed.append(set(databases))
    manager.pending_refresh = True
    manager.touched_databases = {"video"}
    manager.total_updates = 3

    manager.service()

    assert manager.pending_refresh is False
    assert saved == [True]
    assert armed == [{"video"}]
    assert manager.touched_databases == set()
    assert manager.total_updates == 0


def test_a_cycle_with_download_errors_keeps_the_watermark_and_retries(ticking):
    manager, _api, _clock, _spawned = ticking
    saved = []
    manager.save_last_sync = lambda: saved.append(True)
    manager.refresher.arm = lambda databases: None
    manager.pending_refresh = True
    manager.download_errors.set()

    manager.service()

    assert saved == []
    assert manager.retry.due_at is not None
    assert not manager.download_errors.is_set()


def test_a_cycle_is_not_over_while_a_writer_lives(ticking):
    manager, _api, _clock, _spawned = ticking
    saved = []
    manager.save_last_sync = lambda: saved.append(True)
    manager.pending_refresh = True
    manager.writer_threads["updated"] = [FakeWorker(is_done=False, db_file="video")]

    manager.service()

    assert manager.pending_refresh is True and saved == []


def test_commands_are_dispatched_before_anything_else(ticking, monkeypatch):
    manager, _api, _clock, _spawned = ticking
    seen = []
    monkeypatch.setattr(manager, "process_commands", lambda: seen.append("commands"))
    manager.worker_downloads = lambda: seen.append("downloads")

    manager.service()

    assert seen[0] == "commands"
