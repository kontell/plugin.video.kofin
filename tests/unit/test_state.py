import os

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


def test_play_queue_files_are_owner_only(tmp_path, monkeypatch):
    """Transcoding URLs carry api_key=; the queue must not be world-readable."""
    import os
    import stat

    monkeypatch.setattr(state, "_queue_dir", lambda: str(tmp_path / "queue"))
    state.push_play_item({"Path": "http://s/stream?api_key=tok", "Id": "1"})
    files = list((tmp_path / "queue").iterdir())
    assert files
    mode = stat.S_IMODE(os.stat(files[0]).st_mode)
    assert mode == 0o600


def test_claim_by_path_match():
    state.push_play_item({"Path": "http://a", "Id": "1"})
    state.push_play_item({"Path": "http://b", "Id": "2"})
    claimed = state.claim_play_item("http://b")
    assert claimed is not None and claimed["Id"] == "2"
    remaining = state.claim_play_item("http://a")
    assert remaining is not None and remaining["Id"] == "1"
    assert state.claim_play_item("http://a") is None


def test_a_claim_matches_the_file_whatever_options_kodi_reports():
    """A resolved URL can carry protocol options after ``|``; Kodi may report
    the playing file with them or without."""
    state.push_play_item({"Path": "https://s/stream?x=1|verifypeer=false", "Id": "1"})
    state.push_play_item({"Path": "https://s/stream?x=2|verifypeer=false", "Id": "2"})
    assert state.claim_play_item("https://s/stream?x=1")["Id"] == "1"
    assert state.claim_play_item("https://s/stream?x=2|verifypeer=false")["Id"] == "2"


def test_unrelated_play_does_not_claim_a_failed_resolve():
    """A movie resolve can fail while Kodi starts a song."""
    state.push_play_item({"Path": "http://s/Videos/movie/stream", "Id": "movie"})
    song = "http://s/Audio/song/stream.flac"

    assert state.claim_play_item(song) is None
    movie = state.claim_play_item("http://s/Videos/movie/stream")
    assert movie is not None and movie["Id"] == "movie"


def test_claim_on_empty_and_garbage(tmp_path, monkeypatch):
    assert state.claim_play_item("x") is None

    # A half-written or corrupt entry is skipped, not fatal: one bad file
    # must not cost the others.
    import os

    directory = state._queue_dir()
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "%019d-bad.json" % 1), "w") as handle:
        handle.write("not-json")
    assert state.claim_play_item("x") is None


def test_a_claimed_entry_cannot_be_claimed_twice():
    """os.remove is the claim: the filesystem guarantees exactly one caller
    wins, across processes."""
    state.push_play_item({"Path": "http://a", "Id": "1"})

    first = state.claim_play_item("http://a")
    second = state.claim_play_item("http://a")

    assert first is not None and first["Id"] == "1"
    assert second is None


def test_concurrent_claims_hand_out_each_entry_once():
    """Two claimers must never both get the same entry, and nothing queued
    may be resurrected."""
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
    """An entry pushed while a claim runs must still be claimable, or its
    playback is never reported."""
    state.push_play_item({"Path": "http://old", "Id": "old"})
    claimed = state.claim_play_item("http://old")
    state.push_play_item({"Path": "http://new", "Id": "new"})

    assert claimed is not None and claimed["Id"] == "old"
    fresh = state.claim_play_item("http://new")
    assert fresh is not None and fresh["Id"] == "new"


def test_stale_entries_expire_without_disturbing_a_fresh_claim():
    import os

    directory = state._queue_dir()
    os.makedirs(directory, exist_ok=True)
    stale_stamp = 1  # nanoseconds since the epoch: as old as it gets
    with open(os.path.join(directory, "%019d-old.json" % stale_stamp), "w") as handle:
        handle.write('{"Path": "http://ancient", "Id": "ancient"}')

    state.push_play_item({"Path": "http://fresh", "Id": "fresh"})

    assert state.claim_play_item("http://ancient") is None
    claimed = state.claim_play_item("http://fresh")
    assert claimed is not None and claimed["Id"] == "fresh"


def test_clear_all():
    state.set_online(True)
    state.set_playing_id("42")
    state.push_play_item({"Path": "p"})
    state.set_watching_names(["Bob"])
    state.set_menu_who(True)
    state.set_menu_syncplay(True)
    state.clear_all()
    assert state.is_online() is False
    assert state.get_playing_id() == ""
    assert state.claim_play_item("p") is None
    assert state.watching_names() == []
    assert state.PROP_MENU_WHO not in FakeWindow.store
    assert state.PROP_MENU_SYNCPLAY not in FakeWindow.store


def test_a_handed_over_play_is_taken_once():
    state.hand_over_play({"Claim": {"Id": "m1"}, "UserId": "u"})
    assert state.take_handed_over_play() == {"Claim": {"Id": "m1"}, "UserId": "u"}
    assert state.take_handed_over_play() == {}
    assert not os.path.exists(state._handover_path())


def test_a_handed_over_play_is_for_the_owner_alone():
    """A claim's Path is its stream URL, which can carry the token; it is
    never written where another add-on or a JSON-RPC client reads."""
    state.hand_over_play({"Claim": {"Id": "m1", "Path": "http://s/x?api_key=t"}})
    assert os.stat(state._handover_path()).st_mode & 0o777 == 0o600
    assert not any("api_key" in str(value) for value in FakeWindow.store.values())


def test_a_garbled_hand_over_is_taken_as_nothing():
    os.makedirs(os.path.dirname(state._handover_path()), exist_ok=True)
    with open(state._handover_path(), "w") as handle:
        handle.write("not-json")
    assert state.take_handed_over_play() == {}
    assert not os.path.exists(state._handover_path())


def test_the_queue_and_clear_all_leave_a_handed_over_play():
    """An in-process restart clears the live state before the next
    generation exists, and that generation is the one the play is left to."""
    state.hand_over_play({"Claim": {"Id": "m1"}})
    state.clear_all()
    state.clear_play_queue()
    assert state.take_handed_over_play() == {"Claim": {"Id": "m1"}}


def test_one_file_whatever_options_either_path_carries():
    assert state.same_file("https://s/a?x=1|verifypeer=false", "https://s/a?x=1")
    assert not state.same_file("https://s/a?x=1", "https://s/a?x=2")


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


def test_menu_visibility_round_trip():
    """Skins read the two properties by name, so the test does too."""
    assert state.PROP_MENU_WHO not in FakeWindow.store
    assert state.PROP_MENU_SYNCPLAY not in FakeWindow.store
    state.set_menu_who(True)
    state.set_menu_syncplay(True)
    assert FakeWindow.store[state.PROP_MENU_WHO] == "true"
    assert FakeWindow.store[state.PROP_MENU_SYNCPLAY] == "true"
    state.set_menu_who(False)
    state.set_menu_syncplay(False)
    assert state.PROP_MENU_WHO not in FakeWindow.store
    assert state.PROP_MENU_SYNCPLAY not in FakeWindow.store


# --- an entry that names a live stream --------------------------------------


@pytest.fixture
def queue(tmp_path, monkeypatch):
    directory = tmp_path / "queue"
    monkeypatch.setattr(state, "_queue_dir", lambda: str(directory))
    return directory


def _entry(directory, age_seconds, **fields):
    import json
    import os
    import time
    import uuid

    os.makedirs(directory, exist_ok=True)
    stamp = time.time_ns() - int(age_seconds * 1_000_000_000)
    path = os.path.join(directory, "%019d-%s.json" % (stamp, uuid.uuid4().hex[:8]))
    with open(path, "w") as handle:
        json.dump(fields, handle)
    return path


def test_an_expired_live_entry_waits_for_its_stream_to_be_closed(queue):
    """Dropped unclosed, the server's stream and its tuner would stay open."""
    _entry(
        queue, state.PLAY_QUEUE_TTL_SECONDS + 5, Path="http://old", LiveStreamId="ls1"
    )
    state.push_play_item({"Path": "http://fresh", "Id": "fresh"})  # runs the expiry

    closed = []
    assert state.close_stale_live_streams(closed.append) == ["ls1"]
    assert closed == ["ls1"]
    assert state.claim_play_item("http://old") is None


def test_a_live_entry_is_left_to_its_claim_for_a_minute(queue):
    _entry(queue, 5, Path="http://live", LiveStreamId="ls1")

    closed = []
    assert state.close_stale_live_streams(closed.append) == []
    assert closed == []
    assert state.claim_play_item("http://live")["LiveStreamId"] == "ls1"


def test_a_failed_close_keeps_the_entry_for_the_next_sweep(queue):
    _entry(
        queue,
        state.PLAY_QUEUE_LIVE_TTL_SECONDS + 5,
        Path="http://live",
        LiveStreamId="ls1",
    )

    def refuse(live_stream_id):
        raise RuntimeError("server unreachable")

    assert state.close_stale_live_streams(refuse) == []
    closed = []
    assert state.close_stale_live_streams(closed.append) == ["ls1"]


def test_a_live_entry_past_the_queue_ttl_goes_even_when_its_close_fails(queue):
    import os

    _entry(
        queue, state.PLAY_QUEUE_TTL_SECONDS + 5, Path="http://live", LiveStreamId="ls1"
    )

    def refuse(live_stream_id):
        raise RuntimeError("server unreachable")

    assert state.close_stale_live_streams(refuse) == []
    assert os.listdir(queue) == []


def test_clearing_the_queue_leaves_a_live_entry_for_its_close(queue):
    _entry(queue, 5, Path="http://live", LiveStreamId="ls1")
    state.push_play_item({"Path": "p"})

    state.clear_play_queue()

    assert state.claim_play_item("p") is None
    assert state.claim_play_item("http://live")["LiveStreamId"] == "ls1"


def test_a_claim_by_item_leaves_an_older_live_entry_for_its_close(queue):
    _entry(queue, 30, Path="http://old", Id="c1", LiveStreamId="ls-old")
    state.push_play_item({"Path": "http://new", "Id": "c1"})

    assert state.claim_play_item_of("c1")["Path"] == "http://new"
    assert state.claim_play_item("http://old")["LiveStreamId"] == "ls-old"
