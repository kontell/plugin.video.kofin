import pytest

from kofin.core import state
from tests.unit.fakes import FakeWindow


@pytest.fixture(autouse=True)
def fake_window(monkeypatch):
    FakeWindow.store = {}
    monkeypatch.setattr("xbmcgui.Window", FakeWindow)
    return FakeWindow


def test_online_round_trip():
    assert state.is_online() is False
    state.set_online(True)
    assert state.is_online() is True
    state.set_online(False)
    assert state.is_online() is False


def test_claim_by_path_match():
    state.push_play_item({"Path": "http://a", "Id": "1"})
    state.push_play_item({"Path": "http://b", "Id": "2"})
    claimed = state.claim_play_item("http://b")
    assert claimed is not None and claimed["Id"] == "2"
    remaining = state.claim_play_item("http://a")
    assert remaining is not None and remaining["Id"] == "1"
    assert state.claim_play_item("http://a") is None


def test_claim_falls_back_to_oldest():
    state.push_play_item({"Path": "http://a", "Id": "1"})
    state.push_play_item({"Path": "http://b", "Id": "2"})
    claimed = state.claim_play_item("http://other")
    assert claimed is not None and claimed["Id"] == "1"


def test_claim_on_empty_and_garbage(tmp_path, monkeypatch):
    assert state.claim_play_item("x") is None

    # A half-written or corrupt entry is skipped, not fatal: the queue is a
    # directory of files now, and one bad file must not cost the others.
    import os

    directory = state._queue_dir()
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "%019d-bad.json" % 1), "w") as handle:
        handle.write("not-json")
    assert state.claim_play_item("x") is None


def test_a_claimed_entry_cannot_be_claimed_twice():
    """os.remove is the claim: the filesystem guarantees exactly one caller
    wins, which is the mutual exclusion a window property could not give
    across processes (audit finding #12)."""
    state.push_play_item({"Path": "http://a", "Id": "1"})

    first = state.claim_play_item("http://a")
    second = state.claim_play_item("http://a")

    assert first is not None and first["Id"] == "1"
    assert second is None


def test_concurrent_claims_hand_out_each_entry_once():
    """The race the property lost: two claimers must never both get the same
    entry, and nothing queued may be resurrected."""
    import threading

    for index in range(20):
        state.push_play_item({"Path": "http://a", "Id": str(index)})

    claimed = []
    lock = threading.Lock()

    def claim_until_empty():
        while True:
            item = state.claim_play_item("http://a")
            if item is None:
                return
            with lock:
                claimed.append(item["Id"])

    threads = [threading.Thread(target=claim_until_empty) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert sorted(claimed, key=int) == [str(index) for index in range(20)]


def test_a_push_during_a_claim_is_never_lost():
    """The other half of the race: an entry pushed while a claim runs must
    still be claimable — under the property it vanished with the writeback,
    and its playback was never reported."""
    state.push_play_item({"Path": "http://old", "Id": "old"})
    claimed = state.claim_play_item("http://old")
    state.push_play_item({"Path": "http://new", "Id": "new"})

    assert claimed is not None and claimed["Id"] == "old"
    assert state.play_item_queued("http://new") is True
    fresh = state.claim_play_item("http://new")
    assert fresh is not None and fresh["Id"] == "new"


def test_stale_entries_expire_rather_than_being_adopted():
    """An entry whose playback never started would otherwise be handed to an
    unrelated later play by the oldest-entry fallback."""
    import os

    directory = state._queue_dir()
    os.makedirs(directory, exist_ok=True)
    stale_stamp = 1  # nanoseconds since the epoch: as old as it gets
    with open(os.path.join(directory, "%019d-old.json" % stale_stamp), "w") as handle:
        handle.write('{"Path": "http://ancient", "Id": "ancient"}')

    state.push_play_item({"Path": "http://fresh", "Id": "fresh"})

    assert state.play_item_queued("http://ancient") is False
    claimed = state.claim_play_item("http://unrelated")
    assert claimed is not None and claimed["Id"] == "fresh"


def test_clear_all():
    state.set_online(True)
    state.set_playing_id("42")
    state.push_play_item({"Path": "p"})
    state.set_watching_names(["Bob"])
    state.clear_all()
    assert state.is_online() is False
    assert state.get_playing_id() == ""
    assert state.claim_play_item("p") is None
    assert state.watching_names() == []


def test_watching_names_round_trip():
    assert state.watching_names() == []
    state.set_watching_names(["Bob", "Dan"])
    assert state.watching_names() == ["Bob", "Dan"]
    state.set_watching_names([])
    assert state.watching_names() == []


def test_watching_names_garbage_reads_as_nobody():
    FakeWindow.store[state.PROP_WHO_NAMES] = "not-json"
    assert state.watching_names() == []
    FakeWindow.store[state.PROP_WHO_NAMES] = '{"a": 1}'
    assert state.watching_names() == []
