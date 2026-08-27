"""GetItemWorker re-queues a chunk that failed to download, bounded
(docs/sync-refactor-phase1-plan.md P1.4). The transport arm
(ServerUnreachable) keeps its own re-queue-and-back-off; this is the other
one -- an HTTP status, a bad payload, anything -- that used to drop the
chunk on the floor."""

import queue
import threading

import pytest

from kofin.core.http import HttpError
from kofin.sync.downloader import CHUNK_ATTEMPTS, Chunk, GetItemWorker


class Api:
    user_id = "u"

    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    def items(self, params):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return {
            "Items": [
                {"Id": i, "Type": "Movie", "Name": i} for i in params["Ids"].split(",")
            ]
        }


def run_one_worker(api, work, output, error_event, unapplied):
    worker = GetItemWorker(api, work, output, error_event, unapplied=unapplied)
    worker.run()
    return worker


def test_a_failed_chunk_goes_back_with_its_attempt_count_and_the_worker_ends():
    api = Api([HttpError(500, "GET /Items -> 500")])
    work = queue.Queue()
    work.put(["m1", "m2"])
    output = {"Movie": queue.Queue()}
    error_event = threading.Event()
    flagged = []

    worker = run_one_worker(
        api, work, output, error_event, lambda i, r: flagged.append((i, r))
    )

    assert worker.is_done and not worker.unreachable
    assert error_event.is_set()  # the watermark holds
    assert flagged == []
    chunk = work.get_nowait()
    assert (
        isinstance(chunk, Chunk) and list(chunk) == ["m1", "m2"] and chunk.attempts == 1
    )

    # The next worker (the spawn path's, a tick later) succeeds on the retry.
    work.put(chunk)
    run_one_worker(api, work, output, error_event, lambda i, r: flagged.append((i, r)))

    assert [output["Movie"].get_nowait()["Id"] for _ in range(2)] == ["m1", "m2"]
    assert work.qsize() == 0
    assert api.calls == 2


def test_a_chunk_that_keeps_failing_is_dropped_and_every_id_flagged():
    api = Api([RuntimeError("bad payload")] * CHUNK_ATTEMPTS)
    work = queue.Queue()
    work.put(["m1", "m2", "m3"])
    flagged = []

    for _ in range(CHUNK_ATTEMPTS):
        run_one_worker(
            api,
            work,
            {"Movie": queue.Queue()},
            threading.Event(),
            lambda i, r: flagged.append((i, r)),
        )

    assert work.qsize() == 0
    assert [i for i, _ in flagged] == ["m1", "m2", "m3"]
    assert all("failed %s times" % CHUNK_ATTEMPTS in r for _, r in flagged)
    assert api.calls == CHUNK_ATTEMPTS


def test_other_chunks_are_left_for_the_next_worker():
    """One failure ends this worker; it does not take the rest of the queue
    down with it, and it does not spin on its own chunk."""
    api = Api([HttpError(503, "GET /Items -> 503")])
    work = queue.Queue()
    work.put(["m1"])
    work.put(["m2"])

    run_one_worker(api, work, {"Movie": queue.Queue()}, threading.Event(), None)

    assert api.calls == 1
    assert work.qsize() == 2  # the untouched second chunk, then the re-queued first
    assert list(work.get_nowait()) == ["m2"]
    assert list(work.get_nowait()) == ["m1"]


def test_chunk_is_still_a_list_for_everyone_else():
    chunk = Chunk(["a", "b"], attempts=2)
    assert len(chunk) == 2 and ",".join(chunk) == "a,b" and chunk.attempts == 2
