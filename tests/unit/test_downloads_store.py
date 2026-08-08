"""L1 units for the kofin.db download table (plan W1.3)."""

import pytest

from kofin.downloads import store
from kofin.sync import db as sync_db


@pytest.fixture(autouse=True)
def kofin_db(tmp_path, monkeypatch):
    monkeypatch.setattr("xbmcvfs.exists", lambda p: True)
    monkeypatch.setattr("xbmcvfs.translatePath", lambda p: str(tmp_path))
    sync_db.reset_overrides()
    sync_db.set_path_override("kofin", str(tmp_path / "kofin.db"))
    yield
    sync_db.reset_overrides()


def _movie(item_id="m1", **extra):
    values = dict(
        jellyfin_id=item_id,
        media_type="movie",
        quality=store.QUALITY_ORIGINAL,
        size_expected=1000,
        queued_at=100,
    )
    values.update(extra)
    return store.Download(**values)


def test_queue_then_get_round_trips():
    assert store.queue(_movie(userdata_json='{"Played": true}')) is True

    row = store.get("m1")
    assert row is not None
    assert row.state == store.QUEUED
    assert row.media_type == "movie"
    assert row.size_expected == 1000
    assert row.queued_at == 100
    assert row.userdata == {"Played": True}
    assert store.get("missing") is None


def test_queue_is_idempotent_for_live_and_done_rows():
    """A double-tap, or Download offered against a stale menu, must not
    double-fetch or reset a finished row."""
    store.queue(_movie())
    assert store.queue(_movie()) is False

    store.finish("m1", "Movies/M (2001)/m.mkv", "mkv", 1000, done_at=200)
    assert store.queue(_movie()) is False
    row = store.get("m1")
    assert row.state == store.DONE
    assert row.rel_path == "Movies/M (2001)/m.mkv"


def test_a_failed_row_requeues_in_place_keeping_resume_progress():
    store.queue(_movie())
    claimed = store.claim()
    store.record_progress("m1", 512)
    store.fail("m1", "boom")

    assert store.queue(_movie(queued_at=300)) is True
    row = store.get("m1")
    assert row.state == store.QUEUED
    assert row.bytes_done == 512  # Range resume starts here
    assert row.error == ""
    assert row.queued_at == 300
    assert claimed is not None


def test_claim_is_oldest_first_and_single_flight():
    store.queue(_movie("older", queued_at=10))
    store.queue(_movie("newer", queued_at=20))

    first = store.claim()
    second = store.claim()
    third = store.claim()

    assert first.jellyfin_id == "older" and first.state == store.ACTIVE
    assert second.jellyfin_id == "newer"
    assert third is None
    assert store.get("older").state == store.ACTIVE


def test_finish_settles_the_row():
    store.queue(_movie())
    store.claim()
    store.finish("m1", "Movies/M (2001)/m.mkv", "mkv", 999, done_at=500)

    row = store.get("m1")
    assert row.state == store.DONE
    assert row.size_actual == 999
    assert row.bytes_done == 999
    assert row.done_at == 500
    assert store.is_done("m1") is True
    assert store.done_ids() == {"m1"}


def test_remove_deletes_the_row():
    store.queue(_movie())
    store.remove("m1")
    assert store.get("m1") is None
    assert store.rows() == []


def test_rows_filters_by_state_in_queue_order():
    store.queue(_movie("b", queued_at=20))
    store.queue(_movie("a", queued_at=10))
    store.queue(_movie("c", queued_at=30))
    store.claim()  # takes "a"

    queued = store.rows(store.QUEUED)
    assert [row.jellyfin_id for row in queued] == ["b", "c"]
    assert [row.jellyfin_id for row in store.rows()] == ["a", "b", "c"]


def test_series_has_done_is_the_tvshow_tag_lookup():
    store.queue(_movie("e1", media_type="episode", series_id="show9"))
    assert store.series_has_done("show9") is False  # queued is not downloaded

    store.claim()
    store.finish("e1", "TV/S/Season 01/e1.mkv", "mkv", 10)
    assert store.series_has_done("show9") is True
    assert store.series_has_done("othershow") is False
    assert store.series_has_done("") is False


def test_recover_interrupted_requeues_crashed_actives():
    """A crash mid-download leaves rows active with nobody working them;
    manager start moves them back to queued, keeping bytes_done so an
    original resumes with a Range."""
    store.queue(_movie("m1"))
    store.queue(_movie("m2", queued_at=101))
    store.claim()
    store.record_progress("m1", 777)

    assert store.recover_interrupted() == 1
    row = store.get("m1")
    assert row.state == store.QUEUED
    assert row.bytes_done == 777
    assert store.recover_interrupted() == 0
