"""L1 units for parked userdata and its replay conflict rule (plan W2.4)."""

import pytest

from kofin.downloads import pending
from kofin.sync import db as sync_db


@pytest.fixture(autouse=True)
def kofin_db(tmp_path, monkeypatch):
    monkeypatch.setattr("xbmcvfs.exists", lambda p: True)
    monkeypatch.setattr("xbmcvfs.translatePath", lambda p: str(tmp_path))
    sync_db.reset_overrides()
    sync_db.set_path_override("kofin", str(tmp_path / "kofin.db"))
    yield
    sync_db.reset_overrides()


def test_enqueue_then_read_round_trips():
    pending.enqueue("i1", "episode", played=True, snapshot={"LastPlayedDate": "T1"})

    (row,) = pending.rows()
    assert row.jellyfin_id == "i1" and row.media_type == "episode"
    assert row.played == 1 and row.position_ticks is None
    assert row.snapshot == {"LastPlayedDate": "T1"}


def test_a_later_event_coalesces_and_keeps_the_first_snapshot():
    """One row per item: a played flag followed by a position must not become
    two replays, and the snapshot records what the server looked like before
    this device went away — the later event must not overwrite it."""
    pending.enqueue("i1", "episode", played=True, snapshot={"LastPlayedDate": "T1"})
    pending.enqueue("i1", "episode", position_ticks=1200, snapshot={"x": "later"})

    (row,) = pending.rows()
    assert row.played == 1  # kept
    assert row.position_ticks == 1200  # added
    assert row.snapshot == {"LastPlayedDate": "T1"}  # not overwritten


def test_rows_come_back_oldest_first(monkeypatch):
    """Replay order follows the events, not the ids; ties inside one second
    fall back to the id so the order is at least deterministic."""
    clock = {"now": 100}
    monkeypatch.setattr(pending.time, "time", lambda: clock["now"])

    pending.enqueue("b", played=True)
    clock["now"] = 200
    pending.enqueue("a", played=False)
    assert [row.jellyfin_id for row in pending.rows()] == ["b", "a"]

    clock["now"] = 300
    pending.enqueue("b", position_ticks=5)  # a newer event moves it back
    assert [row.jellyfin_id for row in pending.rows()] == ["a", "b"]


def test_attempts_drop_a_row_the_server_will_never_take():
    """A row that keeps failing is not a connection problem; retrying it on
    every connect for the life of the install is."""
    pending.enqueue("i1", played=True)
    for _ in range(pending.MAX_ATTEMPTS - 1):
        pending.record_attempt("i1")
    assert len(pending.rows()) == 1

    pending.record_attempt("i1")
    assert pending.rows() == []


# --- the conflict rule -------------------------------------------------------


def _row(**kwargs):
    values = dict(jellyfin_id="i1", server_snapshot='{"LastPlayedDate": "T1"}')
    values.update(kwargs)
    return pending.Pending(**values)


def test_an_unmoved_server_replays_verbatim():
    row = _row(played=1, position_ticks=1200)
    payload = pending.resolve(row, {"LastPlayedDate": "T1"})
    # Played wins the position: a finished item with a resume point is how a
    # watched episode reappears in Continue Watching (Findroid #406).
    assert payload == {"Played": True, "PlaybackPositionTicks": 0}


def test_position_only_replays_as_a_position():
    row = _row(position_ticks=1200)
    assert pending.resolve(row, {"LastPlayedDate": "T1"}) == {
        "PlaybackPositionTicks": 1200
    }


def test_a_server_that_moved_keeps_the_further_position():
    """Someone watched elsewhere while this device was offline. Dragging them
    back to our older position is the failure every other client ships."""
    row = _row(position_ticks=1200)
    payload = pending.resolve(
        row, {"LastPlayedDate": "T2", "PlaybackPositionTicks": 4000}
    )
    assert payload == {"PlaybackPositionTicks": 4000}

    ahead = _row(position_ticks=9000)
    assert pending.resolve(
        ahead, {"LastPlayedDate": "T2", "PlaybackPositionTicks": 4000}
    ) == {"PlaybackPositionTicks": 9000}


def test_a_server_that_moved_never_un_watches():
    """Setting played on stale local state is recoverable; clearing it is
    not, so a moved server keeps its flag."""
    row = _row(played=0, position_ticks=None)
    assert pending.resolve(row, {"LastPlayedDate": "T2"}) == {}

    still_sets = _row(played=1)
    assert pending.resolve(still_sets, {"LastPlayedDate": "T2"}) == {
        "Played": True,
        "PlaybackPositionTicks": 0,
    }


def test_nothing_to_say_answers_nothing():
    assert pending.resolve(_row(), {"LastPlayedDate": "T1"}) == {}
