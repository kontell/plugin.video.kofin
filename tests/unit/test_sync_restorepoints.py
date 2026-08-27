"""The restore-point module (P2.2): pure over the caller's dict."""

from kofin.sync import restorepoints


def stamped(params, fingerprint="fp", saved_at=1_000_000.0):
    return {"params": params, "Fingerprint": fingerprint, "SavedAt": saved_at}


def test_a_fresh_point_with_the_same_query_resumes():
    store = {"lib/movies": stamped({"StartIndex": 300})}

    assert restorepoints.resume_at(store, "lib/movies", "fp", now=1_000_100.0) == {
        "StartIndex": 300
    }
    assert "lib/movies" in store  # only the walk's completion clears it


def test_a_point_from_a_different_query_is_discarded():
    store = {"lib/movies": stamped({"StartIndex": 300}, fingerprint="old")}

    assert restorepoints.resume_at(store, "lib/movies", "fp", now=1_000_100.0) is None
    assert store == {}


def test_a_point_older_than_the_ttl_is_discarded():
    store = {"lib/movies": stamped({"StartIndex": 300})}

    now = 1_000_000.0 + restorepoints.TTL + 1
    assert restorepoints.resume_at(store, "lib/movies", "fp", now=now) is None
    assert store == {}


def test_an_unstamped_point_is_expired_by_definition():
    """It predates the check, so it is exactly the kind that has been
    sitting there across upgrades."""
    store = {"lib/movies": {"params": {"StartIndex": 1250}, "Fingerprint": "fp"}}

    assert restorepoints.resume_at(store, "lib/movies", "fp") is None
    assert store == {}
    assert restorepoints.expired({"SavedAt": "not a number"}) is True


def test_no_fingerprint_asked_means_no_fingerprint_check():
    store = {"lib/movies": stamped({"StartIndex": 300}, fingerprint="whatever")}

    assert restorepoints.resume_at(store, "lib/movies", now=1_000_100.0) == {
        "StartIndex": 300
    }


def test_save_stamps_the_time_and_the_query():
    store = {}

    restorepoints.save(store, "lib/movies", {"StartIndex": 50}, "fp", now=42.0)

    assert store == {
        "lib/movies": {"StartIndex": 50, "SavedAt": 42.0, "Fingerprint": "fp"}
    }


def test_clear_library_takes_every_slot_of_the_library_and_nothing_else():
    store = {
        "lib1/series": stamped({}),
        "lib1/episodes": stamped({}),
        "lib10/movies": stamped({}),
        "lib2/movies": stamped({}),
    }

    cleared = restorepoints.clear_library(store, "lib1")

    assert sorted(cleared) == ["lib1/episodes", "lib1/series"]
    assert sorted(store) == ["lib10/movies", "lib2/movies"]
    assert restorepoints.clear_library(store, "nope") == []
