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
    store.finish("e1", "Shows/S/Season 01/e1.mkv", "mkv", 10)
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


def test_release_returns_an_active_row_to_queued():
    """The outage interruption (plan W3.1 amendments): the owning worker
    puts the row back itself, because recover_interrupted runs only at
    manager start and an active row would sit stuck until then."""
    store.queue(_movie("m1"))
    store.claim()
    store.record_progress("m1", 42)

    store.release("m1")
    row = store.get("m1")
    assert row.state == store.QUEUED
    assert row.bytes_done == 42  # the watermark survives for a Range resume
    assert row.queued_at == 100  # still at the head of the queue

    store.release("m1")  # only active rows move; a second call is a no-op
    assert store.get("m1").state == store.QUEUED


def test_record_details_stamps_the_decided_quality():
    store.queue(_movie("m1"))
    store.record_details("m1", "movie", "", 0, "", store.QUALITY_TRANSCODE)
    assert store.get("m1").quality == store.QUALITY_TRANSCODE
    store.record_details("m1", "movie", "", 8, "")
    assert store.get("m1").quality == store.QUALITY_ORIGINAL


def test_requeue_clears_a_failed_transcodes_target_but_not_an_originals():
    store.queue(_movie("m1"))
    store.claim()
    store.record_details("m1", "movie", "", 0, "", store.QUALITY_TRANSCODE)
    store.record_target("m1", "Movies/X/X.mp4", "mp4")
    store.record_progress("m1", 42)
    store.fail("m1", "name the attempt could not put on disk")

    assert store.queue(_movie("m1")) is True
    row = store.get("m1")
    assert row.rel_path == "" and row.bytes_done == 0  # re-freezes fresh

    store.claim()
    store.record_details("m1", "movie", "", 8, "", store.QUALITY_ORIGINAL)
    store.record_target("m1", "Movies/X/X.mkv", "mkv")
    store.record_progress("m1", 42)
    store.fail("m1", "connection lost")

    assert store.queue(_movie("m1")) is True
    row = store.get("m1")
    assert row.rel_path == "Movies/X/X.mkv"  # the Range resume
    assert row.bytes_done == 42


def test_pending_count_spans_queued_and_active():
    assert store.pending_count() == 0
    store.queue(_movie("m1"))
    store.queue(_movie("m2", queued_at=101))
    store.claim()
    assert store.pending_count() == 2  # one active, one queued
    store.finish("m1", "a/b.mkv", "mkv", 1)
    assert store.pending_count() == 1


# -- the two worker pools, and container questions ----------------------------


def test_claim_is_scoped_to_a_pools_own_kinds():
    """Music downloads got their own pool because they cost nothing like a
    film; the kind filter is what keeps an album from queueing behind one.
    The unknown kind — rows queued before the type travelled with the id —
    belongs to the video pool, so no work can be stranded."""
    store.queue(store.Download(jellyfin_id="song1", media_type="song", queued_at=100))
    store.queue(store.Download(jellyfin_id="film1", media_type="movie", queued_at=101))
    store.queue(store.Download(jellyfin_id="old1", media_type="", queued_at=102))

    music = store.claim(("song",))
    assert music.jellyfin_id == "song1"
    assert store.claim(("song",)) is None  # nothing else is music

    video = [store.claim(("movie", "episode", "")) for _ in range(2)]
    assert [row.jellyfin_id for row in video] == ["film1", "old1"]
    assert store.claim(("movie", "episode", "")) is None  # drained


def test_claim_with_no_kinds_at_all_claims_nothing():
    store.queue(store.Download(jellyfin_id="m1", media_type="movie"))
    assert store.claim(()) is None
    assert store.claim() is not None  # ... unlike the unscoped call


def test_queue_records_the_media_type_it_was_given():
    store.queue(store.Download(jellyfin_id="s1", media_type="song"))
    assert store.get("s1").media_type == "song"


def _seed_container_child(item_id, state, series_id="", parent_id=""):
    store.queue(store.Download(jellyfin_id=item_id, series_id=series_id))
    if parent_id:
        with sync_db.Database("kofin") as opened:
            opened.cursor.execute(
                "INSERT INTO jellyfin (jellyfin_id, parent_id, media_type) "
                "VALUES (?, ?, 'episode')",
                (item_id, parent_id),
            )
    if state != store.QUEUED:
        store.claim()
    if state == store.DONE:
        store.finish(item_id, "a/%s.mkv" % item_id, "mkv", 1)
    elif state == store.FAILED:
        store.fail(item_id, "nope")


def test_container_counts_see_children_by_series_and_by_parent():
    """Two lookups because the table records one parent: series_id answers a
    Series or an album outright, and a Season — which is nobody's series_id
    — is found through the kofin.db mapping's parent_id."""
    _seed_container_child("e1", store.DONE, series_id="show1")
    _seed_container_child("e2", store.QUEUED, series_id="show1")
    _seed_container_child("e3", store.FAILED, series_id="show1")
    _seed_container_child("s1", store.DONE, parent_id="season1")
    _seed_container_child("s2", store.ACTIVE, parent_id="season1")

    show = store.container_counts("show1")
    assert show == {"done": 1, "pending": 1}  # the failure counts as neither
    assert store.container_done_ids("show1") == ["e1"]
    assert store.container_pending_ids("show1") == ["e2"]

    season = store.container_counts("season1")
    assert season == {"done": 1, "pending": 1}
    assert store.container_done_ids("season1") == ["s1"]
    assert store.container_pending_ids("season1") == ["s2"]

    assert store.container_counts("") == {"done": 0, "pending": 0}
    assert store.container_counts("nothing") == {"done": 0, "pending": 0}


def test_the_video_pool_also_claims_a_null_kind():
    """A row no pool can claim is stuck forever and says nothing about it,
    so the unknown-kind bucket covers NULL as well as ''."""
    store.queue(store.Download(jellyfin_id="odd1"))
    with sync_db.Database("kofin") as opened:
        opened.cursor.execute(
            "UPDATE download SET media_type = NULL WHERE jellyfin_id = 'odd1'"
        )

    assert store.claim(("song",)) is None
    claimed = store.claim(("movie", "episode", ""))
    assert claimed is not None and claimed.jellyfin_id == "odd1"
